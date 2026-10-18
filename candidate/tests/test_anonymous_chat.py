"""/api/chat with the anonymous daily allowance: offline app-level tests.

Scripted providers, a file-backed SQLite database standing in for PostgreSQL
and HTTPS test clients (the visitor cookie is always Secure). No network,
provider, PostgreSQL or application server is used.
"""
import ast
import asyncio
import base64
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app_fastapi_candidate as app
from kalillac_accounts.anonymous_quota import AnonymousQuota, VISITOR_COOKIE, day_digest, parse_visitor_token
from kalillac_accounts.router import AccountSettings
from kalillac_accounts.tokens import hash_session_token
from kalillac_accounts.usage_limits import UsageLimits
from kalillac_db.models import AccountSession, AccountUsageDaily, AnonymousUsageDay, AnonymousUsageSlot, Base, User
from kalillac_db.repositories.entitlements import create_default_entitlement
from account_test_db import make_session_factory, make_sqlite_file_engine

NOW = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
DAY = NOW.date()
BASE_URL = "https://testserver"
SETTINGS = AccountSettings(cookie_secure=True)
ACCOUNT_TOKEN = "synthetic-account-session"
RAW = bytes(range(1, 33))
COOKIE_VALUE = base64.urlsafe_b64encode(RAW).rstrip(b"=").decode("ascii")
MESSAGE = "Explain why encryption uses keys."
REPLY = "Encryption uses keys to control who can read the message."
CANDIDATE_DIR = Path(__file__).resolve().parents[1]


def _unavailable():
    raise RuntimeError("synthetic database outage")


@pytest.fixture
def anon(tmp_path, monkeypatch):
    from test_request_budget_integration import BASE_LIMITS
    from kalillac_routing.request_limits import RequestLimits
    engine = make_sqlite_file_engine(str(tmp_path))
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    data = {"factory": factory, "stable": factory, "engine": engine, "now": NOW, "calls": [], "payloads": []}
    monkeypatch.setattr(app, "SESSION_STATE", type(app.SESSION_STATE)())
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", False)
    monkeypatch.setattr(app, "_request_limits", RequestLimits(**BASE_LIMITS))
    monkeypatch.setattr(app, "_usage_quota", None)
    monkeypatch.setattr(app, "_usage_meter", None)
    monkeypatch.setattr(app, "_anonymous_account_cookie", None)
    service = AnonymousQuota(10, session_factory=lambda: data["factory"], clock=lambda: data["now"])
    monkeypatch.setattr(app, "_anonymous_quota", service)
    data["service"] = service

    def legacy(messages, **kwargs):
        data["calls"].append("legacy")
        data["payloads"].append("\n".join(str(m.content) for m in messages) + repr(kwargs))
        return SimpleNamespace(content=REPLY, incomplete=False, incomplete_reason=None)

    def native(items, instructions):
        data["calls"].append("native")
        data["payloads"].append(repr(items) + instructions)
        return {"output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": REPLY}]}]}

    def forbidden(*args, **kwargs):
        raise AssertionError("No real provider/search work allowed")

    monkeypatch.setattr(app, "invoke_llm", legacy)
    monkeypatch.setattr(app, "_invoke_openai_native_tools", native)
    monkeypatch.setattr(app, "run_web_search", forbidden)
    monkeypatch.setattr(app, "_post_openai_for_attempt", forbidden)
    yield data
    engine.dispose()


def client():
    return TestClient(app.api, base_url=BASE_URL)


def post(c, visitor=COOKIE_VALUE, account=None, body=None, content=None):
    cookies = []
    if visitor is not None:
        cookies.append(f"{VISITOR_COOKIE}={visitor}")
    if account is not None:
        cookies.append(f"{SETTINGS.cookie_name}={account}")
    headers = {"Cookie": "; ".join(cookies)} if cookies else {}
    if content is not None:
        return c.post("/api/chat", content=content, headers={**headers, "Content-Type": "application/json"})
    return c.post("/api/chat", json=body if body is not None else {"message": MESSAGE}, headers=headers)


