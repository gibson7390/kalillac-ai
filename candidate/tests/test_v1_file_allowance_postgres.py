"""Codex finding (checkpoint 8d5a2d4): file- and image-assisted responses were not counted against the
signed-in customer's daily response allowance. On real disposable PostgreSQL 16 after the operator
transition (the Premium journey's fixture: V1 on, E1a's final grants, the V1 daily quota, Stripe faked at
its gateway, the model scripted), through the real application's /api/chat file handoff and the real
/api/files/assist route:

* every successful file, image or ordinary response counts exactly once, against the allowance of the
  verified account's effective plan (one response each: the recorded recommendation of owner decision 16);
* an exhausted allowance is refused before any model work;
* a failed or cancelled request releases its reservation and is not counted;
* concurrent requests cannot exceed the allowance;
* after a Premium cancellation and under quarantine the customer is refused before any model work.

The daily limits here (Free 2, Premium 3) are synthetic test values, not commercial allowances.
"""
# ruff: noqa: F811  (fixtures imported from other test modules are used as parameters)
from __future__ import annotations

import asyncio
import io
import threading
from urllib.parse import quote

import httpx
import pytest
from sqlalchemy import text

import app_fastapi_candidate as app
from kalillac_accounts.quota import UsageQuota
from kalillac_accounts.request_guard import UnsafeRequestGuard
from kalillac_accounts.usage_limits import UsageLimits
from kalillac_files.account_access import build_authenticator
from kalillac_files.assistant import AssistantError
from kalillac_files.intent import wants_file
from kalillac_files.router import FileService, build_files_router, valid_assist_request, valid_confirmations
from kalillac_files.store import FileStore
from test_billing import _subscription
from test_v1_lifecycle_postgres import COOKIE, EMAIL, ORIGIN, PASSWORDS, SETTINGS, account_row, live  # noqa: F401
from test_v1_premium_journey_postgres import chat, journey, post, webhook  # noqa: F401
from test_v1_revocation_postgres import admin_sql
from test_v1_transition_postgres import world  # noqa: F401

Image = pytest.importorskip("PIL.Image")
LIMITS = UsageLimits(free_daily_chats=2, paid_daily_chats=3)
COUNTED = getattr(app, "_counted_file_response", None)       # The fix; see the first test.


@pytest.fixture
def allowance(journey, tmp_path, monkeypatch):
    w = journey
    monkeypatch.setattr(app, "_usage_quota", UsageQuota(LIMITS, session_factory=lambda: w.runtime_factory,
                                                         settings=SETTINGS, v1=True))
    w.file_model_calls, w.hold, w.fail = 0, None, False

    def model(items):
        w.file_model_calls += 1
        if w.hold is not None:
            assert w.hold.wait(10)
        if w.fail:
            raise AssistantError("model_output_unusable")
        return {"output": [{"type": "message", "role": "assistant",
                            "content": [{"type": "output_text", "text": "Here is what the file shows."}]}]}
    w.store = FileStore(tmp_path / "files")
    service = FileService(store=w.store, call_model=model, required_tier="paid", images=True,
                          authenticate=build_authenticator(settings=SETTINGS, v1_security=w.security,
                                                           session_factory=lambda: w.runtime_factory),
                          request_scope=app._file_request_scope, failure_status=app._file_failure,
                          **({"count_response": COUNTED} if COUNTED is not None else {}))
    app.api.include_router(build_files_router(service=service))
    monkeypatch.setattr(app, "_file_chat_handoff", app._build_file_chat_handoff(
        service, wants_file, valid_assist_request, valid_confirmations))
    assert post(w, "/api/account/register", {"email": EMAIL}).status_code == 202
    assert post(w, "/api/account/register/confirm", {"token": w.token(), "password": PASSWORDS[0]}).status_code == 200
    w.cookie = post(w, "/api/account/login", {"email": EMAIL, "password": PASSWORDS[0]}).cookies.get(COOKIE)
    w.user = account_row(w)[0]
    assert post(w, "/api/account/billing/checkout", cookie=w.cookie).status_code == 200
    w.stripe.subscriptions["sub_1"] = _subscription(w.user)
    assert webhook(w, "evt_paid", "checkout.session.completed", w.user).json()["result"] == "processed"
    w.photo = upload_photo(w)
    # One event loop for the whole test, as in a running server: a failed request's release is a
    # background task after its response, and a per-request loop would cancel it before it runs.
    with w.client:
        yield w


def settled(w):
    """The customer's counts once no reservation is still pending (a release lands after its response)."""
    import time
    deadline = time.monotonic() + 5
    while counted(w)[1].get("pending", 0) and time.monotonic() < deadline:
        time.sleep(0.02)
    return counted(w)


def upload_photo(w):
    image = Image.new("RGB", (48, 32), (30, 90, 200))
    out = io.BytesIO()
    image.save(out, format="PNG")
    w.client.cookies.clear()
    stored = w.client.post("/api/files", content=out.getvalue(), headers={
        "Cookie": f"{COOKIE}={w.cookie}", "Origin": ORIGIN, "Content-Type": "application/octet-stream",
        "X-Kalillac-Filename": quote("chart.png")})
    assert stored.status_code == 201, stored.text
    return stored.json()["file"]["id"]


