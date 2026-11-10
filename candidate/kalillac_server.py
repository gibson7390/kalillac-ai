"""Controlled single-worker launcher. Implementation only: no service installation.

Deployment identities scope both PostgreSQL and host-file singleton locks.
A password gate remains process-local; shared limiter storage is Slice 8.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shlex
import tempfile
import threading
import time

HEALTH_INTERVAL_SECONDS = 1.0
HEALTH_TIMEOUT_SECONDS = 3.0
# Operator-approved maximum grace after a fatal singleton/monitor failure.
FATAL_SHUTDOWN_SECONDS = 2.0


class LauncherConfigError(Exception):
    pass


class SingletonHeld(Exception):
    pass


class SingletonLost(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise LauncherConfigError()


@dataclass(frozen=True)
class Settings:
    deployment_id: str
    host: str
    port: int
    lock_directory: Path


def settings(argv=None, environ=None):
    env = os.environ if environ is None else environ
    # The controlled launcher owns worker configuration. No inherited
    # supervisor setting may silently override its fixed workers=1.
    if any(env.get(name) for name in ("WEB_CONCURRENCY", "UVICORN_WORKERS")):
        raise LauncherConfigError()
    try:
        gunicorn = shlex.split(env.get("GUNICORN_CMD_ARGS", ""))
    except ValueError:
        raise LauncherConfigError() from None
    if any(value in {"-w", "--workers"} or value.startswith(("--workers=", "-w"))
           for value in gunicorn):
        raise LauncherConfigError()
    if env.get("UVICORN_RELOAD", "").lower() in {"1", "true", "yes", "on"}:
        raise LauncherConfigError()
    parser = _Parser(description="Kalillac controlled single-worker launcher")
    parser.add_argument("--deployment-id", default=env.get("KALILLAC_DEPLOYMENT_ID"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--lock-directory", type=Path,
                        default=Path(env.get("KALILLAC_SINGLETON_LOCK_DIRECTORY",
                                             str(Path(tempfile.gettempdir()) / "kalillac-singletons"))))
    args = parser.parse_args(argv)
    if (not args.deployment_id
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", args.deployment_id) is None
            or not 1 <= args.port <= 65535):
        raise LauncherConfigError()
    return Settings(args.deployment_id, args.host, args.port, args.lock_directory)


def lock_key(deployment_id):
    material = ("kalillac-singleton-v1\0" + deployment_id).encode("ascii")
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big", signed=True)


class DatabaseSingleton:
    def __init__(self, engine, deployment_id):
        self._engine = engine
        self._key = lock_key(deployment_id)
        self._connection = None
        self._pid = None

    def acquire(self):
        from sqlalchemy import text
        try:
            self._connection = self._engine.connect().execution_options(isolation_level="AUTOCOMMIT")
            self._pid, acquired = self._connection.execute(
                text("SELECT pg_backend_pid(), pg_try_advisory_lock(:key)"),
                {"key": self._key},
            ).one()
            if not acquired:
                raise SingletonHeld()
        except BaseException:
            self.close()
            raise

    def check(self):
        from sqlalchemy import text
        if self._connection is None or self._connection.closed or self._connection.invalidated:
            raise SingletonLost()
        # A reconnect is not continuity of the original session lock.
        key = self._key & ((1 << 64) - 1)
        from sqlalchemy.exc import DBAPIError
        try:
            pid, held = self._connection.execute(text(
                "SELECT pg_backend_pid(), EXISTS (SELECT 1 FROM pg_locks "
                "WHERE locktype='advisory' AND pid=pg_backend_pid() AND granted "
                "AND classid=:high AND objid=:low AND objsubid=1)"
            ), {"high": key >> 32, "low": key & 0xffffffff}).one()
        except DBAPIError as error:
            if (error.connection_invalidated or self._connection.invalidated
                    or self._connection.closed):
                raise SingletonLost() from None
            raise
        if pid != self._pid or not held:
            raise SingletonLost()

    def close(self):
        connection, self._connection = self._connection, None
        if connection is not None:
            # Physical close prevents a session-level advisory lock from
            # leaking back into the application's connection pool.
            try:
                connection.invalidate()
            finally:
                connection.close()


class HostSingleton:
    """Linux flock; the Windows byte-lock branch supports local verification."""
    def __init__(self, directory, deployment_id):
        digest = hashlib.sha256(deployment_id.encode("ascii")).hexdigest()
        self._path = Path(directory) / (digest + ".lock")
        self._file = None

    def acquire(self):
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self._path, flags, 0o600)
        self._file = os.fdopen(fd, "r+b", buffering=0)
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    self._file.write(b"\0")
                self._file.seek(0)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.close()
            raise SingletonHeld() from None

    def check(self):
        if self._file is None or self._file.closed:
            raise SingletonLost()
        try:
            own, current = os.fstat(self._file.fileno()), self._path.stat()
        except (OSError, ValueError) as error:
            import errno
            if (isinstance(error, (ValueError, FileNotFoundError, NotADirectoryError))
                    or error.errno == errno.EBADF):
                raise SingletonLost() from None
            raise
        if (own.st_dev, own.st_ino) != (current.st_dev, current.st_ino):
            raise SingletonLost()

    def close(self):
        if self._file is not None:
            self._file.close()
            self._file = None
        # Never unlink: another process may already be waiting on this inode.


class FatalShutdown:
    """A prestarted supervisor survives request/executor shutdown deadlocks.

    Once tripped it is never disarmed: even a nominal server.run() return cannot
    prove that every non-daemon worker has stopped. Process exit ends the daemon.
    """
    def __init__(self, terminate=None):
        self._terminate = os._exit if terminate is None else terminate
        self._lock = threading.Lock()
        self._trigger = threading.Event()
        self._deadline = threading.Event()
        self._tripped = False
        self._expires_at = None
        self._closed = False
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._watch, daemon=True,
                                        name="kalillac-singleton-fatal")
        self._thread.start()

    def trip(self):
        with self._lock:
            if not self._closed and not self._tripped:
                self._expires_at = time.monotonic() + FATAL_SHUTDOWN_SECONDS
                self._tripped = True
                self._trigger.set()

    def _watch(self):
        try:
            self._trigger.wait()
            with self._lock:
                tripped = self._tripped
            if tripped:
                self._deadline.wait(max(0.0, self._expires_at - time.monotonic()))
                self._terminate(1)
        except BaseException:
            self._terminate(1)

    def close(self):
        with self._lock:
            if self._tripped:
                return
            self._closed = True
            self._trigger.set()
        if self._thread is not None and self._thread.ident is not None:
            self._thread.join()


class SingletonMonitor:
    """Typed failures and a serialized ownership handoff after draining."""
    def __init__(self, lease, server, *, on_failure=None):
        self.lease, self.server = lease, server
        self.lost = False
        self._on_failure = on_failure
        self._state_lock = threading.Lock()
        self._check_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._phase = "running"
        self._cleanup_failed = False
        self._stop = threading.Event()
        self._thread = None
        self._probe = None

    def _lost(self, *, reason="ownership"):
        with self._state_lock:
            if self.lost:
                return
            if self._phase in {"releasing", "closed"} and reason != "handoff":
                return
            if self._phase == "draining" and reason == "infrastructure":
                return
            self.lost = True
            # Confirmed ownership loss is fatal in running AND draining.
            # No thread allocation is needed to arm the prestarted supervisor.
            if self._on_failure is not None:
                try:
                    self._on_failure()
                except BaseException:
                    os._exit(1)
            self.server.should_exit = True
        try:
            print("SINGLETON_LOCK_LOST", flush=True)
        except Exception:
            pass

    def _check_locked(self):
        with self._state_lock:
            if self._phase in {"releasing", "closed"}:
                return
        try:
            self.lease.check()
        except SingletonLost:
            self._lost(reason="ownership")
        except BaseException:
            self._lost(reason="infrastructure")

    def check_once(self):
        with self._check_lock:
            self._check_locked()

    def _finish_release(self):
        # This is the only final ownership-release path once a monitor exists.
        # The final check and release are one serialized handoff: no timer tick
        # or caller-side gap separates stopping checks from intentional release.
        with self._check_lock:
            with self._state_lock:
                if self._phase == "closed":
                    return
            if not self.lost:
                self._check_locked()
            with self._state_lock:
                self._phase = "releasing"
            try:
                self.lease.close()
            except BaseException:
                self._cleanup_failed = True
                self._lost(reason="handoff")
                raise
            finally:
                with self._state_lock:
                    self._phase = "closed"

    def _watch(self):
        while True:
            try:
                # Never overlap access to the dedicated singleton connection,
                # even when a draining probe outlives its health timeout.
                if self._probe is not None and self._probe.ident is not None:
                    while self._probe.is_alive():
                        self._probe.join(timeout=HEALTH_INTERVAL_SECONDS)
                        if self.lost:
                            return
                if self._stop.wait(HEALTH_INTERVAL_SECONDS):
                    self._finish_release()
                    return
                done = threading.Event()
                def probe():
                    try:
                        self.check_once()
                    except BaseException:
                        self._lost(reason="infrastructure")
                    finally:
                        done.set()
                self._probe = threading.Thread(target=probe, daemon=True,
                                               name="kalillac-singleton-probe")
                self._probe.start()
                if not done.wait(HEALTH_TIMEOUT_SECONDS):
                    self._lost(reason="infrastructure")
            except BaseException:
                self._lost(reason="infrastructure")
            if self.lost:
                return
            # A suppressed teardown infrastructure failure does not stop the
            # monitor: it retries ownership checks until final handoff.

    def start(self):
        self._thread = threading.Thread(target=self._watch, daemon=True,
                                        name="kalillac-singleton-monitor")
        self._thread.start()

    def begin_shutdown(self):
        with self._state_lock:
            if self._phase == "running":
                self._phase = "draining"
        # Deliberately keep monitoring throughout graceful draining.

    def close(self):
        with self._close_lock:
            with self._state_lock:
                if self._phase == "closed":
                    return not self._cleanup_failed
            self.begin_shutdown()
            # Ask the existing monitor thread to perform the last check/release.
            # No new probe thread must be allocated during final cleanup.
            self._stop.set()
            if self._thread is not None and self._thread.ident is not None:
                self._thread.join(timeout=2 * HEALTH_TIMEOUT_SECONDS)
                if self._thread.is_alive():
                    self._lost(reason="handoff")
                    return False
            if self._probe is not None and self._probe.ident is not None:
                self._probe.join(timeout=HEALTH_TIMEOUT_SECONDS)
                if self._probe.is_alive():
                    self._lost(reason="handoff")
                    return False
            # Startup failure or an already-latched fatal decision can leave
            # no monitor thread. There is then no concurrent ownership probe.
            self._finish_release()
            return not self._cleanup_failed


def main(argv=None):
    lease = monitor = fatal = None
    result = 1
    try:
        config = settings(argv)
        # These imports remain lazy: disabled database/account mode does not
        # acquire a database dependency or connection merely to parse settings.
        from kalillac_db.config import database_enabled
        if database_enabled():
            from kalillac_db.engine import get_engine
            lease = DatabaseSingleton(get_engine(), config.deployment_id)
        else:
            lease = HostSingleton(config.lock_directory, config.deployment_id)
        lease.acquire()
        fatal = FatalShutdown()
        fatal.start()
        import uvicorn
        class ControlledServer(uvicorn.Server):
            async def shutdown(self, sockets=None):
                # Uvicorn calls this before closing listeners or draining work.
                # Do not defer the monitor transition until run() returns.
                monitor.begin_shutdown()
                await super().shutdown(sockets=sockets)

        server = ControlledServer(uvicorn.Config(
            "app_fastapi_candidate:api", host=config.host, port=config.port,
            workers=1, reload=False,
        ))
        monitor = SingletonMonitor(lease, server, on_failure=fatal.trip)
        monitor.start()
        print("SINGLETON_LOCK_ACQUIRED", flush=True)
        server.run()
        result = 1 if monitor.lost or not server.started else 0
    except LauncherConfigError:
        print("LAUNCHER_CONFIG_INVALID", flush=True)
        result = 2
    except SingletonHeld:
        print("SINGLETON_LOCK_HELD", flush=True)
        result = 1
    except Exception:
        print("SINGLETON_START_FAILED", flush=True)
        result = 1
    finally:
        try:
            if monitor is not None:
                if not monitor.close() or monitor.lost:
                    result = 1
            elif lease is not None:
                # Only acquisition/monitor-construction failure reaches this
                # fallback; a constructed monitor owns final cleanup.
                lease.close()
        finally:
            if fatal is not None:
                fatal.close()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
