"""The shared account guard (kalillac_accounts.auth) and its use by
/api/spaces: session resolution, gate order, same-origin and JSON body
rules, forged identity, and the session recheck immediately before a reply
is saved. Isolated SQLite test database; no network."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import uuid

import pytest
from sqlalchemy import update

from kalillac_accounts.auth import (
    RequestRejected,
    read_json_object,
    require_same_origin,
    resolve_account,
    session_token_hash,
)
from kalillac_accounts.tokens import hash_session_token
from kalillac_db.models import AccountSession, Conversation, Message, Space, User
from spaces_test_support import (
    COOKIE,
    NOW,
    ORIGIN,
    build_app,
    client,
    headers,
    make_world,
    new_request_id,
    token_for,
)


@pytest.fixture
def world(tmp_path):
    w = make_world(tmp_path)
    yield w
    w.engine.dispose()


@pytest.fixture
def api(world):
    with client(build_app(world)) as c:
        yield c


def resolve(world, key, at=NOW):
    with world.factory() as session:
        return resolve_account(session, hash_session_token(token_for(key)), at)


# --- session resolution ---------------------------------------------------------------


def test_missing_cookie_has_no_session_hash():
    assert session_token_hash({}, COOKIE) is None
    assert session_token_hash({COOKIE: ""}, COOKIE) is None
    assert session_token_hash({COOKIE: "x"}, COOKIE) == hash_session_token("x")


def test_live_sessions_resolve_to_their_own_account_only(world):
    for key in ("a", "b", "c", "d"):
        auth = resolve(world, key)
        assert auth.user_id == world.users[key]
        assert auth.email_verified is (key != "d")
        assert repr(auth) == "AuthContext()"            # never prints the digest
    with world.factory() as session:
        assert resolve_account(session, hash_session_token("forged"), NOW) is None


def test_expired_revoked_and_inactive_sessions_resolve_to_nobody(world):
    assert resolve(world, "a", at=NOW + timedelta(days=15)) is None          # expired
    with world.factory() as session, session.begin():
        session.execute(update(AccountSession).where(AccountSession.user_id == world.users["b"])
                        .values(revoked_at=NOW))
        session.execute(update(User).where(User.id == world.users["c"]).values(is_active=False))
    assert resolve(world, "b") is None                                       # revoked
    assert resolve(world, "c") is None                                       # deactivated
    assert resolve(world, "a") is not None


# --- same-origin rule -----------------------------------------------------------------


@pytest.mark.parametrize("request_headers", [
    {"origin": ORIGIN},
    {"Origin": ORIGIN},
    {"ORIGIN": ORIGIN},
])
def test_same_origin_requests_pass(request_headers):
    require_same_origin(request_headers, ORIGIN)


@pytest.mark.parametrize("request_headers", [
    {},
    {"sec-fetch-site": "same-origin"},
    {"sec-fetch-site": " Same-Origin "},
    {"origin": ORIGIN + "/"},
    {"origin": ORIGIN.replace("https", "http")},
    {"origin": ORIGIN + ":444"},
    {"origin": ORIGIN.upper()},
    {"origin": "null"},
    {"origin": ""},
    {"origin": "https://kalillac.test.evil.example"},
    {"origin": "https://evil.example", "sec-fetch-site": "same-origin"},   # a present Origin decides
    {"sec-fetch-site": "same-site"},
    {"sec-fetch-site": "none"},
])
def test_everything_else_is_rejected(request_headers):
    with pytest.raises(RequestRejected) as rejected:
        require_same_origin(request_headers, ORIGIN)
    assert (rejected.value.status, rejected.value.code) == (403, "origin_rejected")


# --- JSON body rule -------------------------------------------------------------------


class FakeRequest:
    def __init__(self, content_type, chunks, content_length=None):
        self.headers = {"content-type": content_type} if content_type is not None else {}
        if content_length is not None:
            self.headers["content-length"] = content_length
        self._chunks = chunks
        self.consumed = 0

    async def stream(self):
        for chunk in self._chunks:
            self.consumed += 1
            yield chunk


def read(request, cap=16):
    return asyncio.run(read_json_object(request, cap))


def rejected_with(request, cap=16):
    with pytest.raises(RequestRejected) as rejected:
        read(request, cap)
    return rejected.value.status, rejected.value.code


def test_a_small_json_object_is_read():
    assert read(FakeRequest("application/json", [b'{"a":', b"1}"])) == {"a": 1}
    assert read(FakeRequest("Application/JSON; charset=utf-8", [b"{}"])) == {}


@pytest.mark.parametrize("content_type", [None, "", "text/plain", "application/json-patch+json",
                                          "multipart/form-data", "application/x-www-form-urlencoded"])
def test_only_application_json_is_accepted(content_type):
    request = FakeRequest(content_type, [b"{}"])
    assert rejected_with(request) == (415, "json_required")
    assert request.consumed == 0                         # refused before reading


def test_body_cap_applies_to_declared_and_streamed_bodies():
    declared = FakeRequest("application/json", [b"{}"], content_length="17")
    assert rejected_with(declared) == (413, "body_too_large") and declared.consumed == 0
    for bogus in ("-1", "1e3", "abc", "9" * 10):
        assert rejected_with(FakeRequest("application/json", [b"{}"], content_length=bogus)) == (413, "body_too_large")
    # No Content-Length (chunked): stops as soon as the running total passes the cap.
    streamed = FakeRequest("application/json", [b'{"a":"', b"x" * 10, b"x" * 10, b"never read"])
    assert rejected_with(streamed) == (413, "body_too_large") and streamed.consumed == 3
    assert read(FakeRequest("application/json", [b'{"a":"' + b"x" * 8 + b'"}'])) == {"a": "x" * 8}  # exactly 16


@pytest.mark.parametrize("raw", [b"", b"{", b"[]", b'"text"', b"1", b"null", b"\xff\xfe"])
def test_anything_but_a_json_object_is_invalid(raw):
    assert rejected_with(FakeRequest("application/json", [raw])) == (422, "invalid_request")


# --- gate order through /api/spaces ----------------------------------------------------


def test_gates_run_in_order_before_origin_and_body_checks(api, world):
    hostile = {"origin": "https://evil.example", "Content-Type": "text/plain"}
    cases = [
        (None, 401, "not_authenticated"),                 # no session
        ("c", 404, "not_found"),                          # verified, not allowlisted
        ("d", 403, "email_verification_required"),        # allowlisted, unverified
        ("a", 403, "origin_rejected"),                    # allowed: origin is next
    ]
    for key, status, code in cases:
        response = api.post("/api/spaces", content=b"not json",
                            headers={**headers(key, origin=None), **hostile})
        assert (response.status_code, response.json()) == (status, {"error": code}), key
    assert world.count(Space) == 0


def test_revoked_or_expired_session_is_401_on_every_method(api, world):
    with world.factory() as session, session.begin():
        session.execute(update(AccountSession).where(AccountSession.user_id == world.users["a"])
                        .values(revoked_at=NOW))
    space_id = str(uuid.uuid4())
    for method, path in (("GET", "/api/spaces"), ("POST", "/api/spaces"), ("GET", f"/api/spaces/{space_id}"),
                         ("DELETE", f"/api/spaces/{space_id}")):
        response = api.request(method, path, json={"name": "x"} if method == "POST" else None, headers=headers("a"))
        assert (response.status_code, response.json()) == (401, {"error": "not_authenticated"}), (method, path)
    world.advance(timedelta(days=15).total_seconds())
    assert api.get("/api/spaces", headers=headers("b")).status_code == 401


def test_reads_need_no_origin_but_writes_do(api):
    assert api.get("/api/spaces", headers=headers("a", origin=None)).status_code == 200
    assert api.post("/api/spaces", json={"name": "x"}, headers=headers("a", origin=None)).status_code == 403


# --- forged identity ------------------------------------------------------------------


def test_forged_account_ids_in_query_body_or_path_change_nothing(api, world):
    other = str(world.users["b"])
    for query in (f"?user_id={other}", f"?account_id={other}", f"?owner={other}"):
        assert api.get("/api/spaces" + query, headers=headers("a")).status_code == 422
    for field in ("user_id", "account_id", "owner_id"):
        response = api.post("/api/spaces", json={"name": "x", field: other}, headers=headers("a"))
        assert (response.status_code, response.json()) == (422, {"error": "invalid_request"})
    assert world.count(Space) == 0

    # b's Space reached through a's session, with b's id anywhere it fits:
    # the same 404 as a Space that does not exist.
    space = api.post("/api/spaces", json={"name": "B"}, headers=headers("b")).json()["space"]
    conversation = api.post(f"/api/spaces/{space['id']}/conversations", json={}, headers=headers("b")).json()["conversation"]
    missing = str(uuid.uuid4())
    for path in (f"/api/spaces/{space['id']}", f"/api/spaces/{space['id']}?user_id={other}",
                 f"/api/spaces/{space['id']}/conversations/{conversation['id']}"):
        assert api.get(path, headers=headers("a")).json() == api.get(
            path.replace(space["id"], missing), headers=headers("a")).json()
    response = api.post(f"/api/spaces/{space['id']}/conversations/{conversation['id']}/messages",
                        json={"message": "hi", "client_request_id": new_request_id()}, headers=headers("a"))
    assert (response.status_code, response.json()) == (404, {"error": "not_found"})
    assert world.count(Message) == 0
    with world.factory() as session:
        assert {s.user_id for s in session.query(Space)} == {world.users["b"]}


# --- recheck before persistence -------------------------------------------------------


@pytest.mark.parametrize("change", ["revoke", "expire", "unverify", "deactivate"])
def test_the_session_is_rechecked_immediately_before_saving(world, change):
    async def generate(request, *, user_id, lock_key, message, history, workspace, budget, finalize):
        # The reply is ready; between generation and saving the account changes.
        with world.factory() as session, session.begin():
            if change == "revoke":
                session.execute(update(AccountSession).where(AccountSession.user_id == user_id).values(revoked_at=NOW))
            elif change == "expire":
                world.advance(timedelta(days=15).total_seconds())
            elif change == "unverify":
                session.execute(update(User).where(User.id == user_id).values(email_verified_at=None))
            else:
                session.execute(update(User).where(User.id == user_id).values(is_active=False))
        return await finalize("A reply that must not be saved.")

    with client(build_app(world, generate=generate)) as api:
        space = api.post("/api/spaces", json={"name": "S"}, headers=headers("a")).json()["space"]
        conversation = api.post(f"/api/spaces/{space['id']}/conversations", json={},
                                headers=headers("a")).json()["conversation"]
        response = api.post(f"/api/spaces/{space['id']}/conversations/{conversation['id']}/messages",
                            json={"message": "hi", "client_request_id": new_request_id()}, headers=headers("a"))
    expected = 403 if change == "unverify" else 401
    assert response.status_code == expected, response.text
    assert world.count(Message) == 0
    with world.factory() as session:
        row = session.get(Conversation, uuid.UUID(conversation["id"]))
        assert (row.message_count, row.active_request_id, row.active_until) == (0, None, None)
