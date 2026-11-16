"""D3, V1 login through the actual HTTP route on real disposable PostgreSQL 16.

The application's startup gate still refuses V1 mode, so this fixture assembles V1 routing itself: the
real account router with V1-01's read path (as the runtime role R) and the issuance authority logged in
as S with its own SCRAM credential, exactly as test_v1_authority_postgres plays the operator transition
for S in the disposable database only. Deletion requests use the real 0023 functions: the operator's as
P (SET LOCAL ROLE from the disposable superuser), the customer's through S. Every password is synthetic.
"""
from __future__ import annotations

import base64
import hashlib
import os
import threading
import time
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from kalillac_accounts.passwords import hash_password
from kalillac_accounts.router import AccountSettings, build_account_router
from kalillac_accounts.v1_security import FLAG, load_v1_security
from test_v1_authority_postgres import build
from test_v1_revocation_postgres import admin_sql
from test_v1_session_end_postgres import head_world

ENV = {FLAG: "true", "KALILLAC_ACCOUNTS_ENABLED": "true", "KALILLAC_DB_ENABLED": "true"}
SETTINGS = AccountSettings(cookie_secure=True, session_days=14)
COOKIE = SETTINGS.cookie_name
PASSWORD = "synthetic correct horse battery"
STORED = hash_password(PASSWORD)                         # One real Argon2id hash for every fixture account.


@pytest.fixture
def world(monkeypatch):
    with head_world(monkeypatch) as w:
        w.service_password = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
        admin_sql(w, f"ALTER ROLE {w.names['service']} LOGIN PASSWORD '{w.service_password}'",
                  f"GRANT CONNECT ON DATABASE {w.migration.url.database} TO {w.names['service']}")
        with w.admin_engine.connect() as c:
            w.instance = c.scalar(text("SELECT database_instance_id FROM kalillac.account_registration_activation"))
        w.built = []
        w.authority = build(w)
        w.security = load_v1_security(lambda: w.runtime_factory, ENV)
        yield w
        for authority in w.built:
            authority.dispose()
        admin_sql(w, f"REVOKE CONNECT ON DATABASE {w.migration.url.database} FROM {w.names['service']}",
                  f"ALTER ROLE {w.names['service']} NOLOGIN PASSWORD NULL")


def http(w, authority="real"):
    """A client of the real account router in V1 mode."""
    api = FastAPI()
    api.include_router(build_account_router(lambda: w.runtime_factory, SETTINGS, v1_security=w.security,
                                            v1_authority=w.authority if authority == "real" else authority))
    return TestClient(api, base_url="https://testserver")


def customer(w):
    """A verified, active account with a real Argon2id password; returns (user_id, email)."""
    user = uuid.uuid4()
    email = f"d3-{user.hex[:16]}@example.test"
    admin_sql(w, "INSERT INTO kalillac.users(id,email,password_hash,is_active,email_verified_at)"
                 " VALUES (:i,:e,:h,true,clock_timestamp())", i=user, e=email, h=STORED)
    admin_sql(w, "INSERT INTO kalillac.account_entitlements(user_id,tier,source) VALUES (:i,'free','registration')", i=user)
    return user, email


def login(client, email, password=PASSWORD):
    return client.post("/api/account/login", json={"email": email, "password": password})


def me(w, token):
    """GET /me with exactly this cookie, through a fresh client."""
    with http(w) as client:
        return client.get("/api/account/me", headers={"Cookie": f"{COOKIE}={token}"}).status_code


def rows(w, user):
    with w.admin_engine.connect() as c:
        return [tuple(r) for r in c.execute(text("SELECT id, token_hash, revoked_at FROM kalillac.account_sessions"
                                                 " WHERE user_id=:u ORDER BY created_at"), {"u": user})]


def signed_in(w, email):
    """Log in through HTTP; returns the session cookie value."""
    with http(w) as client:
        response = login(client, email)
        assert response.status_code == 200, response.text
        return response.cookies.get(COOKIE)


