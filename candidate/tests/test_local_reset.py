"""Local staging's root (tools/local_staging.py): no command stops a process in, or deletes, any directory but
local staging's own, proven by its ownership marker. These run the real command on real directories."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys

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
        assert local._ours(bystander.pid, "app") is False and local._ours(os.getpid(), "web") is False
    finally:
        os.killpg(bystander.pid, signal.SIGKILL)
        bystander.wait()
