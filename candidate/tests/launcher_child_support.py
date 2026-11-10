"""Isolated launcher regression child; synthetic ASGI app, no providers.

The optional PostgreSQL mode is invoked by the disposable recovery verifier.
Ordinary pytest uses the actual host-file ownership primitive.
"""
import asyncio
import base64
import json
import os
from pathlib import Path
import sys
import threading
import types

CANDIDATE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CANDIDATE))
if os.environ.get("SLICE2_REVIEW1_BASELINE"):
    baseline = json.loads(Path(os.environ["SLICE2_REVIEW1_BASELINE"]).read_text())
    old = types.ModuleType("kalillac_server")
    old.__file__ = str(CANDIDATE / "kalillac_server.py")
    sys.modules["kalillac_server"] = old
    exec(compile(base64.b64decode(baseline["review1_source_base64"]["candidate/kalillac_server.py"]),
                 old.__file__, "exec"), old.__dict__)
import kalillac_server as launcher
from kalillac_accounts.password_work import PASSWORD_WORK_GATE
import kalillac_db.config
import kalillac_db.engine
import uvicorn

mode, directory, identity = sys.argv[1:4]
real_pg = os.environ.get("SLICE2_CHILD_REAL_PG") == "1"
armed, worker_release = threading.Event(), threading.Event()
probe_release = threading.Event()
cleanup_release = threading.Event()
state = {}
output_lock = threading.Lock()


def emit(event, **values):
    with output_lock:
        print(json.dumps({"event": event, **values}), flush=True)


OriginalThread = threading.Thread


def threads(*args, **kwargs):
    target = kwargs.get("target")
    is_probe = getattr(target, "__name__", "") == "probe"
    if armed.is_set() and is_probe and mode == "construct":
        raise RuntimeError("synthetic probe construction failure")
    result = OriginalThread(*args, **kwargs)
    if is_probe and mode == "drain-start" and not state.get("probe_failed"):
        def fail_after_drain():
            emit("probe_pending")
            assert probe_release.wait(12)
            state["probe_failed"] = True
            emit("probe_start_failed")
            raise RuntimeError("synthetic probe start failure during draining")
        result.start = fail_after_drain
    elif armed.is_set() and is_probe and mode == "start":
        def fail():
            raise RuntimeError("synthetic probe start failure")
        result.start = fail
    return result


threading.Thread = threads
Host = launcher.HostSingleton
Database = launcher.DatabaseSingleton


class Ownership:
    def observe(self):
        state["lease"] = self
        emit("held", pid=getattr(self, "_pid", None))

    def close(self):
        super().close()
        monitor = state.get("monitor")
        if monitor is not None:
            emit("released", lost=monitor.lost)

    def check(self):
        if armed.is_set() and mode == "execute":
            raise SystemExit("synthetic unexpected probe execution failure")
        result = super().check()
        if state.get("probe_failed"):
            emit("ownership_checked")
        return result


class HostLease(Ownership, Host):
    def acquire(self):
        super().acquire()
        self.observe()


class DatabaseLease(Ownership, Database):
    def acquire(self):
        super().acquire()
        self.observe()


launcher.HostSingleton, launcher.DatabaseSingleton = HostLease, DatabaseLease
Monitor = launcher.SingletonMonitor


class ObservedMonitor(Monitor):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        state["monitor"] = self
        self._reported = False

    def _lost(self, **kwargs):
        super()._lost(**kwargs)
        if kwargs.get("reason") == "infrastructure" and not self.lost:
            emit("probe_ignored")
        if self.lost and not self._reported:
            self._reported = True
            emit("lost")


    def _finish_release(self):
        if mode == "cleanup-loss" and not cleanup_release.is_set():
            emit("release_pending")
            assert cleanup_release.wait(12)
        return super()._finish_release()

    def _watch(self):
        try:
            return super()._watch()
        finally:
            emit("monitor_stopped", lost=self.lost)

launcher.SingletonMonitor = ObservedMonitor
Fatal = launcher.FatalShutdown

class ObservedFatal(Fatal):
    def trip(self):
        super().trip()
        emit("fatal", expires_at=self._expires_at)

launcher.FatalShutdown = ObservedFatal
kalillac_db.config.database_enabled = lambda: real_pg
if real_pg:
    from tools.verify_recovery_postgres import build_engine
    assert os.environ["KALILLAC_DB_HOST"] == "127.0.0.1"
    assert os.environ["KALILLAC_DB_NAME"] == "kalillac_staging"
    assert os.environ["KALILLAC_DB_USER"] == "kalillac_staging_app"
    engine = build_engine(os.environ)
    kalillac_db.engine.get_engine = lambda: engine


def work():
    if real_pg and mode == "rollback":
        from sqlalchemy import text
        tag = os.environ["SLICE2_ROLLBACK_TAG"]
        oid = int(os.environ["SLICE2_ROLLBACK_OID"])
        key = launcher.lock_key("rollback-transaction-" + tag)
        # A separate pooled connection, never the dedicated singleton session.
        with engine.begin() as connection:
            pid = connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
            assert pid != state["lease"]._pid
            connection.execute(text("SELECT set_config('application_name', :name, true)"),
                               {"name": "slice2-rollback-" + tag})
            connection.execute(text("SELECT lo_put(:oid, 0, :value)"),
                               {"oid": oid, "value": tag.encode("ascii")})
            connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
            assert connection.execute(text("SELECT lo_get(:oid)"), {"oid": oid}).scalar_one() == tag.encode("ascii")
            state["transaction"] = dict(pid=pid, singleton_pid=state["lease"]._pid,
                                        oid=oid, tag=tag, lock_key=key)
            # The verifier receives readiness only after actual Uvicorn drain.
            state["server"].should_exit = True
            assert worker_release.wait(30)  # Fatal path must kill without release.
            raise AssertionError("rollback worker must never reach commit")
    else:
        emit("worker")
        assert worker_release.wait(30)


async def application(scope, receive, send):
    assert scope["type"] == "http"
    await PASSWORD_WORK_GATE.run(work)
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


Config = uvicorn.Config
Server = uvicorn.Server


def config(unused_app, **kwargs):
    kwargs.update(port=0, lifespan="off", log_config=None, log_level="critical")
    return Config(application, **kwargs)


class RunningServer(Server):
    def __init__(self, config):
        super().__init__(config)
        state["server"] = self

    async def startup(self, sockets=None):
        await super().startup(sockets=sockets)
        emit("ready", port=self.servers[0].sockets[0].getsockname()[1])

    async def _wait_tasks_to_complete(self):
        # Uvicorn reaches this after listeners/connections enter shutdown.
        emit("draining")
        if real_pg and mode == "rollback":
            emit("transaction", **state["transaction"])
        await super()._wait_tasks_to_complete()


uvicorn.Config, uvicorn.Server = config, RunningServer


def control():
    for command in sys.stdin:
        command = command.strip()
        if command == "FAIL":
            armed.set()
            if mode in {"loss", "cleanup-loss"} and not real_pg:
                state["lease"]._file.close()  # Actual ownership loss.
            state["monitor"].check_once() if mode in {"loss", "cleanup-loss", "execute", "rollback"} else None
        elif command == "RELEASE":
            worker_release.set()
        elif command == "STOP":
            state["server"].should_exit = True
        elif command == "FAIL_PROBE":
            probe_release.set()
        elif command == "DROP_ONLY":
            state["lease"]._file.close()
        elif command == "RELEASE_CLEANUP":
            cleanup_release.set()


OriginalThread(target=control, daemon=True).start()
raise SystemExit(launcher.main(["--deployment-id", identity,
                               "--lock-directory", directory]))
