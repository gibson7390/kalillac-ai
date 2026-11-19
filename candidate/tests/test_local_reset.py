"""Local staging's root (tools/local_staging.py): no command stops a process in, or deletes, any directory but
local staging's own, proven by its ownership marker. These run the real command on real directories."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

pytest.importorskip("pwd")                 # Linux and WSL only, like the tool.
CANDIDATE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CANDIDATE / "tools"))
import local_staging as local  # noqa: E402

RECORD = json.dumps({"deployment": local.DEPLOYMENT, "release_sha": "0" * 40, "web": local.ORIGIN, "stripe": "simulated"})


@pytest.fixture
def tool(monkeypatch, tmp_path):
    """The command as root would run it, with this test's uid standing in for root's."""
    monkeypatch.setattr(local, "ROOT_OWNER", os.getuid())
    monkeypatch.setattr(local, "_running_as_root", lambda: True)
    monkeypatch.setattr(local, "DEFAULT_ROOT", tmp_path / "kalillac-local-staging")
    monkeypatch.setattr(local, "ROOT", local.DEFAULT_ROOT)
    os.chmod(tmp_path, 0o755)
    stopped = []
    real_stop = local.stop
    monkeypatch.setattr(local, "stop", lambda args: stopped.append(str(local.ROOT)) or real_stop(args))

    def run(*argv, root=None):
        if root is None:
            monkeypatch.delenv("KALILLAC_LOCAL_ROOT", raising=False)
        else:
            monkeypatch.setenv("KALILLAC_LOCAL_ROOT", str(root))
        try:
            return local.main(list(argv))
        except SystemExit as exit:
            return exit.code
    run.stopped = stopped
    return run


def tree(path: Path):
    """Every entry under path with its kind and contents, symlinks not followed."""
    out = {}
    for folder, dirs, files in os.walk(path):
        for name in dirs + files:
            full = Path(folder, name)
            rel = str(full.relative_to(path))
            if full.is_symlink():
                out[rel] = ("link", os.readlink(full))
            elif full.is_dir():
                out[rel] = ("dir", oct(full.stat().st_mode))
            else:
                out[rel] = ("file", full.read_bytes())
    return out


def customer_data(path: Path):
    path.mkdir(mode=0o755)
    (path / "important.txt").write_text("someone's work")
    (path / "nested").mkdir()
    (path / "nested" / "photo.jpg").write_bytes(b"\xff\xd8 not local staging")
    return path


def marked(path: Path):
    """A root as setup leaves it."""
    assert local.owned_root(str(path), create=True) == path
    (path / "state").mkdir()
    (path / "state" / "app.log").write_text("log")
    (path / "local.json").write_text(RECORD)
    return path


# --- refused: nothing is stopped and nothing is deleted ---------------------------------------------------

@pytest.mark.parametrize("raw", ["/", "/var", "/var/lib", "/home", "/root", "/tmp", "/mnt/c", "/usr", "/etc",
                                 "kalillac-local-staging", "./kalillac-local-staging",
                                 "/var/lib/kalillac-local-staging/..", "/var/lib//kalillac-local-staging",
                                 "/var/lib/kalillac-local-staging/", "/var/lib/kalillac-local-staging-",
                                 "/var/lib/Kalillac-Local-Staging", "/var/lib/kalillac-local-staging.bak", ""])
def test_a_broad_relative_or_misnamed_root_is_refused_before_anything_runs(tool, raw):
    assert tool("reset", "--yes", root=raw) == 2 and tool.stopped == []


def test_an_unrelated_directory_is_refused_and_untouched(tool, tmp_path):
    home = customer_data(tmp_path / "home")
    before = tree(home)
    assert tool("reset", "--yes", root=home) == 2 and tool("stop", root=home) == 2
    assert tool.stopped == [] and tree(home) == before


def test_a_correctly_named_directory_without_the_marker_is_refused_and_untouched(tool, tmp_path):
    root = customer_data(tmp_path / "kalillac-local-staging-other")
    before = tree(root)
    for argv in (("reset", "--yes"), ("stop",), ("start",), ("status",), ("refresh-web",)):
        assert tool(*argv, root=root) == 2
    assert tool("setup", root=root) == 2                         # Setup takes only an empty directory.
    assert tool.stopped == [] and tree(root) == before and not (root / local.MARKER).exists()


def test_a_symlink_to_a_marked_root_or_to_customer_data_is_refused(tool, tmp_path):
    real = marked(tmp_path / "kalillac-local-staging")
    data = customer_data(tmp_path / "data")
    before, before_data = tree(real), tree(data)
    (tmp_path / "links").mkdir(mode=0o755)
    for target in (real, data):
        link = tmp_path / "links" / "kalillac-local-staging"
        link.unlink(missing_ok=True)
        link.symlink_to(target)
        assert tool("reset", "--yes", root=link) == 2
    inside = tmp_path / "via"
    inside.symlink_to(tmp_path)
    assert tool("reset", "--yes", root=inside / "kalillac-local-staging") == 2
    assert tool.stopped == [] and tree(real) == before and tree(data) == before_data


def test_a_marker_copied_from_another_root_or_deployment_is_refused(tool, tmp_path):
    original = marked(tmp_path / "kalillac-local-staging")
    copy = customer_data(tmp_path / "kalillac-local-staging-copy")
    (copy / local.MARKER).write_bytes((original / local.MARKER).read_bytes())
    other = customer_data(tmp_path / "kalillac-local-staging-other")
    st = os.stat(other)
    (other / local.MARKER).write_text(json.dumps({"deployment": "kalillac-staging-v1", "root": str(other),
                                                  "device": st.st_dev, "inode": st.st_ino}))
    before = tree(copy), tree(other)
    assert tool("reset", "--yes", root=copy) == 2 and tool("reset", "--yes", root=other) == 2
    assert tool.stopped == [] and (tree(copy), tree(other)) == before


def test_a_root_recreated_in_place_no_longer_matches_its_marker(tool, tmp_path):
    root = marked(tmp_path / "kalillac-local-staging")
    marker = (root / local.MARKER).read_bytes()
    os.rename(root, tmp_path / "moved-away")
    customer_data(root)
    (root / local.MARKER).write_bytes(marker)                       # Same path, different directory.
    before = tree(root)
    assert tool("reset", "--yes", root=root) == 2 and tool.stopped == [] and tree(root) == before


def test_a_symlinked_unreadable_or_writable_marker_or_root_is_refused(tool, tmp_path):
    good = marked(tmp_path / "kalillac-local-staging")
    linked = customer_data(tmp_path / "kalillac-local-staging-linked")
    (linked / local.MARKER).symlink_to(good / local.MARKER)
    garbled = customer_data(tmp_path / "kalillac-local-staging-garbled")
    (garbled / local.MARKER).write_text("{not json")
    writable = marked(tmp_path / "kalillac-local-staging-writable")
    os.chmod(writable, 0o777)
    open_marker = marked(tmp_path / "kalillac-local-staging-openmarker")
    os.chmod(open_marker / local.MARKER, 0o666)
    for root in (linked, garbled, writable, open_marker):
        before = tree(root)
        assert tool("reset", "--yes", root=root) == 2 and tree(root) == before
    assert tool.stopped == []


def test_a_root_in_a_directory_others_can_write_is_refused(tool, tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o1777)                                       # Like /tmp.
    root = shared / "kalillac-local-staging"
    assert tool("setup", root=root) == 2 and not root.exists()


# --- accepted: exactly local staging's own directory -----------------------------------------------------

def test_reset_removes_its_own_marked_root_and_nothing_beside_or_beyond_it(tool, tmp_path):
    neighbour = customer_data(tmp_path / "kalillac-local-staging-neighbour")
    outside = customer_data(tmp_path / "outside")
    root = marked(tmp_path / "kalillac-local-staging")
    (root / "state" / "escape").symlink_to(outside)                # A link out must not be followed.
    (root / "state" / "escape-file").symlink_to(outside / "important.txt")
    before = tree(neighbour), tree(outside)
    assert tool("reset", "--yes", root=root) == 0
    assert not os.path.lexists(root) and tool.stopped == [str(root)]
    assert (tree(neighbour), tree(outside)) == before
    assert tool("reset", "--yes", root=root) == 0                  # Nothing left: says so, deletes nothing.


def test_setup_creates_and_marks_a_new_root_or_takes_an_empty_one(tool, tmp_path, monkeypatch):
    monkeypatch.setattr(local, "setup", lambda args: None)        # Only the root's preparation, not the cluster.
    fresh = tmp_path / "kalillac-local-staging"
    assert tool("setup", root=fresh) == 0
    marker = json.loads((fresh / local.MARKER).read_text())
    st = os.stat(fresh)
    assert marker == {"deployment": local.DEPLOYMENT, "root": str(fresh), "device": st.st_dev, "inode": st.st_ino}
    assert oct(st.st_mode & 0o777) == "0o755"
    empty = tmp_path / "kalillac-local-staging-empty"
    empty.mkdir(mode=0o755)
    assert tool("setup", root=empty) == 0 and (empty / local.MARKER).is_file()


def test_a_root_set_up_before_markers_is_adopted_only_at_the_default_path_and_never_by_reset(tool, tmp_path):
    legacy = local.DEFAULT_ROOT
    customer_data(legacy)
    (legacy / "local.json").write_text(RECORD)
    before = tree(legacy)
    assert tool("reset", "--yes") == 2 and tree(legacy) == before   # Reset never adopts.
    elsewhere = customer_data(tmp_path / "kalillac-local-staging-elsewhere")
    (elsewhere / "local.json").write_text(RECORD)
    assert tool("status", root=elsewhere) == 2 and not (elsewhere / local.MARKER).exists()
    assert tool("status") == 0 and (legacy / local.MARKER).is_file()  # Status adopts the default root.
    assert tool("reset", "--yes") == 0 and not legacy.exists()


def test_stop_never_signals_a_process_that_is_not_local_stagings(tool, tmp_path):
    root = marked(tmp_path / "kalillac-local-staging")
    (root / "run").mkdir()
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        for name in ("app", "web"):
            (root / "run" / f"{name}.pid").write_text(str(bystander.pid))
        assert tool("stop", root=root) == 0
        assert bystander.poll() is None                            # Still running: not ours, never signalled.
        for pid in (bystander.pid, os.getpid()):
            assert local._ours({"pid": pid, "written": 0, "legacy": True}, "app", os.getuid()) is False
    finally:
        os.killpg(bystander.pid, signal.SIGKILL)
        bystander.wait()


# --- F1, second round: the whole path, and the process's identity -------------------------------------------

def test_an_ancestor_others_can_write_is_refused_but_a_sticky_one_like_tmp_is_not(tool, tmp_path):
    for label, mode, expected in (("open", 0o777, 2), ("group", 0o775, 2), ("sticky", 0o1777, 0)):
        ancestor = tmp_path / label
        ancestor.mkdir(mode=0o755)
        (ancestor / "parent").mkdir(mode=0o755)
        root = marked(ancestor / "parent" / "kalillac-local-staging")
        os.chmod(ancestor, mode)
        before = tree(root)
        assert tool("reset", "--yes", root=root) == expected, label
        assert (tree(root) == before) if expected else not os.path.lexists(root)


def exchange(first: Path, second: Path, saved: Path):
    """What the owner of an ancestor can do in one instant: move one directory aside, put another in its place."""
    os.rename(first, saved)
    os.rename(second, first)


def raced(real_rmtree, swap):
    def rmtree(path, *args, **kwargs):
        swap()
        return real_rmtree(path, *args, **kwargs)
    rmtree.avoids_symlink_attacks = True
    return rmtree


def test_an_ancestor_exchanged_after_the_last_check_cannot_redirect_the_delete(tool, tmp_path, monkeypatch):
    """Codex's reproduction: the parent exchanged for another directory immediately before the recursive delete.
    The delete is bound to the checked parent's descriptor, so it removes the checked root, wherever it now is,
    and the directory swapped in under the same path survives untouched."""
    ancestor = tmp_path / "ancestor"
    ancestor.mkdir(mode=0o755)
    parent = ancestor / "parent"
    parent.mkdir(mode=0o755)
    root = marked(parent / "kalillac-local-staging")
    (root / "valid-deployment.txt").write_text("disposable fixture")
    replacement = ancestor / "replacement"
    replacement.mkdir(mode=0o755)
    victim = customer_data(replacement / "kalillac-local-staging")
    before = tree(victim)
    monkeypatch.setattr(local.shutil, "rmtree",
                        raced(local.shutil.rmtree, lambda: exchange(parent, replacement, ancestor / "saved")))
    assert tool("reset", "--yes", root=root) == 0
    assert tree(parent / "kalillac-local-staging") == before                      # The unrelated data survives.
    assert not os.path.lexists(ancestor / "saved" / "kalillac-local-staging")     # The checked root was deleted.


def test_an_ancestor_exchanged_before_the_pinned_check_is_refused(tool, tmp_path, monkeypatch):
    """Exchanged after the first check but before the root is pinned (while stopping): the root then found is
    not the marked one, so nothing is deleted, there or in the moved original."""
    ancestor = tmp_path / "ancestor"
    ancestor.mkdir(mode=0o755)
    parent = ancestor / "parent"
    parent.mkdir(mode=0o755)
    root = marked(parent / "kalillac-local-staging")
    replacement = ancestor / "replacement"
    replacement.mkdir(mode=0o755)
    victim = customer_data(replacement / "kalillac-local-staging")
    before, original = tree(victim), tree(root)
    monkeypatch.setattr(local, "stop", lambda args: exchange(parent, replacement, ancestor / "saved"))
    assert tool("reset", "--yes", root=root) == 2
    assert tree(parent / "kalillac-local-staging") == before
    assert tree(ancestor / "saved" / "kalillac-local-staging") == original


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0, reason="needs root: another user's ancestor")
def test_as_root_an_ancestor_another_user_owns_is_refused_and_its_exchange_deletes_nothing(monkeypatch, tmp_path):
    """Codex's exact scenario, as root: an unprivileged user (uid 65534) owns an ancestor and exchanges the
    root-owned parent for its own directory immediately before the delete."""
    monkeypatch.setattr(local, "ROOT_OWNER", 0)
    monkeypatch.setattr(local, "_running_as_root", lambda: True)
    monkeypatch.setattr(local, "stop", lambda args: None)
    os.chmod(tmp_path, 0o755)
    untrusted = tmp_path / "untrusted"
    untrusted.mkdir(mode=0o755)
    parent = untrusted / "root-owned-parent"
    parent.mkdir(mode=0o755)
    root = parent / "kalillac-local-staging"
    os.chown(untrusted, 65534, 65534)
    with pytest.raises(local.UnsafeRoot):                       # Setup itself refuses such a path.
        local.owned_root(str(root), create=True)
    os.chown(untrusted, 0, 0)
    local.owned_root(str(root), create=True)                   # Marked while the path was trusted...
    (root / "valid-deployment.txt").write_text("disposable fixture")
    os.chown(untrusted, 65534, 65534)                          # ...then an ancestor given to another user.
    replacement = untrusted / "replacement"
    replacement.mkdir(mode=0o755)
    victim = customer_data(replacement / "kalillac-local-staging")
    os.chown(replacement, 65534, 65534)
    before, original = tree(victim), tree(root)

    def swap():
        subprocess.run([sys.executable, "-c", "import os,sys;os.rename(sys.argv[1],sys.argv[2]);"
                        "os.rename(sys.argv[3],sys.argv[1])", str(parent), str(untrusted / "saved"), str(replacement)],
                       user=65534, group=65534, extra_groups=[], check=True)
    monkeypatch.setattr(local.shutil, "rmtree", raced(local.shutil.rmtree, swap))
    monkeypatch.setenv("KALILLAC_LOCAL_ROOT", str(root))
    with pytest.raises(SystemExit) as refused:
        local.main(["reset", "--yes"])
    assert refused.value.code == 2 and tree(root) == original and tree(victim) == before


# --- processes: only the ones this deployment launched from this checkout ------------------------------------

HARMLESS = "import time\ntime.sleep(120)\n"


@pytest.fixture
def checkout(tool, tmp_path, monkeypatch):
    """A checkout whose staging scripts are harmless, a marked root with run/ and state/, the test's user as the
    service user."""
    import grp
    candidate = tmp_path / "checkout" / "candidate"
    (candidate / "tools").mkdir(parents=True)
    for script in local.SCRIPTS.values():
        (candidate / script).write_text(HARMLESS)
    monkeypatch.setattr(local, "CANDIDATE", candidate)
    monkeypatch.setattr(local, "identity", lambda: (os.getuid(), os.getgid(), grp.getgrgid(os.getgid()).gr_name,
                                                   str(tmp_path)))
    root = marked(tmp_path / "kalillac-local-staging")
    (root / "run").mkdir()
    monkeypatch.setattr(local, "ROOT", root)
    monkeypatch.setattr(local, "pg_running", lambda *args: False)
    started = []

    def launch(name, cwd=None, argv=None):
        if cwd is None:
            process = local._spawn(name, [], local.paths(), os.getuid(), os.getgid(), dict(os.environ))
        else:
            process = subprocess.Popen(argv or [sys.executable, "-B", local.SCRIPTS[name]], cwd=cwd,
                                       start_new_session=True)
        started.append(process)
        return process
    launch.root = root
    yield launch
    for process in started:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def gone(process):
    try:
        process.wait(timeout=10)
        return True
    except subprocess.TimeoutExpired:
        return False


def test_stop_ends_local_stagings_own_processes(tool, checkout):
    app, web = checkout("app"), checkout("web")
    p = local.paths()
    assert local._running(p, "app") == app.pid and local._running(p, "web") == web.pid
    assert oct(os.stat(p["run"] / "app.json").st_mode & 0o777) == "0o600"
    assert tool("stop", root=checkout.root) == 0
    assert gone(app) and gone(web) and app.returncode == -signal.SIGTERM
    assert not any((p["run"] / f).exists() for f in ("app.json", "web.json", "app.pid", "web.pid"))


def test_stop_never_signals_another_checkouts_process_with_the_same_script(tool, checkout, tmp_path):
    """Codex's reproduction: tools/local_staging_web.py run from another checkout, its PID in run/."""
    other = tmp_path / "other-checkout"
    (other / "tools").mkdir(parents=True)
    (other / "tools" / "local_staging_web.py").write_text(HARMLESS)
    relative = checkout("web", cwd=other)
    absolute = checkout("app", cwd=other, argv=[sys.executable, "-B", str(other / "tools" / "local_staging_web.py")])
    time.sleep(0.2)
    p = local.paths()
    (p["run"] / "web.pid").write_text(str(relative.pid))         # As an older start would have written it.
    (p["run"] / "app.json").write_text(json.dumps({            # A record as complete as one can be without
        "pid": absolute.pid, "started": local._started(absolute.pid), "boot": local._boot(),   # the launch.
        "token": "0" * 32, "script": local._script("app"), "checkout": local._checkout()}))
    assert tool("stop", root=checkout.root) == 0
    assert relative.poll() is None and absolute.poll() is None   # Neither was signalled.


