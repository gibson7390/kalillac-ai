"""Billing, usage and the daily quota under V1 account security, on real disposable PostgreSQL 16 after the
operator transition (A11).

With V1 on, these were the last account paths still resolving the cookie through the legacy session
lookup, which admits any live session row: a quarantined account kept its billing, usage and chat quota.
Now each authorizes through V1-01's original-session authorization and returns its refusals as they are.
Checkout repeats the authorization inside each planning transaction, under the account lock, before any
Stripe call; the quota authorizes inside its own reservation transaction, taking V1's FOR UPDATE lock
before its KEY SHARE lock on the same row.

Stripe is faked at its gateway and the model is scripted; every credential is synthetic; nothing leaves
the disposable database. The daily limits here are synthetic test values, not commercial allowances.
"""
# ruff: noqa: F811  (fixtures imported from other test modules are used as parameters)
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from kalillac_accounts.quota import QuotaAccountRefused, UsageQuota
from kalillac_accounts.request_guard import UnsafeRequestGuard
from kalillac_accounts.tokens import hash_session_token
from kalillac_accounts.usage import build_usage_router
from kalillac_accounts.usage_limits import UsageLimits
from kalillac_billing.router import build_billing_router
from kalillac_db.repositories.quota import QuotaIdentityInvalid, QuotaUnavailable
from test_billing import CONFIG, FakeGateway
from test_v1_lifecycle_postgres import (COOKIE, EMAIL, ORIGIN, PASSWORDS, SETTINGS, account_row, live,  # noqa: F401
                                        operator, post, sign_in, writers)
from test_v1_premium_journey_postgres import chat, journey  # noqa: F401
from test_v1_revocation_postgres import admin_sql
from test_v1_transition_postgres import world  # noqa: F401

LIMITS = UsageLimits(free_daily_chats=10, paid_daily_chats=20)
CONCURRENT = 8


@pytest.fixture
def routes(live):
    w = live
    assert post(w, "/api/account/register", {"email": EMAIL}).status_code == 202
    assert post(w, "/api/account/register/confirm", {"token": w.token(), "password": PASSWORDS[0]}).status_code == 200
    w.user = account_row(w)[0]
    w.cookie = sign_in(w, PASSWORDS[0])
    w.stripe = FakeGateway()
    w.reads, w.after_read = [], None
    original = w.security.read_async

    async def counted(*args, **kwargs):
        w.reads.append(1)
        found = await original(*args, **kwargs)
        if w.after_read is not None:
            change, w.after_read = w.after_read, None
            change(w)
        return found
    w.security.read_async = counted
    api = FastAPI()
    api.include_router(build_billing_router(config=CONFIG, settings=SETTINGS, gateway=w.stripe,
                                            session_factory=lambda: w.runtime_factory, v1_security=w.security))
    api.include_router(build_usage_router(session_factory=lambda: w.runtime_factory, settings=SETTINGS,
                                          v1_security=w.security))
    w.client = TestClient(UnsafeRequestGuard(api, origin=ORIGIN), base_url="https://testserver")
    w.quota = UsageQuota(LIMITS, session_factory=lambda: w.runtime_factory, settings=SETTINGS, v1=True)
    try:
        yield w
    finally:
        w.client.close()


def call(w, method, path, cookie=None):
    w.client.cookies.clear()
    headers = {"Cookie": f"{COOKIE}={cookie or w.cookie}"}
    if method == "POST":
        headers["Origin"] = ORIGIN
    return w.client.request(method, path, json={} if method == "POST" else None, headers=headers)


def quarantine(w):
    """Quarantined with its session row left live: the legacy lookup would still admit it."""
    admin_sql(w, "ALTER TABLE kalillac.users DISABLE TRIGGER USER",
              "UPDATE kalillac.users SET quarantined_at = clock_timestamp() WHERE id = :u",
              "ALTER TABLE kalillac.users ENABLE TRIGGER USER", u=w.user)
    with w.admin_engine.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM kalillac.account_sessions WHERE user_id = :u"
                             " AND revoked_at IS NULL"), {"u": w.user}) == 1


def suspend(w):
    assert operator(w, "suspend", w.user, "abuse_report") == 0


def checkout_attempts(w):
    with w.admin_engine.connect() as c:
        return c.scalar(text("SELECT count(*) FROM kalillac.account_billing WHERE user_id = :u"
                             " AND checkout_attempt_id IS NOT NULL"), {"u": w.user})


def slots(w):
    with w.admin_engine.connect() as c:
        return c.scalar(text("SELECT count(*) FROM kalillac.account_usage_slots WHERE user_id = :u"), {"u": w.user})


def reserve(w, cookie=None):
    request = w.quota.new_request()
    try:
        return w.quota.reserve(hash_session_token(cookie or w.cookie), request)
    finally:
        request.close(True)


ROUTES = (("GET", "/api/account/usage"), ("GET", "/api/account/billing"),
          ("POST", "/api/account/billing/portal"), ("POST", "/api/account/billing/checkout"))