def visitor_headers(headers):
    return [value for value in headers.get_list("set-cookie") if value.startswith(VISITOR_COOKIE + "=")]


def visitor_cookie(response):
    values = visitor_headers(response.headers)
    assert len(values) == 1, values
    assert_exact_attributes(values[0])
    return values[0].split(";", 1)[0].split("=", 1)[1]


def assert_exact_attributes(header):
    parts = [part.strip() for part in header.split(";")]
    name, value = parts[0].split("=", 1)
    attributes = {}
    for part in parts[1:]:
        key, _, setting = part.partition("=")
        attributes[key.lower()] = setting.lower() if setting else True
    assert name == VISITOR_COOKIE and parse_visitor_token(value) is not None
    # Exactly these attributes: no Domain, no Expires, no other knob.
    assert attributes == {"httponly": True, "max-age": "172800", "path": "/", "samesite": "lax", "secure": True}


def no_visitor_cookie(response):
    assert visitor_headers(response.headers) == []


def states(data):
    with data["stable"]() as session:
        return [(row.usage_date, row.state) for row in session.scalars(
            select(AnonymousUsageSlot).order_by(AnonymousUsageSlot.usage_date, AnonymousUsageSlot.slot))]


def visitor_days(data):
    with data["stable"]() as session:
        return [(row.visitor_digest, row.usage_date) for row in session.scalars(select(AnonymousUsageDay))]


def settle():
    for _ in range(500):
        if app._lookups_outstanding == 0:
            return
        time.sleep(0.01)
    pytest.fail("quota work did not finish")


async def settle_async():
    for _ in range(500):
        if app._lookups_outstanding == 0 and not app._session_locks:
            return
        await asyncio.sleep(0.01)
    pytest.fail("quota or chat work did not finish")


def fill(data, day_now, count=10, token=RAW):
    """Completed responses for one visitor on the clock's day, through the service."""
    saved = data["now"]
    data["now"] = day_now
    try:
        for _ in range(count):
            request = data["service"].new_request(token)
            request.reserve_now()
            request.complete_now()
            request.record_sent_now()
    finally:
        data["now"] = saved


class FakeRequest:
    """The parts of a Starlette request /api/chat reads. receive() never
    reports a disconnect unless a subclass says so."""
    def __init__(self, cookies=None):
        self.cookies = {VISITOR_COOKIE: COOKIE_VALUE} if cookies is None else cookies

    async def json(self):
        return {"message": MESSAGE}

    async def receive(self):
        await asyncio.Event().wait()


async def never_receive():
    await asyncio.Event().wait()


# --- feature off ---------------------------------------------------------------------------------

@pytest.mark.parametrize("budgeted", [False, True], ids=["unbudgeted", "budgeted"])
def test_feature_off_sets_no_cookie_and_does_no_anonymous_database_work(anon, monkeypatch, budgeted):
    monkeypatch.setattr(app, "_anonymous_quota", None)
    if not budgeted:
        monkeypatch.setattr(app, "_request_limits", None)
    anon["factory"] = lambda: (_ for _ in ()).throw(AssertionError("anonymous database access"))
    with client() as c:
        for visitor in (None, COOKIE_VALUE):
            response = post(c, visitor=visitor)
            assert response.status_code == 200 and response.json()["reply"] == REPLY
            assert response.headers["cache-control"] == "no-store"
            assert "set-cookie" not in response.headers
    assert states(anon) == [] and app._anonymous_quota_context.get() is None
    assert len(anon["calls"]) == 2


# --- the allowance: ten completed responses, then refusal before the provider -------------------

def test_ten_completed_responses_then_the_eleventh_is_refused_before_the_provider(anon):
    with client() as c:
        for _ in range(10):
            response = post(c)
            assert response.status_code == 200 and response.json()["reply"] == REPLY
            assert visitor_cookie(response) == COOKIE_VALUE
        refused = post(c)
    assert refused.status_code == 429
    assert refused.content == b'{"error":"anonymous_daily_limit_reached"}'
    assert refused.headers["cache-control"] == "no-store"
    assert visitor_cookie(refused) == COOKIE_VALUE
    assert len(anon["calls"]) == 10  # The eleventh never entered the provider.
    assert states(anon) == [(DAY, "completed")] * 10
    assert visitor_days(anon) == [(day_digest(RAW, DAY), DAY)]