def test_a_reused_pid_a_wrong_token_or_another_user_is_not_ours(tool, checkout):
    app = checkout("app")
    p = local.paths()
    record = json.loads((p["run"] / "app.json").read_text())
    uid = os.getuid()
    assert local._ours(record, "app", uid)
    for change in ({"started": record["started"] + 1},             # Same PID, another process: reused.
                   {"boot": "another-boot"},
                   {"token": "f" * 32},                            # Not the token this launch was given.
                   {"token": None},
                   {"script": "/elsewhere/tools/local_staging_app.py"},
                   {"pid": os.getpid()},
                   {"pid": 1}):
        assert not local._ours({**record, **change}, "app", uid), change
    assert not local._ours(record, "web", uid)                    # Not the other name's script.
    assert not local._ours(record, "app", uid + 1)                # Not the service user's.
    (p["run"] / "app.json").unlink()
    (p["run"] / "app.json").write_text(json.dumps({**record, "token": "f" * 32}))
    assert tool("stop", root=checkout.root) == 0 and app.poll() is None


def test_a_process_launched_before_records_existed_is_stopped_only_if_its_pid_file_is_its_own(tool, checkout):
    """Upgrading must not strand the running instance: the old start launched `python -B tools/NAME.py` from the
    checkout and wrote run/NAME.pid. It is ours only if it started no later than that file was written and at
    most two minutes before (a PID reused since then started later)."""
    p = local.paths()
    stale = checkout("web", cwd=local.CANDIDATE)
    time.sleep(0.2)
    (p["run"] / "web.pid").write_text(str(stale.pid))
    hour_ago = time.time() - 3600
    os.utime(p["run"] / "web.pid", (hour_ago, hour_ago))          # Written long before this process started.
    assert local._running(p, "web") is None
    old = checkout("app", cwd=local.CANDIDATE)
    time.sleep(0.2)
    (p["run"] / "app.pid").write_text(str(old.pid))
    assert local._running(p, "app") == old.pid
    assert tool("stop", root=checkout.root) == 0
    assert gone(old) and stale.poll() is None


