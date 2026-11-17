"""Spaces under V1 account security, on real disposable PostgreSQL 16 after the operator transition.

With V1 on, every Spaces request is authorized by V1-01's original-session authorization (the same
read /api/account and /api/files use), and a send's save transaction rechecks it under the account's
V1 lock, taken before any other lock of that transaction. Proved here through the real Spaces router
and the runtime login under E1a's final grants: a verified account creates a Space and saves an
exchange; the authorization each request uses is V1's; an account suspended while its answer is being
generated has nothing saved; an account V1 refuses (quarantined) is refused by Spaces even with a live
session row; and the runtime login writes no account table. The model is scripted; nothing leaves the
disposable database.
"""
# ruff: noqa: F811  (fixtures imported from other test modules are used as parameters)
from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from kalillac_spaces.config import SpacesConfig
from kalillac_spaces.router import build_spaces_router
from test_v1_lifecycle_postgres import (COOKIE, ORIGIN, PASSWORDS, account_row, live, operator, post,  # noqa: F401
                                        sign_in, writers)
from test_v1_revocation_postgres import admin_sql
from test_v1_transition_postgres import world  # noqa: F401

REPLY = "A scripted Space reply."


@pytest.fixture
def spaces(live):
    w = live
    assert post(w, "/api/account/register", {"email": "lifecycle@example.test"}).status_code == 202
    assert post(w, "/api/account/register/confirm", {"token": w.token(), "password": PASSWORDS[0]}).status_code == 200
    w.user = account_row(w)[0]
    w.cookie = sign_in(w, PASSWORDS[0])
    w.reads, w.during_generation = [], None
    original = w.security.read_async

    async def counted(*args, **kwargs):
        w.reads.append(1)
        return await original(*args, **kwargs)
    w.security.read_async = counted

    async def generate(request, *, user_id, lock_key, message, history, workspace, budget, finalize):
        if w.during_generation is not None:
            w.during_generation()
        return await finalize(REPLY, None)

    api = FastAPI()
    api.include_router(build_spaces_router(
        config=SpacesConfig(beta_account_ids=frozenset({w.user}), site_origin=ORIGIN), cookie_name=COOKIE,
        generate=generate, lease_seconds=60, session_factory=lambda: w.runtime_factory, v1_security=w.security))
    w.client = TestClient(api, base_url="https://testserver")
    try:
        yield w
    finally:
        w.client.close()


def call(w, method, path, body=None, cookie=None):
    w.client.cookies.clear()
    headers = {"Cookie": f"{COOKIE}={cookie or w.cookie}"}
    if method == "POST":
        headers["Origin"] = ORIGIN
    return w.client.request(method, "/api/spaces" + path, json=body, headers=headers)


def space_and_conversation(w):
    space = call(w, "POST", "", {"name": "Synthetic", "instructions": ""})
    assert space.status_code in (200, 201), space.text
    sid = space.json()["space"]["id"]
    conversation = call(w, "POST", f"/{sid}/conversations", {"title": "First"})
    assert conversation.status_code in (200, 201), conversation.text
    return sid, conversation.json()["conversation"]["id"]


def send(w, sid, cid, message="Hello"):
    return call(w, "POST", f"/{sid}/conversations/{cid}/messages",
                {"message": message, "client_request_id": str(uuid.uuid4())})


def saved_messages(w):
    with w.admin_engine.connect() as c:
        return c.scalar(text("SELECT count(*) FROM kalillac.messages"))


def test_a_verified_account_uses_spaces_through_v1_and_writes_no_account_table(spaces):
    w = spaces
    sid, cid = space_and_conversation(w)
    before = len(w.reads)
    sent = send(w, sid, cid)
    assert sent.status_code == 200, sent.text
    assert sent.json()["assistant_message"]["content"] == REPLY
    assert len(w.reads) == before + 1                     # The send was authorized by V1's read.
    listed = call(w, "GET", "")
    assert listed.status_code == 200 and [s["id"] for s in listed.json()["spaces"]] == [sid]
    assert saved_messages(w) == 2
    assert w.names["app"] not in writers(w)               # Account tables: never the runtime login.


def test_an_account_suspended_while_its_answer_is_generated_has_nothing_saved(spaces):
    w = spaces
    sid, cid = space_and_conversation(w)
    w.during_generation = lambda: operator(w, "suspend", w.user, "abuse_report")
    refused = send(w, sid, cid)
    assert refused.status_code == 401 and refused.json()["error"] == "not_authenticated"
    assert saved_messages(w) == 0
    with w.admin_engine.connect() as c:                   # The lease was released, not left to expire.
        assert c.scalar(text("SELECT count(*) FROM kalillac.conversations WHERE active_request_id IS NOT NULL")) == 0


def test_an_account_v1_refuses_is_refused_by_spaces_even_with_a_live_session(spaces):
    w = spaces
    sid, cid = space_and_conversation(w)
    # Quarantined but with its session row left live: the legacy lookup would admit it; V1 does not.
    admin_sql(w, "ALTER TABLE kalillac.users DISABLE TRIGGER USER",
              "UPDATE kalillac.users SET quarantined_at = clock_timestamp() WHERE id = :u",
              "ALTER TABLE kalillac.users ENABLE TRIGGER USER", u=w.user)
    with w.admin_engine.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM kalillac.account_sessions WHERE user_id = :u"
                             " AND revoked_at IS NULL"), {"u": w.user}) == 1
    for method, path, body in (("GET", "", None), ("POST", f"/{sid}/conversations", {"title": "x"})):
        refused = call(w, method, path, body)
        assert (refused.status_code, refused.json()["error"]) == (403, "account_unavailable")
    assert send(w, sid, cid).status_code == 403 and saved_messages(w) == 0


def test_another_accounts_cookie_and_no_cookie_are_not_authenticated(spaces):
    w = spaces
    assert call(w, "GET", "", cookie="not-a-session").status_code == 401
    w.client.cookies.clear()
    assert w.client.get("/api/spaces").status_code == 401
