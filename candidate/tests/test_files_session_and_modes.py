"""A file request in the chat belongs to its temporary chat session (F4) and to the chosen reasoning mode (F5),
in the real application with files enabled. The account lookup and the provider are substituted in the child
process; the chat route, End & erase, the handoff, the assistant, the worker and the store are the real code.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("pptx")
pytest.importorskip("typst")

from test_unsafe_request_guard import ORIGIN, _run_app  # noqa: E402

MODES = {
    "KALILLAC_REASONING_MODES_ENABLED": "true",
    "KALILLAC_REASONING_MODE_QUICK_EFFORT": "none",
    "KALILLAC_REASONING_MODE_BALANCED_EFFORT": "low",
    "KALILLAC_REASONING_MODE_DEEP_EFFORT": "high",
    "KALILLAC_REASONING_MODE_QUICK_EXTRA_OUTPUT_TOKENS": "0",
    "KALILLAC_REASONING_MODE_BALANCED_EXTRA_OUTPUT_TOKENS": "0",
    "KALILLAC_REASONING_MODE_DEEP_EXTRA_OUTPUT_TOKENS": "8000",
    "KALILLAC_REASONING_MODE_DEFAULT": "balanced",
    "KALILLAC_REASONING_MODES_FREE": "quick,balanced",
    "KALILLAC_REASONING_MODES_PAID": "quick, balanced, deep",
}

COMMON = r'''
import asyncio, json, sys, threading, uuid
sys.path.insert(0, "tests")
import httpx
import app_fastapi_candidate as a
from kalillac_files.router import FileAccount
from kalillac_accounts.entitlements import PAID
from kalillac_accounts.paid_access import VerifiedAccount
VERIFIED = VerifiedAccount(uuid.uuid4(), PAID)
ACCOUNTS = {"paid": FileAccount("acct-paid", True), "free": FileAccount("acct-free", False),
            "verified": FileAccount("acct-verified", True, VERIFIED)}
async def authenticate(request):
    return ACCOUNTS.get(request.cookies.get("kalillac_session", ""))
a._file_service.authenticate = authenticate
SPEC = {"title": "Launch Plan", "slides": [{"layout": "title", "title": "Launch Plan"},
        {"layout": "points", "title": "Three steps", "points": ["Hire", "Build", "Open"]}]}
CREATE = {"output": [{"type": "function_call", "call_id": "c1", "name": "create_file",
                      "arguments": json.dumps({"format": "pptx", "spec": SPEC})}]}
REPLY = {"output": [{"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "Here is your deck."}]}]}
def headers(account):
    return {"cookie": "kalillac_session=" + account, "origin": "ORIGIN_VALUE"}
def client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=a.api), base_url="https://testserver")
'''

# F4: End & erase while the file assistant is waiting on the model. The model call blocks until the session
# has been ended, then asks for a file: nothing more may run, and the late result is not delivered.
SESSION_SCRIPT = COMMON + r'''
entered, release, calls = threading.Event(), threading.Event(), []
def model(items):
    calls.append(len(items))
    entered.set()
    assert release.wait(30)
    return CREATE if len(calls) == 1 else REPLY
a._file_service.call_model = model

async def scenario(first_reply):
    entered.clear(); release.clear(); del calls[:]
    if not first_reply:
        pass
    sid = a.new_session_id()
    a.get_session_state_by_id(sid)["memory"].append("an earlier temporary turn")
    async with client() as c:
        chat = asyncio.create_task(c.post("/api/chat", headers=headers("paid"),
                                          json={"message": "Make me a slide deck for the launch plan",
                                                "session_id": sid}))
        assert await asyncio.to_thread(entered.wait, 30)
        ended = await c.post("/api/chat/session/end", json={"session_id": sid}, headers=headers("paid"))
        release.set()
        reply = await chat
        again = await c.post("/api/chat", headers=headers("paid"),
                             json={"message": "Make me a slide deck for the launch plan", "session_id": sid})
    return {"ended": ended.json(), "status": reply.status_code, "body": reply.json(), "model_calls": len(calls),
            "files": [f.filename for f in a._file_store.list("acct-paid")], "after": [again.status_code, again.json()],
            "state_gone": sid not in a.SESSION_STATE}

print(json.dumps(asyncio.run(scenario(True))))

# Ending a session the request never joined changes nothing for it: a request in another session completes.
release.set()
async def other():
    entered.clear(); del calls[:]
    mine, theirs = a.new_session_id(), a.new_session_id()
    a.get_session_state_by_id(mine); a.get_session_state_by_id(theirs)
    async with client() as c:
        release.clear()
        chat = asyncio.create_task(c.post("/api/chat", headers=headers("paid"),
                                          json={"message": "Make me a slide deck", "session_id": mine}))
        assert await asyncio.to_thread(entered.wait, 30)
        ended = await c.post("/api/chat/session/end", json={"session_id": theirs}, headers=headers("paid"))
        release.set()
        reply = await chat
    return {"ended": ended.json()["active_requests"], "status": reply.status_code,
            "files": len(reply.json().get("files", [])), "session": reply.json().get("session_id") == mine}
print(json.dumps(asyncio.run(other())))
'''


def _lines(result, count):
    assert result.returncode == 0, result.stderr[-3000:]
    return [json.loads(line) for line in result.stdout.strip().splitlines() if line.startswith("{")][-count:]


def test_ending_the_session_during_a_file_request_stops_it_and_discards_its_late_result(tmp_path):
    env = {"KALILLAC_SITE_ORIGIN": ORIGIN, "KALILLAC_FILES_ENABLED": "true",
           "KALILLAC_FILES_ROOT": str(tmp_path / "files"), "OPENAI_API_KEY": "sk-test-not-real"}
    ended, other = _lines(_run_app(env, SESSION_SCRIPT.replace("ORIGIN_VALUE", ORIGIN)), 2)
    # The file request was registered under the session, so ending it reached the running work.
    assert ended["ended"]["active_requests"] == 1 and ended["ended"]["kalillac_session_state"] == "removed"
    # The late result is not delivered as the session's reply, and nothing ran after the end was observed:
    # the model's file request was not executed and no second model call was made.
    assert ended["status"] == 409 and ended["body"] == {"error": "session_ended"}
    assert ended["model_calls"] == 1 and ended["files"] == [] and ended["state_gone"] is True
    # The ended id stays ended: a stale request learns so instead of starting over.
    assert ended["after"] == [409, {"error": "session_ended"}]
    # Another session's end does not touch this request.
    assert other == {"ended": 0, "status": 200, "files": 1, "session": True}


# F5: the chosen mode is validated before any model work and reaches the provider payload of every file
# assistant call, through /api/chat and /api/files/assist alike.
MODES_SCRIPT = COMMON + r'''
import kalillac_accounts.paid_access as paid_access
a._v1_account_security = object()             # V1 mode: the chat reads the verified account per request.
async def verified_account(request, security, cookie_name):
    return VERIFIED if request.cookies.get("kalillac_session") == "verified" else None
paid_access.verified_account = verified_account

payloads = []
def provider(payload, timeout=None):
    payloads.append({"effort": payload["reasoning"]["effort"], "max": payload["max_output_tokens"],
                     "file_tools": "create_file" in [t["name"] for t in payload.get("tools", [])]})
    return CREATE if len([p for p in payloads if p["file_tools"]]) % 2 == 1 else REPLY
a._post_openai_responses = provider

async def run():
    out = {}
    async with client() as c:
        async def chat(account, mode, attach=None):
            del payloads[:]
            body = {"message": "Make me a slide deck for the launch plan"}
            if mode is not None:
                body["reasoning_mode"] = mode
            reply = await c.post("/api/chat", headers=headers(account), json=body)
            return [reply.status_code, reply.json().get("error"), list(payloads)]
        async def assist(account, mode):
            del payloads[:]
            body = {"message": "Make me a slide deck for the launch plan"}
            if mode is not None:
                body["reasoning_mode"] = mode
            reply = await c.post("/api/files/assist", headers={**headers(account), "content-type": "application/json"},
                                 content=json.dumps(body))
            return [reply.status_code, reply.json().get("error"), list(payloads)]
        for name, call in (("chat", chat), ("assist", assist)):
            out[name] = {
                "deep_verified": await call("verified", "deep"),
                "quick_verified": await call("verified", "quick"),
                "default_verified": await call("verified", None),
                "deep_unverified_paid": await call("paid", "deep"),
                "quick_unverified_paid": await call("paid", "quick"),
                "invalid": await call("verified", "turbo"),
                "wrong_type": await call("verified", ["deep"]),
            }
    return out
print(json.dumps(asyncio.run(run())))
print(json.dumps({"configured": a.OPENAI_REASONING_EFFORT}))
'''


def test_the_chosen_reasoning_mode_is_checked_before_dispatch_and_reaches_every_file_model_call(tmp_path):
    env = {"KALILLAC_SITE_ORIGIN": ORIGIN, "KALILLAC_FILES_ENABLED": "true",
           "KALILLAC_FILES_ROOT": str(tmp_path / "files"), "OPENAI_API_KEY": "sk-test-not-real", **MODES}
    result, configured = _lines(_run_app(env, MODES_SCRIPT.replace("ORIGIN_VALUE", ORIGIN)), 2)
    for path in ("chat", "assist"):
        modes = result[path]
        status, error, calls = modes["deep_verified"]
        # Both of the request's model calls (the tool round and the reply) carry Deep's effort and allowance.
        assert status == 200 and len(calls) == 2, (path, modes)
        assert {call["effort"] for call in calls} == {"high"} and all(call["file_tools"] for call in calls)
        base = calls[0]["max"] - 8000
        status, error, calls = modes["quick_verified"]
        assert status == 200 and [call["effort"] for call in calls] == ["none", "none"]
        assert {call["max"] for call in calls} == {base}
        # No mode: the configured effort, exactly as before modes existed.
        status, error, calls = modes["default_verified"]
        assert status == 200 and {call["effort"] for call in calls} == {configured["configured"]}
        # Refused before any model call: a paid mode without the verified paid entitlement, an unknown mode
        # and a malformed value.
        assert modes["deep_unverified_paid"] == [403, "reasoning_mode_not_allowed", []], path
        assert modes["invalid"] == [422, "invalid_request", []], path
        assert modes["wrong_type"] == [422, "invalid_request", []], path
        status, error, calls = modes["quick_unverified_paid"]
        assert status == 200 and [call["effort"] for call in calls] == ["none", "none"]