def test_a_process_of_this_checkout_without_the_launch_token_is_not_ours(tool, checkout):
    """The same script, from this checkout, started by hand: its PID put in a record copied from a real launch."""
    real = checkout("app")
    p = local.paths()
    record = json.loads((p["run"] / "app.json").read_text())
    by_hand = checkout("web", cwd=local.CANDIDATE, argv=[sys.executable, "-B", local._script("app")])
    time.sleep(0.2)
    forged = {**record, "pid": by_hand.pid, "started": local._started(by_hand.pid)}
    assert not local._ours(forged, "app", os.getuid())
    (p["run"] / "app.json").unlink()
    (p["run"] / "app.json").write_text(json.dumps(forged))
    assert tool("stop", root=checkout.root) == 0
    assert by_hand.poll() is None and real.poll() is None         # The forged one untouched; the real one unnamed.


# --- F1, third round: a stop is confirmed or it fails; every signal goes through a pidfd -------------------

IGNORES_TERM = "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(120)\n"
WITH_CHILD = ("import os, subprocess, sys, time\n"
              "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
              "open(os.environ['CHILD_PID_FILE'], 'w').write(str(child.pid))\n"
              "time.sleep(120)\n")


@pytest.fixture
def quick(monkeypatch):
    """Short grace periods, so an unresponsive process is decided in seconds, not half a minute."""
    monkeypatch.setattr(local, "STOP_GRACE", 1.0, raising=False)
    monkeypatch.setattr(local, "KILL_WAIT", 2.0, raising=False)
    real_select = local.select.select
    monkeypatch.setattr(local.select, "select", lambda r, w, x, timeout=None: real_select(r, w, x, min(timeout, 2.0)))


