"""Daily account quotas: scripted chat providers and offline database tests."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app_fastapi_candidate as app
from kalillac_accounts.router import AccountSettings
from kalillac_accounts.tokens import hash_session_token
from kalillac_accounts.usage import UsageMeter
from kalillac_accounts.usage_limits import UsageLimits
from kalillac_db.models import Base, User, AccountSession, AccountUsageDaily
from kalillac_db.repositories.entitlements import create_default_entitlement
from account_test_db import make_sqlite_file_engine, make_session_factory

NOW = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
SETTINGS = AccountSettings(cookie_secure=True)
TOKEN = "synthetic-account-session"
MESSAGE = "Explain why encryption uses keys."
REPLY = "Encryption uses keys to control who can read the message."


@pytest.fixture
def foundation(tmp_path, monkeypatch):
    engine = make_sqlite_file_engine(str(tmp_path))
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    user_id = uuid.uuid4()
    with factory() as session, session.begin():
        session.add(User(id=user_id, email="quota@example.test", password_hash="synthetic-hash"))
        session.flush()
        create_default_entitlement(session, user_id)
        session.add(AccountSession(user_id=user_id, token_hash=hash_session_token(TOKEN), expires_at=NOW + timedelta(days=14)))
    data = {"factory": factory, "engine": engine, "user_id": user_id, "now": NOW, "calls": []}
    monkeypatch.setattr(app, "SESSION_STATE", type(app.SESSION_STATE)())
    monkeypatch.setattr(app, "_usage_meter", UsageMeter(session_factory=lambda: factory, settings=SETTINGS, clock=lambda: data["now"]))
    def native(items, instructions):
        data["calls"].append("native")
        return {"output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": REPLY}]}]}
    def legacy(messages, **kwargs):
        data["calls"].append("legacy")
        return SimpleNamespace(content=REPLY, incomplete=False, incomplete_reason=None)
    monkeypatch.setattr(app, "_invoke_openai_native_tools", native)
    monkeypatch.setattr(app, "invoke_llm", legacy)
    def forbidden(*args, **kwargs):
        raise AssertionError("No real provider/search work allowed")
    monkeypatch.setattr(app, "run_web_search", forbidden)
    monkeypatch.setattr(app, "_post_openai_for_attempt", forbidden)
    # The lazy facade lets these regressions exercise the old pipeline BEFORE
    # the new quota module exists. The old app ignores it and fails the checks.
    class LazyQuota:
        def new_request(self):
            from kalillac_accounts.quota import UsageQuota
            return UsageQuota(UsageLimits(1, 3), session_factory=lambda: data["factory"], settings=SETTINGS, clock=lambda: data["now"]).new_request()
    monkeypatch.setattr(app, "_usage_quota", LazyQuota(), raising=False)
    yield data
    engine.dispose()


def request(client, token=TOKEN):
    headers = {"Cookie": SETTINGS.cookie_name + "=" + token} if token else {}
    return client.post("/api/chat", json={"message": MESSAGE}, headers=headers)


@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
def test_successful_account_requests_use_last_slot_once(foundation, monkeypatch, native):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    with TestClient(app.api) as client:
        first = request(client)
        second = request(client)
    assert first.status_code == 200 and first.json()["reply"] == REPLY
    assert second.status_code == 429 and second.json() == {"error": "busy"}
    assert second.headers["cache-control"] == "no-store"
    assert foundation["calls"] == (["native"] if native else ["legacy"])
    with foundation["factory"]() as session:
        row = session.scalar(select(AccountUsageDaily))
        assert (row.successful_chats, row.request_chars, row.response_chars) == (1, len(MESSAGE), len(REPLY))


def test_cookie_identity_database_failure_does_not_grant_anonymous_usage(foundation, monkeypatch):
    def unavailable():
        raise RuntimeError("synthetic database outage")
    foundation["factory"] = unavailable
    with TestClient(app.api) as client:
        response = request(client)
    assert response.status_code == 503 and response.json() == {"error": "service_unavailable"}
    assert response.headers["cache-control"] == "no-store"
    assert not foundation["calls"]



def quota_service(data, limits=UsageLimits(1, 3), **kwargs):
    from kalillac_accounts.quota import UsageQuota
    return UsageQuota(limits, session_factory=lambda: data["factory"], settings=SETTINGS, clock=lambda: data["now"], **kwargs)


def admitted(data, service=None):
    from kalillac_accounts.quota import Admission
    service = service or quota_service(data)
    state = service.new_request()
    state.admission = service.reserve(hash_session_token(TOKEN), state)
    return state


def test_repository_success_is_idempotent_and_releases_capacity(foundation):
    from kalillac_db.repositories.quota import lock_account, reserve_slot, complete_slot, record_sent_slot
    factory, user_id = foundation["factory"], foundation["user_id"]
    with factory() as session, session.begin():
        lock_account(session, user_id)
        lease = reserve_slot(session, user_id, NOW.date(), 1, NOW, timedelta(minutes=15))
    for _ in range(2):
        with factory() as session, session.begin():
            lock_account(session, user_id)
            assert complete_slot(session, lease, NOW, 1) == NOW.date()
            assert record_sent_slot(session, lease) == NOW.date()
    with factory() as session:
        assert session.scalar(select(AccountUsageDaily)).successful_chats == 1


@pytest.mark.parametrize("tier,expires,allowed", [("paid", None, 3), ("paid", NOW, 1), (None, None, 1)])
def test_authoritative_entitlement_selects_allowance(foundation, tier, expires, allowed):
    from kalillac_db.repositories.entitlements import set_entitlement_tier
    from kalillac_db.models import AccountEntitlement
    with foundation["factory"]() as session, session.begin():
        if tier is None:
            session.delete(session.get(AccountEntitlement, foundation["user_id"]))
        else:
            set_entitlement_tier(session, foundation["user_id"], tier, source="test", expires_at=expires)
    with TestClient(app.api) as client:
        results = [request(client).status_code for _ in range(allowed + 1)]
    assert results == [200] * allowed + [429]
    assert len(foundation["calls"]) == allowed


@pytest.mark.parametrize("token", ["unknown-session", "expired", "revoked", "inactive"])
def test_invalid_identity_is_not_anonymous(foundation, token):
    from kalillac_db.models import AccountSession, User
    if token != "unknown-session":
        with foundation["factory"]() as session, session.begin():
            row = session.scalar(select(AccountSession))
            if token == "expired": row.expires_at = NOW
            if token == "revoked": row.revoked_at = NOW
            if token == "inactive": session.get(User, foundation["user_id"]).is_active = False
        token = TOKEN
    with TestClient(app.api) as client:
        result = request(client, token)
    assert result.status_code == 401 and result.json() == {"error": "not_authenticated"}
    assert result.headers["cache-control"] == "no-store"
    assert not foundation["calls"]


def test_anonymous_and_default_off_do_not_use_quota_database(foundation, monkeypatch):
    foundation["factory"] = lambda: (_ for _ in ()).throw(AssertionError("anonymous quota DB access"))
    with TestClient(app.api) as client:
        assert request(client, None).status_code == 200
    monkeypatch.setattr(app, "_usage_quota", None)
    with TestClient(app.api) as client:
        assert request(client).status_code == 200
        assert request(client).status_code == 200
    assert len(foundation["calls"]) == 3


@pytest.mark.parametrize("status", ["provider", "internal"])
def test_unsuccessful_pipeline_releases_reservation(foundation, monkeypatch, status):
    from kalillac_db.models import AccountUsageSlot
    def fail(*args, **kwargs):
        if status == "provider": raise app.ModelProviderUnavailable()
        raise app.ChatInternalError()
    monkeypatch.setattr(app, "invoke_llm", fail)
    with TestClient(app.api) as client:
        response = request(client)
        assert response.status_code == (503 if status == "provider" else 500)
        # Wait for associated quota cleanup; it must not invoke a provider.
        import time
        for _ in range(100):
            with foundation["factory"]() as session:
                if session.scalar(select(AccountUsageSlot.state)) == "released": break
            time.sleep(0.01)
        else: pytest.fail("reservation cleanup did not complete")
    with foundation["factory"]() as session:
        assert session.scalar(select(AccountUsageDaily)) is None
    assert response.headers["cache-control"] == "no-store"


def test_expiry_crash_recovery_and_stale_fencing(foundation):
    from kalillac_db.repositories.quota import QuotaUnavailable
    service = quota_service(foundation, ttl=timedelta(seconds=2))
    old = admitted(foundation, service)
    # A new service represents another worker/restart; it must see pending.
    from kalillac_db.repositories.quota import QuotaBusy
    with pytest.raises(QuotaBusy): admitted(foundation, quota_service(foundation))
    foundation["now"] += timedelta(seconds=2)
    replacement = admitted(foundation, quota_service(foundation))
    assert replacement.admission.reservation.slot == old.admission.reservation.slot
    with pytest.raises(QuotaUnavailable): service.complete(old.admission, old)
    service.release(old.admission)
    # Old cleanup cannot free the replacement lease.
    with pytest.raises(QuotaBusy): admitted(foundation)
    replacement.service.complete(replacement.admission, replacement)
    replacement.service.record_sent(replacement.admission)
    with foundation["factory"]() as session:
        assert session.scalar(select(AccountUsageDaily)).successful_chats == 1


def test_release_is_idempotent_and_same_clock_reuse_is_fenced(foundation):
    from kalillac_db.repositories.quota import QuotaBusy
    old = admitted(foundation)
    old.service.release(old.admission)
    old.service.release(old.admission)
    new = admitted(foundation)
    assert new.admission.reservation.expires_at > old.admission.reservation.expires_at
    old.service.release(old.admission)
    with pytest.raises(QuotaBusy): admitted(foundation)


def test_cross_midnight_charges_completion_day_and_character_meter(foundation, monkeypatch):
    foundation["now"] = NOW.replace(hour=23, minute=59, second=59)
    def cross(messages, **kwargs):
        foundation["calls"].append("legacy")
        foundation["now"] += timedelta(seconds=2)
        return SimpleNamespace(content=REPLY, incomplete=False, incomplete_reason=None)
    monkeypatch.setattr(app, "invoke_llm", cross)
    with TestClient(app.api) as client:
        result = request(client)
        denied = request(client)
    assert result.status_code == 200 and denied.status_code == 429
    with foundation["factory"]() as session:
        rows = session.scalars(select(AccountUsageDaily)).all()
        assert [(r.usage_date, r.successful_chats, r.request_chars, r.response_chars) for r in rows] == [(NOW.date() + timedelta(days=1), 1, len(MESSAGE), len(REPLY))]


def test_midnight_full_new_day_cannot_be_bypassed(foundation):
    from kalillac_db.repositories.quota import QuotaBusy
    from kalillac_db.repositories.usage import increment_daily_usage
    foundation["now"] = NOW.replace(hour=23, minute=59, second=59)
    state = admitted(foundation)
    foundation["now"] += timedelta(seconds=2)
    with foundation["factory"]() as session, session.begin():
        increment_daily_usage(session, foundation["user_id"], foundation["now"].date(), request_chars=0, response_chars=0)
    with pytest.raises(QuotaBusy): state.service.complete(state.admission, state)
    state.service.release(state.admission)


def test_database_rollback_does_not_leave_a_reservation(foundation, monkeypatch):
    service = quota_service(foundation)
    from kalillac_db.models import AccountUsageSlot
    from kalillac_db.repositories.quota import QuotaUnavailable
    def broken(*args):
        raise RuntimeError("synthetic entitlement error")
    monkeypatch.setattr(service, "_limit", broken)
    with pytest.raises(QuotaUnavailable): admitted(foundation, service)
    with foundation["factory"]() as session: assert session.scalars(select(AccountUsageSlot)).all() == []



def install_budget(monkeypatch, enabled, **overrides):
    from test_request_budget_integration import BASE_LIMITS
    from kalillac_routing.request_limits import RequestLimits
    monkeypatch.setattr(app, "_request_limits", RequestLimits(**{**BASE_LIMITS, **overrides}) if enabled else None)


@pytest.mark.parametrize("budgeted", [False, True], ids=["unbudgeted", "budgeted"])
def test_concurrent_http_last_slot_and_no_transaction_during_model(foundation, monkeypatch, budgeted):
    import asyncio, threading
    import httpx
    from kalillac_db.models import AccountUsageSlot
    install_budget(monkeypatch, budgeted)
    entered, finish = threading.Event(), threading.Event()
    def model(messages, **kwargs):
        foundation["calls"].append("legacy")
        # A separate connection can obtain a write transaction during model
        # work; no reservation transaction/lock is held across the call.
        with foundation["factory"]() as session, session.begin():
            assert session.scalar(select(AccountUsageSlot.state)) == "pending"
        entered.set()
        assert finish.wait(3)
        return SimpleNamespace(content=REPLY, incomplete=False, incomplete_reason=None)
    monkeypatch.setattr(app, "invoke_llm", model)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.api), base_url="https://testserver") as client:
            first = asyncio.create_task(client.post("/api/chat", json={"message": MESSAGE}, headers={"Cookie": SETTINGS.cookie_name + "=" + TOKEN}))
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                second = await client.post("/api/chat", json={"message": MESSAGE}, headers={"Cookie": SETTINGS.cookie_name + "=" + TOKEN})
                assert second.status_code == 429 and second.json() == {"error": "busy"}
                assert second.headers["cache-control"] == "no-store"
            finally:
                finish.set()
            assert (await first).status_code == 200
    asyncio.run(scenario())
    assert foundation["calls"] == ["legacy"]


@pytest.mark.parametrize("budgeted", [False, True], ids=["unbudgeted", "budgeted"])
def test_cancelled_pipeline_releases_slot_and_ignores_late_reply(foundation, monkeypatch, budgeted):
    import asyncio, threading
    import httpx
    from kalillac_db.models import AccountUsageSlot
    install_budget(monkeypatch, budgeted)
    entered, finish = threading.Event(), threading.Event()
    def model(messages, **kwargs):
        foundation["calls"].append("legacy")
        entered.set()
        assert finish.wait(3)
        return SimpleNamespace(content=REPLY, incomplete=False, incomplete_reason=None)
    monkeypatch.setattr(app, "invoke_llm", model)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.api), base_url="https://testserver") as client:
            task = asyncio.create_task(client.post("/api/chat", json={"message": MESSAGE}, headers={"Cookie": SETTINGS.cookie_name + "=" + TOKEN}))
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                task.cancel()
                with pytest.raises(asyncio.CancelledError): await task
                for _ in range(200):
                    with foundation["factory"]() as session:
                        state = session.scalar(select(AccountUsageSlot.state))
                    if state == "released": break
                    await asyncio.sleep(0.01)
                else: pytest.fail("cancelled reservation not released")
            finally: finish.set()
            for _ in range(200):
                if app._lookups_outstanding == 0 and not app._session_locks: break
                await asyncio.sleep(0.01)
            with foundation["factory"]() as session:
                assert session.scalar(select(AccountUsageDaily)) is None
            assert (await client.post("/api/chat", json={"message": MESSAGE}, headers={"Cookie": SETTINGS.cookie_name + "=" + TOKEN})).status_code == 200
    asyncio.run(scenario())
    assert foundation["calls"] == ["legacy", "legacy"]


def test_independent_workers_compete_for_exactly_one_slot(foundation):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from kalillac_db.repositories.quota import QuotaBusy
    barrier = threading.Barrier(8)
    def attempt(_):
        barrier.wait(timeout=3)
        try: return admitted(foundation, quota_service(foundation))
        except QuotaBusy: return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    winners[0].service.complete(winners[0].admission, winners[0])
    winners[0].service.record_sent(winners[0].admission)
    with foundation["factory"]() as session:
        assert session.scalar(select(AccountUsageDaily)).successful_chats == 1


def test_handler_stops_during_late_identity_reservation(foundation, monkeypatch):
    import asyncio, threading
    import httpx
    from kalillac_accounts.quota import UsageQuota
    from kalillac_db.models import AccountUsageSlot
    service = quota_service(foundation)
    entered, finish = threading.Event(), threading.Event()
    original = service.reserve
    def slow(*args):
        entered.set()
        assert finish.wait(3)
        return original(*args)
    monkeypatch.setattr(service, "reserve", slow)
    monkeypatch.setattr(app, "_usage_quota", service)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.api), base_url="https://testserver") as client:
            task = asyncio.create_task(client.post("/api/chat", json={"message": MESSAGE}, headers={"Cookie": SETTINGS.cookie_name + "=" + TOKEN}))
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                task.cancel()
                with pytest.raises(asyncio.CancelledError): await task
            finally: finish.set()
            for _ in range(200):
                if app._lookups_outstanding == 0: break
                await asyncio.sleep(0.01)
    asyncio.run(scenario())
    assert not foundation["calls"]
    with foundation["factory"]() as session:
        assert session.scalars(select(AccountUsageSlot)).all() == []


@pytest.mark.parametrize("failure_type", ["http.response.start", "http.response.body"])
def test_failed_response_send_releases_successful_count(foundation, failure_type):
    import asyncio
    class Request:
        cookies = {SETTINGS.cookie_name: TOKEN}
        async def json(self): return {"message": MESSAGE}
    async def scenario():
        response = await app.api_chat(Request())
        assert response.status_code == 200
        async def receive(): return {"type": "http.disconnect"}
        async def send(message):
            if message["type"] == failure_type: raise RuntimeError("synthetic failed send")
        with pytest.raises(RuntimeError): await response({"type": "http"}, receive, send)
        for _ in range(100):
            if app._lookups_outstanding == 0: break
            await asyncio.sleep(0.01)
        with foundation["factory"]() as session:
            row = session.scalar(select(AccountUsageDaily))
            assert row is None or (row.successful_chats, row.request_chars, row.response_chars) == (0, 0, 0)
    asyncio.run(scenario())



def test_cancellation_during_ready_preparation_does_not_count_success(foundation, monkeypatch):
    import asyncio, threading
    import httpx
    service = quota_service(foundation)
    committed, finish = threading.Event(), threading.Event()
    original = service.complete
    def slow(*args):
        result = original(*args)
        committed.set()
        assert finish.wait(3)
        return result
    monkeypatch.setattr(service, "complete", slow)
    monkeypatch.setattr(app, "_usage_quota", service)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.api), base_url="https://testserver") as client:
            task = asyncio.create_task(client.post("/api/chat", json={"message": MESSAGE}, headers={"Cookie": SETTINGS.cookie_name + "=" + TOKEN}))
            try:
                assert await asyncio.to_thread(committed.wait, 3)
                task.cancel()
                with pytest.raises(asyncio.CancelledError): await task
            finally: finish.set()
            for _ in range(200):
                if app._lookups_outstanding == 0: break
                await asyncio.sleep(0.01)
    asyncio.run(scenario())
    with foundation["factory"]() as session:
        row = session.scalar(select(AccountUsageDaily))
        assert row is None or (row.successful_chats, row.request_chars, row.response_chars) == (0, 0, 0)
    assert len(foundation["calls"]) == 1


def test_committed_success_survives_new_service_instance(foundation):
    from kalillac_db.repositories.quota import QuotaBusy
    state = admitted(foundation)
    state.service.complete(state.admission, state)
    state.service.record_sent(state.admission)
    # Crash after durable completion cannot turn already-consumed usage free.
    with pytest.raises(QuotaBusy): admitted(foundation, quota_service(foundation))


def test_expired_pipeline_cannot_return_success(foundation, monkeypatch):
    service = quota_service(foundation, ttl=timedelta(seconds=1))
    monkeypatch.setattr(app, "_usage_quota", service)
    def slow(messages, **kwargs):
        foundation["calls"].append("legacy")
        foundation["now"] += timedelta(seconds=1)
        return SimpleNamespace(content=REPLY, incomplete=False, incomplete_reason=None)
    monkeypatch.setattr(app, "invoke_llm", slow)
    with TestClient(app.api) as client:
        result = request(client)
    assert result.status_code == 503 and result.json() == {"error": "service_unavailable"}
    assert result.headers["cache-control"] == "no-store"
    with foundation["factory"]() as session:
        assert session.scalar(select(AccountUsageDaily)) is None


def test_request_deadline_still_bounds_quota_admission(foundation, monkeypatch):
    import asyncio, threading
    import httpx
    install_budget(monkeypatch, True, deadline_seconds=0.05, queue_wait_seconds=0.01)
    service = quota_service(foundation)
    entered, finish = threading.Event(), threading.Event()
    original = service.reserve
    def slow(*args):
        entered.set()
        assert finish.wait(3)
        return original(*args)
    monkeypatch.setattr(service, "reserve", slow)
    monkeypatch.setattr(app, "_usage_quota", service)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.api), base_url="https://testserver") as client:
            try:
                result = await client.post("/api/chat", json={"message": MESSAGE}, headers={"Cookie": SETTINGS.cookie_name + "=" + TOKEN})
                assert result.status_code == 504 and result.json() == {"error": "request_timeout"}
                assert result.headers["cache-control"] == "no-store"
            finally: finish.set()
            for _ in range(200):
                if app._lookups_outstanding == 0: break
                await asyncio.sleep(0.01)
    asyncio.run(scenario())
    assert not foundation["calls"]


def test_utc_date_is_not_clock_offset_calendar_date(foundation):
    foundation["now"] = datetime(2026, 10, 12, 0, 1, tzinfo=timezone(timedelta(hours=14)))
    state = admitted(foundation)
    assert state.admission.reservation.usage_date == NOW.date()
    assert state.admission.reservation.expires_at.tzinfo == timezone.utc


def test_schema_and_postgresql_lock_shape_are_content_free():
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable
    from kalillac_db.models import AccountUsageSlot
    from kalillac_db.repositories.quota import lock_account
    assert set(AccountUsageSlot.__table__.columns.keys()) == {"user_id", "usage_date", "slot", "expires_at", "state", "completed_date"}
    assert "TIMESTAMP WITH TIME ZONE" in str(CreateTable(AccountUsageSlot.__table__).compile(dialect=postgresql.dialect()))
    seen = []
    class Session:
        def execute(self, stmt):
            seen.append(str(stmt.compile(dialect=postgresql.dialect())))
            return SimpleNamespace(scalar_one=lambda: None)
    lock_account(Session(), uuid.uuid4())
    assert "FOR NO KEY UPDATE" in seen[0] and "kalillac.users" in seen[0]


def test_process_restart_uses_existing_durable_count(foundation, tmp_path):
    import os, subprocess, sys
    state = admitted(foundation)
    state.service.complete(state.admission, state)
    state.service.record_sent(state.admission)
    script = """import socket
