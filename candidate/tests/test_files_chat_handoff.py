"""/api/chat hands a file request to the file assistant, in the real application with files enabled.

The account lookup and the model are substituted inside the child process; everything else (the
chat route, the handoff, the worker, the store) is the real code. With files disabled, the
application imports nothing from kalillac_files and /api/chat is unchanged.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("pptx")
pytest.importorskip("typst")

from test_unsafe_request_guard import ORIGIN, _run_app  # noqa: E402

SCRIPT = r'''
import asyncio, json, sys
sys.path.insert(0, "tests")
import app_fastapi_candidate as a
from fastapi.testclient import TestClient
from kalillac_files.router import FileAccount

ACCOUNTS = {"paid": FileAccount("acct-paid", True), "free": FileAccount("acct-free", False)}
async def authenticate(request):
    return ACCOUNTS.get(request.cookies.get("kalillac_session", ""))
a._file_service.authenticate = authenticate

# The real model call: through the application's single OpenAI seam, with the file timeout for its own
# calls only, the file tools and instructions, and the provider never contacted.
seen = []
def fake_post(payload, timeout=None):
    seen.append({"timeout": timeout, "tools": [t["name"] for t in payload["tools"]],
                 "max": payload["max_output_tokens"], "store": payload["store"],
                 "instructions": "FILE CREATION AND EDITING" in payload["instructions"]})
    return {"output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}]}
a._post_openai_responses = fake_post
a._file_service.call_model([{"role": "user", "content": "hi"}])
seam = {"call": seen[-1], "timeout_after": a._OPENAI_CALL_TIMEOUT.get(),
        "same_seam": a._post_openai_for_attempt.__name__}

SPEC = {"title": "Launch Plan", "slides": [{"layout": "title", "title": "Launch Plan"},
        {"layout": "points", "title": "Three steps", "points": ["Hire", "Build", "Open"]}]}
calls = []
def model(items):
    calls.append(items)
    if len(calls) % 2 == 1:  # Odd calls request the file, even calls reply.
        return {"output": [{"type": "function_call", "call_id": "c1", "name": "create_file",
                            "arguments": json.dumps({"format": "pptx", "spec": SPEC})}]}
    return {"output": [{"type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": "Here is a two-slide launch deck."}]}]}
a._file_service.call_model = model

from kalillac_accounts.request_guard import INVENTORY
unsafe = {(m.upper(), p) for p, ops in a.api.openapi()["paths"].items() for m in ops
          if m.upper() in {"POST", "PUT", "PATCH", "DELETE"} and p.startswith("/api/files")}
assert unsafe == {(r.method, r.path) for r in INVENTORY if r.family == "files"}, unsafe

client = TestClient(a.api, base_url="https://testserver")
paid = {"cookie": "kalillac_session=paid"}
reply = client.post("/api/chat", json={"message": "Make me a slide deck for the launch plan"}, headers=paid)
body = reply.json()
download = client.get("/api/files/" + body["files"][0]["id"], headers={**paid, "origin": "ORIGIN_VALUE"})
print(json.dumps({"status": reply.status_code, "keys": sorted(body), "reply": body["reply"],
                  "filename": body["files"][0]["filename"], "session_ok": len(body["session_id"]) >= 32,
                  "download": download.status_code, "is_pptx": download.content[:2] == b"PK",
                  "model_calls": len(calls)}))

class Request:
    def __init__(self, account):
        self.cookies = {"kalillac_session": account}
def handoff(account, message, file_ids=None):
    request = a.ChatRequest(message=message, history=[], file_ids=file_ids)
    found = asyncio.run(a._file_chat_handoff(Request(account), request, []))
    return None if found is None else [found.status_code, json.loads(found.body)]
print(json.dumps({
    "plain_question": handoff("paid", "What is the capital of Peru?"),
    "free_file_request": handoff("free", "Make me a PDF of the plan"),
    "free_with_attachment": handoff("free", "Look at this", ["x" * 22]),
    "signed_out_with_attachment": handoff("nobody", "Look at this", ["x" * 22]),
    "foreign_attachment": handoff("paid", "Edit this", ["x" * 22]),
    "malformed_attachments": handoff("paid", "Edit this", "abc"),
    "model_calls": len(calls),
    "seam": seam,
}))
'''


def test_a_file_request_in_the_chat_reaches_the_file_assistant_and_its_file(tmp_path):
    environment = {"KALILLAC_SITE_ORIGIN": ORIGIN, "KALILLAC_FILES_ENABLED": "true",
                   "KALILLAC_FILES_ROOT": str(tmp_path / "files"), "OPENAI_API_KEY": "sk-test-not-real"}
    result = _run_app(environment, SCRIPT.replace("ORIGIN_VALUE", ORIGIN))
    assert result.returncode == 0, result.stderr[-3000:]
    lines = [json.loads(line) for line in result.stdout.strip().splitlines()[-2:]]
    chat, handoffs = lines
    assert chat == {"status": 200, "keys": ["files", "reply", "session_id"],
                    "reply": "Here is a two-slide launch deck.",
                    "filename": "launch-plan.pptx", "session_ok": True, "download": 200, "is_pptx": True,
                    "model_calls": 2}
    assert handoffs["plain_question"] is None                    # The ordinary chat answers.
    assert handoffs["free_file_request"] is None                 # The ordinary chat answers; no file access.
    assert handoffs["free_with_attachment"] == [403, {"error": "files_require_premium"}]
    assert handoffs["signed_out_with_attachment"] == [401, {"error": "not_authenticated"}]
    assert handoffs["foreign_attachment"] == [404, {"error": "file_not_found"}]
    assert handoffs["malformed_attachments"] == [422, {"error": "invalid_request"}]
    assert handoffs["model_calls"] == 2                          # None of those reached the model.
    assert handoffs["seam"] == {"call": {"timeout": 240, "tools": ["create_file", "read_file", "edit_file"],
                                         "max": 32000, "store": False, "instructions": True},
                                "timeout_after": 90, "same_seam": "_post_openai_for_attempt"}


def test_with_files_disabled_nothing_of_files_is_loaded():
    result = _run_app({"KALILLAC_SITE_ORIGIN": ORIGIN}, (
        "import sys\nimport app_fastapi_candidate as a\n"
        "print(a._file_chat_handoff is None, any(m.startswith('kalillac_files') for m in sys.modules),"
        " any(p.startswith('/api/files') for p in a.api.openapi()['paths']))\n"))
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().splitlines()[-1] == "True False False"