def records(p, name):
    return [f for f in (f"{name}.json", f"{name}.pid") if (p["run"] / f).exists()]


def alive(pid):
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        return state != "Z"
    except OSError:
        return False


def write_script(name, text):
    (local.CANDIDATE / local.SCRIPTS[name]).write_text(text)


def test_a_process_that_ignores_sigterm_is_never_reported_stopped_while_it_runs(tool, checkout, quick):
    """Codex's F1-C: the wait for the exit timed out, yet the records were removed and "stopped" reported, so a
    reset would then have deleted the files under a running process. Now either the stop is confirmed (here, by
    SIGKILL after the grace period, through the pidfd) or it fails and keeps the records."""
    write_script("app", IGNORES_TERM)
    app = checkout("app")
    time.sleep(0.3)
    p = local.paths()
    code = tool("stop", root=checkout.root)
    assert (code == 0 and gone(app) and not records(p, "app")) or \
           (code == 2 and alive(app.pid) and records(p, "app") == ["app.json"]), (code, app.poll(), records(p, "app"))
    assert code == 0 and app.returncode == -signal.SIGKILL


def test_an_unstoppable_process_keeps_its_records_and_reset_deletes_nothing(tool, checkout, quick, monkeypatch):
    """Signals that never take effect (a process stuck in the kernel): the stop is not confirmed, so it fails,
    keeps the records, and reset never reaches its delete."""
    app = checkout("app")
    time.sleep(0.3)
    p = local.paths()
    monkeypatch.setattr(local.signal, "pidfd_send_signal", lambda fd, sig, *args, **kwargs: None)
    monkeypatch.setattr(local.os, "killpg", lambda pid, sig: None)
    deleted = []
    monkeypatch.setattr(local.shutil, "rmtree", raced(local.shutil.rmtree, lambda: deleted.append(True)))
    before = tree(checkout.root)
    assert tool("stop", root=checkout.root) == 2
    assert alive(app.pid) and records(p, "app") == ["app.json"]
    assert tool("reset", "--yes", root=checkout.root) == 2
    assert deleted == [] and alive(app.pid) and tree(checkout.root) == before