def test_one_active_quota_request_for_anonymous_visitors(anon, monkeypatch):
    class AccountQuotaMustNotRun:
        def new_request(self):
            raise AssertionError("an account quota request was created for an anonymous visitor")
    monkeypatch.setattr(app, "_usage_quota", AccountQuotaMustNotRun())
    monkeypatch.setattr(app, "_anonymous_account_cookie", SETTINGS.cookie_name)
    with client() as c:
        response = post(c)
    assert response.status_code == 200 and visitor_cookie(response) == COOKIE_VALUE
    assert states(anon) == [(DAY, "completed")]


# --- the visitor cookie --------------------------------------------------------------------------

def test_minted_cookie_has_the_exact_name_and_attributes(anon):
    with client() as c:
        response = post(c, visitor=None)
    value = visitor_cookie(response)
    assert value != COOKIE_VALUE and len(value) == 43
    assert value not in response.text
    assert "domain" not in visitor_headers(response.headers)[0].lower()
    assert visitor_days(anon) == [(day_digest(parse_visitor_token(value), DAY), DAY)]


@pytest.mark.parametrize("presented", [
    "", "short", COOKIE_VALUE[:-1], COOKIE_VALUE + "A", COOKIE_VALUE + "=", "A" * 42 + "B",
    COOKIE_VALUE[:-1] + "+", COOKIE_VALUE[:-1] + ".",
])
def test_malformed_visitor_cookies_are_replaced(anon, presented):
    with client() as c:
        response = post(c, visitor=presented)
    assert response.status_code == 200
    replacement = visitor_cookie(response)
    assert replacement != presented and parse_visitor_token(replacement) is not None
    assert visitor_days(anon) == [(day_digest(parse_visitor_token(replacement), DAY), DAY)]


def test_a_valid_visitor_cookie_is_kept_and_refreshed(anon):
    with client() as c:
        first, second = post(c), post(c)
    assert visitor_cookie(first) == visitor_cookie(second) == COOKIE_VALUE
    assert visitor_days(anon) == [(day_digest(RAW, DAY), DAY)]


STATUS_CASES = {
    "ok": (200, None),
    "invalid_json": (400, {"error": "invalid_json"}),
    "validation": (422, {"error": "empty_message"}),
    "limit": (429, {"error": "anonymous_daily_limit_reached"}),
    "busy": (429, {"error": "busy"}),
    "cancelled": (499, {"error": "request_cancelled"}),
    "internal": (500, {"error": "internal_error"}),
    "provider": (503, {"error": "model_provider_unavailable"}),
    "database": (503, {"error": "service_unavailable"}),
    "timeout": (504, {"error": "request_timeout"}),
}


@pytest.mark.parametrize("case", list(STATUS_CASES))
def test_every_anonymous_response_refreshes_the_cookie(anon, monkeypatch, case):
    from test_request_budget_integration import BASE_LIMITS
    from kalillac_routing.request_limits import RequestLimits
    finished = threading.Event()
    body = None
    content = None
    if case == "invalid_json":
        content = b"not json"
    elif case == "validation":
        body = {"message": "   "}
    elif case == "limit":
        fill(anon, NOW)
    elif case == "busy":
        monkeypatch.setattr(app, "_lookups_outstanding", app.MAX_QUEUED_CHATS)
    elif case == "database":
        anon["factory"] = _unavailable
    else:
        def model(messages, **kwargs):
            anon["calls"].append("legacy")
            try:
                if case == "cancelled":
                    app.current_budget().cancel()
                elif case == "internal":
                    raise app.ChatInternalError()
                elif case == "provider":
                    raise app.ModelProviderUnavailable()
                elif case == "timeout":
                    time.sleep(0.8)
                return SimpleNamespace(content=REPLY, incomplete=False, incomplete_reason=None)
            finally:
                finished.set()
        monkeypatch.setattr(app, "invoke_llm", model)
        if case == "timeout":
            monkeypatch.setattr(app, "_request_limits", RequestLimits(
                **{**BASE_LIMITS, "deadline_seconds": 0.3, "queue_wait_seconds": 0.1}))
    status, expected = STATUS_CASES[case]
    with client() as c:
        response = post(c, body=body, content=content)
        if case == "busy":
            monkeypatch.setattr(app, "_lookups_outstanding", 0)
        if case in ("cancelled", "internal", "provider", "timeout"):
            assert finished.wait(5)
        settle()
    assert response.status_code == status
    if expected is not None:
        assert response.json() == expected
    assert response.headers["cache-control"] == "no-store"
    assert visitor_cookie(response) == COOKIE_VALUE
    if case in ("cancelled", "internal", "provider", "timeout"):
        assert states(anon) == [(DAY, "released")]  # Known failure: refunded.


