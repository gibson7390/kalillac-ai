"""Local-only email-verification fixtures; the gateway captures delivery in test memory."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from kalillac_accounts.email_verification_router import build_email_verification_router
from kalillac_accounts.passwords import hash_password
from kalillac_accounts.router import AccountSettings, build_account_router
from kalillac_accounts.tokens import hash_session_token
from kalillac_db.models import User
from kalillac_db.repositories.accounts import create_account_session

NOW = datetime(2030, 6, 15, 12, tzinfo=timezone.utc)
PASSWORD = "original verification password"
EMAIL = "person@example.com"
ENV = {
    "KALILLAC_ACCOUNT_EMAIL_VERIFICATION_ENABLED": "true",
    "KALILLAC_ACCOUNTS_ENABLED": "true",
    "KALILLAC_DB_ENABLED": "true",
    "KALILLAC_ACCOUNT_EMAIL_VERIFICATION_URL": "https://kalillac.ai/verify-email",
    "KALILLAC_EMAIL_FROM": "help@kalillac.ai",
}
SETTINGS = AccountSettings(
    cookie_secure=True, trust_cloudflare_client_ip=True, email_verification_enabled=True,
)
COOKIE = SETTINGS.cookie_name
SENSITIVE = "provider-private-detail https://db.invalid/private password-secret"


@dataclass
class FakeVerificationEmail:
    sent: list = field(default_factory=list, repr=False)
    fail: bool = False

    def send_email_verification(self, to, link):
        self.sent.append((to, link))
        if self.fail:
            raise RuntimeError(SENSITIVE)


def application(factory, gateway, *, limiter=None, clock=lambda: NOW, environ=ENV,
                settings=SETTINGS, extra_routers=()):
    app = FastAPI()
    app.include_router(build_account_router(session_factory=lambda: factory, settings=settings))
    app.include_router(build_email_verification_router(
        session_factory=lambda: factory, gateway=gateway, settings=settings,
        limiter=limiter, clock=clock, environ=environ,
    ))
    for router in extra_routers:
        app.include_router(router)
    return app


def client_for(factory, gateway, **kwargs):
    return TestClient(application(factory, gateway, **kwargs), base_url="https://testserver")


def add_user(factory, email=EMAIL, *, active=True, verified_at=None):
    with factory() as session, session.begin():
        user = User(id=uuid.uuid4(), email=email, password_hash=hash_password(PASSWORD),
                    is_active=active, email_verified_at=verified_at)
        session.add(user)
        session.flush()
        return user.id


def add_session(factory, user_id, expires=None):
    """A live sign-in session for user_id; returns its raw cookie value."""
    token = "session-for-test-" + uuid.uuid4().hex
    with factory() as session, session.begin():
        create_account_session(
            session, user_id, hash_session_token(token),
            expires or datetime.now(timezone.utc) + timedelta(days=1),
        )
    return token


def request_verification(client, cookie, peer="192.0.2.10"):
    client.cookies.set(COOKIE, cookie)
    return client.post("/api/account/email-verification/request", json={},
                       headers={"cf-connecting-ip": peer})


def request_link(client, gateway, cookie, peer="192.0.2.10"):
    response = request_verification(client, cookie, peer)
    assert response.status_code == 202 and response.json() == {"ok": True}
    return gateway.sent[-1][1].split("#token=", 1)[1]


def confirm(client, token, peer="192.0.2.10"):
    return client.post("/api/account/email-verification/confirm", json={"token": token},
                       headers={"cf-connecting-ip": peer})


def rows(factory, model):
    with factory() as session:
        return [dict(row._mapping) for row in session.execute(select(model.__table__))]


def user_row(factory, user_id):
    return next(row for row in rows(factory, User) if row["id"] == user_id)
