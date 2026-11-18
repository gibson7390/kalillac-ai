"""/api/files/assist runs each assistant response through the service's allowance hook (count_response):
only after the account and the request are accepted, with the request's characters, and the hook decides
whether the assistant runs at all. The real hook (the application's daily allowance) is proved on
PostgreSQL in test_v1_file_allowance_postgres.py."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from kalillac_accounts.request_guard import UnsafeRequestGuard
from kalillac_files.router import FileAccount, build_files_router
from kalillac_files.store import FileStore

ORIGIN = "https://kalillac.test"


def build(tmp_path, hook):
    calls = []

    async def authenticate(request):
        return {"paid": FileAccount("a", True), "free": FileAccount("b", False)}.get(request.cookies.get("s", ""))

    def model(items):
        calls.append(items)
        return {"output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}]}
    app = FastAPI()
    app.include_router(build_files_router(store=FileStore(tmp_path / "files"), authenticate=authenticate,
                                          call_model=model, count_response=hook))
    app.add_middleware(UnsafeRequestGuard, origin=ORIGIN)
    return TestClient(app), calls


def ask(client, body, who="paid"):
    return client.post("/api/files/assist", json=body, headers={"origin": ORIGIN, "cookie": f"s={who}"})


def test_each_response_runs_inside_the_hook_with_the_account_and_its_characters(tmp_path):
    seen = []

    async def hook(request, account, request_chars, produce):
        seen.append((account.owner, request_chars))
        return await produce()
    client, calls = build(tmp_path, hook)
    reply = ask(client, {"message": "Hello", "history": [{"role": "user", "content": "abc"}]})
    assert reply.status_code == 200 and reply.json()["reply"] == "ok"
    assert seen == [("a", 8)] and len(calls) == 1
    assert reply.headers["cache-control"] == "no-store"


def test_the_hook_can_refuse_before_any_model_work(tmp_path):
    async def hook(request, account, request_chars, produce):
        return JSONResponse(status_code=429, content={"error": "daily_limit_reached"})
    client, calls = build(tmp_path, hook)
    refused = ask(client, {"message": "Hello"})
    assert (refused.status_code, refused.json()) == (429, {"error": "daily_limit_reached"}) and calls == []


def test_refused_accounts_and_invalid_requests_never_reach_the_hook(tmp_path):
    seen = []

    async def hook(request, account, request_chars, produce):
        seen.append(1)
        return await produce()
    client, calls = build(tmp_path, hook)
    assert ask(client, {"message": "Hello"}, who="free").status_code == 403
    assert ask(client, {"message": "Hello"}, who="nobody").status_code == 401
    assert ask(client, {"message": ""}).status_code == 422
    assert ask(client, {"message": "Hello", "extra": 1}).status_code == 422
    assert seen == [] and calls == []


def test_without_a_hook_the_route_is_unchanged(tmp_path):
    client, calls = build(tmp_path, None)
    assert ask(client, {"message": "Hello"}).json()["reply"] == "ok" and len(calls) == 1
