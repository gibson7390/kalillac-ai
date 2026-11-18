"""/api/files over HTTP: authentication and tier, uploads checked by intake, owner isolation, safe
downloads, the assistant route, and the unsafe-request guard in front of every mutation."""
from __future__ import annotations

import copy
import json
from urllib.parse import quote

import pytest

pytest.importorskip("docx")
pytest.importorskip("pptx")
openpyxl = pytest.importorskip("openpyxl")
pytest.importorskip("typst")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from kalillac_accounts.request_guard import UnsafeRequestGuard, guarded_route  # noqa: E402
from kalillac_files import generate  # noqa: E402
from kalillac_files.router import FileAccount, build_files_router  # noqa: E402
from kalillac_files.store import FileStore  # noqa: E402
from quality_benchmark import FORECAST_WORKBOOK, WORD_PROPOSAL  # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore::UserWarning")
ORIGIN = "https://kalillac.test"
ACCOUNTS = {"paid-a": FileAccount("a", True), "paid-b": FileAccount("b", True), "free-c": FileAccount("c", False)}
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class Model:
    def __init__(self, *outputs):
        self.outputs = list(outputs)

    def __call__(self, items):
        return {"output": self.outputs.pop(0)}


def build(tmp_path, model=None, tier="paid"):
    async def authenticate(request):
        return ACCOUNTS.get(request.cookies.get("session", ""))
    app = FastAPI()
    store = FileStore(tmp_path / "files")
    app.include_router(build_files_router(store=store, authenticate=authenticate, call_model=model or Model(),
                                          required_tier=tier))
    app.add_middleware(UnsafeRequestGuard, origin=ORIGIN)
    return TestClient(app), store


def headers(account="paid-a", **extra):
    return {"origin": ORIGIN, "cookie": f"session={account}", **extra}


def upload(client, data, name, account="paid-a"):
    return client.post("/api/files", content=data, headers=headers(
        account, **{"content-type": "application/octet-stream", "x-kalillac-filename": quote(name)}))


@pytest.fixture
def workbook():
    return generate(copy.deepcopy(FORECAST_WORKBOOK), "xlsx").data


def test_the_unsafe_routes_are_in_the_guard_inventory():
    for method, path in (("POST", "/api/files"), ("POST", "/api/files/assist"), ("DELETE", "/api/files/abc")):
        assert guarded_route(method, path).family == "files"
    assert guarded_route("GET", "/api/files/abc") is None


def test_cross_site_and_query_requests_are_refused_before_any_work(tmp_path, workbook):
    client, store = build(tmp_path)
    for bad in ({"origin": "https://evil.test"}, {}):
        reply = client.post("/api/files", content=workbook, headers={**headers(), **bad, "content-type":
                                                                     "application/octet-stream"} if bad else
                            {"cookie": "session=paid-a", "content-type": "application/octet-stream"})
        assert (reply.status_code, reply.json()) == (403, {"error": "origin_rejected"})
    assert client.post("/api/files?x=1", content=workbook, headers=headers()).json() == {"error": "query_not_allowed"}
    assert client.get("/api/files?owner=b", headers=headers()).json() == {"error": "query_not_allowed"}
    assert store.list("a") == []


def test_signed_out_and_free_accounts_are_refused_while_files_require_premium(tmp_path, workbook):
    client, _ = build(tmp_path)
    assert client.get("/api/files", headers=headers("nobody")).status_code == 401
    assert (client.get("/api/files", headers=headers("free-c")).json()) == {"error": "files_require_premium"}
    assert upload(client, workbook, "f.xlsx", "free-c").status_code == 403
    client, _ = build(tmp_path / "any", tier="any")
    assert client.get("/api/files", headers=headers("free-c")).status_code == 200