@pytest.mark.parametrize("unavailable", ["error", "missing"])
def test_without_a_process_handle_the_stop_fails_and_nothing_is_deleted(tool, checkout, quick, monkeypatch,
                                                                       unavailable):
    app = checkout("app")
    time.sleep(0.3)
    p = local.paths()
    if unavailable == "error":
        def refuse(pid, flags=0):
            raise OSError(38, "Function not implemented (injected)")
        monkeypatch.setattr(local.os, "pidfd_open", refuse)
    else:
        monkeypatch.delattr(local.os, "pidfd_open")
    deleted = []
    monkeypatch.setattr(local.shutil, "rmtree", raced(local.shutil.rmtree, lambda: deleted.append(True)))
    assert tool("stop", root=checkout.root) == 2
    assert tool("reset", "--yes", root=checkout.root) == 2
    assert alive(app.pid) and records(p, "app") == ["app.json"] and deleted == []


def numeric_signals_forbidden(patch):
    def forbidden(*args):
        raise AssertionError("a numeric process or group signal was sent")
    patch.setattr(local.os, "killpg", forbidden)
    patch.setattr(local.os, "kill", forbidden)


@pytest.mark.parametrize("path", ["group-flag", "member-by-member"])
def test_stop_ends_the_whole_group_through_pidfds_only(tool, checkout, quick, monkeypatch, tmp_path, path):
    """The application's own children (its group) end with it. With PIDFD_SIGNAL_PROCESS_GROUP (Linux 6.9) the
    group is signalled through the leader's pidfd; where the kernel refuses that flag, each member through its
    own pidfd while the leader is alive, the leader last. Never a numeric kill or killpg."""
    if path == "member-by-member":
        monkeypatch.setattr(local, "PIDFD_SIGNAL_PROCESS_GROUP", 1 << 30, raising=False)   # Refused: EINVAL.
    child_file = tmp_path / "child.pid"
    monkeypatch.setenv("CHILD_PID_FILE", str(child_file))
    write_script("app", WITH_CHILD)
    app = checkout("app")
    deadline = time.monotonic() + 10
    while not child_file.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    child = int(child_file.read_text())
    assert os.getpgid(child) == app.pid
    with monkeypatch.context() as patch:              # Only around the stop: the fixture's cleanup may kill.
        numeric_signals_forbidden(patch)
        assert tool("stop", root=checkout.root) == 0
    assert gone(app) and app.returncode == -signal.SIGTERM
    deadline = time.monotonic() + 5
    while alive(child) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not alive(child) and not records(local.paths(), "app")


