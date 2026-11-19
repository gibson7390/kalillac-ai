"""Customer files on disk: uploads, generated files and edited copies, each owned by one account.

Not reachable from the application unless KALILLAC_FILES_ENABLED is set (see FILE_GENERATION.md).

    store = FileStore(root)
    stored = store.put(owner, data, filename, media_type, origin="generated")
    stored, data = store.get(owner, stored.file_id)

Rules:
  * Every file belongs to exactly one owner (an account id). get(), list() and delete() see only
    the caller's own files; another owner's file and a file that does not exist are the same
    FileNotFound, so an identifier reveals nothing about anyone else.
  * Identifiers are 128-bit random values; nothing a customer supplies ever becomes a path. Owner
    directories are named by an unkeyed SHA-256 of the owner id, never the id itself.
  * Quotas per owner (bytes and count) are checked before anything is written, over what is physically
    in the owner's folder: every file's data whether or not its record exists (an upload interrupted
    between the two), every upload temporary, every record, expired ones included. An account's storage
    is bounded by its quota whether or not the retention cleanup has run, and however its uploads ended.
    A file past its availability period cannot be read (get(), record()) and is listed only on request
    (list(include_expired=True)); its owner can still delete it at once. The retention cleanup
    (kalillac_files/retention.py: the application's scheduled run and tools/data_lifecycle.py) deletes it
    from disk once it has been expired for the configured period.
  * One write at a time per owner, across threads and (POSIX) processes: put() and delete() hold the
    owner's lock (an flock on the owner's folder). While put() holds it no other upload of that owner can
    be in progress, so data without a record and upload temporaries found then are abandoned (their
    writer failed or died): put() removes them, in that owner's folder only, before checking the quota.
  * Writes are atomic (temporary file, then rename), metadata included; a write that fails removes its
    temporary, and an upload whose record cannot be written removes its data.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
from contextlib import contextmanager

ORIGINS = ("uploaded", "generated", "edited")
_ID = re.compile(r"[A-Za-z0-9_-]{22}")
_ENTRY = re.compile(r"([A-Za-z0-9_-]{22})\.(bin|json)(\.[0-9a-f]{12}\.tmp)?")
MAX_FILE_BYTES = 10 * 1024 * 1024
_FLOCK = os.name == "posix" and hasattr(os, "O_NOFOLLOW")


class FileNotFound(LookupError):
    """No such file for this owner (whether it does not exist or belongs to someone else)."""


class QuotaExceeded(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class StoredFile:
    file_id: str
    filename: str
    media_type: str
    size: int
    origin: str
    created_at: str          # ISO 8601, UTC
    expires_at: str
    parent_id: str | None = None

    def public(self):
        """What a customer's client may see: never the owner or a path."""
        return {"id": self.file_id, "filename": self.filename, "media_type": self.media_type, "size": self.size,
                "origin": self.origin, "created_at": self.created_at, "expires_at": self.expires_at,
                "parent_id": self.parent_id}