def test_an_upload_is_checked_by_intake_stored_and_downloaded_safely(tmp_path, workbook):
    client, store = build(tmp_path)
    reply = upload(client, workbook, "Plan 2027 — final.xlsx")
    assert reply.status_code == 201
    file = reply.json()["file"]
    assert file["filename"] == "Plan 2027 _ final.xlsx" and file["media_type"] == XLSX and file["origin"] == "uploaded"
    assert set(file) == {"id", "filename", "media_type", "size", "origin", "created_at", "expires_at", "parent_id"}
    listed = client.get("/api/files", headers=headers()).json()["files"]
    assert [item["id"] for item in listed] == [file["id"]]
    download = client.get(f"/api/files/{file['id']}", headers=headers())
    assert download.content == workbook and download.headers["content-type"] == XLSX
    assert download.headers["content-disposition"].startswith("attachment;")
    assert download.headers["x-content-type-options"] == "nosniff" and download.headers["cache-control"] == "no-store"
    assert "sandbox" in download.headers["content-security-policy"]
    # Another account sees nothing, and cannot tell a file exists.
    assert client.get(f"/api/files/{file['id']}", headers=headers("paid-b")).json() == {"error": "file_not_found"}
    assert client.delete(f"/api/files/{file['id']}", headers=headers("paid-b")).status_code == 404
    assert client.get("/api/files", headers=headers("paid-b")).json() == {"files": []}
    assert client.delete(f"/api/files/{file['id']}", headers=headers()).json() == {"deleted": True}
    assert client.get(f"/api/files/{file['id']}", headers=headers()).status_code == 404


@pytest.mark.parametrize("data, name, status, code", [
    (b"MZ\x90\x00", "tool.exe", 422, "unsupported_type"),
    (b"just text", "x.docx", 422, "type_mismatch"),
    (b"", "x.txt", 400, "empty_file"),
])
def test_unacceptable_uploads_are_refused_and_nothing_is_stored(tmp_path, data, name, status, code):
    client, store = build(tmp_path)
    reply = upload(client, data, name)
    assert (reply.status_code, reply.json()) == (status, {"error": code})
    assert store.list("a") == []


def test_uploads_need_a_name_the_raw_body_and_a_bounded_size(tmp_path, workbook):
    client, _ = build(tmp_path)
    assert client.post("/api/files", content=workbook, headers=headers(**{
        "content-type": "application/octet-stream"})).json() == {"error": "filename_required"}
    assert client.post("/api/files", files={"file": ("f.xlsx", workbook)}, headers=headers(**{
        "x-kalillac-filename": "f.xlsx"})).json() == {"error": "octet_stream_required"}
    assert upload(client, b"x" * (10 * 1024 * 1024 + 1), "big.txt").json() == {"error": "file_too_large"}


