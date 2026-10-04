"""Entitlement foundation: one tier row per account, read-only over HTTP.

Product rules pinned here: anonymous use needs no account or entitlement,
Private Session never depends on tier, an account starts free, and no HTTP
request can promote an account. Persistent memory stays off for every tier.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os

import pytest

os.environ.setdefault("GROQ_API_KEY", "test-not-real")
os.environ.pop("KALILLAC_ACCOUNTS_ENABLED", None)

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

import app_fastapi_candidate as app
import kalillac_accounts.router as account_router
from kalillac_accounts.entitlements import FREE, PAID, effective_entitlement
from kalillac_accounts.router import AccountSettings, build_account_router
from kalillac_db.models import AccountEntitlement, AccountSession, Base, User
from kalillac_db.repositories.entitlements import (
    InvalidEntitlement,
    get_entitlement,
    set_entitlement_tier,
)

from account_test_db import make_session_factory, make_sqlite_engine


PASSWORD = "correct horse battery staple"
EMAIL = "person@example.com"
SETTINGS = AccountSettings(cookie_secure=True, session_days=14)
COOKIE = SETTINGS.cookie_name

FREE_JSON = {
    "entitlements": {
        "tier": "free",
        "saved_mode_access": False,
        "higher_usage_access": False,
        "persistent_memory_access": False,
    }
}

PAID_JSON = {
    "entitlements": {
        "tier": "paid",
        "saved_mode_access": True,
        "higher_usage_access": True,
        "persistent_memory_access": False,
    }
}


@pytest.fixture
def db():
    engine = make_sqlite_engine()
    Base.metadata.create_all(engine)

    yield make_session_factory(engine)

    engine.dispose()


def _account_app(db):
    account_app = FastAPI()
    account_app.include_router(
        build_account_router(session_factory=lambda: db, settings=SETTINGS)
    )
    return account_app


@pytest.fixture
def client(db):
    with TestClient(_account_app(db), base_url="https://testserver") as c:
        yield c


def _register(client, email=EMAIL, **extra):
    return client.post(
        "/api/account/register",
        json={"email": email, "password": PASSWORD, **extra},
    )


def _login(client, email=EMAIL):
    return client.post(
        "/api/account/login",
        json={"email": email, "password": PASSWORD},
    )


def _count(db, model):
    with db() as session:
        return session.scalar(select(func.count()).select_from(model))


def _user_id(db, email=EMAIL):
    with db() as session:
        return session.scalar(select(User.id).where(User.email == email))


def _set_tier(db, tier, **kwargs):
    with db() as session, session.begin():
        set_entitlement_tier(
            session,
            _user_id(db),
            tier,
            source=kwargs.pop("source", "test_internal"),
            **kwargs,
        )


# --- registration --------------------------------------------------------------


def test_registration_creates_exactly_one_free_entitlement(client, db):
    assert _register(client).status_code == 201

    with db() as session:
        rows = session.scalars(select(AccountEntitlement)).all()

    assert len(rows) == 1
    assert rows[0].user_id == _user_id(db)
    assert rows[0].tier == "free"
    assert rows[0].source == "registration"
    assert rows[0].expires_at is None


def test_duplicate_registration_creates_no_extra_entitlement(client, db):
    assert _register(client).status_code == 201
    assert _register(client).status_code == 409
    assert _register(client, email="PERSON@example.com").status_code == 409

    assert _count(db, User) == 1
    assert _count(db, AccountEntitlement) == 1


def test_user_and_entitlement_creation_is_atomic(db, monkeypatch):
    def failing_entitlement(session, user_id):
        raise RuntimeError("entitlement insert failed")

    monkeypatch.setattr(
        account_router,
        "create_default_entitlement",
        failing_entitlement,
    )

    with TestClient(
        _account_app(db),
        base_url="https://testserver",
        raise_server_exceptions=False,
    ) as client:
        response = _register(client)

    assert response.status_code == 500
    # The user insert was rolled back with the failed entitlement.
    assert _count(db, User) == 0
    assert _count(db, AccountEntitlement) == 0


# --- GET /api/account/entitlements ---------------------------------------------


def test_signed_out_entitlements_request_is_rejected(client):
    response = client.get("/api/account/entitlements")

    assert response.status_code == 401
    assert response.json() == {"error": "not_authenticated"}


def test_forged_session_is_rejected(client):
    client.cookies.clear()
    response = client.get(
        "/api/account/entitlements",
        headers={"Cookie": f"{COOKIE}=forged-token"},
    )

    assert response.status_code == 401


def test_signed_in_free_entitlements(client):
    _register(client)
    _login(client)

    response = client.get("/api/account/entitlements")

    assert response.status_code == 200
    assert response.json() == FREE_JSON
    assert response.headers["cache-control"] == "no-store"


def test_internally_set_paid_entitlements(client, db):
    _register(client)
    _login(client)
    _set_tier(db, "paid")

    response = client.get("/api/account/entitlements")

    assert response.status_code == 200
    assert response.json() == PAID_JSON


def test_expired_paid_entitlement_is_free(client, db):
    _register(client)
    _login(client)
    _set_tier(
        db,
        "paid",
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )

    assert client.get("/api/account/entitlements").json() == FREE_JSON


def test_unexpired_paid_entitlement_is_paid(client, db):
    _register(client)
    _login(client)
    _set_tier(
        db,
        "paid",
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )

    assert client.get("/api/account/entitlements").json() == PAID_JSON


def test_every_registered_account_has_exactly_one_entitlement_row(client, db):
    # The normal state: registration (and migration 0002's backfill) give
    # each account exactly one row.
    for index in range(3):
        assert _register(client, email=f"user{index}@example.com").status_code == 201

    with db() as session:
        per_user = session.execute(
            select(User.id, func.count(AccountEntitlement.user_id))
            .outerjoin(AccountEntitlement, AccountEntitlement.user_id == User.id)
            .group_by(User.id)
        ).all()

    assert len(per_user) == 3
    assert all(count == 1 for _, count in per_user)


def test_missing_entitlement_row_fails_closed_to_free(client, db):
    # Not a normal database state (see the test above and the migration
    # backfill); this pins the defensive fallback if a row is ever absent.
    _register(client)
    _login(client)

    with db() as session, session.begin():
        session.execute(delete(AccountEntitlement))

    assert client.get("/api/account/entitlements").json() == FREE_JSON


def test_persistent_memory_access_is_off_for_every_tier():
    now = datetime.now(timezone.utc)

    assert FREE.persistent_memory_access is False
    assert PAID.persistent_memory_access is False
    assert effective_entitlement(None, now).persistent_memory_access is False


def test_me_response_is_unchanged(client, db):
    _register(client)
    _login(client)
    _set_tier(db, "paid")

    user = client.get("/api/account/me").json()["user"]

    assert set(user) == {"id", "email", "created_at"}


# --- no self-promotion ---------------------------------------------------------


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_entitlements_endpoint_is_read_only(client, db, method):
    _register(client)
    _login(client)

    response = client.request(
        method.upper(),
        "/api/account/entitlements",
        json={"tier": "paid"},
    )

    assert response.status_code == 405
    assert client.get("/api/account/entitlements").json() == FREE_JSON


def test_extra_fields_cannot_promote_through_registration_or_login(client, db):
    _register(client, tier="paid", source="stripe", saved_mode_access=True)
    client.post(
        "/api/account/login",
        json={"email": EMAIL, "password": PASSWORD, "tier": "paid"},
    )

    assert client.get("/api/account/entitlements").json() == FREE_JSON

    with db() as session:
        assert session.scalar(select(AccountEntitlement)).tier == "free"


# --- repository and database boundary ------------------------------------------


def test_deleting_user_cascades_entitlement(client, db):
    _register(client)
    assert _count(db, AccountEntitlement) == 1

    with db() as session, session.begin():
        session.execute(delete(User))

    assert _count(db, AccountEntitlement) == 0


def test_service_rejects_invalid_tier(client, db):
    _register(client)

    for tier in ("premium", "PAID", "", "free "):
        with pytest.raises(InvalidEntitlement):
            _set_tier(db, tier)

    with db() as session:
        assert session.scalar(select(AccountEntitlement)).tier == "free"


def test_service_rejects_malformed_source(client, db):
    _register(client)

    with pytest.raises(InvalidEntitlement):
        _set_tier(db, "paid", source="stripe; drop table")


def test_database_rejects_invalid_tier(client, db):
    _register(client)

    with pytest.raises(IntegrityError):
        with db() as session, session.begin():
            session.execute(update(AccountEntitlement).values(tier="premium"))

    with db() as session:
        assert session.scalar(select(AccountEntitlement)).tier == "free"


def test_set_tier_creates_missing_row(client, db):
    _register(client)

    with db() as session, session.begin():
        session.execute(delete(AccountEntitlement))

    _set_tier(db, "paid", source="billing")

    with db() as session:
        row = get_entitlement(session, _user_id(db))

    assert (row.tier, row.source) == ("paid", "billing")
    assert _count(db, AccountEntitlement) == 1


# --- login/logout unchanged ------------------------------------------------------


def test_login_and_logout_unchanged_and_leave_entitlement_alone(client, db):
    _register(client)

    login = _login(client)
    assert login.status_code == 200
    assert set(login.json()) == {"user"}
    assert COOKIE in login.cookies

    assert client.post("/api/account/logout").json() == {"ok": True}
    assert client.get("/api/account/me").status_code == 401
    assert client.get("/api/account/entitlements").status_code == 401

    with db() as session:
        row = session.scalar(select(AccountEntitlement))

    assert (row.tier, row.source) == ("free", "registration")
    assert _count(db, AccountEntitlement) == 1


# --- /api/chat coexistence -------------------------------------------------------


@pytest.fixture
def chat_client(monkeypatch, db):
    """The real app: /api/chat beside the mounted account routes."""

    calls = []

    def fake_chat(message, history, request=None, session_id=None):
        calls.append(session_id)
        return f"echo: {message}"

    monkeypatch.setattr(app, "chat", fake_chat)

    original_routes = list(app.api.router.routes)
    app.api.include_router(
        build_account_router(session_factory=lambda: db, settings=SETTINGS)
    )

    with TestClient(app.api, base_url="https://testserver") as c:
        yield c, calls

    app.api.router.routes[:] = original_routes


def _db_snapshot(db):
    return (
        _count(db, User),
        _count(db, AccountSession),
        _count(db, AccountEntitlement),
    )


def test_anonymous_chat_needs_no_account_or_entitlement(chat_client, db):
    client, calls = chat_client

    response = client.post("/api/chat", json={"message": "hi", "history": []})

    assert response.status_code == 200
    assert set(response.json()) == {"reply", "session_id"}
    assert _db_snapshot(db) == (0, 0, 0)


@pytest.mark.parametrize("tier", ["free", "paid"])
def test_signed_in_chat_persists_nothing(chat_client, db, tier):
    client, calls = chat_client
    _register(client)
    _login(client)
    _set_tier(db, tier)
    before = _db_snapshot(db)

    for _ in range(2):
        response = client.post(
            "/api/chat",
            json={"message": "remember this conversation", "history": []},
        )
        assert response.status_code == 200
        assert set(response.json()) == {"reply", "session_id"}

    # No new rows of any kind, and each request was its own temporary
    # Private Session regardless of tier.
    assert _db_snapshot(db) == before
    assert calls[0] != calls[1]
