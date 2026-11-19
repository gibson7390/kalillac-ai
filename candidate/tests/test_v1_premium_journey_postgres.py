"""The Premium customer journey through the real application, on real disposable PostgreSQL 16 after the
operator transition (final runtime grants), with Stripe faked at its gateway and the model scripted.

One customer, over HTTP, against the application's own /api/chat and the real account, registration,
recovery and billing routers:

register (email-first) → sign in → Free: chat is the FREE scope and DEEP is refused → Checkout → Stripe's
webhook → Premium: the entitlement is paid, chat is the PAID scope, DEEP is selected → manage the account
(what it may manage, change password) → the billing portal → cancellation (Stripe's webhook) → Free again:
FREE scope, DEEP refused → the customer's deletion request → sign-in refused, the old session anonymous.

What it proves together that no single-feature test does: a subscription verified by Stripe's webhook
changes the financial scope and the reasoning permission of the very next chat request, through V1-01's
authorized read, and losing it changes them back; under E1a's grants; with the runtime login writing
nothing to the account tables but the entitlement and billing rows E1b still leaves it. Every credential
is synthetic; nothing leaves the disposable database; no provider is called.
"""
# ruff: noqa: F811  (fixtures imported from other test modules are used as parameters)
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import app_fastapi_candidate as app
from kalillac_accounts.recovery_router import build_recovery_router
from kalillac_accounts.quota import UsageQuota
from kalillac_accounts.registration_http import RegistrationHTTP
from kalillac_accounts.request_guard import UnsafeRequestGuard
from kalillac_accounts.router import build_account_router
from kalillac_accounts.usage import UsageMeter, build_usage_router
from kalillac_accounts.usage_limits import UsageLimits
from kalillac_billing.router import build_billing_router
from kalillac_finance.scopes import TrafficClass
from kalillac_routing import provider_controls
from kalillac_routing import reasoning_modes as modes
from test_billing import CONFIG, FakeGateway, _subscription
from test_reasoning_modes import load as load_modes
from test_usage_enforcement import install_budget
from test_v1_lifecycle_postgres import (COOKIE, EMAIL, ORIGIN, PASSWORDS, SETTINGS, Dispatch, account_row,  # noqa: F401
                                        events, live, reset_admission)
from test_v1_transition_postgres import world  # noqa: F401

REPLY = "A scripted reply."


@pytest.fixture
def journey(live, monkeypatch):
    w = live
    w.stripe = FakeGateway()
    original = list(app.api.router.routes)
    registration = RegistrationHTTP(w.authority, Dispatch(w.authority), SETTINGS)
    app.api.include_router(build_account_router(lambda: w.runtime_factory, SETTINGS, registration_http=registration,
                                                v1_security=w.security, v1_authority=w.service))
    import recovery_test_support as recovery
    app.api.include_router(build_recovery_router(session_factory=lambda: w.runtime_factory, gateway=recovery.FakeEmail(),
                                                 settings=SETTINGS, environ=recovery.ENV, v1_security=w.security,
                                                 v1_authority=w.service))
    app.api.include_router(build_billing_router(config=CONFIG, settings=SETTINGS, gateway=w.stripe,
                                                session_factory=lambda: w.runtime_factory, v1_security=w.security))
    app.api.include_router(build_usage_router(session_factory=lambda: w.runtime_factory, settings=SETTINGS,
                                              v1_security=w.security))
    # The application's chat in V1 mode, metering for the signed-in account and enforcing its daily quota
    # (synthetic test limits, not the commercial allowances), the model scripted.
    monkeypatch.setattr(app, "_v1_account_security", w.security)
    monkeypatch.setattr(app, "_account_settings", SETTINGS, raising=False)
    monkeypatch.setattr(app, "_usage_meter", UsageMeter(session_factory=lambda: w.runtime_factory, settings=SETTINGS,
                                                         v1_security=w.security))
    monkeypatch.setattr(app, "_usage_quota", UsageQuota(UsageLimits(free_daily_chats=20, paid_daily_chats=40),
                                                         session_factory=lambda: w.runtime_factory, settings=SETTINGS,
                                                         v1=True))
    monkeypatch.setattr(app, "SESSION_STATE", type(app.SESSION_STATE)())
    w.model_calls = []

    def native(items, instructions):
        w.model_calls.append("native")
        return {"output": [{"type": "message", "role": "assistant",
                            "content": [{"type": "output_text", "text": REPLY}]}]}

    def legacy(messages, **kwargs):
        w.model_calls.append("legacy")
        return SimpleNamespace(content=REPLY, incomplete=False, incomplete_reason=None)

    def forbidden(*args, **kwargs):
        raise AssertionError("no real provider or search work is allowed")
    monkeypatch.setattr(app, "_invoke_openai_native_tools", native)
    monkeypatch.setattr(app, "invoke_llm", legacy)
    monkeypatch.setattr(app, "run_web_search", forbidden)
    monkeypatch.setattr(app, "_post_openai_for_attempt", forbidden)
    # Provider controls on, recording how each chat request is classified; reasoning modes on, with DEEP
    # reserved for the paid tier.
    install_budget(monkeypatch, True)
    monkeypatch.setattr(provider_controls, "SERVICE", SimpleNamespace(config=SimpleNamespace(abuse=None)))
    w.classified = []
    status = provider_controls.account_status

    def spy(authenticated, paid_entitlement=None, account_id=None):
        status(authenticated, paid_entitlement, account_id=account_id)
        w.classified.append(provider_controls.current_request().traffic_class)
    monkeypatch.setattr(provider_controls, "account_status", spy)
    monkeypatch.setattr(modes, "CONFIG", load_modes())
    w.client = TestClient(UnsafeRequestGuard(app.api, origin=ORIGIN), base_url="https://testserver")
    try:
        yield w
    finally:
        w.client.close()
        app.api.router.routes[:] = original


