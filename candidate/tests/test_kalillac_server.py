"""Controlled-launcher tests: no app services, providers or real database."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

import kalillac_server as server

CANDIDATE = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("argv,environ", [
    (["--workers", "1"], {}), (["--workers", "2"], {}),
    (["--reload"], {}), ([], {"WEB_CONCURRENCY": "2"}),
    ([], {"WEB_CONCURRENCY": "1"}), ([], {"UVICORN_WORKERS": "2"}),
    ([], {"UVICORN_RELOAD": "true"}), ([], {"GUNICORN_CMD_ARGS": "-w 2"}),
    ([], {"GUNICORN_CMD_ARGS": "--workers=2"}),
    ([], {"GUNICORN_CMD_ARGS": "-w2"}), ([], {}),
    (["--deployment-id", "../escape"], {}),
    (["--deployment-id", "prod", "--port", "0"], {}),
])
def test_unsupported_or_missing_configuration_is_refused(argv, environ):
    env = {"KALILLAC_DEPLOYMENT_ID": "prod", **environ}
    if not argv and not environ:
        env = {}
    with pytest.raises(server.LauncherConfigError):
        server.settings(argv, env)


def test_distinct_deployment_settings_have_stable_distinct_keys():
    prod = server.settings(["--deployment-id", "kalillac-site-production"], {})
    staging = server.settings(["--deployment-id", "kalillac-v31-staging", "--port", "8002"], {})
    assert prod.port == 8001 and staging.port == 8002
    assert server.lock_key(prod.deployment_id) != server.lock_key(staging.deployment_id)
    result = subprocess.run(
        [sys.executable, "-B", "-c",
         "from kalillac_server import lock_key; print(lock_key('kalillac-site-production'))"],
        cwd=CANDIDATE, capture_output=True, text=True, check=True,
    )
    assert int(result.stdout.strip()) == server.lock_key(prod.deployment_id)


def test_host_same_identity_exclusion_distinct_identity_and_cleanup(tmp_path):
    first = server.HostSingleton(tmp_path, "production")
    duplicate = server.HostSingleton(tmp_path, "production")
    staging = server.HostSingleton(tmp_path, "v31-staging")
    first.acquire()
    try:
        first.check()
        with pytest.raises(server.SingletonHeld):
            duplicate.acquire()
        staging.acquire()
        staging.check()
        staging.close()
    finally:
        first.close()
    duplicate.acquire()
    duplicate.check()
    duplicate.close()
    duplicate.close()
    with pytest.raises(server.SingletonLost):
        first.check()


def test_host_exclusion_between_processes_and_release_on_process_exit(tmp_path):
    source = (
        "from pathlib import Path; import sys; from kalillac_server import HostSingleton; "
        "lease=HostSingleton(Path(sys.argv[1]),sys.argv[2]); lease.acquire(); "
        "print('HELD',flush=True); sys.stdin.readline(); lease.close()"
    )
    child = subprocess.Popen(
        [sys.executable, "-B", "-c", source, str(tmp_path), "production"],
        cwd=CANDIDATE, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(child.stdout.readline).result(timeout=10).strip() == "HELD"
        duplicate = server.HostSingleton(tmp_path, "production")
        with pytest.raises(server.SingletonHeld):
            duplicate.acquire()
        child.stdin.write("\n")
        child.stdin.flush()
        assert child.wait(timeout=10) == 0
        duplicate.acquire()
        duplicate.close()
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)


class Connection:
    def __init__(self, *, acquired=True):
        self.closed = False
        self.invalidated = False
        self.pid = 1234
        self.held = acquired
        self.statements = []
    def execution_options(self, **kwargs):
        assert kwargs == {"isolation_level": "AUTOCOMMIT"}
        return self
    def execute(self, statement, parameters):
        self.statements.append((str(statement), parameters))
        return SimpleNamespace(one=lambda: (self.pid, self.held))
    def invalidate(self):
        self.invalidated = True
    def close(self):
        self.closed = True


def test_database_session_lock_health_reconnect_detection_and_cleanup():
    connection = Connection()
    lease = server.DatabaseSingleton(SimpleNamespace(connect=lambda: connection), "production")
    lease.acquire()
    lease.check()
    assert "pg_try_advisory_lock" in connection.statements[0][0]
    assert "pg_locks" in connection.statements[1][0]
    connection.pid += 1
    with pytest.raises(server.SingletonLost):
        lease.check()
    lease.close()
    assert connection.closed and connection.invalidated
    lease.close()


def test_database_lock_absent_or_connection_lost_is_not_healthy():
    for fault in ("held", "closed", "invalidated"):
        connection = Connection()
        lease = server.DatabaseSingleton(SimpleNamespace(connect=lambda: connection), "prod")
        lease.acquire()
        setattr(connection, fault, False if fault == "held" else True)
        with pytest.raises(server.SingletonLost):
            lease.check()
        lease.close()


def test_failed_database_acquisition_closes_its_connection():
    connection = Connection(acquired=False)
    lease = server.DatabaseSingleton(SimpleNamespace(connect=lambda: connection), "prod")
    with pytest.raises(server.SingletonHeld):
        lease.acquire()
    assert connection.closed and connection.invalidated


def test_lock_loss_stops_server_and_is_sticky(capsys):
    lease = SimpleNamespace(close=lambda: None, check=lambda: (_ for _ in ()).throw(server.SingletonLost()))
    instance = SimpleNamespace(should_exit=False)
    monitor = server.SingletonMonitor(lease, instance)
    monitor.check_once()
    monitor.check_once()
    assert monitor.lost and instance.should_exit
    assert capsys.readouterr().out.count("SINGLETON_LOCK_LOST") == 1
    assert monitor.close()


def test_monitor_orderly_stop_uses_event_without_sleep():
    instance = SimpleNamespace(should_exit=False)
    monitor = server.SingletonMonitor(SimpleNamespace(close=lambda: None, check=lambda: None), instance)
    monitor.start()
    assert monitor.close()
    assert not monitor._thread.is_alive() and not monitor.lost


@pytest.mark.parametrize("fails,starts", [(False, True), (True, True), (False, False)])
def test_launcher_configures_one_worker_and_always_cleans_up(monkeypatch, tmp_path, fails, starts):
    import kalillac_db.config
    monkeypatch.setattr(kalillac_db.config, "database_enabled", lambda: False)
    for name in ("WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_CMD_ARGS", "UVICORN_RELOAD"):
        monkeypatch.delenv(name, raising=False)
    events = []
    class Lease:
        def __init__(self, *args):
            events.append("lease")
        def acquire(self):
            events.append("acquire")
        def close(self):
            events.append("close")
    class Instance:
        should_exit = False
        started = starts
        def __init__(self, config):
            pass
        def run(self):
            events.append("run")
            if fails:
                raise RuntimeError("synthetic server failure")
    def config(app, **kwargs):
        assert app == "app_fastapi_candidate:api"
        assert kwargs["workers"] == 1 and kwargs["reload"] is False
        return kwargs
    class Monitor:
        lost = False
        def __init__(self, lease, *args, **kwargs):
            self.lease = lease
            assert callable(kwargs["on_failure"])
        def start(self):
            events.append("monitor-start")
        def close(self):
            events.append("monitor-stop")
            self.lease.close()
            return True
    monkeypatch.setattr(server, "HostSingleton", Lease)
    monkeypatch.setattr(server, "SingletonMonitor", Monitor)
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(Config=config, Server=Instance))
    result = server.main(["--deployment-id", "prod", "--lock-directory", str(tmp_path)])
    assert result == (1 if fails or not starts else 0)
    assert events == ["lease", "acquire", "monitor-start", "run", "monitor-stop", "close"]


def test_bounded_health_probe_failure_stops_server_without_closing_live_probe(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def blocked_check():
        entered.set()
        assert release.wait(5)
    instance = SimpleNamespace(should_exit=False)
    monitor = server.SingletonMonitor(SimpleNamespace(close=lambda: None, check=blocked_check), instance)
    monkeypatch.setattr(server, "HEALTH_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(server, "HEALTH_TIMEOUT_SECONDS", 0.0)
    monitor.start()
    try:
        assert entered.wait(5)
        monitor._thread.join(timeout=5)
        assert not monitor._thread.is_alive()
        assert monitor.lost and instance.should_exit
        assert monitor.close() is False
    finally:
        release.set()
        monkeypatch.setattr(server, "HEALTH_TIMEOUT_SECONDS", 3.0)
        assert monitor.close()



class LauncherChild:
    """Bounded pipe/event barriers around the real launcher and Uvicorn."""
    def __init__(self, directory, mode="normal", *, environ=None):
        import json
        import queue
        self._json, self._queue = json, queue.Queue()
        self.directory = directory
        self.identity = "review-singleton"
        self.events = []
        self._pending = []
        self.child = subprocess.Popen(
            [sys.executable, "-B", str(CANDIDATE / "tests/launcher_child_support.py"),
             mode, str(directory), self.identity],
            cwd=CANDIDATE, env=environ, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        def read():
            for line in self.child.stdout:
                self.events.append(line.rstrip())
                try:
                    self._queue.put(json.loads(line))
                except ValueError:
                    pass
        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()

    def event(self, name):
        import time
        deadline = time.monotonic() + 8
        for index, item in enumerate(self._pending):
            if item.get("event") == name:
                return self._pending.pop(index)
        while time.monotonic() < deadline:
            item = self._queue.get(timeout=max(0.01, deadline - time.monotonic()))
            if item.get("event") == name:
                return item
            self._pending.append(item)
        raise TimeoutError("child event: " + name)

    def command(self, command):
        self.child.stdin.write(command + "\n")
        self.child.stdin.flush()

    def close(self):
        if self.child.poll() is None:
            self.child.kill()
        self.child.wait(timeout=8)
        self.reader.join(timeout=8)
        assert not self.reader.is_alive()
        for stream in (self.child.stdin, self.child.stdout):
            stream.close()


@pytest.mark.parametrize("mode", ["loss", "construct", "start", "execute"])
def test_fatal_launcher_exit_kills_a_barrier_held_password_worker(tmp_path, mode):
    import httpx
    child = LauncherChild(tmp_path, mode)
    try:
        child.event("held")
        port = child.event("ready")["port"]
        def request():
            try:
                return httpx.get(f"http://127.0.0.1:{port}/", timeout=12).status_code
            except httpx.HTTPError:
                return "disconnected"
        with ThreadPoolExecutor(max_workers=1) as pool:
            response = pool.submit(request)
            child.event("worker")
            child.command("FAIL")
            child.event("lost")
            # The approved deadline is 2.0s; bounded join includes scheduler margin.
            assert child.child.wait(timeout=5) != 0
            assert response.result(timeout=5) == "disconnected"
        replacement = server.HostSingleton(tmp_path, child.identity)
        replacement.acquire()
        try:
            replacement.check()
        finally:
            replacement.close()
    finally:
        child.close()


def test_ordinary_launcher_shutdown_drains_worker_and_exits_zero(tmp_path):
    import httpx
    child = LauncherChild(tmp_path)
    try:
        child.event("held")
        port = child.event("ready")["port"]
        with ThreadPoolExecutor(max_workers=1) as pool:
            response = pool.submit(httpx.get, f"http://127.0.0.1:{port}/", timeout=10)
            child.event("worker")
            child.command("STOP")
            child.command("RELEASE")
            assert response.result(timeout=5).status_code == 200
        assert child.child.wait(timeout=8) == 0
        assert not any('"event": "lost"' in event for event in child.events)
    finally:
        child.close()


def test_fatal_supervisor_uses_two_seconds_and_cannot_be_disarmed_after_loss(monkeypatch):
    monkeypatch.setattr(server.time, 'monotonic', lambda: 10.0)
    entered, release = threading.Event(), threading.Event()
    calls = []
    fatal = server.FatalShutdown(terminate=lambda status: calls.append(status))
    class Deadline:
        def wait(self, timeout):
            assert timeout == 2.0 == server.FATAL_SHUTDOWN_SECONDS
            entered.set()
            assert release.wait(5)
    fatal._deadline = Deadline()
    fatal.start()
    fatal.trip()
    assert entered.wait(5)
    fatal.close()  # server.run returning cannot prove all workers stopped.
    assert calls == []
    release.set()
    fatal._thread.join(timeout=5)
    assert not fatal._thread.is_alive() and calls == [1]


def test_ordinary_supervisor_close_disarms_only_before_loss():
    calls = []
    fatal = server.FatalShutdown(terminate=lambda status: calls.append(status))
    fatal.start()
    fatal.close()
    fatal.trip()
    assert calls == [] and not fatal._tripped and not fatal._thread.is_alive()


@pytest.mark.parametrize("stage", ["construct", "start", "execute"])
def test_probe_failure_notifies_fatal_supervisor_and_unstarted_cleanup(monkeypatch, stage):
    calls = []
    instance = SimpleNamespace(should_exit=False)
    monitor = server.SingletonMonitor(SimpleNamespace(close=lambda: None, check=lambda: None), instance,
                                      on_failure=lambda: calls.append("fatal"))
    class RunOnce:
        def wait(self, timeout):
            return False
    monitor._stop = RunOnce()
    Thread = threading.Thread
    def threads(*args, **kwargs):
        if stage == "construct":
            raise RuntimeError("synthetic construction failure")
        if stage == "start":
            result = Thread(*args, **kwargs)
            result.start = lambda: (_ for _ in ()).throw(RuntimeError("synthetic start failure"))
            return result
        return Thread(*args, **kwargs)
    if stage == "execute":
        monitor.lease.check = lambda: (_ for _ in ()).throw(SystemExit("synthetic execution failure"))
    with monkeypatch.context() as patch:
        patch.setattr(server.threading, "Thread", threads)
        monitor._watch()
    assert monitor.lost and instance.should_exit and calls == ["fatal"]
    monitor._stop = threading.Event()
    assert monitor.close()  # Must not join a constructed but unstarted thread.


def test_probe_failure_racing_intentional_close_does_not_arm_fatal(monkeypatch):
    constructing, release = threading.Event(), threading.Event()
    calls = []
    monitor = server.SingletonMonitor(SimpleNamespace(close=lambda: None, check=lambda: None),
        SimpleNamespace(should_exit=False), on_failure=lambda: calls.append("fatal"))
    Thread = threading.Thread
    def threads(*args, **kwargs):
        if kwargs.get("name") == "kalillac-singleton-probe":
            constructing.set()
            assert release.wait(5)
            raise RuntimeError("synthetic shutdown race")
        return Thread(*args, **kwargs)
    monkeypatch.setattr(server.threading, "Thread", threads)
    monkeypatch.setattr(server, "HEALTH_INTERVAL_SECONDS", 0.0)
    monitor.start()
    assert constructing.wait(5)
    # State transition precedes releasing the blocked construction barrier.
    monitor.begin_shutdown()
    release.set()
    assert monitor.close()
    assert not monitor.lost and calls == []



@pytest.mark.parametrize("stage", ["construct", "start"])
def test_fatal_supervisor_startup_failure_refuses_server_and_cleans_up(monkeypatch, stage):
    import kalillac_db.config
    monkeypatch.setattr(kalillac_db.config, "database_enabled", lambda: False)
    for name in ("WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_CMD_ARGS", "UVICORN_RELOAD"):
        monkeypatch.delenv(name, raising=False)
    events = []
    class Lease:
        def __init__(self, *args):pass
        def acquire(self):events.append("acquired")
        def close(self):events.append("closed")
    Thread = threading.Thread
    def threads(*args, **kwargs):
        if kwargs.get("name") == "kalillac-singleton-fatal":
            if stage == "construct":
                raise RuntimeError("synthetic supervisor construction failure")
            result = Thread(*args, **kwargs)
            result.start = lambda: (_ for _ in ()).throw(RuntimeError("synthetic supervisor start failure"))
            return result
        return Thread(*args, **kwargs)
    monkeypatch.setattr(server, "HostSingleton", Lease)
    monkeypatch.setattr(server.threading, "Thread", threads)
    assert server.main(["--deployment-id", "prod"]) == 1
    assert events == ["acquired", "closed"]



def test_fatal_deadline_is_measured_from_first_loss_not_watchdog_scheduling(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: clock[0])
    calls, waits = [], []
    fatal = server.FatalShutdown(terminate=lambda status: calls.append(status))
    fatal.trip()
    assert fatal._expires_at == 12.0
    clock[0] = 11.9
    fatal.trip()
    assert fatal._expires_at == 12.0  # Repeated failure cannot extend the grace.
    clock[0] = 15.0
    class Deadline:
        def wait(self, timeout):
            waits.append(timeout)
    fatal._deadline = Deadline()
    fatal._watch()
    assert waits == [0.0] and calls == [1]

def test_actual_launcher_intentional_drain_ignores_probe_start_failure(tmp_path):
    import httpx
    child = LauncherChild(tmp_path, "drain-start")
    replacement = server.HostSingleton(tmp_path, child.identity)
    try:
        child.event("held")
        port = child.event("ready")["port"]
        with ThreadPoolExecutor(max_workers=1) as pool:
            response = pool.submit(httpx.get, f"http://127.0.0.1:{port}/", timeout=12)
            child.event("worker")
            child.event("probe_pending")
            child.command("STOP")  # Actual Uvicorn/ControlledServer shutdown path.
            child.event("draining")
            with pytest.raises(server.SingletonHeld):
                replacement.acquire()
            child.command("FAIL_PROBE")
            child.event("probe_start_failed")
            child.event("probe_ignored")
            child.event("ownership_checked")  # Monitoring resumes before work is released.
            assert child.child.poll() is None
            with pytest.raises(server.SingletonHeld):
                replacement.acquire()
            child.command("RELEASE")
            assert response.result(timeout=5).status_code == 200
        assert child.child.wait(timeout=5) == 0
        assert not any('"event": "fatal"' in event for event in child.events)
        replacement.acquire()
        replacement.check()
    finally:
        child.close()
        replacement.close()


@pytest.mark.parametrize("mode", ["loss", "execute"])
def test_actual_launcher_fatal_before_drain_cannot_be_downgraded(tmp_path, mode):
    import httpx
    import time
    child = LauncherChild(tmp_path, mode)
    try:
        child.event("held")
        port = child.event("ready")["port"]
        def request():
            try:
                return httpx.get(f"http://127.0.0.1:{port}/", timeout=12).status_code
            except httpx.HTTPError:
                return "disconnected"
        with ThreadPoolExecutor(max_workers=1) as pool:
            response = pool.submit(request)
            child.event("worker")
            child.command("FAIL")
            fatal = child.event("fatal")
            child.event("lost")
            child.command("STOP")
            child.event("draining")
            assert child.child.wait(timeout=5) != 0
            # Exact deadline is checked with a controlled clock separately.
            # Real-process observation allows only bounded scheduler margin.
            assert time.monotonic() - fatal["expires_at"] < 1.0
            assert response.result(timeout=5) == "disconnected"
    finally:
        child.close()


def test_intentional_shutdown_preserves_checks_and_distinguishes_failure_types():
    for loss_first in (False, True):
        calls = []
        monitor = server.SingletonMonitor(SimpleNamespace(close=lambda: None, check=lambda: None),
            SimpleNamespace(should_exit=False), on_failure=lambda: calls.append("fatal"))
        if loss_first:
            monitor._lost()
        monitor.begin_shutdown()
        monitor.begin_shutdown()
        monitor._lost(reason="infrastructure")
        assert monitor._phase == "draining" and not monitor._stop.is_set()
        assert monitor.lost is loss_first
        assert calls == (["fatal"] if loss_first else [])
        monitor._lost()  # Confirmed ownership loss always wins, including closing-first.
        assert monitor.lost and calls == ["fatal"]
        assert monitor.close()

@pytest.mark.parametrize("command", ["FAIL", "DROP_ONLY"])
def test_actual_launcher_ownership_loss_after_drain_is_fatal(tmp_path, command):
    import httpx
    import time
    child = LauncherChild(tmp_path, "loss")
    replacement = server.HostSingleton(tmp_path, child.identity)
    try:
        child.event("held")
        port = child.event("ready")["port"]
        def request():
            try:
                return httpx.get(f"http://127.0.0.1:{port}/", timeout=12).status_code
            except httpx.HTTPError:
                return "disconnected"
        with ThreadPoolExecutor(max_workers=1) as pool:
            response = pool.submit(request)
            child.event("worker")
            child.command("STOP")
            child.event("draining")
            child.command(command)  # DROP_ONLY requires the still-active periodic monitor.
            fatal = child.event("fatal")
            child.event("lost")
            replacement.acquire()
            replacement.check()
            assert child.child.poll() is None
            assert child.child.wait(timeout=5) != 0
            assert time.monotonic() - fatal["expires_at"] < 1.0
            assert response.result(timeout=5) == "disconnected"
            replacement.check()
        child.close()
        replacement.check()  # Old-process cleanup cannot release the replacement.
    finally:
        child.close()
        replacement.close()


def test_actual_launcher_loss_immediately_before_final_release_is_fatal(tmp_path):
    child = LauncherChild(tmp_path, "cleanup-loss")
    replacement = server.HostSingleton(tmp_path, child.identity)
    try:
        child.event("held")
        child.event("ready")
        child.command("STOP")
        child.event("draining")
        child.event("release_pending")
        child.command("FAIL")
        child.event("fatal")
        child.event("lost")
        replacement.acquire()
        child.command("RELEASE_CLEANUP")
        assert child.child.wait(timeout=5) != 0
        replacement.check()
        child.close()
        replacement.check()
    finally:
        child.close()
        replacement.close()


def test_final_check_and_release_share_one_exclusive_handoff_and_are_idempotent():
    events = []
    holder = {}
    def check():
        events.append("check")
        assert not holder["monitor"]._check_lock.acquire(blocking=False)
    def release():
        events.append("release")
        monitor = holder["monitor"]
        assert monitor._phase == "releasing"
        assert not monitor._check_lock.acquire(blocking=False)
        monitor._lost()  # Own intentional release is not an ownership-loss event.
    monitor = server.SingletonMonitor(SimpleNamespace(check=check, close=release),
        SimpleNamespace(should_exit=False))
    holder["monitor"] = monitor
    monitor._stop.set()  # Deterministic final handoff, without waiting for a timer tick.
    monitor.start()
    assert monitor.close()
    assert monitor.close()
    monitor._thread.join(timeout=5)
    assert events == ["check", "release"]
    assert monitor._phase == "closed" and not monitor.lost
    assert not monitor._thread.is_alive()


@pytest.mark.parametrize("invalidated", [False, True])
def test_database_transport_loss_is_distinct_from_healthy_session_statement_error(invalidated):
    from sqlalchemy.exc import OperationalError
    connection = Connection()
    lease = server.DatabaseSingleton(SimpleNamespace(connect=lambda: connection), "production")
    lease.acquire()
    def failed(*args, **kwargs):
        raise OperationalError("synthetic statement", {}, None, connection_invalidated=invalidated)
    connection.execute = failed
    try:
        with pytest.raises(server.SingletonLost if invalidated else OperationalError):
            lease.check()
    finally:
        lease.close()


def test_final_check_detects_loss_and_release_failure_cannot_report_success():
    for fail_release in (False, True):
        events = []
        def check():
            if not fail_release:
                raise server.SingletonLost()
        def release():
            events.append("release")
            if fail_release:
                raise RuntimeError("synthetic cleanup failure")
        monitor = server.SingletonMonitor(SimpleNamespace(check=check, close=release),
            SimpleNamespace(should_exit=False), on_failure=lambda: events.append("fatal"))
        monitor.start()
        assert monitor.close() is (not fail_release)
        assert monitor.lost
        assert events.count("fatal") == events.count("release") == 1
        assert monitor.close() is (not fail_release)
        assert events.count("release") == 1

@pytest.mark.parametrize("closed_handle", [False, True])
def test_host_closed_handle_is_distinct_from_unverifiable_permission_error(tmp_path, closed_handle):
    import errno
    lease = server.HostSingleton(tmp_path, "production")
    def fileno():
        if closed_handle:
            raise ValueError("synthetic closed file")
        raise PermissionError(errno.EACCES, "synthetic permission failure")
    lease._file = SimpleNamespace(closed=False, fileno=fileno)
    with pytest.raises(server.SingletonLost if closed_handle else PermissionError):
        lease.check()
