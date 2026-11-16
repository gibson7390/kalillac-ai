"""D3, V1 login routing, without a database server: in V1 mode the route verifies the password and takes
its session only from the issuance authority; a refusal or an unavailable authority never falls back to
the legacy session path. V1-off login is unchanged."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import os
import uuid

import pytest

os.environ.setdefault("GROQ_API_KEY", "test-not-real")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from kalillac_accounts.router import AccountSettings, build_account_router
from kalillac_accounts.v1_authority import SessionIssued, V1AuthorityUnavailable
from kalillac_db.models import AccountSession, Base, User

from account_test_db import make_session_factory, make_sqlite_engine

PASSWORD = "correct horse battery staple"
EMAIL = "person@example.com"
SETTINGS = AccountSettings(cookie_secure=True, session_days=14)
COOKIE = SETTINGS.cookie_name
V1_ON = object()          # Login never calls the read-path security object; it only marks V1 mode.


class Authority:
    """The issuance authority's contract, recorded."""

    def __init__(self, outcome="issued", error=None):
        self.outcome, self.error, self.calls = outcome, error, []

    def issue_session(self, **arguments):
        self.calls.append(arguments)
        if self.error is not None:
            raise self.error
        if self.outcome == "issued":
            at = datetime.now(timezone.utc)
            return SessionIssued("issued", at, at + timedelta(days=arguments["lifetime_days"]), False)
        return SessionIssued(self.outcome, None, None, False)


@pytest.fixture
def db():
    engine = make_sqlite_engine()
    Base.metadata.create_all(engine)
    yield make_session_factory(engine)
    engine.dispose()


def client_for(db, *, v1_security=V1_ON, v1_authority=None):
    api = FastAPI()
    api.include_router(build_account_router(session_factory=lambda: db, settings=SETTINGS,
                                            v1_security=v1_security, v1_authority=v1_authority))
    return TestClient(api, base_url="https://testserver")


def registered(db):
    with client_for(db, v1_security=None) as client:
        assert client.post("/api/account/register", json={"email": EMAIL, "password": PASSWORD}).status_code == 201
    with db() as session:
        user = session.scalar(select(User))
        return user.id, user.password_hash


def sessions(db):
    with db() as session:
        return session.scalar(select(func.count()).select_from(AccountSession))


def test_v1_login_takes_its_session_only_from_the_authority(db):
    user_id, stored = registered(db)
    authority = Authority()
    with client_for(db, v1_authority=authority) as client:
        response = client.post("/api/account/login", json={"email": EMAIL, "password": PASSWORD})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json()["user"]["email"] == EMAIL
    token = response.cookies.get(COOKIE)
    assert token and "HttpOnly" in response.headers["set-cookie"] and "Secure" in response.headers["set-cookie"]
    (call,) = authority.calls
    assert type(call["session_id"]) is uuid.UUID and call["user_id"] == user_id
    assert call["token_hash"] == hashlib.sha256(token.encode()).hexdigest()       # The digest, never the token.
    assert call["verified_password_hash"] == stored and call["lifetime_days"] == 14
    assert token not in repr(authority.calls)
    assert sessions(db) == 0                                                      # Nothing on the legacy path.


@pytest.mark.parametrize("authority, status, code", [
    (Authority("not_authenticated"), 401, "invalid_credentials"),                # Refused (e.g. deletion requested).
    (Authority("security_unavailable"), 503, "account_unavailable"),
    (Authority(error=V1AuthorityUnavailable()), 503, "account_unavailable"),     # The authority cannot answer.
    (None, 503, "account_unavailable"),                                           # No authority at all.
])
def test_a_refused_or_unavailable_authority_never_falls_back_to_a_legacy_session(db, authority, status, code):
    registered(db)
    with client_for(db, v1_authority=authority) as client:
        response = client.post("/api/account/login", json={"email": EMAIL, "password": PASSWORD})
        assert (response.status_code, response.json()) == (status, {"error": code})
        assert "set-cookie" not in response.headers and COOKIE not in client.cookies
    assert sessions(db) == 0
    if authority is not None:
        assert len(authority.calls) == 1


@pytest.mark.parametrize("email, password", [(EMAIL, "wrong password entirely"), ("nobody@example.com", PASSWORD)])
def test_wrong_credentials_never_reach_the_authority(db, email, password):
    registered(db)
    authority = Authority()
    with client_for(db, v1_authority=authority) as client:
        response = client.post("/api/account/login", json={"email": email, "password": password})
    assert (response.status_code, response.json()) == (401, {"error": "invalid_credentials"})
    assert authority.calls == [] and sessions(db) == 0


def test_v1_login_writes_nothing_as_the_runtime_role(db, monkeypatch):
    """Neither a legacy session nor a rehash, even for a hash that would need one: the authority
    compares the exact hash verified here."""
    import kalillac_accounts.router as router
    user_id, stored = registered(db)
    monkeypatch.setattr(router, "needs_rehash", lambda value: True)
    for writer in ("create_account_session", "set_password_hash", "lock_account"):
        monkeypatch.setattr(router, writer, lambda *a, **k: pytest.fail("legacy write in V1 login"))
    authority = Authority()
    with client_for(db, v1_authority=authority) as client:
        assert client.post("/api/account/login", json={"email": EMAIL, "password": PASSWORD}).status_code == 200
    with db() as session:
        assert session.get(User, user_id).password_hash == stored
    assert authority.calls[0]["verified_password_hash"] == stored


def test_v1_off_login_is_unchanged_and_ignores_an_authority(db):
    registered(db)
    authority = Authority()
    with client_for(db, v1_security=None, v1_authority=authority) as client:
        response = client.post("/api/account/login", json={"email": EMAIL, "password": PASSWORD})
        assert response.status_code == 200 and response.cookies.get(COOKIE)
        assert client.get("/api/account/me").json()["user"]["email"] == EMAIL
    assert authority.calls == [] and sessions(db) == 1                            # The legacy session, as before.


def test_the_application_still_passes_no_authority_and_keeps_its_startup_gate():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "app_fastapi_candidate.py").read_text(encoding="utf-8")
    assert "v1_authority" not in source
    assert "_v1_account_security = _load_v1_startup()" in source