def post(w, path, body=None, cookie=None, origin=True):
    headers = {"Origin": ORIGIN} if origin else {}
    if cookie:
        headers["Cookie"] = f"{COOKIE}={cookie}"
    w.client.cookies.clear()
    return w.client.post(path, json=body if body is not None else {}, headers=headers)


def get(w, path, cookie):
    w.client.cookies.clear()
    return w.client.get(path, headers={"Cookie": f"{COOKIE}={cookie}"})


def chat(w, cookie, mode=None):
    """One chat request; returns (status, error code or None, how it was classified)."""
    body = {"message": "What is a webhook?"}
    if mode:
        body["reasoning_mode"] = mode
    before = len(w.classified)
    w.client.cookies.clear()
    response = w.client.post("/api/chat", json=body, headers={"Cookie": f"{COOKIE}={cookie}"} if cookie else {})
    classified = w.classified[before:]
    return response.status_code, response.json().get("error"), classified[-1] if classified else None


def webhook(w, event_id, event_type, user):
    import json
    w.client.cookies.clear()
    return w.client.post("/api/billing/stripe/webhook", content=json.dumps(
        {"id": event_id, "type": event_type, "subscription": "sub_1", "customer": "cus_1", "user": str(user)}).encode(),
        headers={"Stripe-Signature": "valid", "Content-Type": "application/json"})


def tier(w, cookie):
    return get(w, "/api/account/entitlements", cookie).json()["entitlements"]["tier"]


def runtime_writes(w):
    """(table, operation) for every statement the runtime login wrote."""
    with w.admin_engine.connect() as c:
        return {(r[0], r[1]) for r in c.execute(text("SELECT tbl, op FROM public.e3_writes WHERE who = :r"),
                                                {"r": w.names["app"]})}


