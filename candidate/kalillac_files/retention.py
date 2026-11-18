"""Deleting expired files from storage: the enforcement half of the file retention policy.

A file stops being available at its recorded expires_at (FileStore's availability period, 30 days today).
This module deletes it from disk once it has been expired for an explicitly configured further period
(`delete_after`, required, no default; the owner's decision D-9) plus a fixed safety margin. It is run by
an operator command (tools/data_lifecycle.py files), as the service identity that owns the files; the
application never calls it.

Safety rules, each tested in tests/test_file_retention.py:

* **Account isolation.** Only folders whose names are exactly a FileStore owner folder (32 lowercase hex)
  are entered, and in each, only entries named exactly as FileStore writes them (`<id>.json`,
  `<id>.bin`, `<id>.<bin|json>.<12 hex>.tmp`). A file is deleted only on the evidence of its own record in
  its own folder; nothing in one folder is decided from another. Anything else (another name, a
  directory, a symlink, a device) is counted as skipped and never touched.
* **No symlink is ever followed.** On POSIX each folder is opened with O_NOFOLLOW | O_DIRECTORY and
  every stat and unlink is relative to that descriptor, so a folder or file replaced by a symlink during
  the run cannot redirect a deletion.
* **Data before record.** An expired file's data is deleted before its record, so an interrupted run
  leaves an expired record (finished by the next run), never data without a record.
* **No race with the application.** A file is deleted only when it has been expired for `delete_after`
  plus SAFETY_MARGIN (an hour), long after FileStore stopped serving it. An upload's temporary file, and
  data whose record is not written yet, is removed only once older than STALE_TEMPORARY (an hour) by the
  real clock; a fresh one is an upload in progress.
* **Unreadable records are never deleted automatically**; they are counted for an operator to inspect.
* **Dry run** (`apply=False`) decides exactly the same and deletes nothing.

Reports carry counts only: never a file name, an owner, a folder name or a path.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import stat
import time

SAFETY_MARGIN = timedelta(hours=1)
STALE_TEMPORARY = timedelta(hours=1)
MAX_RECORD_BYTES = 64 * 1024
LOCK_NAME = ".retention.lock"

_FOLDER = re.compile(r"[0-9a-f]{32}")
_ID = r"[A-Za-z0-9_-]{22}"
_RECORD = re.compile(_ID + r"\.json")
_DATA = re.compile(_ID + r"\.bin")
_TEMPORARY = re.compile(_ID + r"\.(?:bin|json)\.[0-9a-f]{12}\.tmp")
_POSIX_FDS = os.name == "posix" and os.unlink in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW")


@dataclass
class Report:
    applied: bool
    folders: int = 0
    expired_files: int = 0          # records past expires_at + delete_after + margin
    deleted_files: int = 0          # of those, deleted in this run (data and record)
    deleted_bytes: int = 0
    stale_temporaries: int = 0      # abandoned upload temporaries (deleted when applied)
    orphaned_data: int = 0          # data with no record, older than STALE_TEMPORARY (deleted when applied)
    unreadable_records: int = 0     # never deleted automatically
    skipped_entries: int = 0        # anything not named and typed as FileStore writes it

    def as_dict(self):
        return asdict(self)


class _Folder:
    """One owner folder, every operation relative to it and never following a symlink."""

    def __init__(self, path: Path):
        self.path, self.fd = path, None
        if _POSIX_FDS:
            self.fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)

    def names(self):
        return os.listdir(self.fd if self.fd is not None else self.path)

    def lstat(self, name):
        if self.fd is not None:
            return os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        return os.lstat(self.path / name)

    def read(self, name, limit):
        if self.fd is not None:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.fd)
            with os.fdopen(fd, "rb") as handle:
                return handle.read(limit + 1)
        with open(self.path / name, "rb") as handle:
            return handle.read(limit + 1)

    def unlink(self, name):
        try:
            if self.fd is not None:
                os.unlink(name, dir_fd=self.fd)
            else:
                os.unlink(self.path / name)
            return True
        except FileNotFoundError:
            return False                      # another run (or the customer) deleted it first
        except PermissionError:
            return False                      # in use or not ours to delete right now: left for the next run


def _regular(folder, name):
    try:
        info = folder.lstat(name)
    except FileNotFoundError:
        return None
    return info if stat.S_ISREG(info.st_mode) else None


def _expiry(folder, name):
    raw = folder.read(name, MAX_RECORD_BYTES)
    if len(raw) > MAX_RECORD_BYTES:
        raise ValueError("record too large")
    record = json.loads(raw.decode("utf-8"))
    if not isinstance(record, dict) or record.get("file_id") != name[:-5]:
        raise ValueError("record does not describe this file")
    expires = datetime.fromisoformat(record["expires_at"])
    if expires.tzinfo is None:
        raise ValueError("naive expiry")
    return expires


def clean(root, *, delete_after: timedelta, now: datetime | None = None, apply: bool = False) -> Report:
    """Delete (or with apply=False, count) files expired for longer than delete_after + SAFETY_MARGIN."""
    if not isinstance(delete_after, timedelta) or delete_after < timedelta(0):
        raise ValueError("delete_after must be an explicit, non-negative period")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("the files root must be a real directory")
    report = Report(applied=apply)
    deadline = now - delete_after - SAFETY_MARGIN
    # Ages on disk (temporaries, orphaned data) are judged by the real clock, whatever `now` the expiry
    # decisions use: an upload in progress must never look old.
    stale_before = time.time() - STALE_TEMPORARY.total_seconds()
    for entry in sorted(os.scandir(root), key=lambda e: e.name):
        if entry.name == LOCK_NAME and entry.is_file(follow_symlinks=False):
            continue
        if not (_FOLDER.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False)):
            report.skipped_entries += 1
            continue
        try:
            folder = _Folder(Path(entry.path))
        except OSError:                      # replaced by a symlink or removed since the scan
            report.skipped_entries += 1
            continue
        report.folders += 1
        try:
            _clean_folder(folder, report, deadline, stale_before, apply)
        finally:
            folder.close()
    return report


def _clean_folder(folder, report, deadline, stale_before, apply):
    names = set(folder.names())
    for name in sorted(names):
        if _RECORD.fullmatch(name):
            if _regular(folder, name) is None:
                report.skipped_entries += 1
                continue
            try:
                expires = _expiry(folder, name)
            except (OSError, ValueError, KeyError, TypeError, UnicodeDecodeError):
                report.unreadable_records += 1
                continue
            if expires > deadline:
                continue
            report.expired_files += 1
            data = name[:-5] + ".bin"
            info = _regular(folder, data)
            if apply:
                if info is not None and folder.unlink(data):       # data first, then the record
                    report.deleted_bytes += info.st_size
                if folder.unlink(name):
                    report.deleted_files += 1
        elif _DATA.fullmatch(name):
            if name[:-4] + ".json" in names:
                continue                                           # decided with its record
            info = _regular(folder, name)
            if info is None:
                report.skipped_entries += 1
            elif info.st_mtime < stale_before:
                report.orphaned_data += 1
                if apply and folder.unlink(name):
                    report.deleted_bytes += info.st_size
        elif _TEMPORARY.fullmatch(name):
            info = _regular(folder, name)
            if info is None:
                report.skipped_entries += 1
            elif info.st_mtime < stale_before:
                report.stale_temporaries += 1
                if apply:
                    folder.unlink(name)
        else:
            report.skipped_entries += 1
