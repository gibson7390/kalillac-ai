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
  * Quotas per owner (bytes and count) are checked before anything is written; a file past its
    availability period is invisible to get() and list() at once, and is deleted from disk by the
    retention cleanup (kalillac_files/retention.py, run by tools/data_lifecycle.py) once it has been
    expired for the explicitly configured period.
  * Writes are atomic (temporary file, then rename), metadata included.
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
import threading

ORIGINS = ("uploaded", "generated", "edited")
_ID = re.compile(r"[A-Za-z0-9_-]{22}")
MAX_FILE_BYTES = 10 * 1024 * 1024


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

    @staticmethod
    def _write(path, data):
        temporary = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
        with open(temporary, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    def _records(self, folder):
        now = self.clock()
        found = []
        if not folder.is_dir():
            return found
        for meta in folder.glob("*.json"):
            try:
                record = StoredFile(**json.loads(meta.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError):
                continue
            if datetime.fromisoformat(record.expires_at) > now:
                found.append(record)
        return found

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
        with self._lock(owner):
            existing = self._records(folder)
            if len(existing) + 1 > self.max_owner_files:
                raise QuotaExceeded("file_count_quota_exceeded")
            if sum(record.size for record in existing) + len(data) > self.max_owner_bytes:
                raise QuotaExceeded("storage_quota_exceeded")
            folder.mkdir(parents=True, exist_ok=True)
            now = self.clock()
            record = StoredFile(secrets.token_urlsafe(16), filename, media_type, len(data), origin,
                                now.isoformat(), (now + self.retention).isoformat(), parent_id)
            self._write(folder / (record.file_id + ".bin"), data)
            self._write(folder / (record.file_id + ".json"), json.dumps(asdict(record)).encode("utf-8"))
            return record

    def record(self, owner, file_id):
        if type(file_id) is not str or _ID.fullmatch(file_id) is None:
            raise FileNotFound(file_id)
        meta = self._folder(owner) / (file_id + ".json")
        try:
            record = StoredFile(**json.loads(meta.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            raise FileNotFound(file_id) from None
        if datetime.fromisoformat(record.expires_at) <= self.clock():
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

    def list(self, owner):
        return sorted(self._records(self._folder(owner)), key=lambda record: record.created_at, reverse=True)

    def delete(self, owner, file_id):
        self.record(owner, file_id)
        folder = self._folder(owner)
        with self._lock(owner):
            for suffix in (".json", ".bin"):
                try:
                    (folder / (file_id + suffix)).unlink()
                except FileNotFoundError:
                    pass

    def purge(self, delete_after=timedelta(0)):
        """Delete files expired for longer than delete_after (plus retention.SAFETY_MARGIN), abandoned
        upload temporaries and orphaned data, through kalillac_files.retention's rules; returns how many
        files were deleted. The operator command (tools/data_lifecycle.py files) is the scheduled way."""
        from .retention import clean
        return clean(self.root, delete_after=delete_after, now=self.clock(), apply=True).deleted_files