# --- identity: accounts disabled and enabled ---------------------------------------------------

def test_account_named_cookie_with_accounts_disabled_is_charged_as_anonymous(anon):
    with client() as c:
        response = post(c, account="forged-session-value")
    assert response.status_code == 200 and visitor_cookie(response) == COOKIE_VALUE
    assert states(anon) == [(DAY, "completed")]


@pytest.fixture
def accounts(anon, monkeypatch):
    from kalillac_accounts.quota import UsageQuota
    user_id = uuid.uuid4()
    with anon["factory"]() as session, session.begin():
        session.add(User(id=user_id, email="quota@example.test", password_hash="synthetic-hash"))
        session.flush()
        create_default_entitlement(session, user_id)
        session.add(AccountSession(user_id=user_id, token_hash=hash_session_token(ACCOUNT_TOKEN),
                                   expires_at=NOW + timedelta(days=14)))
    monkeypatch.setattr(app, "_usage_quota", UsageQuota(
        UsageLimits(1, 3), session_factory=lambda: anon["factory"], settings=SETTINGS, clock=lambda: anon["now"]))
    monkeypatch.setattr(app, "_anonymous_account_cookie", SETTINGS.cookie_name)
    anon["user_id"] = user_id
    return anon


def test_valid_account_uses_only_its_account_quota(accounts):
    with client() as c:
        response = post(c, account=ACCOUNT_TOKEN)
        refused = post(c, account=ACCOUNT_TOKEN)
    assert response.status_code == 200 and response.json()["reply"] == REPLY
    assert refused.status_code == 429 and refused.json() == {"error": "busy"}  # The account's own limit.
    no_visitor_cookie(response)
    no_visitor_cookie(refused)
    assert states(accounts) == [] and visitor_days(accounts) == []
    with accounts["stable"]() as session:
        assert session.scalar(select(AccountUsageDaily)).successful_chats == 1


@pytest.mark.parametrize("token", ["unknown-session", "expired", "revoked", "inactive"])
def test_invalid_account_sessions_are_refused_without_a_visitor_cookie(accounts, token):
    if token != "unknown-session":
        with accounts["factory"]() as session, session.begin():
            row = session.scalar(select(AccountSession))
            if token == "expired":
                row.expires_at = NOW
            if token == "revoked":
                row.revoked_at = NOW
            if token == "inactive":
                session.get(User, accounts["user_id"]).is_active = False
        token = ACCOUNT_TOKEN
    with client() as c:
        response = post(c, account=token)
    assert response.status_code == 401 and response.json() == {"error": "not_authenticated"}
    assert response.headers["cache-control"] == "no-store"
    no_visitor_cookie(response)
    assert not accounts["calls"] and states(accounts) == [] and visitor_days(accounts) == []


def test_no_account_cookie_with_accounts_enabled_is_anonymous(accounts):
    with client() as c:
        response = post(c)
    assert response.status_code == 200 and visitor_cookie(response) == COOKIE_VALUE
    assert states(accounts) == [(DAY, "completed")]
    with accounts["stable"]() as session:
        assert session.scalar(select(AccountUsageDaily)) is None


# --- delivery: final-body charging, failed and unknown delivery ---------------------------------

