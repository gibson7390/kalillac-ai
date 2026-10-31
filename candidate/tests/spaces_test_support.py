"""Shared fixtures for the Spaces suites: an isolated file-backed SQLite test
database (no shared, staging or production database is ever touched), four
accounts covering every gate state, a controlled clock and app builders."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from account_test_db import make_session_factory, make_sqlite_file_engine
from kalillac_accounts.router import AccountSettings
from kalillac_accounts.tokens import hash_session_token
from kalillac_db.models import AccountSession, Base, User
from kalillac_db.repositories.entitlements import create_default_entitlement
from kalillac_routing.request_budget import RequestBudget
from kalillac_spaces.config import SpacesConfig
from kalillac_spaces.router import build_spaces_router

NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
ORIGIN = "https://kalillac.test"
SETTINGS = AccountSettings(cookie_secure=True)
COOKIE = SETTINGS.cookie_name

# a, b: allowlisted and verified. c: verified, not allowlisted.
# d: allowlisted, email not verified.
ACCOUNTS = {
    "a": {"allow": True, "verified": True},
    "b": {"allow": True, "verified": True},
    "c": {"allow": False, "verified": True},
    "d": {"allow": True, "verified": False},
}


def token_for(key: str) -> str:
    return f"synthetic-spaces-session-{key}"


@dataclass
class World:
    engine: Any
    factory: Any
    users: dict[str, uuid.UUID]
    now: list = field(default_factory=lambda: [NOW])
    ticks: list = field(default_factory=lambda: [1000.0])

    def clock(self) -> datetime:
        return self.now[0]

    def monotonic(self) -> float:
        return self.ticks[0]

    def advance(self, seconds: float) -> None:
        # Wall time and the send budget's monotonic clock move together.
        self.now[0] = self.now[0] + timedelta(seconds=seconds)
        self.ticks[0] += seconds

    def budget(self, seconds: float = 30) -> RequestBudget:
        """A send budget on this world's controlled clock."""
        return RequestBudget(duration_seconds=seconds, max_model_attempts=6, max_search_attempts=6,
                             clock=self.monotonic)

    def count(self, model, **filters) -> int:
        with self.factory() as session:
            statement = select(func.count()).select_from(model)
            for name, value in filters.items():
                statement = statement.where(getattr(model, name) == value)
            return session.scalar(statement)

    def config(self) -> SpacesConfig:
        return SpacesConfig(
            beta_account_ids=frozenset(self.users[k] for k, v in ACCOUNTS.items() if v["allow"]),
            site_origin=ORIGIN,
        )


def make_world(tmp_path) -> World:
    engine = make_sqlite_file_engine(str(tmp_path))
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    users = {}
    with factory() as session, session.begin():
        for key, spec in ACCOUNTS.items():
            user = User(
                id=uuid.uuid4(), email=f"spaces-{key}@example.test", password_hash="synthetic-hash",
                email_verified_at=NOW - timedelta(days=1) if spec["verified"] else None,
            )
            session.add(user)
            session.flush()
            create_default_entitlement(session, user.id)
            session.add(AccountSession(
                user_id=user.id, token_hash=hash_session_token(token_for(key)),
                expires_at=NOW + timedelta(days=14),
            ))
            users[key] = user.id
    return World(engine=engine, factory=factory, users=users)


async def echo_generate(request, *, user_id, lock_key, message, history, workspace, budget, finalize):
    """A provider-free generator for API tests: saves a deterministic reply."""
    return await finalize("Echo: " + message)


def build_app(world: World, generate=echo_generate, lease_seconds: int = 60, config: SpacesConfig | None = None,
              new_budget=None, cleanup_seconds: float | None = None) -> FastAPI:
    """new_budget defaults to a real-clock send budget (needed when generate
    is the real chat lifecycle); pass world.budget for a controlled clock."""
    application = FastAPI()
    extra = {} if cleanup_seconds is None else {"cleanup_seconds": cleanup_seconds}
    application.include_router(build_spaces_router(
        config=config or world.config(),
        cookie_name=COOKIE,
        generate=generate,
        lease_seconds=lease_seconds,
        session_factory=lambda: world.factory,
        clock=world.clock,
        new_budget=new_budget,
        **extra,
    ))
    return application


def client(application) -> TestClient:
    return TestClient(application, base_url="https://testserver")


def headers(key: str | None, *, origin: str | None = ORIGIN, **extra) -> dict[str, str]:
    out = dict(extra)
    if key is not None:
        out["Cookie"] = f"{COOKIE}={token_for(key)}"
    if origin is not None:
        out["Origin"] = origin
    return out


def new_request_id() -> str:
    return str(uuid.uuid4())
