"""D3, the remaining V1 account routes without a database server: sign-out, revoking sessions, password
change, the customer's own deletion request, recovery confirmation and email verification. In V1 mode
each change goes only through its private authority (recorded here), with the exact original session;
nothing is written as the runtime role, and a refused, absent or malformed answer never falls back to
the legacy path. V1-off behaviour is unchanged. Real PostgreSQL: test_v1_account_routes_postgres.py."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
import uuid

import pytest

os.environ.setdefault("GROQ_API_KEY", "test-not-real")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from kalillac_accounts.auth import AccountIdentity, AuthorizedAccount, RequestRejected
from kalillac_accounts.passwords import MAX_PASSWORD_LENGTH, verify_password
from kalillac_accounts.rate_limit import LoginRateLimiter
from kalillac_accounts.router import AccountSettings, build_account_router
from kalillac_accounts.v1_authority import (DeletionRequested, PasswordChanged, SessionsRevoked,
                                            V1AuthorityUnavailable)
from kalillac_accounts.v1_security import V1SecurityUnavailable
from kalillac_db.models import AccountSession, Base, User

from account_test_db import make_session_factory, make_sqlite_engine

PASSWORD = "correct horse battery staple"
NEW_PASSWORD = "an entirely new passphrase"
EMAIL = "person@example.com"
SETTINGS = AccountSettings(cookie_secure=True, session_days=14)
COOKIE = SETTINGS.cookie_name


class FakeSecurity:
    """V1-01's read path, over SQLite: the exact binding for a digest, then (when asked) the account's
    authorization. Records how it was called; writes nothing."""

    def __init__(self, db, unavailable=False):
        self.db, self.unavailable, self.calls = db, unavailable, []

    async def read_async(self, token_hash, work, with_identity=False, authorize=True):
        self.calls.append({"with_identity": with_identity, "authorize": authorize})
        if self.unavailable:
            raise V1SecurityUnavailable()
        with self.db() as session:
            row = session.scalar(select(AccountSession).where(AccountSession.token_hash == token_hash))
            if row is None:
                raise RequestRejected(401, "not_authenticated")
            identity = AccountIdentity(row.user_id, row.id, row.token_hash)
            account = None
            if authorize:
                user = session.get(User, row.user_id)
                if row.revoked_at is not None:
                    raise RequestRejected(401, "not_authenticated")
                if user.quarantined_at is not None:
                    raise RequestRejected(403, "account_unavailable")
                now = datetime.now(timezone.utc)
                account = AuthorizedAccount(user.id, user.email, user.created_at, now, now)
            result = work(session, account, identity) if with_identity else work(session, account)
            assert not (session.new or session.dirty or session.deleted)
            return result


class Authority:
    """Every V1 authority the routes call, each giving the listed answers in turn (exceptions raised)."""

    def __init__(self, **answers):
        self.answers = {name: list(values) for name, values in answers.items()}
        self.calls = []

    def _answer(self, name, arguments):
        self.calls.append((name, arguments))
        answer = self.answers[name].pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def end_session(self, **arguments):
        return self._answer("end_session", arguments)

    def revoke_sessions(self, **arguments):
        return self._answer("revoke_sessions", arguments)

    def change_password(self, **arguments):
        return self._answer("change_password", arguments)

    def request_own_deletion(self, **arguments):
        return self._answer("request_own_deletion", arguments)


class RecordingLimiter(LoginRateLimiter):
    def __init__(self):
        super().__init__()
        self.calls = []

    def begin(self, keys):
        self.calls.append("begin")
        return super().begin(keys)

    def record_failure(self, keys):
        self.calls.append("failure")
        super().record_failure(keys)

    def release(self, keys):
        self.calls.append("release")
        super().release(keys)

    def record_success(self, keys):
        self.calls.append("success")
        super().record_success(keys)

    def in_flight(self):
        return sum(bucket.in_flight for bucket in self._store._buckets.values())


@pytest.fixture
def db():
    engine = make_sqlite_engine()
    Base.metadata.create_all(engine)
    yield make_session_factory(engine)
    engine.dispose()


def legacy_client(db):
    api = FastAPI()
    api.include_router(build_account_router(session_factory=lambda: db, settings=SETTINGS, v1_security=None))
    return TestClient(api, base_url="https://testserver")


def v1_client(db, authority, *, security=None, limiter=None, raise_server_exceptions=True):
    from test_v1_login import attach_credentials
    attach_credentials(authority, db)
    api = FastAPI()
    api.include_router(build_account_router(session_factory=lambda: db, settings=SETTINGS,
                                            limiter=limiter or LoginRateLimiter(),
                                            v1_security=security or FakeSecurity(db), v1_authority=authority))
    return TestClient(api, base_url="https://testserver", raise_server_exceptions=raise_server_exceptions)


def signed_in(db):
    """A registered account with one legacy session (fixture only); returns (user_id, stored hash, token,
    session id)."""
    with legacy_client(db) as client:
        assert client.post("/api/account/register", json={"email": EMAIL, "password": PASSWORD}).status_code == 201
        response = client.post("/api/account/login", json={"email": EMAIL, "password": PASSWORD})
        token = response.cookies.get(COOKIE)
    with db() as session:
        user = session.scalar(select(User))
        row = session.scalar(select(AccountSession))
        return user.id, user.password_hash, token, row.id


def state(db):
    """Everything the routes must never write as the runtime role."""
    with db() as session:
        user = session.scalar(select(User))
        return (user.password_hash, user.deletion_requested_at, user.sessions_valid_after,
                tuple(session.execute(select(AccountSession.id, AccountSession.revoked_at)).all()),
                session.scalar(select(func.count()).select_from(AccountSession)))


def post(client, path, token=None, **kwargs):
    headers = kwargs.pop("headers", {})
    if token is not None:
        headers["Cookie"] = f"{COOKIE}={token}"
    return client.post(path, headers=headers, **kwargs)


def cleared(response):
    return 'Max-Age=0' in response.headers.get("set-cookie", "") or 'expires=' in response.headers.get(
        "set-cookie", "").lower()


V1_ROUTES = ["/api/account/sessions/revoke-others", "/api/account/sessions/revoke-all",
             "/api/account/password", "/api/account/deletion-request"]


# --- V1 off -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", V1_ROUTES)
def test_v1_off_mounts_none_of_the_new_routes(db, path):
    signed_in(db)
    with legacy_client(db) as client:
        assert client.post(path, json={}).status_code == 404


def test_v1_off_logout_is_unchanged(db):
    _, _, token, _ = signed_in(db)
    with legacy_client(db) as client:
        response = post(client, "/api/account/logout", token)
        assert (response.status_code, response.json()) == (200, {"ok": True}) and cleared(response)
    with db() as session:
        assert session.scalar(select(AccountSession.revoked_at)) is not None      # Revoked as before.


def test_v1_mode_never_registers_on_the_legacy_path(db):
    """Without the registration authority, V1 mode refuses to create an account as the runtime role."""
    with v1_client(db, Authority()) as client:
        response = client.post("/api/account/register", json={"email": "new@example.com", "password": PASSWORD})
    assert (response.status_code, response.json()) == (503, {"error": "registration_unavailable"})
    with db() as session:
        assert session.scalar(select(func.count()).select_from(User)) == 0


# --- sign-out -----------------------------------------------------------------------------------------

def test_v1_logout_ends_exactly_the_presented_session_through_the_authority(db):
    user_id, _, token, session_id = signed_in(db)
    before = state(db)
    authority, security = Authority(end_session=["ended"]), FakeSecurity(db)
    with v1_client(db, authority, security=security) as client:
        response = post(client, "/api/account/logout", token)
    assert (response.status_code, response.json()) == (200, {"ok": True}) and cleared(response)
    assert authority.calls == [("end_session", {"session_id": session_id,
                                                "token_hash": hashlib.sha256(token.encode()).hexdigest()})]
    assert security.calls == [{"with_identity": True, "authorize": False}]   # Any binding may be ended.
    assert state(db) == before                                                 # Nothing written as R.


@pytest.mark.parametrize("token", [None, "not-a-session-anyone-holds"])
def test_v1_logout_without_a_bound_session_succeeds_without_calling_the_authority(db, token):
    signed_in(db)
    authority = Authority()
    with v1_client(db, authority) as client:
        response = post(client, "/api/account/logout", token)
    assert (response.status_code, response.json()) == (200, {"ok": True}) and cleared(response)
    assert authority.calls == []


@pytest.mark.parametrize("answer", [V1AuthorityUnavailable(), None, "ENDED", object()],
                         ids=["unavailable", "none", "another_string", "another_type"])
def test_v1_logout_that_cannot_be_confirmed_is_503_and_keeps_the_cookie_for_a_retry(db, answer):
    _, _, token, _ = signed_in(db)
    before = state(db)
    with v1_client(db, Authority(end_session=[answer])) as client:
        response = post(client, "/api/account/logout", token)
    assert (response.status_code, response.json()) == (503, {"error": "account_unavailable"})
    assert "set-cookie" not in response.headers
    assert state(db) == before


def test_v1_logout_without_an_authority_or_a_read_path_is_503(db):
    _, _, token, _ = signed_in(db)
    with v1_client(db, None) as client:
        assert post(client, "/api/account/logout", token).status_code == 503
    with v1_client(db, Authority(), security=FakeSecurity(db, unavailable=True)) as client:
        response = post(client, "/api/account/logout", token)
        assert (response.status_code, response.json()) == (503, {"error": "account_unavailable"})
        assert "set-cookie" not in response.headers


# --- revoking sessions --------------------------------------------------------------------------------

@pytest.mark.parametrize("scope", ["others", "all"])
def test_revoking_sessions_goes_through_the_authority_with_the_original_session(db, scope):
    _, _, token, session_id = signed_in(db)
    before = state(db)
    authority = Authority(revoke_sessions=[SessionsRevoked("revoked", None, False)])
    with v1_client(db, authority) as client:
        response = post(client, f"/api/account/sessions/revoke-{scope}", token, json={})
    assert (response.status_code, response.json()) == (200, {"ok": True})
    assert cleared(response) == (scope == "all")                 # Revoke-all ended this session too.
    ((name, call),) = authority.calls
    assert name == "revoke_sessions" and call["scope"] == scope and call["session_id"] == session_id
    assert call["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert type(call["operation_id"]) is uuid.UUID
    assert state(db) == before


BAD_BODIES = {
    "not_json_type": ({"content": b"{}", "headers": {"Content-Type": "text/plain"}}, 415, "json_required"),
    "invalid_json": ({"content": b"{", "headers": {"Content-Type": "application/json"}}, 400, "invalid_json"),
    "a_list": ({"content": b"[]", "headers": {"Content-Type": "application/json"}}, 400, "invalid_body"),
    "extra_key": ({"content": b'{"x":1}', "headers": {"Content-Type": "application/json"}}, 400, "invalid_body"),
    "too_large": ({"content": b"{}" + b" " * 4095, "headers": {"Content-Type": "application/json"}}, 413,
                  "body_too_large"),
}


@pytest.mark.parametrize("case", list(BAD_BODIES))
@pytest.mark.parametrize("scope", ["others", "all"])
def test_revoking_refuses_any_body_but_an_exact_empty_object_before_any_work(db, scope, case):
    _, _, token, _ = signed_in(db)
    kwargs, status, code = BAD_BODIES[case]
    authority, security = Authority(), FakeSecurity(db)
    with v1_client(db, authority, security=security) as client:
        response = post(client, f"/api/account/sessions/revoke-{scope}", token, **kwargs)
    assert (response.status_code, response.json()) == (status, {"error": code})
    assert authority.calls == [] and security.calls == []


def test_duplicate_keys_are_refused(db):
    _, _, token, _ = signed_in(db)
    authority = Authority()
    with v1_client(db, authority) as client:
        response = post(client, "/api/account/password", token,
                        content=b'{"current_password":"a","current_password":"b","new_password":"c"}',
                        headers={"Content-Type": "application/json"})
    assert (response.status_code, response.json()) == (400, {"error": "invalid_body"})
    assert authority.calls == []


REFUSALS = [
    ("not_authenticated", None, 401, "not_authenticated"),
    ("account_unavailable", None, 403, "account_unavailable"),
    ("too_many_operations", 7, 429, "too_many_operations"),
    ("security_unavailable", None, 503, "security_unavailable"),
]


@pytest.mark.parametrize("outcome, retry, status, code", REFUSALS)
def test_every_revocation_refusal_is_mapped_and_nothing_is_cleared(db, outcome, retry, status, code):
    _, _, token, _ = signed_in(db)
    with v1_client(db, Authority(revoke_sessions=[SessionsRevoked(outcome, retry, False)])) as client:
        response = post(client, "/api/account/sessions/revoke-all", token, json={})
    assert (response.status_code, response.json()) == (status, {"error": code})
    assert "set-cookie" not in response.headers
    assert response.headers.get("retry-after") == (str(retry) if retry else None)


class LooksLike:
    def __init__(self, value):
        self.value = value

    def __eq__(self, other):
        return other == self.value

    def __ne__(self, other):
        return not self.__eq__(other)

    __hash__ = None


MALFORMED = {
    "unavailable": V1AuthorityUnavailable(),
    "none": None,
    "another_type": DeletionRequested("revoked", None, False),
    "a_tuple": ("revoked", None, False),
    "unknown_outcome": SessionsRevoked("granted", None, False),
    "look_alike_outcome": SessionsRevoked(LooksLike("revoked"), None, False),
    "retry_not_an_int": SessionsRevoked("too_many_operations", "7", False),
    "retry_zero": SessionsRevoked("too_many_operations", 0, False),
    "retry_a_bool": SessionsRevoked("too_many_operations", True, False),
}


@pytest.mark.parametrize("answer", list(MALFORMED.values()), ids=list(MALFORMED))
def test_a_malformed_revocation_answer_is_503_and_clears_nothing(db, answer):
    _, _, token, _ = signed_in(db)
    with v1_client(db, Authority(revoke_sessions=[answer])) as client:
        response = post(client, "/api/account/sessions/revoke-all", token, json={})
    assert (response.status_code, response.json()) == (503, {"error": "security_unavailable"})
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize("path", V1_ROUTES)
def test_without_a_live_session_no_route_reaches_an_authority(db, path):
    _, _, token, _ = signed_in(db)
    body = {"current_password": PASSWORD, "new_password": NEW_PASSWORD} if path.endswith("password") else (
        {"current_password": PASSWORD} if path.endswith("deletion-request") else {})
    authority = Authority()
    with v1_client(db, authority) as client:
        assert post(client, path, None, json=body).json() == {"error": "not_authenticated"}
        assert post(client, path, "unknown", json=body).status_code == 401
    with db() as session:
        session.scalar(select(AccountSession)).revoked_at = datetime.now(timezone.utc)
        session.commit()
    with v1_client(db, authority) as client:
        assert post(client, path, token, json=body).status_code == 401
    with v1_client(db, authority, security=FakeSecurity(db, unavailable=True)) as client:
        response = post(client, path, token, json=body)
        assert (response.status_code, response.json()) == (503, {"error": "account_unavailable"})
    assert authority.calls == []


# --- password change and the customer's deletion request ----------------------------------------------

def test_password_change_verifies_the_current_password_and_sends_only_hashes(db):
    user_id, stored, token, session_id = signed_in(db)
    before = state(db)
    limiter = RecordingLimiter()
    authority = Authority(change_password=[PasswordChanged("changed", None, False)])
    with v1_client(db, authority, limiter=limiter) as client:
        response = post(client, "/api/account/password", token,
                        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD})
    assert (response.status_code, response.json()) == (200, {"ok": True})
    assert "set-cookie" not in response.headers                   # This session stays.
    ((name, call),) = authority.calls
    assert name == "change_password" and call["session_id"] == session_id
    assert call["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert call["verified_password_hash"] == stored
    assert call["new_password_hash"].startswith("$argon2id$") and verify_password(call["new_password_hash"],
                                                                                  NEW_PASSWORD)
    assert PASSWORD not in repr(call) and NEW_PASSWORD not in repr(call) and token not in repr(call)
    assert limiter.calls == ["begin", "success"] and limiter.in_flight() == 0
    assert state(db) == before                                     # The hash is unchanged as R.


def test_the_customers_deletion_request_ends_this_session_and_sends_only_the_verified_hash(db):
    _, stored, token, session_id = signed_in(db)
    before = state(db)
    limiter = RecordingLimiter()
    authority = Authority(request_own_deletion=[DeletionRequested("requested", None, False)])
    with v1_client(db, authority, limiter=limiter) as client:
        response = post(client, "/api/account/deletion-request", token, json={"current_password": PASSWORD})
    assert (response.status_code, response.json()) == (200, {"ok": True}) and cleared(response)
    ((name, call),) = authority.calls
    assert name == "request_own_deletion" and call["session_id"] == session_id
    assert call["verified_password_hash"] == stored and set(call) == {
        "operation_id", "session_id", "token_hash", "verified_password_hash"}
    assert limiter.calls == ["begin", "success"] and limiter.in_flight() == 0
    assert state(db) == before


PROVEN = {
    "password": ("/api/account/password", {"current_password": PASSWORD, "new_password": NEW_PASSWORD},
                 "change_password", PasswordChanged, "changed"),
    "deletion": ("/api/account/deletion-request", {"current_password": PASSWORD},
                 "request_own_deletion", DeletionRequested, "requested"),
}


@pytest.mark.parametrize("route", list(PROVEN))
def test_a_wrong_current_password_is_403_counts_as_a_failed_guess_and_reaches_no_authority(db, route):
    path, body, name, _kind, _success = PROVEN[route]
    _, _, token, _ = signed_in(db)
    limiter, authority = RecordingLimiter(), Authority()
    with v1_client(db, authority, limiter=limiter) as client:
        for wrong in ("not the password", "x" * (MAX_PASSWORD_LENGTH + 1)):
            response = post(client, path, token, json={**body, "current_password": wrong})
            assert (response.status_code, response.json()) == (403, {"error": "invalid_current_password"})
    assert authority.calls == [] and limiter.calls == ["begin", "failure", "begin", "failure"]
    assert limiter.in_flight() == 0


@pytest.mark.parametrize("route", list(PROVEN))
def test_failed_current_password_guesses_share_the_sign_in_limiter(db, route):
    path, body, _name, _kind, _success = PROVEN[route]
    _, _, token, _ = signed_in(db)
    limiter = LoginRateLimiter()
    with v1_client(db, Authority(), limiter=limiter) as client:
        statuses = [post(client, path, token, json={**body, "current_password": "wrong"}).status_code
                    for _ in range(12)]
        assert statuses[0] == 403 and statuses[-1] == 429
        limited = post(client, path, token, json=body)
        assert limited.status_code == 429 and int(limited.headers["retry-after"]) >= 1
        # Sign-in for the same account and client is limited by the same buckets.
        assert client.post("/api/account/login", json={"email": EMAIL, "password": PASSWORD}).status_code == 429


@pytest.mark.parametrize("outcome, retry, status, code", REFUSALS)
@pytest.mark.parametrize("route", list(PROVEN))
def test_every_proven_refusal_is_mapped_and_releases_the_attempt_once(db, route, outcome, retry, status, code):
    path, body, name, kind, _success = PROVEN[route]
    _, _, token, _ = signed_in(db)
    before = state(db)
    limiter = RecordingLimiter()
    with v1_client(db, Authority(**{name: [kind(outcome, retry, False)]}), limiter=limiter) as client:
        response = post(client, path, token, json=body)
    assert (response.status_code, response.json()) == (status, {"error": code})
    assert "set-cookie" not in response.headers
    assert limiter.calls == ["begin", "release"] and limiter.in_flight() == 0
    assert state(db) == before


@pytest.mark.parametrize("answer", ["unavailable", "none", "another_type", "look_alike", "unknown"])
@pytest.mark.parametrize("route", list(PROVEN))
def test_a_malformed_proven_answer_is_503_releases_once_and_writes_nothing(db, route, answer):
    path, body, name, kind, success = PROVEN[route]
    _, _, token, _ = signed_in(db)
    before = state(db)
    value = {"unavailable": V1AuthorityUnavailable(), "none": None,
             "another_type": SessionsRevoked(success, None, False),
             "look_alike": kind(LooksLike(success), None, False),
             "unknown": kind("granted", None, False)}[answer]
    limiter = RecordingLimiter()
    with v1_client(db, Authority(**{name: [value]}), limiter=limiter) as client:
        response = post(client, path, token, json=body)
    assert (response.status_code, response.json()) == (503, {"error": "security_unavailable"})
    assert "set-cookie" not in response.headers
    assert limiter.calls == ["begin", "release"] and limiter.in_flight() == 0
    assert state(db) == before


@pytest.mark.parametrize("route", list(PROVEN))
def test_cancellation_releases_the_attempt_once_and_propagates(db, route):
    class Cancelled(BaseException):
        pass
    path, body, name, _kind, _success = PROVEN[route]
    _, _, token, _ = signed_in(db)
    limiter = RecordingLimiter()
    client = v1_client(db, Authority(**{name: [Cancelled()]}), limiter=limiter)
    with pytest.raises(Cancelled):
        post(client, path, token, json=body)
    assert limiter.calls == ["begin", "release"] and limiter.in_flight() == 0


@pytest.mark.parametrize("body, status, code", [
    ({"current_password": PASSWORD, "new_password": "short"}, 422, "password_length"),
    ({"current_password": PASSWORD, "new_password": 5}, 422, "invalid_password"),
    ({"current_password": None, "new_password": NEW_PASSWORD}, 422, "invalid_password"),
    ({"current_password": PASSWORD}, 400, "invalid_body"),
    ({"current_password": PASSWORD, "new_password": NEW_PASSWORD, "email": EMAIL}, 400, "invalid_body"),
])
def test_password_change_body_policy_runs_before_any_work(db, body, status, code):
    _, _, token, _ = signed_in(db)
    limiter, authority, security = RecordingLimiter(), Authority(), FakeSecurity(db)
    with v1_client(db, authority, security=security, limiter=limiter) as client:
        response = post(client, "/api/account/password", token, json=body)
    assert (response.status_code, response.json()) == (status, {"error": code})
    assert authority.calls == [] and security.calls == [] and limiter.calls == []


def test_a_quarantined_account_reaches_no_authority(db):
    user_id, _, token, _ = signed_in(db)
    with db() as session:
        session.get(User, user_id).quarantined_at = datetime.now(timezone.utc)
        session.commit()
    authority = Authority()
    with v1_client(db, authority) as client:
        for path in V1_ROUTES:
            body = {"current_password": PASSWORD, "new_password": NEW_PASSWORD} if path.endswith("password") else (
                {"current_password": PASSWORD} if path.endswith("deletion-request") else {})
            response = post(client, path, token, json=body)
            assert (response.status_code, response.json()) == (403, {"error": "account_unavailable"})
    assert authority.calls == []


# --- recovery confirmation in V1 mode -----------------------------------------------------------------

class ResetAuthority:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def reset_password(self, **arguments):
        self.calls.append(arguments)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def recovery_world(security="v1", authority=None):
    import recovery_test_support as support
    from kalillac_accounts.recovery_router import build_recovery_router
    engine = make_sqlite_engine()
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    gateway = support.FakeEmail()
    user_id = support.add_user(factory)
    tokens = support.add_sessions(factory, user_id)
    legacy = TestClient(support.application(factory, gateway), base_url="https://testserver")
    link_token = support.request_link(legacy, gateway)                 # Issued on the existing path (fixture).
    api = FastAPI()
    api.include_router(build_recovery_router(
        session_factory=lambda: factory, gateway=gateway, settings=support.SETTINGS, clock=lambda: support.NOW,
        environ=support.ENV, v1_security=FakeSecurity(factory) if security == "v1" else None,
        v1_authority=authority))
    return support, factory, gateway, user_id, tokens, link_token, TestClient(api, base_url="https://testserver")


def recovery_state(factory):
    from kalillac_db.models import AccountRecoveryToken
    with factory() as session:
        user = session.scalar(select(User))
        return (user.password_hash, user.sessions_valid_after,
                tuple(session.execute(select(AccountSession.id, AccountSession.revoked_at)).all()),
                tuple(session.execute(select(AccountRecoveryToken.id, AccountRecoveryToken.consumed_at)).all()))


def test_v1_recovery_confirmation_resets_only_through_the_authority_and_writes_nothing_as_r():
    from kalillac_accounts.v1_authority import PasswordReset
    authority = ResetAuthority(PasswordReset("reset", None, False))
    support, factory, gateway, _, _, link_token, client = recovery_world(authority=authority)
    before = recovery_state(factory)
    response = support.confirm(client, link_token)
    assert (response.status_code, response.json()) == (200, {"ok": True})
    assert cleared(response)                                             # A reset never signs in.
    (call,) = authority.calls
    assert call["token_hash"] == hashlib.sha256(link_token.encode()).hexdigest()
    assert verify_password(call["new_password_hash"], support.NEW_PASSWORD)
    assert type(call["operation_id"]) is uuid.UUID and link_token not in repr(call)
    assert support.NEW_PASSWORD not in repr(call)
    assert recovery_state(factory) == before                            # Token, hash and sessions untouched as R.
    assert gateway.changed == ["person@example.com"]                    # Notified after the response.


@pytest.mark.parametrize("outcome, retry, status, code", [
    ("invalid_token", None, 400, "invalid_or_expired_token"),
    ("account_unavailable", None, 403, "account_unavailable"),
    ("too_many_operations", 9, 429, "too_many_operations"),
    ("security_unavailable", None, 503, "recovery_unavailable"),
])
def test_every_v1_reset_refusal_is_mapped_and_notifies_no_one(outcome, retry, status, code):
    from kalillac_accounts.v1_authority import PasswordReset
    support, factory, gateway, _, _, link_token, client = recovery_world(
        authority=ResetAuthority(PasswordReset(outcome, retry, False)))
    before = recovery_state(factory)
    response = support.confirm(client, link_token)
    assert (response.status_code, response.json()) == (status, {"error": code})
    assert response.headers.get("retry-after") == (str(retry) if retry else None)
    assert recovery_state(factory) == before and gateway.changed == []


@pytest.mark.parametrize("answer", ["no_authority", "unavailable", "none", "another_type", "look_alike"])
def test_a_missing_or_malformed_reset_answer_is_503_and_never_the_legacy_path(answer):
    from kalillac_accounts.v1_authority import PasswordReset
    value = {"unavailable": V1AuthorityUnavailable(), "none": None,
             "another_type": PasswordChanged("reset", None, False),
             "look_alike": PasswordReset(LooksLike("reset"), None, False)}.get(answer)
    authority = None if answer == "no_authority" else ResetAuthority(value)
    support, factory, gateway, _, _, link_token, client = recovery_world(authority=authority)
    before = recovery_state(factory)
    response = support.confirm(client, link_token)
    assert (response.status_code, response.json()) == (503, {"error": "recovery_unavailable"})
    assert recovery_state(factory) == before and gateway.changed == []


def test_an_invalid_link_is_refused_before_password_work_or_the_authority():
    authority = ResetAuthority()
    support, factory, _, _, _, _, client = recovery_world(authority=authority)
    response = support.confirm(client, "A" * 43)
    assert (response.status_code, response.json()) == (400, {"error": "invalid_or_expired_token"})
    assert authority.calls == []


# --- email verification in V1 mode --------------------------------------------------------------------

def test_v1_mode_mounts_no_email_verification_route_and_v1_off_is_unchanged():
    import verification_test_support as support
    from kalillac_accounts.email_verification_router import build_email_verification_router
    v1 = build_email_verification_router(gateway=support.FakeVerificationEmail(), settings=support.SETTINGS,
                                         environ=support.ENV, v1_security=object())
    assert v1.routes == []
    off = build_email_verification_router(gateway=support.FakeVerificationEmail(), settings=support.SETTINGS,
                                          environ=support.ENV)
    assert sorted(route.path for route in off.routes) == ["/api/account/email-verification/confirm",
                                                          "/api/account/email-verification/request"]


def test_the_new_routes_are_guarded_before_any_work(db):
    from kalillac_accounts.request_guard import V1_INVENTORY, UnsafeRequestGuard, guarded_route
    assert {(r.method, r.path) for r in V1_INVENTORY} == {("POST", p) for p in V1_ROUTES}
    for path in V1_ROUTES:
        assert guarded_route("POST", path).family == "account"
    _, _, token, _ = signed_in(db)
    authority, security = Authority(revoke_sessions=[SessionsRevoked("revoked", None, False)]), FakeSecurity(db)
    api = FastAPI()
    api.include_router(build_account_router(session_factory=lambda: db, settings=SETTINGS, v1_security=security,
                                            v1_authority=authority))
    origin = "https://kalillac.test"
    with TestClient(UnsafeRequestGuard(api, origin=origin), base_url="https://testserver") as client:
        for path in V1_ROUTES:
            for headers, query, status, code in (
                    ({}, "", 403, "origin_rejected"),
                    ({"Origin": "https://evil.example"}, "", 403, "origin_rejected"),
                    ({"Origin": origin}, "?x=1", 400, "query_not_allowed")):
                response = post(client, path + query, token, json={}, headers=dict(headers))
                assert (response.status_code, response.json()) == (status, {"error": code})
        assert authority.calls == [] and security.calls == []
        response = post(client, "/api/account/sessions/revoke-others", token, json={}, headers={"Origin": origin})
        assert response.status_code == 200 and len(authority.calls) == 1