attempts=[]
def blocked(*args, **kwargs):
    attempts.append(1); raise AssertionError('network blocked')
socket.socket.connect=socket.socket.connect_ex=socket.getaddrinfo=blocked
from datetime import datetime, timezone
from account_test_db import make_sqlite_file_engine, make_session_factory
from kalillac_accounts.quota import UsageQuota
from kalillac_accounts.usage_limits import UsageLimits
from kalillac_accounts.tokens import hash_session_token
from kalillac_db.repositories.quota import QuotaBusy
import sys
engine=make_sqlite_file_engine(sys.argv[1]); factory=make_session_factory(engine)
service=UsageQuota(UsageLimits(1,3),session_factory=lambda:factory,clock=lambda:datetime(2026,10,11,12,tzinfo=timezone.utc))
try:
 service.reserve(hash_session_token('synthetic-account-session'),service.new_request())
 raise AssertionError('restart granted extra capacity')
except QuotaBusy: print('blocked; outbound='+str(len(attempts)))
finally: engine.dispose()
"""
    env = dict(os.environ, PYTHONPATH=str(__import__("pathlib").Path(__file__).parents[1]) + os.pathsep + str(__import__("pathlib").Path(__file__).parent))
    result = subprocess.run([sys.executable, "-B", "-c", script, str(tmp_path)], capture_output=True, text=True, env=env, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "blocked; outbound=0"



def test_lease_expiring_in_queue_never_starts_provider(foundation, monkeypatch):
    import asyncio, threading
    import httpx
    from kalillac_db.models import AccountUsageSlot
    monkeypatch.setattr(app, "_usage_quota", quota_service(foundation, ttl=timedelta(seconds=1)))
    install_budget(monkeypatch, False)
    entered, finish = threading.Event(), threading.Event()
    def model(messages, **kwargs):
        foundation["calls"].append("legacy")
        entered.set()
        assert finish.wait(3)
        return SimpleNamespace(content=REPLY, incomplete=False, incomplete_reason=None)
    monkeypatch.setattr(app, "invoke_llm", model)
    async def scenario():
        sid = app.resolve_session_id(None)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.api), base_url="https://testserver") as client:
            first = asyncio.create_task(client.post("/api/chat", json={"message": MESSAGE, "session_id": sid}))
            second = None
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                second = asyncio.create_task(client.post("/api/chat", json={"message": MESSAGE, "session_id": sid}, headers={"Cookie": SETTINGS.cookie_name + "=" + TOKEN}))
                for _ in range(100):
                    with foundation["factory"]() as session:
                        if session.scalar(select(AccountUsageSlot.state)) == "pending": break
                    await asyncio.sleep(0.01)
                else: pytest.fail("queued reservation not admitted")
                foundation["now"] += timedelta(seconds=2)
            finally: finish.set()
            assert (await first).status_code == 200
            assert (await second).status_code == 503
    asyncio.run(scenario())
    assert foundation["calls"] == ["legacy"]



def test_settlement_database_outage_is_fail_closed_and_expiry_recovers(foundation, monkeypatch):
    from kalillac_db.models import AccountUsageSlot
    from kalillac_db.repositories.quota import QuotaBusy
    stable = foundation["factory"]
    def unavailable(): raise RuntimeError("synthetic settlement database outage")
    def model(messages, **kwargs):
        foundation["calls"].append("legacy")
        foundation["factory"] = unavailable
        return SimpleNamespace(content=REPLY, incomplete=False, incomplete_reason=None)
    monkeypatch.setattr(app, "invoke_llm", model)
    with TestClient(app.api) as client:
        result = request(client)
        assert result.status_code == 503 and result.json() == {"error": "service_unavailable"}
        assert result.headers["cache-control"] == "no-store"
        import time
        for _ in range(100):
            if app._lookups_outstanding == 0: break
            time.sleep(0.01)
    with stable() as session:
        assert session.scalar(select(AccountUsageDaily)) is None
        assert session.scalar(select(AccountUsageSlot.state)) == "pending"
    foundation["factory"] = stable
    with pytest.raises(QuotaBusy): admitted(foundation)
    foundation["now"] += timedelta(minutes=16)
    assert admitted(foundation).admission.reservation is not None
    assert len(foundation["calls"]) == 1


def test_failed_after_response_character_meter_does_not_undo_success(foundation, monkeypatch):
    def unavailable(): raise RuntimeError("synthetic character meter outage")
    monkeypatch.setattr(app._usage_meter, "_session_factory", unavailable)
    with TestClient(app.api) as client:
        result = request(client)
        assert result.status_code == 200 and result.json()["reply"] == REPLY
        assert request(client).status_code == 429
    with foundation["factory"]() as session:
        row = session.scalar(select(AccountUsageDaily))
        assert (row.successful_chats, row.request_chars, row.response_chars) == (1, 0, 0)


def test_malformed_requests_do_not_reserve_or_resolve_identity(foundation):
    foundation["factory"] = lambda: (_ for _ in ()).throw(AssertionError("validation used quota database"))
    with TestClient(app.api) as client:
        result = client.post("/api/chat", json={"message": ""}, headers={"Cookie": SETTINGS.cookie_name + "=" + TOKEN})
    assert result.status_code == 422 and result.json() == {"error": "empty_message"}
    assert not foundation["calls"]


def test_quota_reservations_are_isolated_between_accounts(foundation):
    from kalillac_db.repositories.quota import QuotaBusy
    other_id = uuid.uuid4()
    other_token = "another-synthetic-session"
    with foundation["factory"]() as session, session.begin():
        session.add(User(id=other_id, email="other-quota@example.test", password_hash="synthetic-hash"))
        session.flush()
        create_default_entitlement(session, other_id)
        session.add(AccountSession(user_id=other_id, token_hash=hash_session_token(other_token), expires_at=NOW + timedelta(days=14)))
    first = admitted(foundation)
    service = quota_service(foundation)
    second = service.new_request()
    second.admission = service.reserve(hash_session_token(other_token), second)
    assert first.admission.user_id != second.admission.user_id
    first.service.release(first.admission)
    with pytest.raises(QuotaBusy): service.reserve(hash_session_token(other_token), service.new_request())
    assert admitted(foundation).admission.user_id == foundation["user_id"]



def test_upgrade_does_not_invalidate_already_reserved_free_slot(foundation):
    from kalillac_db.repositories.entitlements import set_entitlement_tier
    first = admitted(foundation)
    with foundation["factory"]() as session, session.begin():
        set_entitlement_tier(session, foundation["user_id"], "paid", source="test")
    later = [admitted(foundation), admitted(foundation)]
    for state in [first, *later]:
        assert state.service.complete(state.admission, state) == NOW.date()
        state.service.record_sent(state.admission)
    with foundation["factory"]() as session:
        assert session.scalar(select(AccountUsageDaily)).successful_chats == 3


def test_downgrade_honors_existing_leases_but_no_new_paid_slots(foundation):
    from kalillac_db.repositories.entitlements import set_entitlement_tier
    from kalillac_db.repositories.quota import QuotaBusy
    with foundation["factory"]() as session, session.begin():
        set_entitlement_tier(session, foundation["user_id"], "paid", source="test")
    pending = [admitted(foundation) for _ in range(3)]
    with foundation["factory"]() as session, session.begin():
        set_entitlement_tier(session, foundation["user_id"], "free", source="test")
    with pytest.raises(QuotaBusy): admitted(foundation)
    for state in pending:
        state.service.complete(state.admission, state)
        state.service.record_sent(state.admission)
    with pytest.raises(QuotaBusy): admitted(foundation)



@pytest.mark.parametrize("enabled", [False, True])
def test_startup_feature_gate_is_lazy_and_explicit(enabled):
    import os, subprocess, sys
    env = dict(os.environ)
    env.update({
        "KALILLAC_USAGE_ENFORCEMENT_ENABLED": "true" if enabled else "false",
        "KALILLAC_ACCOUNTS_ENABLED": "true" if enabled else "false",
        "KALILLAC_DB_ENABLED": "true" if enabled else "false",
        "KALILLAC_USAGE_METERING_ENABLED": "true" if enabled else "false",
        "KALILLAC_USAGE_LIMIT_FREE_DAILY": "2" if enabled else "malformed-unused",
        "KALILLAC_USAGE_LIMIT_PAID_DAILY": "5" if enabled else "malformed-unused",
        "KALILLAC_BILLING_ENABLED": "false", "KALILLAC_REQUEST_BUDGET_ENABLED": "false",
        "OPENAI_API_KEY": "", "TAVILY_API_KEY": "", "BRAVE_API_KEY": "", "DEBUG_MODE": "false",
    })
    script = """import json,socket,sys
