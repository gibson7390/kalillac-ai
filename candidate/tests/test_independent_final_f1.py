"""Independent extra checks; disposable fixture code only."""
import os
from pathlib import Path
import signal
import subprocess
import time
import pytest
import test_local_reset as cases
from test_local_reset import tool, checkout, quick
local = cases.local
REAL_PG_RUNNING = local.pg_running
def deletion_recorder(calls):
    def record(*args, **kwargs):
        calls.append((args, kwargs))
    record.avoids_symlink_attacks = True
    return record

@pytest.mark.parametrize("command", ["stop", "reset"])
def test_unreaped_leader_with_live_child_is_not_confirmed_stopped(tool, checkout, quick, monkeypatch, tmp_path, command):
    child_file = tmp_path / "child.pid"
    monkeypatch.setenv("CHILD_PID_FILE", str(child_file))
    cases.write_script("app", cases.WITH_CHILD.replace("time.sleep(120)\n", ""))
    app = checkout("app")
    child = None
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if child_file.exists() and local._stat(app.pid)[0] == "Z":
                break
            time.sleep(0.05)
        child = int(child_file.read_text())
        assert local._stat(app.pid)[0] == "Z" and cases.alive(child)
        p = local.paths()
        before_records = cases.records(p, "app")
        deletion_calls = []
        monkeypatch.setattr(local.shutil, "rmtree", deletion_recorder(deletion_calls))
        args = ("reset", "--yes") if command == "reset" else ("stop",)
        code = tool(*args, root=checkout.root)
        observation = {
            "command": command, "exit": code, "child_alive": cases.alive(child),
            "records_before": before_records, "records_after": cases.records(p, "app"),
            "deletion_calls": len(deletion_calls)
        }
        print("UNREAPED_LEADER", observation)
        assert code == 2 and cases.records(p, "app") == before_records and not deletion_calls, observation
    finally:
        if child is not None and cases.alive(child):
            os.kill(child, signal.SIGKILL)
        app.wait(timeout=10)

@pytest.mark.parametrize("statuses,stop_rc,expected", [
    ([1], 0, 2), ([4], 0, 2), ([0,1], 0, 2), ([0,4], 0, 2),
    ([3], 0, 0), ([0,3], 0, 0), ([0], 1, 2),
])
def test_postgresql_shutdown_requires_an_established_state(tool, checkout, monkeypatch, statuses, stop_rc, expected):
    (checkout.root / "pg").mkdir()
    monkeypatch.setattr(local, "pg_bin", lambda home: "/nonexistent")
    pending = iter(statuses)
    calls = []
    def pg_command(argv, uid, gid, **kwargs):
        command = "status" if argv[-1] == "status" else "stop"
        calls.append(command)
        rc = next(pending) if command == "status" else stop_rc
        return subprocess.CompletedProcess(argv, rc, stderr=b"injected PostgreSQL result")
    monkeypatch.setattr(local, "pg_running", REAL_PG_RUNNING)
    monkeypatch.setattr(local, "as_user", pg_command)
    deletion_calls = []
    monkeypatch.setattr(local.shutil, "rmtree", deletion_recorder(deletion_calls))
    code = tool("reset", "--yes", root=checkout.root)
    observation = {"status_returncodes":statuses, "stop_returncode":stop_rc, "exit":code,
                   "commands":calls, "deletion_calls":len(deletion_calls)}
    print("POSTGRES_STATUS", observation)
    assert code == expected and len(deletion_calls) == (1 if expected == 0 else 0), observation

@pytest.mark.parametrize("command", ["stop", "reset", "refresh-web"])
def test_unreadable_process_identity_prevents_follow_on_work(tool, checkout, monkeypatch, command):
    app = checkout("web" if command == "refresh-web" else "app")
    time.sleep(0.2)
    p = local.paths()
    monkeypatch.setattr(local, "_identity", lambda *a: "unknown")
    changes = []
    monkeypatch.setattr(local.shutil, "rmtree", deletion_recorder(changes))
    monkeypatch.setattr(local, "build_webroot", lambda *a: changes.append("build"))
    args = ("reset", "--yes") if command == "reset" else (command,)
    assert tool(*args, root=checkout.root) == 2
    assert cases.alive(app.pid) and not changes
    assert cases.records(p, "web" if command == "refresh-web" else "app")
