"""kalillac_files.retention: deleting expired files from real storage, safely (data lifecycle, item 1).

Every case uses the real FileStore on a real filesystem: expiry, the explicitly configured period, account
isolation (other owners, foreign names, symlinks), data-before-record ordering, abandoned temporaries and
orphaned data, unreadable records, repeated runs, and concurrent runs with uploads and reads."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import multiprocessing
import os
from pathlib import Path
import time

import pytest

from kalillac_files import retention
from kalillac_files.retention import SAFETY_MARGIN, clean
from kalillac_files.store import FileNotFound, FileStore

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
DAY = timedelta(days=1)
POSIX = os.name == "posix"


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def store_at(root, when):
    return FileStore(root, retention=timedelta(days=30), clock=Clock(when))


def folder_of(root, owner):
    return store_at(root, T0)._folder(owner)


def put(root, owner, when, data=b"content", name="report.pdf"):
    return store_at(root, when).put(owner, data, name, "application/pdf", "generated")


def snapshot(root):
    return sorted(str(p.relative_to(root)) for p in Path(root).rglob("*") if p.is_file() or p.is_symlink())


@pytest.fixture
def root(tmp_path):
    path = tmp_path / "files"
    path.mkdir()
    return path


def test_a_file_is_deleted_only_after_expiry_plus_the_configured_period_plus_the_margin(root):
    stored = put(root, "a", T0)                                     # expires T0 + 30 days
    expiry = T0 + 30 * DAY
    for now in (expiry, expiry + 7 * DAY, expiry + 7 * DAY + SAFETY_MARGIN - timedelta(seconds=1)):
        report = clean(root, delete_after=7 * DAY, now=now, apply=True)
        assert report.expired_files == 0 and report.deleted_files == 0, now
    report = clean(root, delete_after=7 * DAY, now=expiry + 7 * DAY + SAFETY_MARGIN, apply=True)
    assert (report.expired_files, report.deleted_files, report.deleted_bytes) == (1, 1, len(b"content"))
    assert snapshot(root) == []
    with pytest.raises(FileNotFound):
        store_at(root, T0).get("a", stored.file_id)


def test_a_dry_run_decides_the_same_and_deletes_nothing(root):
    for when in (T0, T0 + DAY, T0 + 40 * DAY):
        put(root, "a", when)
    before = snapshot(root)
    now = T0 + 45 * DAY
    dry = clean(root, delete_after=timedelta(0), now=now, apply=False)
    assert snapshot(root) == before and dry.applied is False and dry.expired_files == 2 and dry.deleted_files == 0
    real = clean(root, delete_after=timedelta(0), now=now, apply=True)
    assert real.expired_files == 2 and real.deleted_files == 2 and len(snapshot(root)) == 2   # one file left


@pytest.mark.parametrize("delete_after", [None, -DAY, 7, "7d"])
def test_the_period_must_be_explicit_and_non_negative(root, delete_after):
    with pytest.raises(ValueError):
        clean(root, delete_after=delete_after, now=T0)


def test_refuses_a_naive_clock_and_a_root_that_is_not_a_real_directory(root, tmp_path):
    with pytest.raises(ValueError):
        clean(root, delete_after=DAY, now=datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        clean(tmp_path / "missing", delete_after=DAY, now=T0)


def test_only_the_expired_files_of_each_account_are_deleted_and_no_other_account_is_touched(root):
    old_a = put(root, "account-a", T0)
    new_a = put(root, "account-a", T0 + 20 * DAY)
    new_b = put(root, "account-b", T0 + 20 * DAY)
    old_c = put(root, "account-c", T0)
    report = clean(root, delete_after=timedelta(0), now=T0 + 31 * DAY, apply=True)
    assert report.deleted_files == 2 and report.folders == 3
    for owner, stored in (("account-a", new_a), ("account-b", new_b)):
        assert store_at(root, T0 + 31 * DAY).get(owner, stored.file_id)[1] == b"content"
    for owner, stored in (("account-a", old_a), ("account-c", old_c)):
        assert not (folder_of(root, owner) / (stored.file_id + ".bin")).exists()


def test_entries_not_written_by_the_store_are_never_deleted(root):
    stored = put(root, "a", T0)
    folder = folder_of(root, "a")
    # A foreign folder holding a perfect copy of an expired file, a stray file at the root, and odd names
    # inside an owner folder: none is FileStore's, so none is touched.
    foreign = root / "not-an-owner-folder"
    foreign.mkdir()
    for name in (stored.file_id + ".json", stored.file_id + ".bin"):
        (foreign / name).write_bytes((folder / name).read_bytes())
    (root / "notes.txt").write_text("keep")
    for name in ("readme.json", "x.bin", stored.file_id + ".json.bak", "leftover.tmp"):
        (folder / name).write_text("keep")
    report = clean(root, delete_after=timedelta(0), now=T0 + 31 * DAY, apply=True)
    assert report.deleted_files == 1 and report.skipped_entries == 2 + 4
    assert sorted(p.name for p in foreign.iterdir()) == sorted([stored.file_id + ".json", stored.file_id + ".bin"])
    assert (root / "notes.txt").exists() and len(list(folder.iterdir())) == 4


def test_a_record_that_does_not_describe_its_own_file_is_never_deleted(root):
    first, second = put(root, "a", T0), put(root, "a", T0)
    folder = folder_of(root, "a")
    record = json.loads((folder / (first.file_id + ".json")).read_text())
    record["file_id"] = second.file_id                            # copied or tampered record
    (folder / (first.file_id + ".json")).write_text(json.dumps(record))
    (folder / (second.file_id + ".json")).write_text("{not json")
    report = clean(root, delete_after=timedelta(0), now=T0 + 31 * DAY, apply=True)
    assert report.unreadable_records == 2 and report.deleted_files == 0 and len(snapshot(root)) == 4


@pytest.mark.skipif(not POSIX, reason="symlink safety is enforced with POSIX directory descriptors")
def test_symlinks_are_never_followed(root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = put(outside, "victim", T0)                           # an expired file outside the root
    victim_folder = folder_of(outside, "victim")
    # 1. An owner-named folder that is a symlink to another account's folder elsewhere.
    os.symlink(victim_folder, root / ("f" * 32))
    # 2. Inside a real owner folder: an expired record whose data is a symlink to a file outside.
    mine = put(root, "a", T0)
    folder = folder_of(root, "a")
    target = outside / "precious.bin"
    target.write_bytes(b"precious")
    (folder / (mine.file_id + ".bin")).unlink()
    os.symlink(target, folder / (mine.file_id + ".bin"))
    # 3. A record name that is a symlink to an expired record elsewhere.
    os.symlink(victim_folder / (victim.file_id + ".json"), folder / ("Z" * 22 + ".json"))
    clean(root, delete_after=timedelta(0), now=T0 + 31 * DAY, apply=True)
    assert (victim_folder / (victim.file_id + ".bin")).read_bytes() == b"content"
    assert (victim_folder / (victim.file_id + ".json")).exists() and target.read_bytes() == b"precious"


def test_data_is_deleted_before_its_record_so_an_interruption_never_orphans_data(root, monkeypatch):
    stored = put(root, "a", T0)
    folder = folder_of(root, "a")
    original = retention._Folder.unlink

    def interrupted(self, name):
        if name.endswith(".json"):
            raise KeyboardInterrupt                               # the run stops between the two deletions
        return original(self, name)
    monkeypatch.setattr(retention._Folder, "unlink", interrupted)
    with pytest.raises(KeyboardInterrupt):
        clean(root, delete_after=timedelta(0), now=T0 + 31 * DAY, apply=True)
    assert not (folder / (stored.file_id + ".bin")).exists() and (folder / (stored.file_id + ".json")).exists()
    monkeypatch.setattr(retention._Folder, "unlink", original)
    report = clean(root, delete_after=timedelta(0), now=T0 + 31 * DAY, apply=True)
    assert report.deleted_files == 1 and snapshot(root) == []


def test_abandoned_temporaries_and_orphaned_data_go_only_once_they_are_old(root):
    live = put(root, "a", T0 + 29 * DAY)
    folder = folder_of(root, "a")
    now = T0 + 31 * DAY
    entries = {"fresh tmp": (live.file_id + ".bin.0123456789ab.tmp", 0), "old tmp": ("B" * 22 + ".json.0123456789ab.tmp", 2),
               "fresh orphan": ("C" * 22 + ".bin", 0), "old orphan": ("D" * 22 + ".bin", 2)}
    for name, hours in entries.values():
        (folder / name).write_bytes(b"x")
        stamp = time.time() - hours * 3600              # ages on disk are judged by the real clock
        os.utime(folder / name, (stamp, stamp))
    report = clean(root, delete_after=timedelta(0), now=now, apply=True)
    assert (report.stale_temporaries, report.orphaned_data, report.deleted_files) == (1, 1, 0)
    present = {p.name for p in folder.iterdir()}
    assert {entries["fresh tmp"][0], entries["fresh orphan"][0]} <= present
    assert not {entries["old tmp"][0], entries["old orphan"][0]} & present
    assert store_at(root, now).get("a", live.file_id)[1] == b"content"


def test_an_upload_caught_between_its_data_and_its_record_survives_any_expiry_clock(root):
    # Found by the concurrency test: FileStore writes the data, then the record. Ages on disk are judged
    # by the real clock, so even a cleanup whose expiry clock is years ahead leaves a fresh upload alone.
    stored = put(root, "a", T0 + 29 * DAY)
    folder = folder_of(root, "a")
    (folder / (stored.file_id + ".json")).unlink()              # the record is not written yet
    (folder / (stored.file_id + ".json.0123456789ab.tmp")).write_bytes(b"{}")
    report = clean(root, delete_after=timedelta(0), now=T0 + 1000 * DAY, apply=True)
    assert report.orphaned_data == 0 and report.stale_temporaries == 0
    assert (folder / (stored.file_id + ".bin")).exists()


def test_repeated_cleanup_is_idempotent(root):
    for owner in ("a", "b"):
        for _ in range(3):
            put(root, owner, T0)
    now = T0 + 31 * DAY
    first = clean(root, delete_after=timedelta(0), now=now, apply=True)
    second = clean(root, delete_after=timedelta(0), now=now, apply=True)
    assert first.deleted_files == 6 and second.as_dict() == {**first.as_dict(), "expired_files": 0, "deleted_files": 0,
                                                             "deleted_bytes": 0}
    assert snapshot(root) == []


def test_concurrent_cleanups_with_uploads_and_reads_lose_nothing_live_and_delete_each_expired_file_once(root):
    owners = [f"account-{i}" for i in range(6)]
    expired = [put(root, owner, T0) for owner in owners for _ in range(5)]
    now = T0 + 31 * DAY
    live = [put(root, owner, now - DAY) for owner in owners for _ in range(3)]
    writer = store_at(root, now)

    def upload(i):
        return writer.put(owners[i % 6], b"fresh %d" % i, "new.pdf", "application/pdf", "uploaded")

    def read(stored_and_owner):
        stored, owner = stored_and_owner
        return writer.get(owner, stored.file_id)[1]

    with ThreadPoolExecutor(max_workers=16) as pool:
        cleanups = [pool.submit(clean, root, delete_after=timedelta(0), now=now, apply=True) for _ in range(6)]
        uploads = [pool.submit(upload, i) for i in range(30)]
        reads = [pool.submit(read, (stored, owners[i // 3])) for i, stored in enumerate(live)]
        reports = [f.result() for f in cleanups]
        fresh = [f.result() for f in uploads]
        assert all(f.result() == b"content" for f in reads)
    # A file another process holds open (Windows) is left for the next run, never an error; one more run
    # finishes, and across all runs each expired file is deleted exactly once.
    reports.append(clean(root, delete_after=timedelta(0), now=now, apply=True))
    if POSIX:
        # unlink is atomic: exactly one run deletes each file. (Windows can report a delete-pending file's
        # removal to two runs; only the count is affected, never which files go.)
        assert sum(r.deleted_files for r in reports) == len(expired)
    for stored in expired:
        assert not any(folder_of(root, o).joinpath(stored.file_id + ".bin").exists() for o in owners)
    for i, stored in enumerate(fresh):
        assert writer.get(owners[i % 6], stored.file_id)[1] == b"fresh %d" % i
    assert len(snapshot(root)) == 2 * (len(live) + len(fresh))


def _clean_in_process(args):
    root, now = args
    return clean(root, delete_after=timedelta(0), now=datetime.fromisoformat(now), apply=True).deleted_files


@pytest.mark.skipif(not POSIX, reason="separate processes on the same files: the deployment platform")
def test_cleanups_in_separate_processes_delete_each_file_once(root):
    for owner in ("a", "b", "c"):
        for _ in range(10):
            put(root, owner, T0)
    keep = put(root, "a", T0 + 29 * DAY)
    with multiprocessing.get_context("spawn").Pool(4) as pool:
        counts = pool.map(_clean_in_process, [(str(root), (T0 + 31 * DAY).isoformat())] * 4)
    assert sum(counts) == 30
    assert store_at(root, T0 + 31 * DAY).get("a", keep.file_id)[1] == b"content"
    assert len(snapshot(root)) == 2


# --- the operator command (tools/data_lifecycle.py files) ------------------------------------------------

import io  # noqa: E402
import sys as _sys  # noqa: E402

_sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import data_lifecycle  # noqa: E402

ENV = {"KALILLAC_DEPLOYMENT_ID": "kalillac-local-staging"}


def command(root, *extra, environ=ENV):
    out = io.StringIO()
    code = data_lifecycle.main(["files", "--root", str(root), *extra], environ=environ, out=out)
    return code, json.loads(out.getvalue())


@pytest.mark.parametrize("extra,environ", [
    (("--delete-after-expiry-days", "0", "--apply"), ENV),                                  # no confirmation
    (("--delete-after-expiry-days", "0", "--apply", "--confirm-deployment", "production"), ENV),
    (("--delete-after-expiry-days", "0", "--apply", "--confirm-deployment", "x"), {}),      # no deployment id
    (("--delete-after-expiry-days", "-1"), ENV), (("--delete-after-expiry-days", "soon"), ENV)])
def test_the_command_refuses_without_an_explicit_period_and_the_operators_confirmation(root, extra, environ):
    stored = put(root, "a", T0)
    before = snapshot(root)
    code, report = command(root, *extra, environ=environ)
    assert code == 2 and report["result"] == "refused" and snapshot(root) == before, report
    assert stored.file_id not in json.dumps(report)


def test_the_command_reports_counts_only_and_deletes_only_when_confirmed(root):
    stored = put(root, "account-a", T0 - 60 * DAY)                 # long expired by the real clock
    code, dry = command(root, "--delete-after-expiry-days", "0")
    assert code == 0 and dry["applied"] is False and dry["expired_files"] == 1 and dry["deleted_files"] == 0
    code, done = command(root, "--delete-after-expiry-days", "0", "--apply", "--confirm-deployment",
                         "kalillac-local-staging")
    assert code == 0 and done["applied"] is True and done["deleted_files"] == 1
    for report in (dry, done):
        text = json.dumps(report)
        assert stored.file_id not in text and "account-a" not in text and str(root) not in text
        assert folder_of(root, "account-a").name not in text


@pytest.mark.skipif(not POSIX, reason="the run lock is a POSIX advisory lock")
def test_a_second_concurrent_command_finds_the_run_lock_held(root):
    import fcntl
    held = os.open(root / retention.LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        code, report = command(root, "--delete-after-expiry-days", "0")
        assert code == 3 and report == {"command": "files", "result": "busy"}
    finally:
        os.close(held)
    assert command(root, "--delete-after-expiry-days", "0")[0] == 0