def test_a_member_is_signalled_only_while_it_is_in_the_leaders_group(checkout, monkeypatch):
    """The member path checks each process through its own pidfd: a process outside the group (here, one in a
    session of its own) is never signalled, even when its PID is offered as a member."""
    leader = checkout("app")
    outsider = checkout("web", cwd=local.CANDIDATE)
    time.sleep(0.3)
    fd = os.pidfd_open(leader.pid)
    try:
        since = local._started(leader.pid)
        assert local._signal_member(outsider.pid, fd, leader.pid, since, signal.SIGTERM) is False
        time.sleep(0.2)
        assert outsider.poll() is None
    finally:
        os.close(fd)


def test_a_group_left_behind_by_an_exited_leader_is_reported_not_guessed(tool, checkout, quick, monkeypatch,
                                                                        tmp_path):
    """The leader is gone but processes of its group remain: without a live leader the group's id is no longer
    proof of identity, so nothing is signalled, the stop fails with their PIDs, and the records are kept."""
    child_file = tmp_path / "child.pid"
    monkeypatch.setenv("CHILD_PID_FILE", str(child_file))
    write_script("app", WITH_CHILD.replace("time.sleep(120)\n", "") + "")
    app = checkout("app")
    app.wait(timeout=10)                                   # The leader exits; its child stays in the group.
    child = int(child_file.read_text())
    try:
        assert alive(child)
        assert tool("stop", root=checkout.root) == 2
        assert alive(child) and records(local.paths(), "app") == ["app.json"]
    finally:
        os.kill(child, signal.SIGKILL)


