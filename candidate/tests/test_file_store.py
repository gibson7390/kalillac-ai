"""The owner-scoped file store: isolation, identifiers, quotas, retention and atomic writes."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import time

import pytest

from kalillac_files.store import FileNotFound, FileStore, QuotaExceeded

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 15, 9, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    return FileStore(tmp_path / "files", max_owner_bytes=1_000, max_owner_files=3, retention=timedelta(days=30),
                     clock=clock)


def test_a_file_is_stored_and_read_back_by_its_owner_only(store):
    stored = store.put("account-a", b"hello", "notes.docx", DOCX, "uploaded")
    record, data = store.get("account-a", stored.file_id)
    assert data == b"hello" and record == stored and record.size == 5
    for owner in ("account-b", "account-a "):
        with pytest.raises(FileNotFound):
            store.get(owner, stored.file_id)
        with pytest.raises(FileNotFound):
            store.delete(owner, stored.file_id)
        assert store.list(owner) == []
    assert [r.file_id for r in store.list("account-a")] == [stored.file_id]


def test_identifiers_are_random_and_never_paths(store, tmp_path):
    ids = {store.put("a", b"x", f"f{i}.txt", "text/plain", "generated").file_id for i in range(3)}
    assert len(ids) == 3 and all(len(file_id) == 22 for file_id in ids)
    for hostile in ("../a", "..", "a" * 22 + "/x", "", None, 7, "x" * 23, "AAAAAAAAAAAAAAAAAAAAA."):
        with pytest.raises(FileNotFound):
            store.get("a", hostile)
    folders = [path.name for path in (tmp_path / "files").iterdir()]
    assert folders and "a" not in folders and all(len(name) == 32 for name in folders)  # Hashed, not the id.
    public = store.list("a")[0].public()
    assert set(public) == {"id", "filename", "media_type", "size", "origin", "created_at", "expires_at", "parent_id"}


def test_quotas_are_checked_before_anything_is_written(store, tmp_path):
    for index in range(3):
        store.put("a", b"x" * 100, f"{index}.txt", "text/plain", "generated")
    with pytest.raises(QuotaExceeded) as full:
        store.put("a", b"x", "3.txt", "text/plain", "generated")
    assert full.value.code == "file_count_quota_exceeded"
    with pytest.raises(QuotaExceeded) as big:
        store.put("b", b"x" * 1_001, "big.txt", "text/plain", "generated")
    assert big.value.code == "storage_quota_exceeded"
    assert store.list("b") == []
    with pytest.raises(QuotaExceeded):
        store.put("c", b"", "empty.txt", "text/plain", "generated")


@pytest.mark.parametrize("filename, media_type, origin", [
    ("a/b.txt", "text/plain", "generated"), ("a\\b.txt", "text/plain", "generated"), ("", "text/plain", "generated"),
    ("x.txt", "text/plain", "downloaded"), ("x" * 256, "text/plain", "generated"), ("x.txt", 3, "generated"),
])
def test_invalid_records_are_refused(store, filename, media_type, origin):
    with pytest.raises(ValueError):
        store.put("a", b"x", filename, media_type, origin)


def test_expired_files_disappear_and_are_purged(store, clock, tmp_path):
    old = store.put("a", b"old", "old.txt", "text/plain", "generated")
    clock.now += timedelta(days=20)
    new = store.put("a", b"new", "new.txt", "text/plain", "edited", parent_id=old.file_id)
    clock.now += timedelta(days=11)
    with pytest.raises(FileNotFound):
        store.get("a", old.file_id)
    assert [r.file_id for r in store.list("a")] == [new.file_id]
    folder = next((tmp_path / "files").iterdir())
    # An upload's temporary file: removed only once abandoned (older than an hour), never while in progress.
    in_progress = folder / (new.file_id + ".bin.0123456789ab.tmp")
    abandoned = folder / (old.file_id + ".json.abcdefabcdef.tmp")
    for path, age in ((in_progress, timedelta(0)), (abandoned, timedelta(hours=2))):
        path.write_bytes(b"partial")
        stamp = time.time() - age.total_seconds()        # ages on disk are judged by the real clock
        os.utime(path, (stamp, stamp))
    assert store.purge() == 1 and not abandoned.exists() and in_progress.exists()
    assert sorted(path.name for path in folder.iterdir()) == sorted(
        [new.file_id + ".bin", new.file_id + ".json", in_progress.name])
    assert store.get("a", new.file_id)[1] == b"new"


def test_a_damaged_file_is_not_served(store, tmp_path):
    stored = store.put("a", b"hello", "x.txt", "text/plain", "generated")
    folder = next((tmp_path / "files").iterdir())
    (folder / (stored.file_id + ".bin")).write_bytes(b"hell")
    with pytest.raises(FileNotFound):
        store.get("a", stored.file_id)
    (folder / (stored.file_id + ".json")).write_text("{not json")
    with pytest.raises(FileNotFound):
        store.record("a", stored.file_id)


def test_deleting_removes_both_parts(store, tmp_path):
    stored = store.put("a", b"hello", "x.txt", "text/plain", "generated")
    store.delete("a", stored.file_id)
    assert list(next((tmp_path / "files").iterdir()).iterdir()) == []
    with pytest.raises(FileNotFound):
        store.delete("a", stored.file_id)
    assert not [name for name in os.listdir(tmp_path / "files") if name.endswith(".tmp")]
