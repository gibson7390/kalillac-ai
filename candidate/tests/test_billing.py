"""Stripe billing foundation, with an injected fake gateway (no network).

Pinned rules: billing is off by default and imports nothing when off;
Checkout/Portal are server-controlled and grant nothing; only a verified,
deduplicated webhook reconciled from Stripe's CURRENT subscription changes
entitlement; nothing about payments or webhook payloads is stored; account,
entitlement, usage, and Private Session behavior are unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
import subprocess
import sys
import threading
import uuid

import pytest

os.environ.setdefault("GROQ_API_KEY", "test-not-real")
for _flag in (
    "KALILLAC_ACCOUNTS_ENABLED",
    "KALILLAC_USAGE_METERING_ENABLED",
    "KALILLAC_BILLING_ENABLED",
):
    os.environ.pop(_flag, None)

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

import app_fastapi_candidate as app
import kalillac_billing.service as billing_service
from kalillac_accounts.router import AccountSettings, build_account_router
from kalillac_billing.config import (
    BillingConfig,
    BillingConfigError,
    load_billing_config,
)
from kalillac_billing.gateway import (
    BillingProviderError,
    CheckoutSessionState,
    InvalidWebhookSignature,
    SubscriptionState,
    WebhookEvent,
)
from kalillac_billing.router import MAX_WEBHOOK_BYTES, build_billing_router
from kalillac_billing.service import PAID_STATUSES, tier_for_subscription
from kalillac_db.models import (
    AccountBilling,
    AccountEntitlement,
    AccountUsageDaily,
    Base,
    StripeWebhookEvent,
    User,
)

from account_test_db import make_session_factory, make_sqlite_engine


PASSWORD = "correct horse battery staple"
EMAIL = "person@example.com"
SETTINGS = AccountSettings(cookie_secure=True, session_days=14)
COOKIE = SETTINGS.cookie_name
PAID_PRICE = "price_kalillacpaid"
PERIOD_END = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)

CONFIG = BillingConfig(
    secret_key="sk_test_secretvalue123",
    webhook_secret="whsec_secretvalue456",
    price_id=PAID_PRICE,
    success_url="https://kalillac.com/billing/success",
    cancel_url="https://kalillac.com/billing/cancel",
    portal_return_url="https://kalillac.com/account",
)

CANDIDATE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --- fake Stripe -------------------------------------------------------------------


@dataclass
class FakeGateway:
    """Fake Stripe. Checkout creation honors idempotency keys like Stripe:
    a repeated key returns the original session, a new key a new one."""

    subscriptions: dict = field(default_factory=dict)
    checkout_calls: list = field(default_factory=list)
    portal_calls: list = field(default_factory=list)
    retrieve_calls: list = field(default_factory=list)
    checkout_retrievals: list = field(default_factory=list)
    sessions_by_key: dict = field(default_factory=dict)
    sessions_by_id: dict = field(default_factory=dict)
    fail_provider: bool = False
    # Concurrency hooks: a barrier all creates must reach together, and a
    # one-shot crash after Stripe has created the session.
    create_barrier: object = None
    crash_after_create: bool = False
    lock: object = field(default_factory=threading.Lock)
    # Stripe's same-key behavior: a request reusing a key that is still in
    # progress gets a 409 idempotency conflict (opt-in here; hold_create
    # keeps the first request in progress), and a key reused with different
    # parameters is rejected. Both surface as BillingProviderError, as the
    # real adapter reports an IdempotencyError.
    emulate_in_progress_conflicts: bool = False
    hold_create: object = None
    in_progress: set = field(default_factory=set)
    params_by_key: dict = field(default_factory=dict)
    # Stripe replays the ORIGINAL saved response for a repeated key, so a
    # replay can report a stale status; retrieval reports the current one.
    original_by_key: dict = field(default_factory=dict)
    conflicts: list = field(default_factory=list)

    def create_checkout_session(self, **kwargs):
        self.checkout_calls.append(kwargs)
        if self.fail_provider:
            raise BillingProviderError()

        if self.create_barrier is not None:
            self.create_barrier.wait(timeout=10)

        key = kwargs["idempotency_key"]
        params = {k: v for k, v in kwargs.items() if k != "idempotency_key"}

        with self.lock:
            if key in self.params_by_key and self.params_by_key[key] != params:
                self.conflicts.append(("parameter_mismatch", key))
                raise BillingProviderError()

            if self.emulate_in_progress_conflicts and key in self.in_progress:
                self.conflicts.append(("in_progress", key))
                raise BillingProviderError()

            self.params_by_key[key] = params
            self.in_progress.add(key)

        try:
            if self.hold_create is not None:
                assert self.hold_create.wait(timeout=20)

            with self.lock:
                session_id = self._session_for_key(key)
        finally:
            with self.lock:
                self.in_progress.discard(key)

        if self.crash_after_create:
            # Stripe created the session; Kalillac never received/stored it.
            self.crash_after_create = False
            raise RuntimeError("process crashed after Stripe created session")

        return self.original_by_key[key]

    def _session_for_key(self, key):
        if key not in self.sessions_by_key:
            session_id = f"cs_test_{len(self.sessions_by_key) + 1}"
            state = CheckoutSessionState(
                session_id=session_id,
                status="open",
                url=f"https://checkout.stripe.com/c/pay/{session_id}",
                expires_at=datetime(2026, 10, 5, tzinfo=timezone.utc),
                subscription_id=None,
            )
            self.sessions_by_key[key] = session_id
            self.sessions_by_id[session_id] = state
            self.original_by_key[key] = state

        return self.sessions_by_key[key]

    def retrieve_checkout_session(self, session_id):
        self.checkout_retrievals.append(session_id)
        if self.fail_provider:
            raise BillingProviderError()
        return self.sessions_by_id[session_id]

    def set_checkout_status(self, session_id, status, subscription_id=None):
        old = self.sessions_by_id[session_id]
        self.sessions_by_id[session_id] = CheckoutSessionState(
            session_id=old.session_id,
            status=status,
            url=old.url if status == "open" else None,
            expires_at=old.expires_at,
            subscription_id=subscription_id,
        )

    def create_portal_session(self, **kwargs):
        self.portal_calls.append(kwargs)
        if self.fail_provider:
            raise BillingProviderError()
        return "https://billing.stripe.com/p/session/fake"

    def retrieve_subscription(self, subscription_id):
        self.retrieve_calls.append(subscription_id)
        if self.fail_provider:
            raise BillingProviderError()
        return self.subscriptions[subscription_id]

    def parse_webhook(self, payload, signature):
        # Stand-in for Stripe signature verification.
        if signature != "valid":
            raise InvalidWebhookSignature()
        data = json.loads(payload)
        return WebhookEvent(
            event_id=data["id"],
            event_type=data["type"],
            subscription_id=data.get("subscription"),
            customer_id=data.get("customer"),
            kalillac_user_id=data.get("user"),
        )


def _subscription(user_id, status="active", **overrides):
    values = dict(
        subscription_id="sub_1",
        customer_id="cus_1",
        status=status,
        price_id=PAID_PRICE,
        cancel_at_period_end=False,
        current_period_end=PERIOD_END,
        kalillac_user_id=str(user_id) if user_id else None,
    )
    values.update(overrides)
    return SubscriptionState(**values)


# --- fixtures ----------------------------------------------------------------------


@pytest.fixture
def db():
    engine = make_sqlite_engine()
    Base.metadata.create_all(engine)

    yield make_session_factory(engine)

    engine.dispose()


@pytest.fixture
def gateway():
    return FakeGateway()


@pytest.fixture
def chat_calls(monkeypatch):
    calls = []

    def chat(message, history, request=None, session_id=None):
        calls.append(session_id)
        return f"echo: {message}"

    monkeypatch.setattr(app, "chat", chat)
    monkeypatch.setattr(app, "_usage_meter", None)
    return calls


@pytest.fixture
def client(db, gateway, chat_calls):
    """Real app.api with account and billing routes mounted."""

    original_routes = list(app.api.router.routes)
    app.api.include_router(
        build_account_router(session_factory=lambda: db, settings=SETTINGS)
    )
    app.api.include_router(
        build_billing_router(
            config=CONFIG,
            settings=SETTINGS,
            gateway=gateway,
            session_factory=lambda: db,
        )
    )

    with TestClient(app.api, base_url="https://testserver") as c:
        yield c

    app.api.router.routes[:] = original_routes


def _register_and_login(client, email=EMAIL):
    assert client.post(
        "/api/account/register",
        json={"email": email, "password": PASSWORD},
    ).status_code == 201
    response = client.post(
        "/api/account/login",
        json={"email": email, "password": PASSWORD},
    )
    return response.cookies[COOKIE]


def _as(client, token):
    client.cookies.clear()
    return {"Cookie": f"{COOKIE}={token}"}


def _user_id(db, email=EMAIL):
    with db() as session:
        return session.scalar(select(User.id).where(User.email == email))


def _entitlement(db, email=EMAIL):
    with db() as session:
        row = session.get(AccountEntitlement, _user_id(db, email))
        return (row.tier, row.source, row.expires_at)


def _billing(db, email=EMAIL):
    with db() as session:
        return session.get(AccountBilling, _user_id(db, email))


def _count(db, model):
    with db() as session:
        return session.scalar(select(func.count()).select_from(model))


def _deliver(client, event_id, event_type, *, subscription="sub_1",
             customer="cus_1", user=None, signature="valid", extra=None):
    body = {
        "id": event_id,
        "type": event_type,
        "subscription": subscription,
        "customer": customer,
        "user": str(user) if user else None,
        **(extra or {}),
    }
    client.cookies.clear()
    return client.post(
        "/api/billing/stripe/webhook",
        content=json.dumps(body).encode(),
        headers={"Stripe-Signature": signature, "Content-Type": "application/json"},
    )


# --- feature flag and startup ------------------------------------------------------


def _run_app_with(env_overrides, extra_code=""):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("KALILLAC_", "CREDENTIALS_DIRECTORY"))
    }
    env.update(env_overrides)
    env.setdefault("GROQ_API_KEY", "test-not-real")

    script = (
        extra_code
        + "import sys;"
        "import app_fastapi_candidate as a;"
        "from fastapi.testclient import TestClient;"
        "r = TestClient(a.api, base_url='https://testserver')"
        ".get('/api/account/billing');"
        "print('stripe' in sys.modules, 'kalillac_billing' in sys.modules,"
        " r.status_code)"
    )

    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=CANDIDATE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


ACCOUNT_ENV = {
    "KALILLAC_ACCOUNTS_ENABLED": "true",
    "KALILLAC_DB_ENABLED": "true",
    "KALILLAC_DATABASE_URL": (
        "postgresql+psycopg://user:pw@unreachable.invalid/kalillac"
    ),
}

BILLING_ENV = {
    **ACCOUNT_ENV,
    "KALILLAC_BILLING_ENABLED": "true",
    "KALILLAC_STRIPE_SECRET_KEY": "sk_test_secretvalue123",
    "KALILLAC_STRIPE_WEBHOOK_SECRET": "whsec_secretvalue456",
    "KALILLAC_STRIPE_PRICE_ID": PAID_PRICE,
    "KALILLAC_BILLING_SUCCESS_URL": "https://kalillac.com/billing/success",
    "KALILLAC_BILLING_CANCEL_URL": "https://kalillac.com/billing/cancel",
    "KALILLAC_BILLING_PORTAL_RETURN_URL": "https://kalillac.com/account",
}


def test_billing_off_by_default_imports_nothing():
    result = _run_app_with({})

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "False False 404"


def test_billing_off_with_accounts_still_imports_nothing():
    result = _run_app_with(ACCOUNT_ENV)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "False False 404"


def test_billing_off_works_without_stripe_installed():
    result = _run_app_with(ACCOUNT_ENV, "import sys; sys.modules['stripe'] = None;")

    assert result.returncode == 0, result.stderr


def test_billing_without_accounts_fails_at_startup():
    result = _run_app_with({"KALILLAC_BILLING_ENABLED": "true"})

    assert result.returncode != 0
    assert "KALILLAC_BILLING_ENABLED requires KALILLAC_ACCOUNTS_ENABLED" in (
        result.stderr
    )


def test_billing_without_stripe_package_fails_at_startup():
    result = _run_app_with(
        BILLING_ENV,
        "import sys; sys.modules['stripe'] = None;",
    )

    assert result.returncode != 0
    assert "install requirements-billing.txt" in result.stderr


@pytest.mark.parametrize(
    "missing",
    [
        "KALILLAC_STRIPE_SECRET_KEY",
        "KALILLAC_STRIPE_WEBHOOK_SECRET",
        "KALILLAC_STRIPE_PRICE_ID",
        "KALILLAC_BILLING_SUCCESS_URL",
        "KALILLAC_BILLING_PORTAL_RETURN_URL",
    ],
)
def test_incomplete_billing_config_fails_at_startup(missing):
    env = {k: v for k, v in BILLING_ENV.items() if k != missing}

    result = _run_app_with(env)

    assert result.returncode != 0
    assert "BillingConfigError" in result.stderr
    # Neither secret is ever echoed.
    assert "secretvalue" not in result.stderr


def test_fully_configured_billing_mounts_routes_without_network():
    result = _run_app_with(BILLING_ENV)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "True True 401"


# --- configuration ------------------------------------------------------------------


@pytest.fixture
def config_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("KALILLAC_STRIPE") or key.startswith("KALILLAC_BILLING"):
            monkeypatch.delenv(key)
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)

    for key, value in BILLING_ENV.items():
        if key.startswith(("KALILLAC_STRIPE", "KALILLAC_BILLING_")):
            monkeypatch.setenv(key, value)

    return monkeypatch


def test_config_loads_and_hides_secrets(config_env):
    config = load_billing_config()

    assert config.price_id == PAID_PRICE
    assert "secretvalue" not in repr(config)


def test_systemd_credentials_take_precedence(config_env, tmp_path):
    (tmp_path / "stripe_secret_key").write_text("sk_live_fromcredential\n")
    (tmp_path / "stripe_webhook_secret").write_text("whsec_fromcredential\n")
    config_env.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))

    config = load_billing_config()

    assert config.secret_key == "sk_live_fromcredential"
    assert config.webhook_secret == "whsec_fromcredential"


@pytest.mark.parametrize(
    "key, bad_value",
    [
        ("KALILLAC_STRIPE_SECRET_KEY", "pk_test_publishable"),
        ("KALILLAC_STRIPE_WEBHOOK_SECRET", "not-a-webhook-secret"),
        ("KALILLAC_STRIPE_PRICE_ID", "prod_notaprice"),
        ("KALILLAC_BILLING_SUCCESS_URL", "http://kalillac.com/success"),
        ("KALILLAC_BILLING_CANCEL_URL", "javascript:alert(1)"),
        ("KALILLAC_BILLING_PORTAL_RETURN_URL", "ftp://kalillac.com"),
    ],
)
def test_invalid_config_values_are_rejected_without_echo(
    config_env, key, bad_value
):
    config_env.setenv(key, bad_value)

    with pytest.raises(BillingConfigError) as raised:
        load_billing_config()

    assert bad_value not in str(raised.value)


def test_localhost_http_allowed_for_development(config_env):
    config_env.setenv("KALILLAC_BILLING_SUCCESS_URL", "http://localhost:8002/ok")

    assert load_billing_config().success_url == "http://localhost:8002/ok"


# --- Checkout --------------------------------------------------------------------------


def test_checkout_requires_sign_in(client, gateway):
    response = client.post("/api/account/billing/checkout")

    assert response.status_code == 401
    assert response.json() == {"error": "not_authenticated"}
    assert gateway.checkout_calls == []


def test_checkout_uses_only_server_controlled_values(client, db, gateway):
    token = _register_and_login(client)

    response = client.post(
        "/api/account/billing/checkout",
        json={
            "price": "price_attacker",
            "price_id": "price_attacker",
            "tier": "paid",
            "customer": "cus_victim",
            "customer_id": "cus_victim",
            "subscription_id": "sub_victim",
            "success_url": "https://evil.example",
            "cancel_url": "https://evil.example",
        },
        headers=_as(client, token),
    )

    assert response.status_code == 200
    assert response.json() == {
        "checkout_url": "https://checkout.stripe.com/c/pay/cs_test_1"
    }
    assert response.headers["cache-control"] == "no-store"

    billing = _billing(db)
    assert gateway.checkout_calls == [
        {
            "price_id": PAID_PRICE,
            "customer_id": None,
            "kalillac_user_id": str(_user_id(db)),
            "success_url": CONFIG.success_url,
            "cancel_url": CONFIG.cancel_url,
            "idempotency_key": (
                f"kalillac-checkout-{_user_id(db)}-{billing.checkout_attempt_id}"
            ),
        }
    ]
    # The session is stored on the server-generated attempt.
    assert billing.stripe_checkout_session_id == "cs_test_1"


def test_checkout_grants_nothing(client, db):
    token = _register_and_login(client)

    client.post("/api/account/billing/checkout", headers=_as(client, token))

    assert _entitlement(db) == ("free", "registration", None)

    # Only a pending, server-generated attempt exists: no subscription.
    billing = _billing(db)
    assert billing.checkout_attempt_id is not None
    assert billing.stripe_checkout_session_id == "cs_test_1"
    assert billing.subscription_status is None
    assert billing.stripe_subscription_id is None
    assert billing.stripe_customer_id is None


def test_no_route_turns_a_success_redirect_into_paid(client, db):
    token = _register_and_login(client)
    headers = _as(client, token)
    client.post("/api/account/billing/checkout", headers=headers)

    for path in (
        "/api/account/billing/success",
        "/api/billing/success",
        "/billing/success",
    ):
        client.get(path, headers=headers)
        client.post(path, headers=headers)

    assert _entitlement(db)[0] == "free"


def test_checkout_reuses_known_customer(client, db, gateway):
    token = _register_and_login(client)
    user_id = _user_id(db)
    gateway.subscriptions["sub_1"] = _subscription(user_id, status="canceled")
    _deliver(client, "evt_1", "customer.subscription.deleted", user=user_id)

    client.post("/api/account/billing/checkout", headers=_as(client, token))

    assert gateway.checkout_calls[-1]["customer_id"] == "cus_1"


@pytest.mark.parametrize("status", sorted(PAID_STATUSES))
def test_checkout_refused_while_subscribed(client, db, gateway, status):
    token = _register_and_login(client)
    user_id = _user_id(db)
    gateway.subscriptions["sub_1"] = _subscription(user_id, status=status)
    _deliver(client, "evt_1", "customer.subscription.updated", user=user_id)

    response = client.post(
        "/api/account/billing/checkout",
        headers=_as(client, token),
    )

    assert response.status_code == 409
    assert response.json() == {"error": "already_subscribed"}
    assert gateway.checkout_calls == []


@pytest.mark.parametrize("status", ["incomplete", "unpaid", "paused"])
def test_checkout_blocked_by_unresolved_subscription(client, db, gateway, status):
    token = _register_and_login(client)
    user_id = _user_id(db)
    gateway.subscriptions["sub_1"] = _subscription(user_id, status=status)
    _deliver(client, "evt_1", "customer.subscription.updated", user=user_id)

    response = client.post(
        "/api/account/billing/checkout",
        headers=_as(client, token),
    )

    assert response.status_code == 409
    assert response.json() == {"error": "subscription_unresolved"}
    assert gateway.checkout_calls == []
    # Entitlement for these states is unchanged: free.
    assert _entitlement(db) == ("free", "stripe", None)


@pytest.mark.parametrize("status", ["canceled", "incomplete_expired"])
def test_checkout_allowed_after_terminal_subscription(client, db, gateway, status):
    token = _register_and_login(client)
    user_id = _user_id(db)
    gateway.subscriptions["sub_1"] = _subscription(user_id, status=status)
    _deliver(client, "evt_1", "customer.subscription.updated", user=user_id)

    response = client.post(
        "/api/account/billing/checkout",
        headers=_as(client, token),
    )

    assert response.status_code == 200
    assert len(gateway.checkout_calls) == 1


def test_checkout_provider_failure_is_a_clean_error(client, db, gateway, capsys):
    token = _register_and_login(client)
    gateway.fail_provider = True

    response = client.post(
        "/api/account/billing/checkout",
        headers=_as(client, token),
    )

    assert response.status_code == 502
    assert response.json() == {"error": "billing_unavailable"}
    assert "secretvalue" not in capsys.readouterr().out
    assert _entitlement(db)[0] == "free"


# --- Customer Portal ---------------------------------------------------------------------


def test_portal_requires_sign_in(client):
    assert client.post("/api/account/billing/portal").status_code == 401


def test_portal_without_customer_is_unavailable(client, gateway):
    token = _register_and_login(client)

    response = client.post(
        "/api/account/billing/portal",
        json={"customer_id": "cus_someone_else"},
        headers=_as(client, token),
    )

    assert response.status_code == 409
    assert response.json() == {"error": "billing_profile_unavailable"}
    assert gateway.portal_calls == []


def test_portal_uses_only_the_accounts_own_customer(client, db, gateway):
    token_a = _register_and_login(client, "a@example.com")
    _register_and_login(client, "b@example.com")
    user_a = _user_id(db, "a@example.com")
    user_b = _user_id(db, "b@example.com")

    gateway.subscriptions["sub_a"] = _subscription(
        user_a, subscription_id="sub_a", customer_id="cus_a"
    )
    gateway.subscriptions["sub_b"] = _subscription(
        user_b, subscription_id="sub_b", customer_id="cus_b"
    )
    _deliver(client, "evt_a", "customer.subscription.created",
             subscription="sub_a", customer="cus_a", user=user_a)
    _deliver(client, "evt_b", "customer.subscription.created",
             subscription="sub_b", customer="cus_b", user=user_b)

    response = client.post(
        "/api/account/billing/portal",
        json={"customer_id": "cus_b", "return_url": "https://evil.example"},
        headers=_as(client, token_a),
    )

    assert response.status_code == 200
    assert response.json() == {
        "portal_url": "https://billing.stripe.com/p/session/fake"
    }
    assert gateway.portal_calls == [
        {"customer_id": "cus_a", "return_url": CONFIG.portal_return_url}
    ]


def test_portal_provider_failure_is_a_clean_error(client, db, gateway):
    token = _register_and_login(client)
    gateway.subscriptions["sub_1"] = _subscription(_user_id(db))
    _deliver(client, "evt_1", "customer.subscription.created", user=_user_id(db))
    gateway.fail_provider = True

    response = client.post("/api/account/billing/portal", headers=_as(client, token))

    assert response.status_code == 502
    assert response.json() == {"error": "billing_unavailable"}


# --- billing status ---------------------------------------------------------------------


def test_billing_status_requires_sign_in(client):
    response = client.get("/api/account/billing")

    assert response.status_code == 401
    assert response.json() == {"error": "not_authenticated"}


def test_billing_status_without_billing(client):
    token = _register_and_login(client)

    response = client.get("/api/account/billing", headers=_as(client, token))

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "billing": {
            "subscription_status": None,
            "cancel_at_period_end": False,
            "current_period_end": None,
            "portal_available": False,
        }
    }


def test_billing_status_after_subscription_exposes_no_stripe_ids(
    client, db, gateway
):
    token = _register_and_login(client)
    gateway.subscriptions["sub_1"] = _subscription(
        _user_id(db), cancel_at_period_end=True
    )
    _deliver(client, "evt_1", "customer.subscription.updated", user=_user_id(db))

    response = client.get("/api/account/billing", headers=_as(client, token))

    assert response.json() == {
        "billing": {
            "subscription_status": "active",
            "cancel_at_period_end": True,
            "current_period_end": PERIOD_END.isoformat(),
            "portal_available": True,
        }
    }
    for leaked in ("cus_", "sub_", "price_", "secret"):
        assert leaked not in response.text


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_no_http_write_to_billing_state(client, db, method):
    token = _register_and_login(client)

    response = client.request(
        method,
        "/api/account/billing",
        json={"subscription_status": "active", "tier": "paid"},
        headers=_as(client, token),
    )

    assert response.status_code == 405
    assert _billing(db) is None
    assert _entitlement(db)[0] == "free"


# --- webhook: signature, size, dedup ------------------------------------------------


def test_webhook_rejects_missing_signature(client, db):
    client.cookies.clear()
    response = client.post("/api/billing/stripe/webhook", content=b"{}")

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_signature"}


def test_webhook_rejects_invalid_signature_before_any_work(client, db, gateway):
    _register_and_login(client)

    response = _deliver(
        client, "evt_forged", "customer.subscription.updated",
        user=_user_id(db), signature="forged",
    )

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_signature"}
    assert gateway.retrieve_calls == []
    assert _count(db, StripeWebhookEvent) == 0
    assert _entitlement(db)[0] == "free"


def test_webhook_ignores_account_cookies(client, db, gateway):
    token = _register_and_login(client)
    client.cookies.clear()

    response = client.post(
        "/api/billing/stripe/webhook",
        content=b"{}",
        headers={"Cookie": f"{COOKIE}={token}"},
    )

    assert response.status_code == 400


def test_webhook_body_size_limit(client):
    client.cookies.clear()
    response = client.post(
        "/api/billing/stripe/webhook",
        content=b"x" * (MAX_WEBHOOK_BYTES + 1),
        headers={"Stripe-Signature": "valid"},
    )

    assert response.status_code == 413
    assert response.json() == {"error": "payload_too_large"}


def test_irrelevant_events_are_acknowledged_and_not_stored(client, db, gateway):
    response = _deliver(client, "evt_x", "invoice.paid")

    assert response.status_code == 200
    assert response.json()["result"] == "ignored"
    assert _count(db, StripeWebhookEvent) == 0
    assert gateway.retrieve_calls == []


def test_checkout_completed_reconciles_to_paid(client, db, gateway):
    _register_and_login(client)
    user_id = _user_id(db)
    gateway.subscriptions["sub_1"] = _subscription(user_id)

    response = _deliver(client, "evt_1", "checkout.session.completed", user=user_id)

    assert response.status_code == 200
    assert response.json()["result"] == "processed"
    assert gateway.retrieve_calls == ["sub_1"]
    assert _entitlement(db) == ("paid", "stripe", None)

    billing = _billing(db)
    assert (
        billing.stripe_customer_id,
        billing.stripe_subscription_id,
        billing.stripe_price_id,
        billing.subscription_status,
    ) == ("cus_1", "sub_1", PAID_PRICE, "active")


def test_duplicate_event_is_processed_once(client, db, gateway):
    _register_and_login(client)
    user_id = _user_id(db)
    gateway.subscriptions["sub_1"] = _subscription(user_id)

    first = _deliver(client, "evt_1", "customer.subscription.created", user=user_id)
    second = _deliver(client, "evt_1", "customer.subscription.created", user=user_id)

    assert first.json()["result"] == "processed"
    assert second.status_code == 200
    assert second.json()["result"] == "duplicate"
    assert gateway.retrieve_calls == ["sub_1"]
    assert _count(db, StripeWebhookEvent) == 1


def test_out_of_order_events_follow_current_stripe_state(client, db, gateway):
    _register_and_login(client)
    user_id = _user_id(db)

    # Stripe's current truth: the subscription has already been canceled.
    gateway.subscriptions["sub_1"] = _subscription(user_id, status="canceled")

    # The deletion arrives first, then a stale "created" event (whose own
    # payload would have said active) arrives late.
    _deliver(client, "evt_deleted", "customer.subscription.deleted", user=user_id)
    _deliver(client, "evt_created", "customer.subscription.created", user=user_id)

    assert _entitlement(db)[0] == "free"
    assert _billing(db).subscription_status == "canceled"


@pytest.mark.parametrize(
    "status, tier",
    [
        ("active", "paid"),
        ("trialing", "paid"),
        ("past_due", "paid"),
        ("incomplete", "free"),
        ("incomplete_expired", "free"),
        ("canceled", "free"),
        ("unpaid", "free"),
        ("paused", "free"),
        ("some_future_status", "free"),
    ],
)
def test_status_to_entitlement_mapping(client, db, gateway, status, tier):
    _register_and_login(client)
    user_id = _user_id(db)
    gateway.subscriptions["sub_1"] = _subscription(user_id, status=status)

    _deliver(client, f"evt_{status}", "customer.subscription.updated", user=user_id)

    assert _entitlement(db) == (tier, "stripe", None)
    assert tier_for_subscription(
        _subscription(user_id, status=status), PAID_PRICE
    ) == tier


def test_other_price_never_grants_paid(client, db, gateway):
    _register_and_login(client)
    user_id = _user_id(db)
    gateway.subscriptions["sub_1"] = _subscription(
        user_id, price_id="price_someotherproduct"
    )

    _deliver(client, "evt_1", "customer.subscription.created", user=user_id)

    assert _entitlement(db)[0] == "free"


def test_cancel_at_period_end_stays_paid_until_canceled(client, db, gateway):
    _register_and_login(client)
    user_id = _user_id(db)

    gateway.subscriptions["sub_1"] = _subscription(
        user_id, cancel_at_period_end=True
    )
    _deliver(client, "evt_1", "customer.subscription.updated", user=user_id)

    assert _entitlement(db)[0] == "paid"
    assert _billing(db).cancel_at_period_end is True

    gateway.subscriptions["sub_1"] = _subscription(user_id, status="canceled")
    _deliver(client, "evt_2", "customer.subscription.deleted", user=user_id)

    assert _entitlement(db) == ("free", "stripe", None)


def test_webhook_finds_account_from_existing_billing_link(client, db, gateway):
    _register_and_login(client)
    user_id = _user_id(db)
    gateway.subscriptions["sub_1"] = _subscription(user_id)
    _deliver(client, "evt_1", "customer.subscription.created", user=user_id)

    # A later subscription object without Kalillac metadata still resolves
    # through the stored customer/subscription link.
    gateway.subscriptions["sub_1"] = _subscription(
        None, status="past_due", kalillac_user_id=None
    )
    _deliver(client, "evt_2", "customer.subscription.updated")

    assert _billing(db).subscription_status == "past_due"
    assert _entitlement(db)[0] == "paid"


def test_unmatched_subscription_changes_nothing(client, db, gateway):
    _register_and_login(client)
    gateway.subscriptions["sub_1"] = _subscription(
        None,
        kalillac_user_id=str(uuid.uuid4()),
        customer_id="cus_unknown",
    )

    response = _deliver(client, "evt_1", "customer.subscription.created")

    assert response.json()["result"] == "unmatched"
    assert _count(db, AccountBilling) == 0
    assert _entitlement(db)[0] == "free"


def test_subscription_claiming_another_accounts_customer_is_refused(
    client, db, gateway
):
    _register_and_login(client, "a@example.com")
    _register_and_login(client, "b@example.com")
    user_a = _user_id(db, "a@example.com")
    user_b = _user_id(db, "b@example.com")

    gateway.subscriptions["sub_a"] = _subscription(
        user_a, subscription_id="sub_a", customer_id="cus_a"
    )
    _deliver(client, "evt_a", "customer.subscription.created",
             subscription="sub_a", customer="cus_a", user=user_a)

    # Metadata names B but the customer belongs to A.
    gateway.subscriptions["sub_x"] = _subscription(
        user_b, subscription_id="sub_x", customer_id="cus_a"
    )
    response = _deliver(client, "evt_x", "customer.subscription.created",
                        subscription="sub_x", customer="cus_a", user=user_b)

    assert response.json()["result"] == "unmatched"
    assert _entitlement(db, "b@example.com")[0] == "free"
    assert _billing(db, "b@example.com") is None


def test_stale_other_subscription_does_not_replace_paying_one(client, db, gateway):
    _register_and_login(client)
    user_id = _user_id(db)

    gateway.subscriptions["sub_new"] = _subscription(
        user_id, subscription_id="sub_new"
    )
    _deliver(client, "evt_1", "customer.subscription.created",
             subscription="sub_new", user=user_id)

    gateway.subscriptions["sub_old"] = _subscription(
        user_id, subscription_id="sub_old", status="canceled"
    )
    _deliver(client, "evt_2", "customer.subscription.deleted",
             subscription="sub_old", user=user_id)

    assert _billing(db).stripe_subscription_id == "sub_new"
    assert _entitlement(db)[0] == "paid"


def test_conflicting_nonterminal_subscription_is_surfaced(client, db, gateway, capsys):
    _register_and_login(client)
    user_id = _user_id(db)

    gateway.subscriptions["sub_b"] = _subscription(
        user_id, subscription_id="sub_b", status="incomplete"
    )
    _deliver(client, "evt_b", "customer.subscription.created",
             subscription="sub_b", user=user_id)

    gateway.subscriptions["sub_c"] = _subscription(
        user_id, subscription_id="sub_c", status="active"
    )
    response = _deliver(client, "evt_c", "customer.subscription.created",
                        subscription="sub_c", user=user_id)

    # Non-2xx so Stripe keeps reporting it; nothing chosen or recorded.
    assert response.status_code == 409
    assert response.json() == {"error": "reconciliation_conflict"}
    assert _billing(db).stripe_subscription_id == "sub_b"
    assert _entitlement(db)[0] == "free"

    with db() as session:
        recorded = set(session.scalars(select(StripeWebhookEvent.event_id)))
    assert recorded == {"evt_b"}

    output = capsys.readouterr().out
    assert "WARN: STRIPE_WEBHOOK_CONFLICT" in output
    for leaked in ("sub_b", "sub_c", "cus_1", str(user_id)):
        assert leaked not in output


# --- webhook failure handling ------------------------------------------------------


def test_stripe_retrieval_failure_is_not_marked_processed(client, db, gateway, capsys):
    _register_and_login(client)
    user_id = _user_id(db)
    gateway.subscriptions["sub_1"] = _subscription(user_id)
    gateway.fail_provider = True

    response = _deliver(client, "evt_1", "customer.subscription.created", user=user_id)

    assert response.status_code == 500
    assert response.json() == {"error": "webhook_processing_failed"}
    assert _count(db, StripeWebhookEvent) == 0
    assert _entitlement(db)[0] == "free"
    assert "WARN: STRIPE_WEBHOOK_FAILED BillingProviderError" in capsys.readouterr().out

    # Stripe's retry then succeeds.
    gateway.fail_provider = False
    retry = _deliver(client, "evt_1", "customer.subscription.created", user=user_id)

    assert retry.json()["result"] == "processed"
    assert _entitlement(db)[0] == "paid"


def test_database_failure_rolls_back_and_is_retryable(
    client, db, gateway, monkeypatch
):
    _register_and_login(client)
    user_id = _user_id(db)
    gateway.subscriptions["sub_1"] = _subscription(user_id)

    def failing_set_tier(*args, **kwargs):
        raise RuntimeError("database write failed")

    monkeypatch.setattr(billing_service, "set_entitlement_tier", failing_set_tier)

    response = _deliver(client, "evt_1", "customer.subscription.created", user=user_id)

    assert response.status_code == 500
    # The billing upsert in the same transaction was rolled back too.
    assert _count(db, StripeWebhookEvent) == 0
    assert _count(db, AccountBilling) == 0
    assert _entitlement(db)[0] == "free"


def test_webhook_logs_never_include_payload_or_signature(client, db, gateway, capsys):
    _register_and_login(client)
    gateway.fail_provider = True

    _deliver(
        client, "evt_secretmarker", "customer.subscription.created",
        user=_user_id(db), extra={"card": "4242424242424242"},
    )

    output = capsys.readouterr().out
    for leaked in ("4242", "evt_secretmarker", "valid", "whsec", "sk_test"):
        assert leaked not in output


# --- storage and schema boundaries ---------------------------------------------


def test_webhook_storage_holds_only_proof_of_processing(client, db, gateway):
    _register_and_login(client)
    user_id = _user_id(db)
    gateway.subscriptions["sub_1"] = _subscription(user_id)

    _deliver(client, "evt_1", "customer.subscription.created", user=user_id,
             extra={"card": "4242424242424242", "email": "payer@example.com"})

    columns = {c.name for c in StripeWebhookEvent.__table__.columns}
    assert columns == {"event_id", "event_type", "processed_at"}

    with db() as session:
        dumped = "\n".join(
            repr(tuple(row))
            for table in Base.metadata.sorted_tables
            for row in session.execute(select(table)).all()
        )

    assert "4242" not in dumped
    assert "payer@example.com" not in dumped


def test_billing_schema_has_no_payment_fields():
    columns = {c.name for c in AccountBilling.__table__.columns}

    assert columns == {
        "user_id",
        "stripe_customer_id",
        "stripe_subscription_id",
        "stripe_price_id",
        "subscription_status",
        "cancel_at_period_end",
        "current_period_end",
        "checkout_attempt_id",
        "checkout_customer_id",
        "checkout_attempt_created_at",
        "checkout_price_id",
        "checkout_success_url",
        "checkout_cancel_url",
        "stripe_checkout_session_id",
        "checkout_session_expires_at",
        "created_at",
        "updated_at",
    }


def test_account_deletion_cascades_billing(client, db, gateway):
    _register_and_login(client)
    user_id = _user_id(db)
    gateway.subscriptions["sub_1"] = _subscription(user_id)
    _deliver(client, "evt_1", "customer.subscription.created", user=user_id)
    assert _count(db, AccountBilling) == 1

    with db() as session, session.begin():
        session.execute(delete(User))

    assert _count(db, AccountBilling) == 0
    assert _count(db, AccountEntitlement) == 0


# --- no self-promotion; everything else unchanged ------------------------------------


def test_only_verified_webhooks_can_set_tier(client, db, gateway, monkeypatch):
    calls = []
    original = billing_service.set_entitlement_tier

    def tracking(*args, **kwargs):
        calls.append(kwargs.get("source"))
        return original(*args, **kwargs)

    monkeypatch.setattr(billing_service, "set_entitlement_tier", tracking)
    token = _register_and_login(client)
    headers = _as(client, token)

    for method, path in (
        ("POST", "/api/account/billing/checkout"),
        ("POST", "/api/account/billing/portal"),
        ("GET", "/api/account/billing"),
        ("GET", "/api/account/entitlements"),
    ):
        client.request(method, path, json={"tier": "paid"}, headers=headers)

    assert calls == []
    assert _entitlement(db)[0] == "free"

    gateway.subscriptions["sub_1"] = _subscription(_user_id(db))
    _deliver(client, "evt_1", "customer.subscription.created", user=_user_id(db))

    assert calls == ["stripe"]


def test_entitlement_contract_unchanged_for_paid_account(client, db, gateway):
    token = _register_and_login(client)
    gateway.subscriptions["sub_1"] = _subscription(_user_id(db))
    _deliver(client, "evt_1", "customer.subscription.created", user=_user_id(db))

    response = client.get("/api/account/entitlements", headers=_as(client, token))

    assert response.json() == {
        "entitlements": {
            "tier": "paid",
            "saved_mode_access": True,
            "higher_usage_access": True,
            "persistent_memory_access": False,
        }
    }


def test_chat_and_private_session_unchanged_with_billing(
    client, db, gateway, chat_calls
):
    token = _register_and_login(client)
    gateway.subscriptions["sub_1"] = _subscription(_user_id(db))
    _deliver(client, "evt_1", "customer.subscription.created", user=_user_id(db))

    counts_before = (
        _count(db, AccountBilling),
        _count(db, StripeWebhookEvent),
        _count(db, AccountUsageDaily),
    )

    anonymous = client.post("/api/chat", json={"message": "hi", "history": []})
    paid = client.post(
        "/api/chat",
        json={"message": "hi", "history": []},
        headers=_as(client, token),
    )

    for response in (anonymous, paid):
        assert response.status_code == 200
        assert set(response.json()) == {"reply", "session_id"}

    assert anonymous.json()["reply"] == paid.json()["reply"]
    assert chat_calls[0] != chat_calls[1]
    assert (
        _count(db, AccountBilling),
        _count(db, StripeWebhookEvent),
        _count(db, AccountUsageDaily),
    ) == counts_before


def test_registration_remains_free(client, db):
    _register_and_login(client)

    assert _entitlement(db) == ("free", "registration", None)
    assert _billing(db) is None