def test_success_is_charged_only_after_the_final_body_chunk(anon):
    seen = []
    async def scenario():
        response = await app.api_chat(FakeRequest())
        assert response.status_code == 200
        assert states(anon) == [(DAY, "ready")]
        async def send(message):
            seen.append((message["type"], states(anon)))
        await response({"type": "http"}, never_receive, send)
        start = [m for m in seen if m[0] == "http.response.start"]
        assert start and start[0][1] == [(DAY, "ready")]
        await settle_async()
    asyncio.run(scenario())
    assert [kind for kind, _ in seen] == ["http.response.start", "http.response.body"]
    assert seen[-1][1] == [(DAY, "ready")]  # Still ready while the final chunk is handed over.
    assert states(anon) == [(DAY, "completed")]


@pytest.mark.parametrize("failure_type", ["http.response.start", "http.response.body"])
def test_failed_send_before_the_final_chunk_refunds(anon, failure_type):
    async def scenario():
        response = await app.api_chat(FakeRequest())
        assert response.status_code == 200
        async def send(message):
            if message["type"] == failure_type:
                raise RuntimeError("synthetic failed send")
        with pytest.raises(RuntimeError):
            await response({"type": "http"}, never_receive, send)
        await settle_async()
    asyncio.run(scenario())
    assert states(anon) == [(DAY, "released")]


@pytest.mark.parametrize("failure", ["database", "lookup_capacity"])
def test_unknown_delivery_after_ready_stays_charged(anon, monkeypatch, failure):
    async def scenario():
        response = await app.api_chat(FakeRequest())
        assert response.status_code == 200
        if failure == "database":
            anon["factory"] = _unavailable
        else:
            monkeypatch.setattr(app, "_lookups_outstanding", app.MAX_QUEUED_CHATS)
        async def send(message):
            pass
        await response({"type": "http"}, never_receive, send)  # Sent; settlement cannot be recorded.
        if failure == "lookup_capacity":
            monkeypatch.setattr(app, "_lookups_outstanding", 0)
        await settle_async()
    asyncio.run(scenario())
    anon["factory"] = anon["stable"]
    assert states(anon) == [(DAY, "ready")]
    anon["now"] += timedelta(minutes=16)
    fill(anon, anon["now"], count=1)
    assert states(anon) == [(DAY, "uncertain"), (DAY, "completed")]  # Expired ready stays charged.


# --- cancellation, disconnect and timeout --------------------------------------------------------

def test_cancellation_releases_the_lease_and_ignores_the_late_reply(anon, monkeypatch):
    import httpx
    entered, finish = threading.Event(), threading.Event()
    def model(messages, **kwargs):
        anon["calls"].append("legacy")
        entered.set()
        assert finish.wait(5)
        return SimpleNamespace(content=REPLY, incomplete=False, incomplete_reason=None)
    monkeypatch.setattr(app, "invoke_llm", model)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.api), base_url=BASE_URL) as c:
            task = asyncio.create_task(post_async(c))
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                for _ in range(300):
                    if states(anon) == [(DAY, "released")]:
                        break
                    await asyncio.sleep(0.01)
                else:
                    pytest.fail("cancelled reservation not released")
            finally:
                finish.set()
            await settle_async()
    asyncio.run(scenario())
    assert states(anon) == [(DAY, "released")]


async def post_async(c):
    return await c.post("/api/chat", json={"message": MESSAGE}, headers={"Cookie": f"{VISITOR_COOKIE}={COOKIE_VALUE}"})


def test_disconnect_during_model_work_refunds_with_a_cookie(anon, monkeypatch):
    entered, finish = threading.Event(), threading.Event()
    def model(messages, **kwargs):
        anon["calls"].append("legacy")
        entered.set()
        assert finish.wait(5)
        return SimpleNamespace(content=REPLY, incomplete=False, incomplete_reason=None)
    monkeypatch.setattr(app, "invoke_llm", model)

    class Disconnecting(FakeRequest):
        async def receive(self):
            await asyncio.to_thread(entered.wait, 5)
            return {"type": "http.disconnect"}

    async def scenario():
        try:
            response = await app.api_chat(Disconnecting())
        finally:
            finish.set()
        assert response.status_code == 499
        assert json.loads(response.body) == {"error": "request_cancelled"}
        values = [v for v in response.headers.getlist("set-cookie") if v.startswith(VISITOR_COOKIE + "=")]
        assert len(values) == 1
        assert_exact_attributes(values[0])
        await settle_async()
    asyncio.run(scenario())
    assert states(anon) == [(DAY, "released")]