attempts=[]
def block(*args,**kwargs): attempts.append(1);raise AssertionError('network blocked')
socket.socket.connect=socket.socket.connect_ex=socket.getaddrinfo=block
import app_fastapi_candidate as app
from kalillac_db import engine
print(json.dumps({'enabled':app._usage_quota is not None,'limits':None if app._usage_limits is None else [app._usage_limits.free_daily_chats,app._usage_limits.paid_daily_chats],'engine':engine._ENGINE is not None,'outbound':len(attempts),'quota_imported':'kalillac_accounts.quota' in sys.modules}))
"""
    result = subprocess.run([sys.executable, "-B", "-c", script], cwd=str(__import__("pathlib").Path(__file__).parents[1]), env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"enabled": enabled, "limits": [2, 5] if enabled else None, "engine": False, "outbound": 0, "quota_imported": enabled}


def test_default_off_provider_payload_is_unchanged(foundation, monkeypatch):
    monkeypatch.setattr(app, "_usage_quota", None)
    seen = []
    def capture(messages, **kwargs):
        seen.append(([m.content for m in messages], kwargs))
        return SimpleNamespace(content=REPLY, incomplete=False, incomplete_reason=None)
    monkeypatch.setattr(app, "invoke_llm", capture)
    with TestClient(app.api) as client:
        assert request(client).status_code == 200
    assert seen[0][1] == {"max_tokens": None}
    assert MESSAGE in seen[0][0][-1]
    assert TOKEN not in "\n".join(seen[0][0])
    assert str(foundation["user_id"]) not in "\n".join(seen[0][0])



def test_cancel_after_lease_validation_never_begins_provider(foundation, monkeypatch):
    import asyncio, threading
    import httpx
    service = quota_service(foundation)
    validated, finish = threading.Event(), threading.Event()
    original = service.validate
    def pause(*args):
        result = original(*args)
        validated.set()
        assert finish.wait(3)
        return result
    monkeypatch.setattr(service, "validate", pause)
    monkeypatch.setattr(app, "_usage_quota", service)
    install_budget(monkeypatch, False)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.api), base_url="https://testserver") as client:
            task = asyncio.create_task(client.post("/api/chat", json={"message": MESSAGE}, headers={"Cookie": SETTINGS.cookie_name + "=" + TOKEN}))
            try:
                assert await asyncio.to_thread(validated.wait, 3)
                task.cancel()
                with pytest.raises(asyncio.CancelledError): await task
            finally: finish.set()
            for _ in range(200):
                if not app._session_locks and app._lookups_outstanding == 0: break
                await asyncio.sleep(0.01)
    asyncio.run(scenario())
    assert not foundation["calls"]



def test_postgresql_transaction_uses_fresh_snapshots_before_locking():
    service = None
    seen = []
    class Session:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def begin(self): return self
        def get_bind(self): return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
        def execute(self, statement): seen.append(str(statement))
    from kalillac_accounts.quota import UsageQuota
    service = UsageQuota(UsageLimits(1, 3), session_factory=lambda: Session, settings=SETTINGS)
    assert service._transaction(lambda session: "ok") == "ok"
    assert seen == ["SET TRANSACTION ISOLATION LEVEL READ COMMITTED", "SET LOCAL lock_timeout = '3s'", "SET LOCAL statement_timeout = '3s'"]


@pytest.mark.parametrize("budgeted", [False, True])
def test_anonymous_does_not_need_database_lookup_capacity(foundation, monkeypatch, budgeted):
    install_budget(monkeypatch, budgeted)
    monkeypatch.setattr(app, "_lookups_outstanding", app.MAX_QUEUED_CHATS)
    foundation["factory"] = lambda: (_ for _ in ()).throw(AssertionError("anonymous DB access"))
    with TestClient(app.api) as client:
        result = request(client, None)
    assert result.status_code == 200 and result.json()["reply"] == REPLY
    assert result.headers["cache-control"] == "no-store"



def test_failed_send_with_full_lookup_capacity_never_records_success(foundation, monkeypatch):
    import asyncio
    class Request:
        cookies = {SETTINGS.cookie_name: TOKEN}
        async def json(self): return {"message": MESSAGE}
    async def scenario():
        response = await app.api_chat(Request())
        monkeypatch.setattr(app, "_lookups_outstanding", app.MAX_QUEUED_CHATS)
        async def receive(): return {"type": "http.disconnect"}
        async def send(message): raise RuntimeError("synthetic failed send")
        with pytest.raises(RuntimeError): await response({"type": "http"}, receive, send)
        with foundation["factory"]() as session:
            row = session.scalar(select(AccountUsageDaily))
            assert row is None or row.successful_chats == 0
    asyncio.run(scenario())



def test_ready_crash_expiry_holds_capacity_without_false_success(foundation):
    from kalillac_db.models import AccountUsageSlot
    from kalillac_db.repositories.quota import QuotaBusy
    state = admitted(foundation)
    state.service.complete(state.admission, state)
    # A crash after preparation cannot prove whether a body was sent.
    foundation["now"] += timedelta(minutes=16)
    with pytest.raises(QuotaBusy): admitted(foundation, quota_service(foundation))
    with foundation["factory"]() as session:
        row = session.scalar(select(AccountUsageDaily))
        assert row is None or row.successful_chats == 0
        assert session.scalar(select(AccountUsageSlot.state)) in {"ready", "uncertain"}
    state.service.record_sent(state.admission)
    state.service.record_sent(state.admission)
    with foundation["factory"]() as session:
        assert session.scalar(select(AccountUsageDaily)).successful_chats == 1


def test_full_after_send_capacity_does_not_make_unknown_delivery_free(foundation, monkeypatch):
    import asyncio
    from kalillac_db.models import AccountUsageSlot
    from kalillac_db.repositories.quota import QuotaBusy
    class Request:
        cookies = {SETTINGS.cookie_name: TOKEN}
        async def json(self): return {"message": MESSAGE}
    async def scenario():
        response = await app.api_chat(Request())
        monkeypatch.setattr(app, "_lookups_outstanding", app.MAX_QUEUED_CHATS)
        async def receive(): return {"type": "http.disconnect"}
        async def send(message): pass
        await response({"type": "http"}, receive, send)
    asyncio.run(scenario())
    with pytest.raises(QuotaBusy): admitted(foundation)
    with foundation["factory"]() as session:
        row = session.scalar(select(AccountUsageDaily))
        assert row.successful_chats == 0
        assert (row.request_chars, row.response_chars) == (len(MESSAGE), len(REPLY))
        assert session.scalar(select(AccountUsageSlot.state)) == "ready"


@pytest.mark.parametrize("completion_order", [(0, 1, 2), (1, 2, 0)])
def test_repository_free_upgrade_downgrade_honors_all_same_day_leases(foundation, completion_order):
    from kalillac_db.repositories.quota import (
        lock_account, reserve_slot, complete_slot, record_sent_slot, QuotaBusy,
    )
    factory, user_id = foundation["factory"], foundation["user_id"]
    leases = []
    # Synthetic free allowance, then two slots admitted under an upgrade.
    for limit in (1, 3, 3):
        with factory() as session, session.begin():
            lock_account(session, user_id)
            leases.append(reserve_slot(session, user_id, NOW.date(), limit, NOW, timedelta(minutes=15)))
    # Downgrade: no fresh reservation, but every existing lease must survive.
    with pytest.raises(QuotaBusy):
        with factory() as session, session.begin():
            lock_account(session, user_id)
            reserve_slot(session, user_id, NOW.date(), 1, NOW, timedelta(minutes=15))
    for index in completion_order:
        for _ in range(2):
            with factory() as session, session.begin():
                lock_account(session, user_id)
                assert complete_slot(session, leases[index], NOW, 1) == NOW.date()
                assert record_sent_slot(session, leases[index]) == NOW.date()
    with factory() as session:
        assert session.scalar(select(AccountUsageDaily)).successful_chats == 3
    with pytest.raises(QuotaBusy):
        with factory() as session, session.begin():
            lock_account(session, user_id)
            reserve_slot(session, user_id, NOW.date(), 1, NOW, timedelta(minutes=15))


@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
@pytest.mark.parametrize("budgeted", [False, True], ids=["unbudgeted", "budgeted"])
def test_pipeline_free_upgrade_downgrade_preserves_pending_responses(foundation, monkeypatch, native, budgeted):
    import asyncio, threading
    import httpx
    from kalillac_db.repositories.entitlements import set_entitlement_tier
    from kalillac_db.models import AccountUsageSlot
    install_budget(monkeypatch, budgeted)
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    entered = [threading.Event() for _ in range(3)]
    finish = [threading.Event() for _ in range(3)]
    counter_lock = threading.Lock()
    def model(*args, **kwargs):
        with counter_lock:
            index = len(foundation["calls"])
            foundation["calls"].append("native" if native else "legacy")
        entered[index].set()
        assert finish[index].wait(5)
        if native:
            return {"output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": REPLY}]}]}
        return SimpleNamespace(content=REPLY, incomplete=False, incomplete_reason=None)
    monkeypatch.setattr(app, "_invoke_openai_native_tools" if native else "invoke_llm", model)
    def tier(value):
        with foundation["factory"]() as session, session.begin():
            set_entitlement_tier(session, foundation["user_id"], value, source="test")
    async def scenario():
        tasks = []
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.api), base_url="https://testserver") as client:
            async def chat():
                return await client.post("/api/chat", json={"message": MESSAGE}, headers={"Cookie": SETTINGS.cookie_name + "=" + TOKEN})
            try:
                tasks.append(asyncio.create_task(chat()))
                assert await asyncio.to_thread(entered[0].wait, 3)
                tier("paid")
                for index in (1, 2):
                    tasks.append(asyncio.create_task(chat()))
                    assert await asyncio.to_thread(entered[index].wait, 3)
                tier("free")
                with foundation["factory"]() as session:
                    assert list(session.scalars(select(AccountUsageSlot.state))) == ["pending"] * 3
                denied = await chat()
                assert denied.status_code == 429 and denied.json() == {"error": "busy"}
                assert denied.headers["cache-control"] == "no-store"
                # Complete the original free lease first while both paid leases
                # still hold capacity; each delivered body is counted once.
                for index, task in enumerate(tasks):
                    finish[index].set()
                    response = await task
                    assert response.status_code == 200 and response.json()["reply"] == REPLY
                denied = await chat()
                assert denied.status_code == 429 and denied.json() == {"error": "busy"}
            finally:
                for event in finish: event.set()
                await asyncio.gather(*tasks, return_exceptions=True)
    asyncio.run(scenario())
    assert foundation["calls"] == ["native" if native else "legacy"] * 3
    with foundation["factory"]() as session:
        row = session.scalar(select(AccountUsageDaily))
        assert (row.successful_chats, row.request_chars, row.response_chars) == (3, 3 * len(MESSAGE), 3 * len(REPLY))
        assert list(session.scalars(select(AccountUsageSlot.state))) == ["completed"] * 3
