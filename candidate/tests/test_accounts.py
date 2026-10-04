"""Account foundation: registration, sign-in, current user, logout.

Accounts are identity only. These tests also pin the product rules:
anonymous /api/chat is unchanged, and creating an account or signing in
creates no saved chats, conversation history, or memory.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os

import pytest

os.environ.setdefault("GROQ_API_KEY", "test-not-real")
os.environ.pop("KALILLAC_ACCOUNTS_ENABLED", None)

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, inspect, select, update

import app_fastapi_candidate as app
from kalillac_accounts.router import AccountSettings, build_account_router
from kalillac_accounts.tokens import hash_session_token
from kalillac_db.models import AccountSession, Base, User

from account_test_db import make_session_factory, make_sqlite_engine


PASSWORD = "correct horse battery staple"
SETTINGS = AccountSettings(cookie_secure=True, session_days=14)
COOKIE = SETTINGS.cookie_name


@pytest.fixture
def db():
    engine = make_sqlite_engine()
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)

    yield factory

    engine.dispose()


@pytest.fixture
def client(db):
    account_app = FastAPI()
    account_app.include_router(
        build_account_router(session_factory=lambda: db, settings=SETTINGS)
    )

    # HTTPS so the Secure __Host- cookie is sent back, as in production.
    with TestClient(account_app, base_url="https://testserver") as c:
        yield c


def _register(client, email="Person@Example.com", password=PASSWORD):
    return client.post(
        "/api/account/register",
        json={"email": email, "password": password},
    )


def _login(client, email="person@example.com", password=PASSWORD):
    return client.post(
        "/api/account/login",
        json={"email": email, "password": password},
    )


def _count(db, model):
    with db() as session:
        return session.scalar(select(func.count()).select_from(model))


# --- registration ------------------------------------------------------------


def test_successful_registration(client, db):
    response = _register(client)

    assert response.status_code == 201
    user = response.json()["user"]
    assert user["email"] == "person@example.com"
    assert set(user) == {"id", "email", "created_at"}
    assert response.headers["cache-control"] == "no-store"

    with db() as session:
        stored = session.scalar(select(User))

    assert stored.is_active is True
    assert stored.password_hash.startswith("$argon2id$")
    assert PASSWORD not in stored.password_hash

    # Registration does not sign in.
    assert COOKIE not in response.cookies
    assert _count(db, AccountSession) == 0


@pytest.mark.parametrize(
    "email",
    ["person@example.com", "PERSON@EXAMPLE.COM", "  Person@Example.com  "],
)
def test_duplicate_email_rejected(client, db, email):
    assert _register(client).status_code == 201

    response = _register(client, email=email)

    assert response.status_code == 409
    assert response.json() == {"error": "email_already_registered"}
    assert _count(db, User) == 1


@pytest.mark.parametrize(
    "body, status, code",
    [
        ({"email": "not-an-email", "password": PASSWORD}, 422, "invalid_email"),
        ({"email": "a@example.com", "password": "short"}, 422, "password_length"),
        ({"email": "a@example.com", "password": "x" * 257}, 422, "password_length"),
        ({"email": "a@example.com", "password": 123456789012}, 422, "invalid_password"),
        ({"email": "a@example.com"}, 422, "invalid_password"),
    ],
)
def test_registration_validation(client, db, body, status, code):
    response = client.post("/api/account/register", json=body)

    assert response.status_code == status
    assert response.json() == {"error": code}
    assert _count(db, User) == 0


def test_registration_requires_json(client, db):
    response = client.post(
        "/api/account/register",
        data={"email": "a@example.com", "password": PASSWORD},
    )

    assert response.status_code == 415
    assert response.json() == {"error": "json_required"}
    assert _count(db, User) == 0


def test_error_responses_never_echo_password(client):
    response = client.post(
        "/api/account/register",
        json={"email": "bad", "password": "secret-value-xyz"},
    )

    assert "secret-value-xyz" not in response.text


# --- login ---------------------------------------------------------------------


def test_successful_login_sets_secure_session_cookie(client, db):
    _register(client)

    response = _login(client)

    assert response.status_code == 200
    assert response.json()["user"]["email"] == "person@example.com"

    set_cookie = response.headers["set-cookie"]
    assert set_cookie.startswith(f"{COOKIE}=")
    for attribute in ("HttpOnly", "Secure", "SameSite=lax", "Path=/"):
        assert attribute.lower() in set_cookie.lower()
    assert "domain=" not in set_cookie.lower()

    token = response.cookies[COOKIE]

    with db() as session:
        account_session = session.scalar(select(AccountSession))

    # Only the digest is stored; the raw token is never persisted.
    assert account_session.token_hash == hash_session_token(token)
    assert token not in account_session.token_hash


def test_login_email_is_case_insensitive(client):
    _register(client)

    assert _login(client, email="PERSON@example.COM").status_code == 200


def test_wrong_password_rejected(client, db):
    _register(client)

    response = _login(client, password="wrong password value")

    assert response.status_code == 401
    assert response.json() == {"error": "invalid_credentials"}
    assert COOKIE not in response.cookies
    assert _count(db, AccountSession) == 0


def test_unknown_email_gets_identical_rejection(client):
    _register(client)

    wrong_password = _login(client, password="wrong password value")
    unknown_email = _login(client, email="nobody@example.com")

    assert unknown_email.status_code == wrong_password.status_code == 401
    assert unknown_email.json() == wrong_password.json()


def test_inactive_user_cannot_login(client, db):
    _register(client)

    with db() as session, session.begin():
        session.execute(update(User).values(is_active=False))

    assert _login(client).status_code == 401


# --- current user ------------------------------------------------------------


def test_authenticated_me(client):
    _register(client)
    _login(client)

    response = client.get("/api/account/me")

    assert response.status_code == 200
    assert response.json()["user"]["email"] == "person@example.com"
    assert "password_hash" not in response.text


def test_me_without_session(client):
    response = client.get("/api/account/me")

    assert response.status_code == 401
    assert response.json() == {"error": "not_authenticated"}


def _me_with_token(client, token):
    """Send exactly this token, bypassing the client's cookie jar."""
    client.cookies.clear()
    return client.get(
        "/api/account/me",
        headers={"Cookie": f"{COOKIE}={token}"},
    )