def counted(w):
    """(successful responses today, slots by state) for the customer, read as the superuser."""
    with w.admin_engine.connect() as c:
        chats = c.scalar(text("SELECT coalesce(sum(successful_chats), 0) FROM kalillac.account_usage_daily"
                              " WHERE user_id = :u"), {"u": w.user})
        states = dict(c.execute(text("SELECT state, count(*) FROM kalillac.account_usage_slots WHERE user_id = :u"
                                     " GROUP BY state"), {"u": w.user}).all())
    return int(chats), states


def file_chat(w, message="What does this chart show?", attach=True):
    w.client.cookies.clear()
    body = {"message": message}
    if attach:
        body["file_ids"] = [w.photo]
    return w.client.post("/api/chat", json=body, headers={"Cookie": f"{COOKIE}={w.cookie}"})


def assist(w, message="Summarize this image."):
    return post(w, "/api/files/assist", {"message": message, "file_ids": [w.photo]}, cookie=w.cookie)


def test_the_application_counts_file_responses():
    assert COUNTED is not None, "file responses are not admitted against the daily allowance"


def test_every_file_and_image_response_counts_once_and_an_exhausted_allowance_is_refused_before_work(allowance):
    w = allowance
    assert file_chat(w).status_code == 200                       # An image-assisted chat (the handoff).
    assert counted(w)[0] == 1
    assert assist(w).status_code == 200                          # A direct file-assistant request.
    assert counted(w)[0] == 2
    assert chat(w, w.cookie)[0] == 200                            # An ordinary chat.
    assert counted(w)[0] == 3                                     # Premium's synthetic 3: used up.
    calls = w.file_model_calls
    for refused in (file_chat(w), assist(w)):
        assert (refused.status_code, refused.json()["error"]) == (429, "daily_limit_reached")
    assert w.file_model_calls == calls and counted(w)[0] == 3     # Refused before any model work.
    assert settled(w)[1].get("pending", 0) == 0


def test_a_failed_file_response_releases_its_reservation_and_is_not_counted(allowance):
    w = allowance
    w.fail = True
    for failed in (file_chat(w), assist(w)):
        assert failed.status_code == 502
    chats, states = settled(w)
    assert chats == 0 and set(states) <= {"released"}
    w.fail = False
    assert assist(w).status_code == 200 and counted(w)[0] == 1


def test_concurrent_file_requests_cannot_exceed_the_allowance(allowance):
    w = allowance
    assert assist(w).status_code == 200 and assist(w).status_code == 200      # 2 of 3 used.
    w.hold = threading.Event()

    async def race():
        transport = httpx.ASGITransport(app=UnsafeRequestGuard(app.api, origin=ORIGIN))
        headers = {"Cookie": f"{COOKIE}={w.cookie}", "Origin": ORIGIN}
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as http:
            first = asyncio.create_task(http.post("/api/files/assist", headers=headers,
                                                  json={"message": "One", "file_ids": [w.photo]}))
            while w.file_model_calls < 3:
                await asyncio.sleep(0.02)
            second = await http.post("/api/files/assist", headers=headers,
                                     json={"message": "Two", "file_ids": [w.photo]})
            w.hold.set()
            return await first, second
    first, second = asyncio.run(race())
    assert first.status_code == 200
    assert second.status_code == 429 and second.json()["error"] in ("busy", "daily_limit_reached")
    assert w.file_model_calls == 3 and counted(w)[0] == 3


def test_a_request_cancelled_during_model_work_is_not_counted(allowance):
    w = allowance
    w.hold = threading.Event()

    async def cancelled():
        transport = httpx.ASGITransport(app=UnsafeRequestGuard(app.api, origin=ORIGIN))
        headers = {"Cookie": f"{COOKIE}={w.cookie}", "Origin": ORIGIN}
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as http:
            task = asyncio.create_task(http.post("/api/files/assist", headers=headers,
                                                 json={"message": "Long one", "file_ids": [w.photo]}))
            while w.file_model_calls < 1:
                await asyncio.sleep(0.02)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            w.hold.set()
            for _ in range(250):                                 # The release lands after the cancellation.
                if counted(w)[1].get("pending", 0) == 0:
                    break
                await asyncio.sleep(0.02)
    asyncio.run(cancelled())
    chats, states = counted(w)
    assert chats == 0 and states.get("pending", 0) == 0 and states.get("ready", 0) == 0


def test_after_premium_is_cancelled_the_free_allowance_and_plan_apply(allowance):
    w = allowance
    assert assist(w).status_code == 200 and assist(w).status_code == 200      # 2 used as Premium.
    w.stripe.subscriptions["sub_1"] = _subscription(w.user, status="canceled")
    assert webhook(w, "evt_canceled", "customer.subscription.deleted", w.user).json()["result"] == "processed"
    calls = w.file_model_calls
    refused = assist(w)
    assert (refused.status_code, refused.json()["error"]) == (403, "files_require_premium")
    limited = chat(w, w.cookie)                                   # Free's synthetic 2: already used.
    assert limited[:2] == (429, "daily_limit_reached")
    assert w.file_model_calls == calls and counted(w)[0] == 2


def test_a_quarantined_account_is_refused_before_any_file_work(allowance):
    w = allowance
    admin_sql(w, "ALTER TABLE kalillac.users DISABLE TRIGGER USER",
              "UPDATE kalillac.users SET quarantined_at = clock_timestamp() WHERE id = :u",
              "ALTER TABLE kalillac.users ENABLE TRIGGER USER", u=w.user)
    calls = w.file_model_calls
    assert assist(w).status_code in (401, 403)
    assert file_chat(w).status_code in (401, 403)
    assert w.file_model_calls == calls and counted(w) == (0, {})