def test_the_premium_journey_from_registration_to_the_deletion_request(journey):
    w = journey
    # Register (the emailed link proves ownership) and sign in.
    assert post(w, "/api/account/register", {"email": EMAIL}).status_code == 202
    assert post(w, "/api/account/register/confirm", {"token": w.token(), "password": PASSWORDS[0]}).status_code == 200
    signed_in = post(w, "/api/account/login", {"email": EMAIL, "password": PASSWORDS[0]})
    assert signed_in.status_code == 200
    cookie = signed_in.cookies.get(COOKIE)
    user = account_row(w)[0]

    # Free: the FREE scope; DEEP is refused before any work; QUICK is allowed.
    assert tier(w, cookie) == "free"
    assert chat(w, cookie) == (200, None, TrafficClass.FREE_ACCOUNT)
    calls = len(w.model_calls)
    assert chat(w, cookie, "deep") == (403, "reasoning_mode_not_allowed", None)
    assert len(w.model_calls) == calls
    assert chat(w, cookie, "quick")[:2] == (200, None)
    usage = get(w, "/api/account/usage", cookie)
    assert usage.status_code == 200 and usage.json()["usage"]["successful_chats"] == 2

    # Checkout: the server chooses everything; nothing is granted until Stripe's webhook says so.
    checkout = post(w, "/api/account/billing/checkout", cookie=cookie)
    assert checkout.status_code == 200 and checkout.json()["checkout_url"].startswith("https://checkout.stripe.com/")
    assert tier(w, cookie) == "free"
    w.stripe.subscriptions["sub_1"] = _subscription(user)
    delivered = webhook(w, "evt_paid", "checkout.session.completed", user)
    assert delivered.status_code == 200 and delivered.json()["result"] == "processed"

    # Premium, on the very next request: paid entitlement, the PAID scope, DEEP selected.
    assert tier(w, cookie) == "paid"
    billing = get(w, "/api/account/billing", cookie).json()["billing"]
    assert billing["subscription_status"] == "active" and billing["portal_available"] is True
    assert chat(w, cookie) == (200, None, TrafficClass.PAID_ACCOUNT)
    assert chat(w, cookie, "deep") == (200, None, TrafficClass.PAID_ACCOUNT)
    # Another visitor is not paid because this account is.
    assert chat(w, None) == (200, None, TrafficClass.ANONYMOUS)

    # Account management: what this account may manage, and a password change that keeps this session.
    assert get(w, "/api/account/security", cookie).json() == {
        "security": {"sessions": True, "password_change": True, "deletion_request": True}}
    changed = post(w, "/api/account/password", {"current_password": PASSWORDS[0], "new_password": PASSWORDS[1]},
                   cookie=cookie)
    assert changed.status_code == 200 and get(w, "/api/account/me", cookie).status_code == 200
    assert chat(w, cookie) == (200, None, TrafficClass.PAID_ACCOUNT)        # Still Premium afterwards.

    # The billing portal, where the customer cancels.
    portal = post(w, "/api/account/billing/portal", cookie=cookie)
    assert portal.status_code == 200 and portal.json()["portal_url"].startswith("https://billing.stripe.com/")

    # Cancellation, reported by Stripe: Free again on the very next request.
    w.stripe.subscriptions["sub_1"] = _subscription(user, status="canceled")
    assert webhook(w, "evt_canceled", "customer.subscription.deleted", user).json()["result"] == "processed"
    assert tier(w, cookie) == "free"
    assert chat(w, cookie) == (200, None, TrafficClass.FREE_ACCOUNT)
    assert chat(w, cookie, "deep") == (403, "reasoning_mode_not_allowed", None)

    # The customer's deletion request: every session ends and sign-in is refused.
    reset_admission(w, user)
    assert post(w, "/api/account/deletion-request", {"current_password": PASSWORDS[1]}, cookie=cookie).status_code == 200
    assert get(w, "/api/account/me", cookie).status_code == 401
    assert post(w, "/api/account/login", {"email": EMAIL, "password": PASSWORDS[1]}).status_code == 401
    # The old cookie is nobody: refused by the quota's V1 authorization, never served anonymously.
    assert chat(w, cookie) == (401, "not_authenticated", None)
    assert get(w, "/api/account/usage", cookie).status_code == 401

    assert events(w, user) == ["password_change", "account_deletion_request"]
    # The runtime login wrote entitlement rows (billing; E1b moves that behind a reviewed path) and
    # nothing else on the recorded account tables.
    assert runtime_writes(w) <= {("account_entitlements", "INSERT"), ("account_entitlements", "UPDATE")}
    assert not {t for t, _ in runtime_writes(w)} & {"users", "account_sessions", "account_recovery_tokens"}