def test_handler_stopping_during_the_reservation_hands_cleanup_to_the_late_worker(anon, monkeypatch):
    import httpx
    service = anon["service"]
    entered, finish = threading.Event(), threading.Event()
    original = service.reserve
    def slow(token, request):
        entered.set()
        assert finish.wait(5)
        return original(token, request)
    monkeypatch.setattr(service, "reserve", slow)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.api), base_url=BASE_URL) as c:
            task = asyncio.create_task(post_async(c))
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                finish.set()
            await settle_async()
    asyncio.run(scenario())
    assert not anon["calls"]
    assert states(anon) in ([], [(DAY, "released")])  # Closed before or after its commit: never charged.


# --- database failures ---------------------------------------------------------------------------

def test_database_failure_during_reservation_fails_closed(anon):
    anon["factory"] = _unavailable
    with client() as c:
        response = post(c)
    assert response.status_code == 503 and response.json() == {"error": "service_unavailable"}
    assert not anon["calls"] and visitor_cookie(response) == COOKIE_VALUE


def test_database_failure_during_revalidation_fails_closed(anon, monkeypatch):
    service = anon["service"]
    original = service.reserve
    def reserve_then_outage(token, request):
        lease = original(token, request)
        anon["factory"] = _unavailable
        return lease
    monkeypatch.setattr(service, "reserve", reserve_then_outage)
    with client() as c:
        response = post(c)
        settle()
    assert response.status_code == 503 and response.json() == {"error": "service_unavailable"}
    assert not anon["calls"] and visitor_cookie(response) == COOKIE_VALUE
    assert states(anon) == [(DAY, "pending")]  # Charged until lazy expiry recovery.


def test_database_failure_during_preparation_fails_closed(anon, monkeypatch):
    def model(messages, **kwargs):
        anon["calls"].append("legacy")
        anon["factory"] = _unavailable
        return SimpleNamespace(content=REPLY, incomplete=False, incomplete_reason=None)
    monkeypatch.setattr(app, "invoke_llm", model)
    with client() as c:
        response = post(c)
        settle()
    assert response.status_code == 503 and response.json() == {"error": "service_unavailable"}
    assert visitor_cookie(response) == COOKIE_VALUE
    assert states(anon) == [(DAY, "pending")]  # Never a false success.


# --- UTC midnight ----------------------------------------------------------------------------------

def test_midnight_rollover_charges_the_completion_day(anon, monkeypatch):
    anon["now"] = NOW.replace(hour=23, minute=59, second=59)
    def model(messages, **kwargs):
        anon["calls"].append("legacy")
        anon["now"] += timedelta(seconds=2)
        return SimpleNamespace(content=REPLY, incomplete=False, incomplete_reason=None)
    monkeypatch.setattr(app, "invoke_llm", model)
    with client() as c:
        response = post(c)
    assert response.status_code == 200 and visitor_cookie(response) == COOKIE_VALUE
    next_day = DAY + timedelta(days=1)
    assert states(anon) == [(DAY, "released"), (next_day, "completed")]
    assert sorted(visitor_days(anon), key=lambda row: row[1]) == [
        (day_digest(RAW, DAY), DAY), (day_digest(RAW, next_day), next_day)]


