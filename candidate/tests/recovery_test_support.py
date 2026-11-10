"""Local-only recovery fixtures; gateways capture delivery in test memory."""
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from kalillac_accounts.passwords import hash_password
from kalillac_accounts.recovery_config import load_recovery_config
from kalillac_accounts.recovery_router import build_recovery_router
from kalillac_accounts.recovery_rate_limit import RecoveryRateLimiter
from kalillac_accounts.router import AccountSettings, build_account_router
from kalillac_db.models import AccountRecoveryToken, AccountSession, User
from kalillac_db.repositories.accounts import create_account_session
from kalillac_accounts.tokens import hash_session_token

NOW = datetime(2030, 6, 15, 12, tzinfo=timezone.utc)
PASSWORD = "original recovery password"
NEW_PASSWORD = "replacement recovery password"
ENV = {
    "KALILLAC_ACCOUNT_RECOVERY_ENABLED": "true",
    "KALILLAC_ACCOUNTS_ENABLED": "true",
    "KALILLAC_DB_ENABLED": "true",
    "KALILLAC_ACCOUNT_RECOVERY_URL": "https://kalillac.ai/reset",
    "KALILLAC_EMAIL_FROM": "help@kalillac.ai",
}
SETTINGS = AccountSettings(cookie_secure=True, trust_cloudflare_client_ip=True)
COOKIE = SETTINGS.cookie_name
SENSITIVE = "provider-private-detail https://db.invalid/private password-secret"


@dataclass
class FakeEmail:
    reset: list = field(default_factory=list, repr=False)
    changed: list = field(default_factory=list, repr=False)
    fail_reset: bool = False
    fail_changed: bool = False

    def send_password_reset(self, to, link):
        self.reset.append((to, link))
        if self.fail_reset:
            raise RuntimeError(SENSITIVE)

    def send_password_changed(self, to):
        self.changed.append(to)
        if self.fail_changed:
            raise RuntimeError(SENSITIVE)


def application(factory, gateway, *, limiter=None, clock=lambda: NOW, environ=ENV):
    app = FastAPI()
    app.include_router(build_account_router(session_factory=lambda: factory, settings=SETTINGS))
    app.include_router(build_recovery_router(
        session_factory=lambda: factory, gateway=gateway, settings=SETTINGS,
        limiter=limiter, clock=clock, environ=environ,
    ))
    return app


def client_for(factory, gateway, **kwargs):
    return TestClient(application(factory, gateway, **kwargs), base_url="https://testserver")


def add_user(factory, email="person@example.com", *, active=True):
    with factory() as session, session.begin():
        user = User(id=uuid.uuid4(), email=email, password_hash=hash_password(PASSWORD), is_active=active)
        session.add(user)
        session.flush()
        return user.id


def add_sessions(factory, user_id, count=2):
    tokens = ["session-for-test-" + uuid.uuid4().hex for _ in range(count)]
    with factory() as session, session.begin():
        for token in tokens:
            create_account_session(
                session, user_id, hash_session_token(token), NOW + timedelta(days=1),
            )
    return tokens


def request_link(client, gateway, email="person@example.com", peer="192.0.2.10"):
    response = client.post("/api/account/recovery/request", json={"email": email},
                           headers={"cf-connecting-ip": peer})
    assert response.status_code == 202 and response.json() == {"ok": True}
    return gateway.reset[-1][1].split("#token=", 1)[1]


def confirm(client, token, password=NEW_PASSWORD, peer="192.0.2.10"):
    return client.post("/api/account/recovery/confirm", json={"token": token, "password": password},
                       headers={"cf-connecting-ip": peer})


def rows(factory, model):
    with factory() as session:
        return [dict(row._mapping) for row in session.execute(select(model.__table__))]