def operator_request(w, user):
    with w.admin_engine.begin() as c:
        c.exec_driver_sql("SET LOCAL ROLE " + w.names["operator"])
        return tuple(c.execute(text("SELECT * FROM kalillac.v1_request_deletion(:o,:u)"),
                               {"o": uuid.uuid4(), "u": user}).one())


def customer_request(w, user, token):
    digest = hashlib.sha256(token.encode()).hexdigest()
    (session_id,) = [r[0] for r in rows(w, user) if r[1] == digest]
    done = w.authority.request_own_deletion(operation_id=uuid.uuid4(), session_id=session_id, token_hash=digest,
                                            verified_password_hash=STORED)
    return done.outcome


def request_deletion(w, requester, user, token):
    if requester == "operator":
        assert operator_request(w, user) == ("requested", False)
    else:
        assert customer_request(w, user, token) == "requested"


def refused_without_cookie(response):
    assert (response.status_code, response.json()) == (401, {"error": "invalid_credentials"})
    assert "set-cookie" not in response.headers


# --- sign-in before a request -------------------------------------------------------------------------

def test_a_verified_account_signs_in_through_the_route_and_the_session_is_usable(world):
    w = world
    user, email = customer(w)
    with http(w) as client:
        response = login(client, email)
        assert response.status_code == 200 and response.json()["user"] == {
            "id": str(user), "email": email, "created_at": response.json()["user"]["created_at"]}
        token = response.cookies.get(COOKIE)
        assert token and client.get("/api/account/me").json()["user"]["email"] == email
    ((session_id, digest, revoked),) = rows(w, user)
    assert digest == hashlib.sha256(token.encode()).hexdigest() and revoked is None
    with w.admin_engine.connect() as c:                       # Issued by the authority, not written as R.
        assert c.scalar(text("SELECT password_hash FROM kalillac.users WHERE id=:u"), {"u": user}) == STORED
    with http(w) as client:                                   # Wrong password: refused, nothing written.
        refused_without_cookie(login(client, email, "not the password"))
    assert len(rows(w, user)) == 1


# --- after a deletion request -------------------------------------------------------------------------

@pytest.mark.parametrize("requester", ["operator", "customer"])
def test_after_a_deletion_request_no_login_creates_a_session_and_existing_ones_are_unusable(world, requester):
    w = world
    user, email = customer(w)
    first, second = signed_in(w, email), signed_in(w, email)
    stranger, stranger_email = customer(w)
    theirs = signed_in(w, stranger_email)
    assert me(w, first) == me(w, second) == 200
    request_deletion(w, requester, user, first)
    before = rows(w, user)
    assert len(before) == 2 and all(r[2] is not None for r in before)
    assert me(w, first) == me(w, second) == 401                        # Existing sessions are unusable.
    for _ in range(2):                                                  # Correct credentials, repeatedly.
        with http(w) as client:
            refused_without_cookie(login(client, email))
            assert COOKIE not in client.cookies
            assert client.get("/api/account/me").status_code == 401
    assert rows(w, user) == before                                      # No new session row at all.
    assert me(w, theirs) == 200                                         # Another account is untouched.


# --- a login and a request overlapping ----------------------------------------------------------------

def _waiting(w, function):
    """Block until a backend is waiting for a lock inside `function`."""
    deadline = time.monotonic() + 20
    with w.admin_engine.connect() as c:
        while time.monotonic() < deadline:
            if c.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'"
                             " AND query LIKE :q AND pid <> pg_backend_pid()"), {"q": f"%{function}%"}):
                return
            c.rollback()
            time.sleep(0.02)
    pytest.fail(f"{function} never waited for the account lock")