def test_me_with_forged_token(client):
    _register(client)
    real_token = _login(client).cookies[COOKIE]

    # Control: the header path works with the real token.
    assert _me_with_token(client, real_token).status_code == 200
    assert _me_with_token(client, "forged-token-value").status_code == 401


def test_expired_session_is_rejected(client, db):
    _register(client)
    _login(client)

    with db() as session, session.begin():
        session.execute(
            update(AccountSession).values(
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
            )
        )

    assert client.get("/api/account/me").status_code == 401


# --- logout --------------------------------------------------------------------


def test_logout_revokes_session_server_side(client, db):
    _register(client)
    token = _login(client).cookies[COOKIE]

    # Control: a replayed token sent as a raw header is accepted.
    assert _me_with_token(client, token).status_code == 200

    response = client.post(
        "/api/account/logout",
        headers={"Cookie": f"{COOKIE}={token}"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    # Cookie is cleared in the browser...
    assert f"{COOKIE}=" in response.headers["set-cookie"]
    assert client.get("/api/account/me").status_code == 401

    # ...and a replayed copy of the old token is dead on the server.
    assert _me_with_token(client, token).status_code == 401

    with db() as session:
        assert session.scalar(select(AccountSession)).revoked_at is not None


def test_logout_without_session_is_idempotent(client):
    response = client.post("/api/account/logout")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


# --- product rules: no persistence from accounts -----------------------------


def test_account_schema_holds_identity_only():
    assert set(Base.metadata.tables) == {
        "kalillac.users",
        "kalillac.account_sessions",
        "kalillac.account_entitlements",
    }

    # Identity and sign-in tables carry no plan, billing, or conversation
    # data; the tier lives only in its own entitlement table.
    columns = {
        column.name
        for name in ("kalillac.users", "kalillac.account_sessions")
        for column in Base.metadata.tables[name].columns
    }

    for forbidden in (
        "conversation", "message", "history", "chat", "memory",
        "transcript", "saved", "plan", "price", "entitlement", "stripe",
        "tier",
    ):
        assert not any(forbidden in column for column in columns), forbidden

    # Nothing anywhere stores conversations, memory, or billing ids.
    all_columns = {
        column.name
        for table in Base.metadata.tables.values()
        for column in table.columns
    }

    for forbidden in (
        "conversation", "message", "history", "chat", "memory",
        "transcript", "price", "stripe", "customer", "subscription",
        "usage",
    ):
        assert not any(forbidden in column for column in all_columns), forbidden


def test_account_creation_creates_no_conversation_state(client, db):
    chat_state_before = dict(app.SESSION_STATE)

    _register(client)
    _login(client)
    client.get("/api/account/me")

    # Only identity, one sign-in session, and the entitlement tier exist.
    with db() as session:
        tables = set(inspect(session.connection()).get_table_names("kalillac"))

    assert tables == {"users", "account_sessions", "account_entitlements"}
    assert _count(db, User) == 1
    assert _count(db, AccountSession) == 1

    # No temporary chat session was created or touched.
    assert app.SESSION_STATE == chat_state_before


# --- anonymous /api/chat coexistence -------------------------------------------


def test_account_routes_absent_unless_enabled():
    # Behavioral check: FastAPI may mount included routers without
    # flattening their paths into api.routes.
    with TestClient(app.api, base_url="https://testserver") as c:
        for path in ("/api/account/me", "/api/account/register"):
            response = (
                c.get(path) if path.endswith("/me") else c.post(path, json={})
            )
            assert response.status_code == 404, path


@pytest.fixture
def chat_with_accounts(monkeypatch, db):
    """The real app with account routes mounted alongside /api/chat."""

    calls = []

    def fake_chat(message, history, request=None, session_id=None):
        calls.append(
            {"message": message, "history": history, "session_id": session_id}
        )
        return f"echo: {message}"

    monkeypatch.setattr(app, "chat", fake_chat)

    original_routes = list(app.api.router.routes)
    app.api.include_router(
        build_account_router(session_factory=lambda: db, settings=SETTINGS)
    )

    with TestClient(app.api, base_url="https://testserver") as c:
        yield c, calls

    app.api.router.routes[:] = original_routes


def test_anonymous_chat_unchanged_with_and_without_account(chat_with_accounts):
    client, calls = chat_with_accounts

    anonymous = client.post("/api/chat", json={"message": "hello", "history": []})

    assert anonymous.status_code == 200
    assert set(anonymous.json()) == {"reply", "session_id"}
    assert anonymous.json()["reply"] == "echo: hello"
    assert COOKIE not in anonymous.cookies

    _register(client)
    _login(client)

    signed_in = client.post("/api/chat", json={"message": "hello", "history": []})

    assert signed_in.status_code == 200
    assert set(signed_in.json()) == {"reply", "session_id"}
    assert signed_in.json()["reply"] == anonymous.json()["reply"]

    # Chat received identical inputs and a fresh temporary session either
    # way: the account cookie does not reach or alter the chat pipeline.
    assert calls[0]["message"] == calls[1]["message"] == "hello"
    assert calls[0]["history"] == calls[1]["history"] == []
    assert calls[0]["session_id"] != calls[1]["session_id"]


def test_chat_never_touches_the_account_database(chat_with_accounts, db, monkeypatch):
    client, _ = chat_with_accounts

    _register(client)
    _login(client)
    users_before = _count(db, User)
    sessions_before = _count(db, AccountSession)

    opened = []
    original_call = type(db).__call__
    monkeypatch.setattr(
        type(db),
        "__call__",
        lambda self, *a, **k: opened.append(1) or original_call(self, *a, **k),
    )

    client.post("/api/chat", json={"message": "save this chat", "history": []})

    assert opened == []

    # Control: the probe does see account requests open a DB session.
    client.get("/api/account/me")
    assert opened

    monkeypatch.undo()
    assert _count(db, User) == users_before
    assert _count(db, AccountSession) == sessions_before


# --- startup hook ----------------------------------------------------------------


def _import_app_with(env_overrides, block_module=None):
    import subprocess
    import sys

    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("KALILLAC_")
    }
    env.update(env_overrides)
    env.setdefault("GROQ_API_KEY", "test-not-real")

    # Optionally make one module unimportable, as on a host where it was
    # never installed.
    blocker = (
        "import sys;"
        "sys.modules[%r] = None;" % block_module
        if block_module
        else ""
    )

    # /api/account/me without a cookie answers before any DB access.
    script = blocker + (
        "import app_fastapi_candidate as a;"
        "from fastapi.testclient import TestClient;"
        "r = TestClient(a.api, base_url='https://testserver')"
        ".get('/api/account/me');"
        "print(r.status_code, r.text)"
    )

    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_enabled_accounts_require_database():
    result = _import_app_with({"KALILLAC_ACCOUNTS_ENABLED": "true"})

    assert result.returncode != 0
    assert "KALILLAC_ACCOUNTS_ENABLED requires KALILLAC_DB_ENABLED" in result.stderr


def test_enabled_accounts_mount_routes_without_connecting():
    # The engine is lazy: mounting routes must not open a DB connection,
    # so an unreachable host is fine at import time.
    result = _import_app_with(
        {
            "KALILLAC_ACCOUNTS_ENABLED": "true",
            "KALILLAC_DB_ENABLED": "true",
            "KALILLAC_DATABASE_URL": (
                "postgresql+psycopg://user:pw@unreachable.invalid/kalillac"
            ),
        }
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == (
        '401 {"error":"not_authenticated"}'
    )


def test_enabled_accounts_fail_fast_without_argon2():
    result = _import_app_with(
        {
            "KALILLAC_ACCOUNTS_ENABLED": "true",
            "KALILLAC_DB_ENABLED": "true",
            "KALILLAC_DATABASE_URL": (
                "postgresql+psycopg://user:pw@unreachable.invalid/kalillac"
            ),
        },
        block_module="argon2",
    )

    assert result.returncode != 0
    assert "install requirements-database.txt" in result.stderr
    assert "missing: argon2" in result.stderr


def test_disabled_accounts_start_without_argon2():
    result = _import_app_with({}, block_module="argon2")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1].startswith("404")
