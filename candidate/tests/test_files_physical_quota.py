"""F2, second round: an account's quota bounds the storage it physically occupies, however its uploads ended.

Codex's reproduction (verification of c6fcaeb): with a 4-byte, one-file quota, three uploads killed after their
data was renamed into place but before their record was written left 12 bytes on disk, none of it counted, and
the startup cleanup kept all three (a fresh blob is presumed an upload in progress). These tests kill real
processes at those points, restart the store, run concurrent uploads in separate processes, and inject write
failures. Every byte in the owner's folder is measured directly on disk.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

CANDIDATE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CANDIDATE))
from kalillac_files import store as store_module  # noqa: E402
from kalillac_files.store import FileStore, QuotaExceeded  # noqa: E402

POSIX = os.name == "posix"
OWNER = "synthetic-account"

# A child process that uploads once and dies (os._exit: no cleanup, no finally) at the chosen point.
DIE = r"""
import os, sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from kalillac_files.store import FileStore
root, point, quota_bytes, quota_files = Path(sys.argv[2]), sys.argv[3], int(sys.argv[4]), int(sys.argv[5])
s = FileStore(root, max_owner_bytes=quota_bytes, max_owner_files=quota_files)
original = FileStore._write
def write(path, data):
    if point == "before-rename" and path.suffix == ".bin":
        temporary = path.with_name(path.name + ".0123456789ab.tmp")
        temporary.write_bytes(data)
        os._exit(0)
    original(path, data)
    if point == "after-rename" and path.suffix == ".bin":
        os._exit(0)
s._write = write
try:
    s.put("synthetic-account", b"1234", "a.txt", "text/plain", "uploaded")
except Exception as error:
    print(type(error).__name__, getattr(error, "code", ""))
    os._exit(3)
"""


def physical(root: Path):
    """Bytes of file data (with or without a record) and of upload temporaries, under root, measured on disk."""
    return sum(f.stat().st_size for f in root.rglob("*") if f.is_file() and (f.suffix == ".bin" or ".bin." in f.name))


def die(root, point, quota_bytes=4, quota_files=1):
    return subprocess.run([sys.executable, "-B", "-c", DIE, str(CANDIDATE), str(root), point, str(quota_bytes),
                           str(quota_files)], capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("point", ["after-rename", "before-rename"])
def test_interrupted_uploads_never_occupy_more_than_the_quota_and_a_restart_recovers(tmp_path, point):
    """Codex's scenario: three uploads killed mid-way, then a restarted store."""
    root = tmp_path / "files"
    for _ in range(3):
        result = die(root, point)
        assert physical(root) <= 4, (point, physical(root))
        if not POSIX:                         # No cross-process lock: the abandoned data is counted, not reclaimed.
            assert result.returncode in (0, 3)
    restarted = FileStore(root, max_owner_bytes=4, max_owner_files=1)
    if POSIX:
        stored = restarted.put(OWNER, b"5678", "b.txt", "text/plain", "uploaded")
        assert [r.file_id for r in restarted.list(OWNER)] == [stored.file_id]
        assert restarted.get(OWNER, stored.file_id)[1] == b"5678"
    else:
        with pytest.raises(QuotaExceeded):
            restarted.put(OWNER, b"5678", "b.txt", "text/plain", "uploaded")
    assert physical(root) <= 4
    with pytest.raises(QuotaExceeded):
        restarted.put(OWNER, b"9", "c.txt", "text/plain", "uploaded")
    assert physical(root) <= 4


def test_abandoned_data_counts_against_the_quota_until_reclaimed(tmp_path, monkeypatch):
    """Data without its record and a temporary are counted as storage. Without a lock spanning processes (as on
    Windows) they cannot be told from an upload in progress, so they are counted and left for the cleanup."""
    root = tmp_path / "files"
    s = FileStore(root, max_owner_bytes=10, max_owner_files=3)
    folder = s._folder(OWNER)
    folder.mkdir(parents=True)
    (folder / ("A" * 22 + ".bin")).write_bytes(b"x" * 6)
    (folder / ("B" * 22 + ".bin.0123456789ab.tmp")).write_bytes(b"y" * 3)
    ids, size, leftover = FileStore._usage(folder)
    assert ids == {"A" * 22, "B" * 22} and size == 9 and sorted(leftover) == sorted(p.name for p in folder.iterdir())
    monkeypatch.setattr(store_module, "_FLOCK", False)
    with pytest.raises(QuotaExceeded, match="storage_quota_exceeded"):
        s.put(OWNER, b"zz", "z.txt", "text/plain", "uploaded")
    (folder / ("C" * 22 + ".json")).write_text("{}")
    with pytest.raises(QuotaExceeded, match="file_count_quota_exceeded"):
        s.put(OWNER, b"z", "z.txt", "text/plain", "uploaded")
    assert physical(root) == 9


def test_a_failed_record_write_leaves_no_data_and_no_temporary(tmp_path, monkeypatch):
    root = tmp_path / "files"
    s = FileStore(root, max_owner_bytes=4, max_owner_files=1)
    real = FileStore._write

    def write(path, data):
        if path.suffix == ".json":
            raise OSError(28, "No space left on device (injected)")
        real(path, data)
    monkeypatch.setattr(s, "_write", write)
    for _ in range(3):
        with pytest.raises(OSError):
            s.put(OWNER, b"1234", "a.txt", "text/plain", "uploaded")
        assert list(s._folder(OWNER).iterdir()) == []
    monkeypatch.undo()
    assert s.put(OWNER, b"1234", "a.txt", "text/plain", "uploaded").size == 4