def test_postgresql_that_does_not_stop_fails_the_stop(tool, checkout, quick, monkeypatch):
    (checkout.root / "pg").mkdir()
    monkeypatch.setattr(local, "pg_running", lambda *args: True)
    monkeypatch.setattr(local, "pg_bin", lambda home: "/nonexistent")
    monkeypatch.setattr(local, "as_user", lambda argv, uid, gid, **kwargs: subprocess.CompletedProcess(argv, 1))
    deleted = []
    monkeypatch.setattr(local.shutil, "rmtree", raced(local.shutil.rmtree, lambda: deleted.append(True)))
    assert tool("stop", root=checkout.root) == 2
    assert tool("reset", "--yes", root=checkout.root) == 2 and deleted == []


def test_this_kernel_either_signals_a_group_through_a_pidfd_or_refuses_the_flag(tmp_path):
    """Records which path the environment uses (CI and WSL): the flag works, or is refused with EINVAL, never
    anything else."""
    import errno
    import platform
    leader = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    fd = os.pidfd_open(leader.pid)
    try:
        try:
            signal.pidfd_send_signal(fd, 0, None, local.PIDFD_SIGNAL_PROCESS_GROUP)
            outcome = "supported"
        except OSError as error:
            assert error.errno == errno.EINVAL, error
            outcome = "refused (EINVAL): member-by-member"
        print(f"kernel {platform.release()}: PIDFD_SIGNAL_PROCESS_GROUP {outcome}")
    finally:
        os.close(fd)
        leader.kill()
        leader.wait()


# --- F1-C, final round: an exited leader and PostgreSQL's status prove nothing they do not say ---------------
# Codex's own cases are kept unchanged in tests/test_independent_final_f1.py; these complete them.

EXITS_AT_ONCE = "import sys\nsys.exit(0)\n"
REAL_PG_RUNNING = local.pg_running              # The checkout fixture replaces it; these tests need it.