def test_billing_and_usage_authorize_through_v1_and_the_runtime_login_writes_no_account_table(routes):
    w = routes
    for method, path in ROUTES[:3]:
        before = len(w.reads)
        response = call(w, method, path)
        assert len(w.reads) == before + 1, path                  # Authorized by V1's read.
        # No Stripe customer yet, so the portal has nothing to open.
        assert response.status_code == (409 if path.endswith("portal") else 200), (path, response.text)
    assert call(w, "GET", "/api/account/usage").json()["usage"]["successful_chats"] == 0
    checkout = call(w, "POST", "/api/account/billing/checkout")
    assert checkout.status_code == 200 and checkout.json()["checkout_url"].startswith("https://checkout.stripe.com/")
    assert len(w.stripe.checkout_calls) == 1 and checkout_attempts(w) == 1
    assert w.names["app"] not in writers(w)


def test_an_account_v1_refuses_is_refused_by_billing_and_usage_even_with_a_live_session(routes):
    w = routes
    quarantine(w)
    for method, path in ROUTES:
        refused = call(w, method, path)
        assert (refused.status_code, refused.json()["error"]) == (403, "account_unavailable"), path
    assert w.stripe.checkout_calls == [] and w.stripe.portal_calls == [] and checkout_attempts(w) == 0


@pytest.mark.parametrize("cookie", ["not-a-session", None])
def test_no_session_is_not_authenticated(routes, cookie):
    w = routes
    for method, path in ROUTES:
        w.client.cookies.clear()
        headers = {"Origin": ORIGIN} if method == "POST" else {}
        if cookie:
            headers["Cookie"] = f"{COOKIE}={cookie}"
        response = w.client.request(method, path, json={} if method == "POST" else None, headers=headers)
        assert (response.status_code, response.json()["error"]) == (401, "not_authenticated"), path
    assert w.stripe.checkout_calls == []


@pytest.mark.parametrize("change, expected", [
    (quarantine, (403, "account_unavailable")),
    (suspend, (401, "not_authenticated")),
], ids=["quarantined", "suspended"])
def test_checkout_rechecks_v1_under_the_account_lock_before_any_stripe_call(routes, change, expected):
    """The account changes after the route's own V1 read and before Checkout's planning transaction: the
    recheck there refuses it, no Stripe call is made and no attempt is recorded."""
    w = routes
    w.after_read = change
    refused = call(w, "POST", "/api/account/billing/checkout")
    assert (refused.status_code, refused.json()["error"]) == expected
    assert w.stripe.checkout_calls == [] and checkout_attempts(w) == 0


def test_the_quota_reserves_only_for_the_account_v1_authorizes(routes):
    w = routes
    admission = reserve(w)
    assert admission.user_id == w.user and admission.reservation is not None and slots(w) == 1
    with pytest.raises(QuotaIdentityInvalid):
        reserve(w, "not-a-session")
    quarantine(w)
    with pytest.raises(QuotaAccountRefused) as refused:
        reserve(w)
    assert (refused.value.status_code, refused.value.code) == (403, "account_unavailable")
    assert slots(w) == 1                                          # Nothing reserved for the refusal.


def test_the_quota_refuses_a_session_the_operator_ended(routes):
    w = routes
    suspend(w)
    with pytest.raises(QuotaIdentityInvalid):
        reserve(w)
    assert slots(w) == 0


def test_concurrent_reservations_for_one_account_never_deadlock(routes, capsys):
    """V1's FOR UPDATE is each reservation's first lock on the account row, so concurrent reservations
    queue on it; none holds KEY SHARE while waiting to upgrade, which PostgreSQL would break as a
    deadlock (SQLSTATE 40P01). Reservations of one account are serialized by that lock, so on a slow
    machine a queued one can reach the quota's 3 s lock or statement timeout (55P03, 57014; both start
    with the statement): a refusal, never a deadlock, and the only failure accepted here."""
    w = routes
    assert CONCURRENT < LIMITS.free_daily_chats
    start = threading.Barrier(CONCURRENT)

    def one(_):
        start.wait()
        try:
            return reserve(w).user_id
        except QuotaUnavailable:
            return "unavailable"
    with ThreadPoolExecutor(CONCURRENT) as pool:
        outcomes = list(pool.map(one, range(CONCURRENT)))
    failures = [line for line in capsys.readouterr().out.splitlines() if "USAGE_QUOTA_DATABASE_FAILED" in line]
    assert len(failures) == outcomes.count("unavailable")
    assert all(line.endswith(("sqlstate=55P03", "sqlstate=57014")) for line in failures), failures
    assert outcomes.count(w.user) >= CONCURRENT // 2 and set(outcomes) <= {w.user, "unavailable"}
    assert slots(w) == outcomes.count(w.user)


def test_a_quarantined_account_cannot_chat_on_its_quota(journey):
    """Through the application's /api/chat with the V1 quota: refused before any model work."""
    w = journey
    assert post(w, "/api/account/register", {"email": EMAIL}).status_code == 202
    assert post(w, "/api/account/register/confirm", {"token": w.token(), "password": PASSWORDS[0]}).status_code == 200
    cookie = sign_in(w, PASSWORDS[0])
    w.user = account_row(w)[0]
    assert chat(w, cookie)[:2] == (200, None)
    calls = len(w.model_calls)
    quarantine(w)
    assert chat(w, cookie) == (403, "account_unavailable", None)
    assert len(w.model_calls) == calls