@pytest.mark.parametrize("requester", ["operator", "customer"])
@pytest.mark.parametrize("first", ["login", "request"])
def test_a_login_overlapping_a_request_leaves_no_usable_session(world, requester, first):
    """The account row lock is held while both the login's issuance and the deletion request queue on it,
    in a chosen order; then it is released. Whichever goes first, no usable session survives."""
    w = world
    user, email = customer(w)
    proof = signed_in(w, email) if requester == "customer" else None
    holder = w.admin_engine.connect()
    results = {}
    with http(w) as client:
        def sign_in():
            results["login"] = login(client, email)

        def request():
            results["request"] = (operator_request(w, user)[0] if requester == "operator"
                                  else customer_request(w, user, proof))
        threads = {"login": threading.Thread(target=sign_in), "request": threading.Thread(target=request)}
        functions = {"login": "v1_issue_session",
                     "request": "v1_request_deletion" if requester == "operator" else "v1_request_own_deletion"}
        try:
            holder.begin()
            holder.execute(text("SELECT 1 FROM kalillac.users WHERE id=:u FOR UPDATE"), {"u": user})
            order = [first, "request" if first == "login" else "login"]
            for name in order:
                threads[name].start()
                _waiting(w, functions[name])
            holder.rollback()
        finally:
            holder.close()
        for thread in threads.values():
            thread.join(60)
    assert results["request"] == "requested"
    response = results["login"]
    if first == "login":
        # Issued first, then revoked by the request in the same instant it marked the account.
        assert response.status_code == 200
        assert me(w, response.cookies.get(COOKIE)) == 401
    else:
        refused_without_cookie(response)
    assert all(r[2] is not None for r in rows(w, user))                 # Nothing live after both finish.
    with http(w) as client:
        refused_without_cookie(login(client, email))


def test_unforced_concurrent_logins_and_a_request_leave_no_usable_session(world):
    w = world
    for _ in range(3):
        user, email = customer(w)
        results, start = [], threading.Barrier(4)

        def sign_in():
            with http(w) as client:
                start.wait(10)
                response = login(client, email)
                results.append(response.cookies.get(COOKIE) if response.status_code == 200 else None)

        def request():
            start.wait(10)
            assert operator_request(w, user) == ("requested", False)
        threads = [threading.Thread(target=sign_in) for _ in range(3)] + [threading.Thread(target=request)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        assert len(results) == 3
        assert all(me(w, token) == 401 for token in results if token)
        assert all(r[2] is not None for r in rows(w, user))


# --- an unavailable authority ---------------------------------------------------------------------------

def test_without_an_authority_v1_login_creates_no_session_on_the_legacy_path(world):
    w = world
    user, email = customer(w)
    with http(w, authority=None) as client:
        response = login(client, email)
        assert (response.status_code, response.json()) == (503, {"error": "account_unavailable"})
        assert "set-cookie" not in response.headers
    assert rows(w, user) == []


def test_an_authority_that_cannot_issue_creates_no_session_on_the_legacy_path(world):
    w = world
    user, email = customer(w)
    issue = "kalillac.v1_issue_session(uuid,text,uuid,text,integer)"
    admin_sql(w, f"REVOKE EXECUTE ON FUNCTION {issue} FROM {w.names['service']}")
    try:
        with http(w) as client:
            response = login(client, email)
            assert (response.status_code, response.json()) == (503, {"error": "account_unavailable"})
            assert "set-cookie" not in response.headers
    finally:
        admin_sql(w, f"GRANT EXECUTE ON FUNCTION {issue} TO {w.names['service']}")
    assert rows(w, user) == []
    assert signed_in(w, email)                                          # Restored, it signs in again.


def test_an_authority_blocked_past_its_lock_timeout_creates_no_session(world):
    w = world
    user, email = customer(w)
    holder = w.admin_engine.connect()
    try:
        holder.begin()
        holder.execute(text("SELECT 1 FROM kalillac.users WHERE id=:u FOR UPDATE"), {"u": user})
        with http(w) as client:
            response = login(client, email)                             # The authority's 3 s lock timeout.
            assert (response.status_code, response.json()) == (503, {"error": "account_unavailable"})
            assert "set-cookie" not in response.headers
        holder.rollback()
    finally:
        holder.close()
    assert rows(w, user) == []