def zombie(process, timeout=10):
    """Wait until the process has exited but is not reaped (its parent, this test, has not waited for it)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if local._stat(process.pid)[0] == "Z":
            return True
        time.sleep(0.05)
    return False


def test_an_unreaped_leader_with_a_live_child_is_reported_never_signalled_and_recoverable(tool, checkout, quick,
                                                                                           monkeypatch, tmp_path):
    """The leader has exited (a zombie, its PID still held) and its child runs on: the stop fails and keeps the
    record, nothing is signalled (the child is untouched), and once the child is gone the next stop confirms."""
    child_file = tmp_path / "child.pid"
    monkeypatch.setenv("CHILD_PID_FILE", str(child_file))
    write_script("app", WITH_CHILD.replace("time.sleep(120)\n", ""))
    app = checkout("app")
    deadline = time.monotonic() + 10
    while not child_file.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert zombie(app)
    child = int(child_file.read_text())
    sent = []
    real = local.signal.pidfd_send_signal
    monkeypatch.setattr(local.signal, "pidfd_send_signal",
                        lambda fd, sig, *a: sent.append(int(sig)) if sig else real(fd, sig, *a))
    try:
        assert tool("stop", root=checkout.root) == 2
        assert alive(child) and sent == [] and records(local.paths(), "app") == ["app.json"]
    finally:
        os.kill(child, signal.SIGKILL)
    deadline = time.monotonic() + 5
    while alive(child) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert tool("stop", root=checkout.root) == 0 and not records(local.paths(), "app")
    app.wait(timeout=10)


def test_an_unreaped_leader_whose_group_has_ended_is_confirmed_stopped(tool, checkout, quick):
    """Normal shutdown is unchanged: a leader that exited with nothing left in its group is simply gone."""
    write_script("app", EXITS_AT_ONCE)
    app = checkout("app")
    assert zombie(app)
    assert tool("stop", root=checkout.root) == 0 and not records(local.paths(), "app")
    app.wait(timeout=10)


@pytest.mark.parametrize("returncode", [1, 2, 4, 127])
def test_start_refuses_when_postgresqls_status_is_unknown(checkout, monkeypatch, returncode):
    """Every command that asks pg_ctl status treats an answer other than 0 or 3 as unknown: start fails rather
    than starting a second server over a data directory it could not read."""
    started = []

    def pg_command(argv, uid, gid, **kwargs):
        started.append(argv[-1])
        return subprocess.CompletedProcess(argv, returncode if argv[-1] == "status" else 0, stdout="", stderr="")
    monkeypatch.setattr(local, "as_user", pg_command)
    monkeypatch.setattr(local, "pg_running", REAL_PG_RUNNING)
    with pytest.raises(SystemExit) as refused:
        local.pg_start(local.paths(), "/nonexistent", os.getuid(), os.getgid())
    assert refused.value.code == 2 and started == ["status"]


def _pg_bin():
    for candidate in (os.environ.get("KALILLAC_LOCAL_PG_BIN", ""),
                      str(Path.home() / "pg16tools/lib/python3.12/site-packages/pgserver/pginstall/bin"),
                      "/usr/lib/postgresql/16/bin"):
        if candidate and Path(candidate, "pg_ctl").is_file():
            return candidate
    return None


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0 or _pg_bin() is None,
                    reason="initdb refuses root; needs PostgreSQL 16 binaries")
def test_pg_running_reads_a_real_pg_ctl_status(tmp_path, monkeypatch, checkout):
    """Against the real pg_ctl and a disposable cluster: 4 (no cluster there) is unknown, 3 is stopped, 0 is
    running, and stop() confirms the stop through the same status."""
    import socket
    bindir = _pg_bin()
    monkeypatch.setattr(local, "pg_running", REAL_PG_RUNNING)
    monkeypatch.setattr(local, "as_user", lambda argv, uid, gid, env=None, **kwargs: subprocess.run(argv, env=env,
                                                                                                    **kwargs))
    p = dict(local.paths(), pg=tmp_path / "cluster", state=tmp_path / "state", pgsock=tmp_path / "sock")
    p["state"].mkdir()
    p["pgsock"].mkdir()
    p["pg"].mkdir(mode=0o700)
    uid, gid = os.getuid(), os.getgid()
    with pytest.raises(local.PgStatusUnknown, match="exited 4"):
        local.pg_running(p, bindir, uid, gid)
    subprocess.run([f"{bindir}/initdb", "-D", str(p["pg"]), "-U", "postgres", "--auth=trust", "-E", "UTF8"],
                   check=True, capture_output=True)
    assert local.pg_running(p, bindir, uid, gid) is False
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr(local, "PG_PORT", port)
    try:
        local.pg_start(p, bindir, uid, gid)
        assert local.pg_running(p, bindir, uid, gid) is True
    finally:
        subprocess.run([f"{bindir}/pg_ctl", "-D", str(p["pg"]), "stop", "-m", "immediate"], capture_output=True)
    assert local.pg_running(p, bindir, uid, gid) is False