class FileStore:
    def __init__(self, root, *, max_owner_bytes=250 * 1024 * 1024, max_owner_files=300,
                 retention=timedelta(days=30), clock=None):
        self.root = Path(root)
        self.max_owner_bytes, self.max_owner_files = max_owner_bytes, max_owner_files
        self.retention = retention
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._locks, self._guard = {}, threading.Lock()
        self.root.mkdir(parents=True, exist_ok=True)

    # --- paths ------------------------------------------------------------------------------

    def _folder(self, owner):
        if type(owner) is not str or not owner or len(owner) > 200:
            raise ValueError("owner must be a non-empty string")
        return self.root / hashlib.sha256(owner.encode("utf-8")).hexdigest()[:32]

    def _lock(self, owner):
        with self._guard:
            return self._locks.setdefault(owner, threading.Lock())

    @contextmanager
    def _exclusive(self, owner, folder):
        """The owner's lock: this process's threads, and on POSIX every process using this root (an flock on
        the owner's folder, released by the kernel if its holder dies). Yields whether it spans processes."""
        with self._lock(owner):
            if not _FLOCK:
                yield False
                return
            import fcntl
            fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield True
            finally:
                os.close(fd)

    @staticmethod
    def _write(path, data):
        temporary = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
        try:
            with open(temporary, "xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise

    @staticmethod
    def _usage(folder):
        """What is physically in an owner's folder: (file ids, data bytes, abandoned entry names). Data is
        counted whether or not its record exists, temporaries included; a file id counts once whatever of it
        is on disk. Abandoned: data without a record, and temporaries (only ever so while the owner's lock is
        held). Entries FileStore does not write (another name, a link, a directory) are left alone."""
        ids, size, leftover = set(), 0, []
        try:
            entries = list(os.scandir(folder))
        except FileNotFoundError:
            return ids, size, leftover
        names = {entry.name for entry in entries}
        for entry in entries:
            match = _ENTRY.fullmatch(entry.name)
            if match is None:
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            file_id, kind, temporary = match.groups()
            ids.add(file_id)
            if kind == "bin":
                size += info.st_size
            if temporary or (kind == "bin" and file_id + ".json" not in names):
                leftover.append(entry.name)
        return ids, size, leftover

    @staticmethod
    def _remove(folder, names):
        for name in names:
            try:
                (folder / name).unlink()
            except FileNotFoundError:
                pass

    def _records(self, folder, include_expired=False):
        now = self.clock()
        found = []
        if not folder.is_dir():
            return found
        for meta in folder.glob("*.json"):
            try:
                record = StoredFile(**json.loads(meta.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError):
                continue
            if include_expired or datetime.fromisoformat(record.expires_at) > now:
                found.append(record)
        return found

    def available(self, record):
        """Whether a record is still within its availability period (readable and downloadable)."""
        return datetime.fromisoformat(record.expires_at) > self.clock()

    # --- operations -------------------------------------------------------------------------

    def put(self, owner, data, filename, media_type, origin, parent_id=None):
        if type(data) is not bytes or not data or len(data) > MAX_FILE_BYTES:
            raise QuotaExceeded("file_too_large")
        if origin not in ORIGINS or type(filename) is not str or not filename or len(filename) > 255 \
                or "/" in filename or "\\" in filename or type(media_type) is not str or len(media_type) > 120:
            raise ValueError("invalid file record")
        if parent_id is not None and _ID.fullmatch(parent_id) is None:
            raise ValueError("invalid parent")
        folder = self._folder(owner)
        folder.mkdir(parents=True, exist_ok=True)
        with self._exclusive(owner, folder) as across_processes:
            ids, size, leftover = self._usage(folder)               # Everything on disk counts.
            if leftover and across_processes:
                # No other upload of this owner can be running: these were abandoned. Reclaim them.
                self._remove(folder, leftover)
                ids, size, _leftover = self._usage(folder)
            if len(ids) + 1 > self.max_owner_files:
                raise QuotaExceeded("file_count_quota_exceeded")
            if size + len(data) > self.max_owner_bytes:
                raise QuotaExceeded("storage_quota_exceeded")
            now = self.clock()
            record = StoredFile(secrets.token_urlsafe(16), filename, media_type, len(data), origin,
                                now.isoformat(), (now + self.retention).isoformat(), parent_id)
            blob = folder / (record.file_id + ".bin")
            self._write(blob, data)
            try:
                self._write(folder / (record.file_id + ".json"), json.dumps(asdict(record)).encode("utf-8"))
            except BaseException:
                self._remove(folder, [blob.name])                   # No data is left without its record.
                raise
            return record

    def _own(self, owner, file_id):
        """The owner's record, expired or not."""
        if type(file_id) is not str or _ID.fullmatch(file_id) is None:
            raise FileNotFound(file_id)
        meta = self._folder(owner) / (file_id + ".json")
        try:
            return StoredFile(**json.loads(meta.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            raise FileNotFound(file_id) from None

    def record(self, owner, file_id):
        record = self._own(owner, file_id)
        if not self.available(record):
            raise FileNotFound(file_id)
        return record

    def get(self, owner, file_id):
        record = self.record(owner, file_id)
        try:
            data = (self._folder(owner) / (file_id + ".bin")).read_bytes()
        except OSError:
            raise FileNotFound(file_id) from None
        if len(data) != record.size:
            raise FileNotFound(file_id)
        return record, data

    def list(self, owner, include_expired=False):
        return sorted(self._records(self._folder(owner), include_expired), key=lambda record: record.created_at,
                      reverse=True)

    def delete(self, owner, file_id):
        """Delete one of the owner's files, expired or not: its data, then its record."""
        self._own(owner, file_id)
        folder = self._folder(owner)
        with self._exclusive(owner, folder):
            for suffix in (".bin", ".json"):
                try:
                    (folder / (file_id + suffix)).unlink()
                except FileNotFoundError:
                    pass

    def purge(self, delete_after=timedelta(0)):
        """Delete files expired for longer than delete_after (plus retention.SAFETY_MARGIN), abandoned
        upload temporaries and orphaned data, through kalillac_files.retention's rules; returns how many
        files were deleted, or None when another run holds the retention lock. The application's scheduled
        run and the operator command (tools/data_lifecycle.py files) share that lock."""
        from .retention import run_exclusive
        report = run_exclusive(self.root, delete_after=delete_after, now=self.clock(), apply=True)
        return None if report is None else report.deleted_files
