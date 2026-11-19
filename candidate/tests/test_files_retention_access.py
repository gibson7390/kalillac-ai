"""Stored files after expiry and after Premium ends (Codex F2, F6), on the real FileStore and /api/files router.

F2: an expired file is still the customer's to delete; every file on disk, expired or not, counts toward the
account's quota, so storage stays bounded whether or not the cleanup has run; the application runs the cleanup
itself on a schedule, under the same lock as the operator command.
F6: managing one's own files (list, download, delete) needs only the signed-in account; adding files and the
file assistant still need the paid tier; nobody reaches another account's files.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import threading
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kalillac_files import retention
from kalillac_files.router import FileAccount, FileService, build_files_router
from kalillac_files.store import FileNotFound, FileStore, QuotaExceeded

NOW = datetime(2026, 11, 18, 12, tzinfo=timezone.utc)
ACCOUNTS = {"premium": FileAccount("acct-premium", True), "downgraded": FileAccount("acct-downgraded", False),
            "free": FileAccount("acct-free", False)}


def make_store(tmp_path, **limits):
    clock = {"now": NOW}
    return FileStore(tmp_path / "files", clock=lambda: clock["now"], **limits), clock


def on_disk(store, owner):
    folder = store._folder(owner)
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


# --- F2: the store -------------------------------------------------------------------------------------

def test_an_owner_deletes_an_expired_file_from_disk_and_no_one_else_can(tmp_path):
    store, clock = make_store(tmp_path)
    kept = store.put("acct-a", b"quarterly numbers", "q3.txt", "text/plain", "uploaded")
    clock["now"] += timedelta(days=31)                      # Past its availability period.
    with pytest.raises(FileNotFound):
        store.get("acct-a", kept.file_id)                   # No longer readable...
    assert [r.file_id for r in store.list("acct-a", include_expired=True)] == [kept.file_id]
    assert store.list("acct-a") == []                       # ...and listed only on request.
    with pytest.raises(FileNotFound):
        store.delete("acct-b", kept.file_id)                # Another account: as if it did not exist.
    assert len(on_disk(store, "acct-a")) == 2
    store.delete("acct-a", kept.file_id)                    # The owner: gone from disk at once.
    assert on_disk(store, "acct-a") == []
    with pytest.raises(FileNotFound):
        store.delete("acct-a", kept.file_id)


def test_expired_files_count_toward_the_quota_so_an_accounts_storage_stays_bounded(tmp_path):
    store, clock = make_store(tmp_path, max_owner_files=3, max_owner_bytes=30)
    first = [store.put("acct-a", b"0123456789", f"{n}.txt", "text/plain", "uploaded") for n in range(3)]
    clock["now"] += timedelta(days=31)
    # Before F2 the expired three were not counted and three more were accepted (six on disk); now the
    # account cannot pass its quota on disk, cleanup or not.
    with pytest.raises(QuotaExceeded) as full:
        store.put("acct-a", b"x", "more.txt", "text/plain", "uploaded")
    assert full.value.code == "file_count_quota_exceeded" and len(on_disk(store, "acct-a")) == 6
    store.delete("acct-a", first[0].file_id)               # The customer frees room by deleting...
    store.put("acct-a", b"0123456789", "new.txt", "text/plain", "uploaded")
    clock["now"] += timedelta(days=31)
    assert store.purge(timedelta(0)) == 3                   # ...or the cleanup does.
    assert on_disk(store, "acct-a") == []
    store.put("acct-b", b"0123456789" * 3, "b.txt", "text/plain", "uploaded")   # Each account its own quota.


def test_the_byte_quota_counts_expired_files_too(tmp_path):
    store, clock = make_store(tmp_path, max_owner_files=10, max_owner_bytes=25)
    store.put("acct-a", b"x" * 20, "big.txt", "text/plain", "uploaded")
    clock["now"] += timedelta(days=31)
    with pytest.raises(QuotaExceeded) as full:
        store.put("acct-a", b"x" * 10, "more.txt", "text/plain", "uploaded")
    assert full.value.code == "storage_quota_exceeded"


# --- F2: one cleanup at a time, run by the application on a schedule ------------------------------------

def test_only_one_cleanup_runs_at_a_time_in_a_process_and_across_processes(tmp_path):
    store, clock = make_store(tmp_path)
    store.put("acct-a", b"data", "a.txt", "text/plain", "uploaded")
    clock["now"] += timedelta(days=32)
    entered, release = threading.Event(), threading.Event()
    real_clean = retention.clean

    def slow_clean(root, **options):
        entered.set()
        assert release.wait(10)
        return real_clean(root, **options)
    retention.clean = slow_clean
    try:
        first = []
        runner = threading.Thread(target=lambda: first.append(store.purge(timedelta(0))))
        runner.start()
        assert entered.wait(10)
        assert store.purge(timedelta(0)) is None           # Busy: nothing done, nothing waited for.
        release.set()
        runner.join(10)
    finally:
        retention.clean = real_clean
    assert first == [1]
    if os.name == "posix":                                 # Another process (the operator command) holding it.
        import fcntl
        handle = os.open(store.root / retention.LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            assert retention.run_exclusive(store.root, delete_after=timedelta(0), apply=True) is None
        finally:
            os.close(handle)


SCHEDULE_SCRIPT = r'''
import json, os, sys, time
from datetime import datetime, timedelta, timezone
sys.path.insert(0, "tests")
import app_fastapi_candidate as a
from fastapi.testclient import TestClient
from kalillac_files import retention
store = a._file_store
old = store.put("acct-a", b"old", "old.txt", "text/plain", "uploaded")
fresh = store.put("acct-a", b"fresh", "fresh.txt", "text/plain", "uploaded")
meta = store._folder("acct-a") / (old.file_id + ".json")
record = json.loads(meta.read_text())
record["expires_at"] = (datetime.now(timezone.utc) - timedelta(days=DAYS, hours=2)).isoformat()
meta.write_text(json.dumps(record))
with TestClient(a.api):                      # startup: the scheduled cleanup's first run
    for _ in range(100):
        if not (store._folder("acct-a") / (old.file_id + ".bin")).exists():
            break
        time.sleep(0.05)
    print(json.dumps({"left": sorted(r.filename for r in store.list("acct-a", True)),
                      "interval_hours": retention.CLEANUP_INTERVAL.total_seconds() / 3600,
                      "thread": any(t.name == "kalillac-file-retention" for t in __import__("threading").enumerate())}))
'''


@pytest.mark.parametrize("days", ["0", "7"])
def test_a_files_deployment_deletes_expired_files_itself_at_startup_and_every_hour(tmp_path, days):
    pytest.importorskip("pptx")
    pytest.importorskip("typst")
    from test_unsafe_request_guard import ORIGIN, _run_app
    env = {"KALILLAC_SITE_ORIGIN": ORIGIN, "KALILLAC_FILES_ENABLED": "true", "OPENAI_API_KEY": "sk-test-not-real",
           "KALILLAC_FILES_ROOT": str(tmp_path / "files"), "KALILLAC_FILES_DELETE_AFTER_EXPIRY_DAYS": days}
    result = _run_app(env, SCHEDULE_SCRIPT.replace("DAYS", days))
    assert result.returncode == 0, result.stderr[-3000:]
    line = [json.loads(x) for x in result.stdout.splitlines() if x.startswith("{")][-1]
    # Expired for the configured period plus the margin: deleted; the available file is untouched.
    assert line == {"left": ["fresh.txt"], "interval_hours": 1.0, "thread": True}


@pytest.mark.parametrize("value", ["-1", "seven", "3651", "1.5", " "])
def test_an_invalid_deletion_period_refuses_to_start(tmp_path, value):
    pytest.importorskip("pptx")
    pytest.importorskip("typst")
    from test_unsafe_request_guard import ORIGIN, _run_app
    env = {"KALILLAC_SITE_ORIGIN": ORIGIN, "KALILLAC_FILES_ENABLED": "true", "OPENAI_API_KEY": "sk-test-not-real",
           "KALILLAC_FILES_ROOT": str(tmp_path / "files"), "KALILLAC_FILES_DELETE_AFTER_EXPIRY_DAYS": value}
    result = _run_app(env, "import app_fastapi_candidate\n")
    assert result.returncode != 0 and "KALILLAC_FILES_DELETE_AFTER_EXPIRY_DAYS" in result.stderr


# --- F6: who may do what --------------------------------------------------------------------------------

@pytest.fixture
def api(tmp_path):
    store, clock = make_store(tmp_path)
    models = []

    async def authenticate(request):
        return ACCOUNTS.get(request.cookies.get("session", ""))
    service = FileService(store=store, authenticate=authenticate, required_tier="paid",
                          call_model=lambda items: models.append(items))
    app = FastAPI()
    app.include_router(build_files_router(service=service))
    client = TestClient(app)
    client.store, client.clock, client.models = store, clock, models
    return client


def call(client, account, method, path, **kwargs):
    client.cookies.clear()
    if account:
        client.cookies.set("session", account)
    return client.request(method, path, **kwargs)


def test_after_premium_ends_a_customer_keeps_managing_their_own_files_and_adds_nothing(api):
    # Stored while the account was Premium (the store is the same; only the entitlement changed).
    kept = api.store.put("acct-downgraded", b"%PDF-1.7 kept", "plan.pdf", "application/pdf", "generated")
    old = api.store.put("acct-downgraded", b"old notes", "old.txt", "text/plain", "uploaded")
    meta = api.store._folder("acct-downgraded") / (old.file_id + ".json")
    record = json.loads(meta.read_text())
    record["expires_at"] = (NOW - timedelta(days=1)).isoformat()
    meta.write_text(json.dumps(record))

    listed = call(api, "downgraded", "GET", "/api/files")
    assert listed.status_code == 200 and listed.json()["can_add"] is False
    assert {(f["filename"], f["available"]) for f in listed.json()["files"]} == {("plan.pdf", True),
                                                                                 ("old.txt", False)}
    download = call(api, "downgraded", "GET", f"/api/files/{kept.file_id}")
    assert download.status_code == 200 and download.content == b"%PDF-1.7 kept"
    assert call(api, "downgraded", "GET", f"/api/files/{old.file_id}").status_code == 404   # Expired: delete only.
    assert call(api, "downgraded", "DELETE", f"/api/files/{old.file_id}").json() == {"deleted": True}
    assert call(api, "downgraded", "DELETE", f"/api/files/{kept.file_id}").json() == {"deleted": True}
    assert on_disk(api.store, "acct-downgraded") == []

    # Nothing new: no upload, no file assistant (no model call).
    upload = call(api, "downgraded", "POST", "/api/files", content=b"new",
                  headers={"content-type": "application/octet-stream", "x-kalillac-filename": quote("n.txt")})
    assist = call(api, "downgraded", "POST", "/api/files/assist", content=json.dumps({"message": "Make a PDF"}),
                  headers={"content-type": "application/json"})
    assert [(r.status_code, r.json()["error"]) for r in (upload, assist)] == [(403, "files_require_premium")] * 2
    assert api.models == []


def test_no_account_reaches_another_accounts_files_and_signed_out_reaches_none(api):
    theirs = api.store.put("acct-premium", b"private", "private.txt", "text/plain", "uploaded")
    for account in ("downgraded", "free"):
        assert call(api, account, "GET", f"/api/files/{theirs.file_id}").status_code == 404
        assert call(api, account, "DELETE", f"/api/files/{theirs.file_id}").status_code == 404
        assert call(api, account, "GET", "/api/files").json() == {"files": [], "can_add": False}
    for method, path in (("GET", "/api/files"), ("GET", f"/api/files/{theirs.file_id}"),
                         ("DELETE", f"/api/files/{theirs.file_id}")):
        assert call(api, None, method, path).status_code == 401
    assert api.store.get("acct-premium", theirs.file_id)[1] == b"private"
    mine = call(api, "premium", "GET", "/api/files").json()
    assert mine["can_add"] is True and [f["filename"] for f in mine["files"]] == ["private.txt"]