def test_the_assistant_route_makes_files_for_the_account_and_lists_them(tmp_path):
    model = Model([{"type": "function_call", "call_id": "c1", "name": "create_file",
                    "arguments": json.dumps({"format": "pdf", "spec": copy.deepcopy(WORD_PROPOSAL)})}],
                  [{"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "Your proposal is ready."}]}])
    client, store = build(tmp_path, model)
    reply = client.post("/api/files/assist", json={"message": "Write the café proposal as a PDF"}, headers=headers())
    assert reply.status_code == 200
    body = reply.json()
    assert body["reply"] == "Your proposal is ready." and len(body["files"]) == 1
    made = body["files"][0]
    assert made["media_type"] == "application/pdf" and made["origin"] == "generated"
    assert client.get(f"/api/files/{made['id']}", headers=headers()).content.startswith(b"%PDF-")
    assert store.list("b") == []


def test_the_assistant_route_checks_its_request_and_the_attached_files(tmp_path, workbook):
    client, _ = build(tmp_path, Model())
    theirs = upload(client, workbook, "theirs.xlsx", "paid-b").json()["file"]["id"]
    for body in ({}, {"message": ""}, {"message": "x", "extra": 1}, {"message": "x" * 20_001},
                 {"message": "x", "history": [{"role": "system", "content": "x"}]},
                 {"message": "x", "file_ids": ["a"] * 6}):
        assert client.post("/api/files/assist", json=body, headers=headers()).json() == {"error": "invalid_request"}
    assert client.post("/api/files/assist", json={"message": "edit", "file_ids": [theirs]},
                       headers=headers()).json() == {"error": "file_not_found"}
    assert client.post("/api/files/assist", content=b"message=x", headers=headers(
        **{"content-type": "application/x-www-form-urlencoded"})).json() == {"error": "json_required"}


def test_a_failing_model_is_a_stable_error_not_a_crash(tmp_path):
    def broken(items):
        raise TimeoutError("provider timed out")
    client, _ = build(tmp_path, broken)
    reply = client.post("/api/files/assist", json={"message": "Make a report"}, headers=headers())
    assert (reply.status_code, reply.json()) == (502, {"error": "file_assistant_unavailable"})


def test_the_assistant_runs_inside_the_request_scope_and_maps_its_stops(tmp_path):
    """The application's controls wrap each assistant run in the worker thread; a stop they raise becomes
    its stable code, and the scope is left on every exit."""
    from contextlib import contextmanager
    from contextvars import ContextVar
    scope_var, events = ContextVar("scope", default=None), []

    class Stop(Exception):
        pass

    @contextmanager
    def request_scope(account):
        token = scope_var.set(account.owner)
        events.append("enter")
        try:
            yield
        finally:
            scope_var.reset(token)
            events.append("exit")

    def model(items):
        events.append(("call", scope_var.get()))
        if len(events) > 3:
            raise Stop()
        return {"output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}]}

    async def authenticate(request):
        return ACCOUNTS.get(request.cookies.get("session", ""))
    app = FastAPI()
    app.include_router(build_files_router(store=FileStore(tmp_path / "files"), authenticate=authenticate,
                                          call_model=model, request_scope=request_scope,
                                          failure_status=lambda error: (429, "busy") if isinstance(error, Stop) else None))
    client = TestClient(app)
    first = client.post("/api/files/assist", json={"message": "Make a report"}, headers=headers())
    second = client.post("/api/files/assist", json={"message": "Make a report"}, headers=headers())
    assert (first.status_code, second.status_code, second.json()) == (200, 429, {"error": "busy"})
    assert events == ["enter", ("call", "a"), "exit", "enter", ("call", "a"), "exit"]


# --- the customer, not the model, authorizes an exact edit (Codex reverification, section 3) ---------

def _edit_call(file_id, operations, exact=True):
    return [{"type": "function_call", "call_id": "e1", "name": "edit_file",
             "arguments": json.dumps({"file_id": file_id, "operations": operations, "exact": exact})}]


DONE = [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Done."}]}]


def _proposal_and_fee_edit(client, account="paid-a"):
    from kalillac_files.editing import outline
    data = generate(copy.deepcopy(WORD_PROPOSAL), "docx").data
    file_id = upload(client, data, "proposal.docx", account).json()["file"]["id"]
    cell = next(u.address for u in outline(data, "p.docx").units if u.kind == "cell" and u.text == "$11,000")
    return file_id, [{"op": "set_text", "at": cell, "text": "$12,000"}]


def _assist(client, file_id, confirmations=None, account="paid-a"):
    body = {"message": "Change the permits fee to $12,000", "file_ids": [file_id]}
    if confirmations is not None:
        body["confirmations"] = confirmations
    return client.post("/api/files/assist", json=body, headers=headers(account))


def test_the_models_exact_flag_alone_never_bypasses_the_total_check(tmp_path):
    model = Model()
    client, store = build(tmp_path, model)
    file_id, operations = _proposal_and_fee_edit(client)
    model.outputs += [_edit_call(file_id, operations), DONE]
    body = _assist(client, file_id).json()
    assert body["files"] == [] and len(store.list("a")) == 1                 # Nothing was edited.
    (offer,) = body["confirmations"]
    assert offer["file_id"] == file_id and len(offer["id"]) >= 24


def test_a_customer_confirmation_applies_exactly_that_edit_once(tmp_path):
    model = Model()
    client, store = build(tmp_path, model)
    file_id, operations = _proposal_and_fee_edit(client)
    model.outputs += [_edit_call(file_id, operations), DONE]
    confirmation = _assist(client, file_id).json()["confirmations"][0]["id"]
    model.outputs += [_edit_call(file_id, operations), DONE]
    applied = _assist(client, file_id, [confirmation]).json()
    assert len(applied["files"]) == 1 and "confirmations" not in applied
    model.outputs += [_edit_call(file_id, operations), DONE]
    again = _assist(client, file_id, [confirmation]).json()                  # Single use.
    assert again["files"] == [] and again["confirmations"][0]["id"] != confirmation


def test_a_confirmation_is_bound_to_its_edit_account_and_lifetime(tmp_path):
    model = Model()
    client, store = build(tmp_path, model)
    file_id, operations = _proposal_and_fee_edit(client)
    model.outputs += [_edit_call(file_id, operations), DONE]
    confirmation = _assist(client, file_id).json()["confirmations"][0]["id"]
    other = [{**operations[0], "text": "$13,000"}]                          # Different operations.
    model.outputs += [_edit_call(file_id, other), DONE]
    assert _assist(client, file_id, [confirmation]).json()["files"] == []
    their_file, their_ops = _proposal_and_fee_edit(client, "paid-b")        # Another account presents it.
    model.outputs += [_edit_call(their_file, their_ops), DONE]
    assert _assist(client, their_file, [confirmation], "paid-b").json()["files"] == []
    model.outputs += [_edit_call(file_id, operations, exact=False), DONE]    # Confirmed, but not asked exact.
    assert _assist(client, file_id, [confirmation]).json()["files"] == []
    for bad in (["x" * 65], [3], ["a"] * 5, "id"):
        reply = client.post("/api/files/assist", json={"message": "x", "confirmations": bad}, headers=headers())
        assert reply.json() == {"error": "invalid_request"}


def test_an_expired_confirmation_authorizes_nothing(tmp_path, monkeypatch):
    from kalillac_files import router
    now = [1000.0]
    monkeypatch.setattr(router.time, "monotonic", lambda: now[0])
    model = Model()
    client, store = build(tmp_path, model)
    file_id, operations = _proposal_and_fee_edit(client)
    model.outputs += [_edit_call(file_id, operations), DONE]
    confirmation = _assist(client, file_id).json()["confirmations"][0]["id"]
    now[0] += router.CONFIRMATION_SECONDS + 1
    model.outputs += [_edit_call(file_id, operations), DONE]
    assert _assist(client, file_id, [confirmation]).json()["files"] == []


# --- a confirmation authorizes exactly one attempt (Codex reverification 3, finding 1) ---------------

class Stateless:
    """A thread-safe scripted model: an exact edit, then a closing message once the tool has answered."""

    def __init__(self, file_id, operations, fail_after_tool=False):
        self.file_id, self.operations, self.fail_after_tool = file_id, operations, fail_after_tool

    def __call__(self, items):
        if any(item.get("type") == "function_call_output" for item in items):
            if self.fail_after_tool:
                raise TimeoutError("provider timed out")
            return {"output": DONE}
        return {"output": _edit_call(self.file_id, self.operations)}


def _offer(tmp_path):
    """A client and store, the uploaded proposal, the fee edit, and a confirmation the service offered."""
    holder = {}
    client, store = build(tmp_path, lambda items: holder["model"](items))
    file_id, operations = _proposal_and_fee_edit(client)
    holder["model"] = Stateless(file_id, operations)
    confirmation = _assist(client, file_id).json()["confirmations"][0]["id"]
    return client, store, file_id, operations, confirmation, holder


def test_two_concurrent_requests_cannot_both_use_one_confirmation(tmp_path):
    import threading
    client, store, file_id, operations, confirmation, holder = _offer(tmp_path)
    start, replies = threading.Barrier(2), []

    def send():
        start.wait(10)
        replies.append(_assist(client, file_id, [confirmation]).json())
    threads = [threading.Thread(target=send) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(120)
    assert sorted(len(reply["files"]) for reply in replies) == [0, 1]     # Exactly one exact edit.
    assert len(store.list("a")) == 2                                       # The upload and one edit.


def test_a_provider_failure_after_the_edit_is_stored_does_not_free_the_confirmation(tmp_path):
    client, store, file_id, operations, confirmation, holder = _offer(tmp_path)
    holder["model"] = Stateless(file_id, operations, fail_after_tool=True)
    reply = _assist(client, file_id, [confirmation])
    assert (reply.status_code, reply.json()) == (502, {"error": "file_assistant_unavailable"})
    assert len(store.list("a")) == 2                                       # The edit was stored before the failure.
    holder["model"] = Stateless(file_id, operations)
    retry = _assist(client, file_id, [confirmation]).json()
    assert retry["files"] == [] and retry["confirmations"][0]["id"] != confirmation
    assert len(store.list("a")) == 2                                       # Not applied a second time.


@pytest.mark.parametrize("failure", ["cancelled", "storage"])
def test_a_failed_attempt_consumes_the_confirmation_and_a_retry_needs_a_new_one(tmp_path, monkeypatch, failure):
    from kalillac_files import worker
    from kalillac_files.store import QuotaExceeded
    client, store, file_id, operations, confirmation, holder = _offer(tmp_path)
    if failure == "cancelled":                       # The worker is killed at its time limit.
        def killed(*args, **kwargs):
            raise worker.WorkerError("timeout")
        monkeypatch.setattr(worker, "run_edit", killed)
    else:                                            # The account's storage refuses the edited copy.
        def full(*args, **kwargs):
            raise QuotaExceeded("storage_quota_exceeded")
        monkeypatch.setattr(store, "put", full)
    assert _assist(client, file_id, [confirmation]).json()["files"] == []
    monkeypatch.undo()
    retry = _assist(client, file_id, [confirmation]).json()
    assert retry["files"] == [] and retry["confirmations"][0]["id"] != confirmation
    assert len(store.list("a")) == 1


def test_a_confirmation_stays_bound_to_tracked_mode(tmp_path):
    client, store, file_id, operations, confirmation, holder = _offer(tmp_path)

    class Tracked(Stateless):
        def __call__(self, items):
            if any(item.get("type") == "function_call_output" for item in items):
                return {"output": DONE}
            call = _edit_call(self.file_id, self.operations)
            arguments = json.loads(call[0]["arguments"])
            call[0]["arguments"] = json.dumps({**arguments, "track_changes": True})
            return {"output": call}
    holder["model"] = Tracked(file_id, operations)
    assert _assist(client, file_id, [confirmation]).json()["files"] == []   # Confirmed untracked, asked tracked.


# --- the release integration: exact-edit confirmations under the daily allowance hook ------------------
# The atomic claim (Codex reverification 3) and the allowance accounting (the correction on 8d5a2d4) met
# for the first time in release/staging-v1. The application's hook charges a response only when it
# succeeds (proved on PostgreSQL in test_v1_file_allowance_postgres.py); here the hook records each
# outcome, so a failed confirmed attempt is shown to be uncharged while its confirmation stays consumed.

def test_confirmed_edits_are_counted_once_and_a_failed_confirmed_attempt_is_not_charged(tmp_path):
    outcomes, holder = [], {}

    async def authenticate(request):
        return ACCOUNTS.get(request.cookies.get("session", ""))

    async def hook(request, account, request_chars, produce):
        response = await produce()
        outcomes.append((account.owner, response.status_code))
        return response
    app = FastAPI()
    store = FileStore(tmp_path / "files")
    app.include_router(build_files_router(store=store, authenticate=authenticate,
                                          call_model=lambda items: holder["model"](items), count_response=hook))
    app.add_middleware(UnsafeRequestGuard, origin=ORIGIN)
    client = TestClient(app)
    file_id, operations = _proposal_and_fee_edit(client)
    holder["model"] = Stateless(file_id, operations)

    offered = _assist(client, file_id).json()                       # Refused; an offer is minted.
    confirmation = offered["confirmations"][0]["id"]
    holder["model"] = Stateless(file_id, operations, fail_after_tool=True)
    failed = _assist(client, file_id, [confirmation])                # Confirmed, stored, then the provider fails.
    assert (failed.status_code, failed.json()) == (502, {"error": "file_assistant_unavailable"})
    holder["model"] = Stateless(file_id, operations)
    retried = _assist(client, file_id, [confirmation]).json()        # The chat's retry resends the same id.
    assert retried["files"] == [] and retried["confirmations"][0]["id"] != confirmation
    applied = _assist(client, file_id, [retried["confirmations"][0]["id"]]).json()
    assert len(applied["files"]) == 1 and "confirmations" not in applied

    # One hook call per request; only the three that returned 200 are chargeable, the failure is not.
    assert outcomes == [("a", 200), ("a", 502), ("a", 200), ("a", 200)]
    assert len(store.list("a")) == 3                                 # The upload, the failed attempt's stored
                                                                     # copy, and the applied edit: never twice
                                                                     # from one confirmation.