def test_full_new_day_refuses_at_preparation_and_releases_the_old_lease(anon, monkeypatch):
    anon["now"] = NOW.replace(hour=23, minute=59, second=59)
    next_morning = NOW.replace(hour=23, minute=59, second=59) + timedelta(seconds=2)
    def model(messages, **kwargs):
        anon["calls"].append("legacy")
        fill(anon, next_morning)  # The same browser used the whole new day meanwhile.
        anon["now"] = next_morning
        return SimpleNamespace(content=REPLY, incomplete=False, incomplete_reason=None)
    monkeypatch.setattr(app, "invoke_llm", model)
    with client() as c:
        response = post(c)
        settle()
    assert response.status_code == 429 and response.json() == {"error": "anonymous_daily_limit_reached"}
    assert visitor_cookie(response) == COOKIE_VALUE
    next_day = DAY + timedelta(days=1)
    assert states(anon) == [(DAY, "released")] + [(next_day, "completed")] * 10


# --- privacy -------------------------------------------------------------------------------------

def test_tokens_and_digests_never_leak(anon, capsys):
    with client() as c:
        minted = post(c, visitor=None)
        ok = post(c)
        fill(anon, NOW, count=9)
        limited = post(c)
        anon["factory"] = _unavailable
        failed = post(c)
        anon["factory"] = anon["stable"]
        settle()
    minted_value = visitor_cookie(minted)
    secrets = {COOKIE_VALUE, RAW.hex(), day_digest(RAW, DAY), minted_value,
               day_digest(parse_visitor_token(minted_value), DAY)}
    exposed = capsys.readouterr()
    for response in (minted, ok, limited, failed):
        other_headers = "\n".join(f"{k}: {v}" for k, v in response.headers.multi_items() if k.lower() != "set-cookie")
        surfaces = [response.text, other_headers]
        assert not any(secret in surface for secret in secrets for surface in surfaces)
    surfaces = [exposed.out, exposed.err, "\n".join(anon["payloads"]), repr(app.SESSION_STATE)]
    assert not any(secret in surface for secret in secrets for surface in surfaces)
    assert (ok.status_code, limited.status_code, failed.status_code) == (200, 429, 503)


# --- structure and startup ------------------------------------------------------------------------

def test_account_and_anonymous_quotas_share_one_response_and_action():
    import kalillac_accounts.quota as account_quota
    import kalillac_accounts.quota_runtime as quota_runtime
    assert account_quota.QuotaResponse is quota_runtime.QuotaResponse
    assert account_quota.QuotaAction is quota_runtime.QuotaAction
    # One implementation each: the account module re-exports, never redefines.
    for path in (CANDIDATE_DIR / "kalillac_accounts").glob("*.py"):
        classes = {node.name for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                   if isinstance(node, ast.ClassDef)}
        if path.name != "quota_runtime.py":
            assert not classes & {"QuotaResponse", "QuotaAction", "AnonymousResponse"}, path.name
    app_source = (CANDIDATE_DIR / "app_fastapi_candidate.py").read_text(encoding="utf-8")
    assert "class _QuotaAction" not in app_source and "AnonymousResponse" not in app_source
    # Both successful-response wraps use the shared class.
    assert app_source.count("response = QuotaResponse(response, visit.request,") == 1
    assert app_source.count("response = QuotaResponse(response, state,") == 1
    assert "from kalillac_accounts.quota import QuotaResponse" not in app_source


def test_successful_anonymous_response_is_the_shared_quota_response(anon):
    from kalillac_accounts.quota_runtime import QuotaResponse
    async def scenario():
        response = await app.api_chat(FakeRequest())
        assert response.status_code == 200 and type(response) is QuotaResponse
        async def send(message):
            pass
        await response({"type": "http"}, never_receive, send)
        await settle_async()
    asyncio.run(scenario())
    assert states(anon) == [(DAY, "completed")]


def _imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    return names | {("." * node.level) + (node.module or "") for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}


def test_wiring_and_runtime_modules_do_not_import_the_account_stack():
    accounts = CANDIDATE_DIR / "kalillac_accounts"
    assert _imports(accounts / "anonymous_chat.py") == {"kalillac_accounts.anonymous_quota"}
    assert _imports(accounts / "quota_runtime.py") == {"starlette.responses"}


ACCOUNT_STACK = ("kalillac_accounts.quota", "kalillac_accounts.router", "kalillac_accounts.tokens",
                 "kalillac_accounts.passwords", "kalillac_accounts.entitlements", "argon2")