def test_a_data_write_failing_part_way_removes_its_temporary(tmp_path, monkeypatch):
    root = tmp_path / "files"
    s = FileStore(root, max_owner_bytes=100, max_owner_files=3)
    real_fsync = os.fsync

    def fsync(fd):
        raise OSError(5, "I/O error (injected)")
    monkeypatch.setattr(store_module.os, "fsync", fsync)
    with pytest.raises(OSError):
        s.put(OWNER, b"1234", "a.txt", "text/plain", "uploaded")
    monkeypatch.setattr(store_module.os, "fsync", real_fsync)
    assert list(s._folder(OWNER).iterdir()) == [] and physical(root) == 0


CONCURRENT = r"""
import os, sys, time
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from kalillac_files.store import FileStore, QuotaExceeded
root, go = Path(sys.argv[2]), Path(sys.argv[3])
s = FileStore(root, max_owner_bytes=10, max_owner_files=100)
while not go.exists():
    time.sleep(0.005)
try:
    s.put("synthetic-account", b"abc", "a.txt", "text/plain", "uploaded")
    print("stored")
except QuotaExceeded as error:
    print(error.code)
"""


@pytest.mark.skipif(not POSIX, reason="the cross-process lock is POSIX (flock)")
def test_concurrent_uploads_in_separate_processes_cannot_exceed_the_quota(tmp_path):
    root = tmp_path / "files"
    FileStore(root)._folder(OWNER).mkdir(parents=True)
    go = tmp_path / "go"
    children = [subprocess.Popen([sys.executable, "-B", "-c", CONCURRENT, str(CANDIDATE), str(root), str(go)],
                                 stdout=subprocess.PIPE, text=True) for _ in range(8)]
    time.sleep(1.0)
    go.write_text("")
    outcomes = sorted(child.communicate(timeout=60)[0].strip() for child in children)
    assert outcomes.count("stored") == 3 and outcomes.count("storage_quota_exceeded") == 5, outcomes
    assert physical(root) == 9 and len(FileStore(root).list(OWNER)) == 3


def test_concurrent_uploads_in_threads_cannot_exceed_the_quota(tmp_path):
    root = tmp_path / "files"
    s = FileStore(root, max_owner_bytes=10, max_owner_files=100)
    barrier, outcomes = threading.Barrier(8), []

    def upload():
        barrier.wait()
        try:
            s.put(OWNER, b"abc", "a.txt", "text/plain", "uploaded")
            outcomes.append("stored")
        except QuotaExceeded as error:
            outcomes.append(error.code)
    threads = [threading.Thread(target=upload) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert outcomes.count("stored") == 3 and physical(root) == 9


IN_PROGRESS = r"""
import os, sys, time
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from kalillac_files.store import FileStore
root, started, release = Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4])
s = FileStore(root, max_owner_bytes=100, max_owner_files=10)
original = FileStore._write
def write(path, data):
    original(path, data)
    if path.suffix == ".bin":           # Data in place, record not yet written: an upload in progress.
        started.write_text("")
        while not release.exists():
            time.sleep(0.01)
s._write = write
print(s.put("synthetic-account", b"in progress", "p.txt", "text/plain", "uploaded").file_id)
"""


@pytest.mark.skipif(not POSIX, reason="the cross-process lock is POSIX (flock)")
def test_an_upload_in_progress_in_another_process_is_never_reclaimed(tmp_path):
    """While another process is between writing its data and its record, an upload here waits for it instead
    of treating its data as abandoned, and both files end complete."""
    root = tmp_path / "files"
    started, release = tmp_path / "started", tmp_path / "release"
    child = subprocess.Popen([sys.executable, "-B", "-c", IN_PROGRESS, str(CANDIDATE), str(root), str(started),
                              str(release)], stdout=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 30
    while not started.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert started.exists()
    here = FileStore(root, max_owner_bytes=100, max_owner_files=10)
    done = []
    worker = threading.Thread(target=lambda: done.append(here.put(OWNER, b"second", "s.txt", "text/plain",
                                                                  "uploaded")))
    worker.start()
    time.sleep(0.5)
    assert not done                                         # Waiting for the other upload, not reclaiming it.
    release.write_text("")
    other_id = child.communicate(timeout=30)[0].strip()
    worker.join(30)
    assert done and here.get(OWNER, other_id)[1] == b"in progress" and here.get(OWNER, done[0].file_id)[1] == b"second"


def test_reclaiming_never_touches_another_owners_folder_or_unknown_entries(tmp_path):
    root = tmp_path / "files"
    s = FileStore(root, max_owner_bytes=100, max_owner_files=10)
    other = s._folder("another-account")
    other.mkdir(parents=True)
    theirs = {other / ("D" * 22 + ".bin"): b"their abandoned data",
              other / ("E" * 22 + ".bin.0123456789ab.tmp"): b"their temporary"}
    mine = s._folder(OWNER)
    mine.mkdir(parents=True)
    unknown = {mine / "notes.txt": b"not FileStore's", mine / ("F" * 22 + ".bin.bak"): b"not FileStore's either"}
    for path, data in {**theirs, **unknown}.items():
        path.write_bytes(data)
    (mine / ("G" * 22 + ".bin")).write_bytes(b"my abandoned data")
    s.put(OWNER, b"new", "n.txt", "text/plain", "uploaded")
    assert all(path.read_bytes() == data for path, data in {**theirs, **unknown}.items())
    assert (mine / ("G" * 22 + ".bin")).exists() is not POSIX       # Reclaimed only under the cross-process lock.