def test_importing_the_shared_runtime_and_anonymous_path_loads_no_account_stack():
    probe = ("import json, sys; import kalillac_accounts.quota_runtime, kalillac_accounts.anonymous_chat, "
             "kalillac_accounts.anonymous_quota; print(json.dumps(sorted(sys.modules)))")
    result = subprocess.run([sys.executable, "-B", "-c", probe], cwd=str(CANDIDATE_DIR),
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    loaded = set(json.loads(result.stdout.strip().splitlines()[-1]))
    assert {"kalillac_accounts.quota_runtime", "kalillac_accounts.anonymous_chat"} <= loaded
    assert not {name for name in loaded for bad in ACCOUNT_STACK if name == bad or name.startswith(bad + ".")}


def _startup(values):
    from test_request_limits import VALID as BUDGET
    env = {key: value for key, value in os.environ.items() if not key.startswith("KALILLAC_")}
    env.update({"OPENAI_API_KEY": "", "TAVILY_API_KEY": "", "BRAVE_API_KEY": "", "DEBUG_MODE": "false"})
    if values.pop("budget", False):
        env.update(BUDGET)
    env.update(values)
    script = """import json,socket,sys
attempts=[]
def block(*args,**kwargs): attempts.append(1);raise AssertionError('network blocked')
socket.socket.connect=socket.socket.connect_ex=socket.getaddrinfo=block
import app_fastapi_candidate as app
from kalillac_db import engine
print(json.dumps({'anonymous':app._anonymous_quota is not None,'limit':None if app._anonymous_quota is None else app._anonymous_quota.limit,'engine':engine._ENGINE is not None,'outbound':len(attempts),'service_imported':'kalillac_accounts.anonymous_quota' in sys.modules,'account_quota_imported':'kalillac_accounts.quota' in sys.modules,'argon2_imported':'argon2' in sys.modules}))
"""
    return subprocess.run([sys.executable, "-B", "-c", script], cwd=str(CANDIDATE_DIR), env=env,
                          capture_output=True, text=True, timeout=60)


ON = {"KALILLAC_ANONYMOUS_DAILY_LIMIT_ENABLED": "true", "KALILLAC_ANONYMOUS_DAILY_LIMIT": "10"}
DB = {"KALILLAC_DB_ENABLED": "true"}


@pytest.mark.parametrize("values, error", [
    ({**ON, "budget": True}, "KALILLAC_ANONYMOUS_DAILY_LIMIT_ENABLED requires KALILLAC_DB_ENABLED."),
    ({**ON, **DB}, "KALILLAC_ANONYMOUS_DAILY_LIMIT_ENABLED requires KALILLAC_REQUEST_BUDGET_ENABLED."),
    ({**ON, **DB, "budget": True, "KALILLAC_ACCOUNTS_ENABLED": "true"},
     "KALILLAC_ANONYMOUS_DAILY_LIMIT_ENABLED with KALILLAC_ACCOUNTS_ENABLED requires "
     "KALILLAC_USAGE_ENFORCEMENT_ENABLED."),
    ({**ON, **DB, "budget": True, "KALILLAC_ANONYMOUS_DAILY_LIMIT": "11"},
     "KALILLAC_ANONYMOUS_DAILY_LIMIT must be exactly 10."),
], ids=["no-database", "no-request-budget", "accounts-without-enforcement", "not-ten"])
def test_startup_refuses_every_missing_prerequisite(values, error):
    result = _startup(dict(values))
    assert result.returncode != 0
    assert error in result.stderr
    assert "anon-leak" not in result.stderr


@pytest.mark.parametrize("enabled", [False, True], ids=["off", "on"])
def test_startup_gate_is_lazy_and_explicit(enabled):
    values = {**ON, **DB, "budget": True} if enabled else {"KALILLAC_ANONYMOUS_DAILY_LIMIT": "anon-leak-1"}
    result = _startup(values)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "anonymous": enabled, "limit": 10 if enabled else None, "engine": False, "outbound": 0,
        "service_imported": enabled, "account_quota_imported": False, "argon2_imported": False}
