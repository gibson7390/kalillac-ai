"""Slice 2 production-route admission, accounting and recovery race contracts."""
import asyncio
from collections import Counter
from datetime import timedelta
import hashlib
import threading
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import delete, event, select, update

from account_test_db import make_session_factory, make_sqlite_file_engine
from recovery_test_support import ENV, NOW, PASSWORD, NEW_PASSWORD, SETTINGS, FakeEmail, add_user, add_sessions, rows
from kalillac_accounts.passwords import hash_password
from kalillac_accounts.password_work import PasswordWorkGate
from kalillac_accounts.registration_rate_limit import RegistrationRateLimiter
from kalillac_accounts.recovery_tokens import hash_recovery_token, new_recovery_token
from kalillac_accounts.request_guard import UnsafeRequestGuard
from kalillac_db.models import Base, User, AccountRecoveryToken, AccountSession
import kalillac_accounts.router as accounts
import kalillac_accounts.recovery_router as recovery

ORIGIN = {"Origin": "https://testserver", "CF-Connecting-IP": "192.0.2.10"}


@pytest.fixture
def db(tmp_path):
    engine = make_sqlite_file_engine(str(tmp_path))
    Base.metadata.create_all(engine)
    yield make_session_factory(engine)
    engine.dispose()


def app_for(db, *, gate=None, limiter=None, gateway=None, clock=lambda: NOW):
    app = FastAPI()
    gateway = gateway if gateway is not None else FakeEmail()
    app.include_router(accounts.build_account_router(
        session_factory=lambda: db, settings=SETTINGS,
        password_gate=gate, registration_limiter=limiter,
    ))
    app.include_router(recovery.build_recovery_router(
        session_factory=lambda: db, settings=SETTINGS, gateway=gateway,
        clock=clock, environ=ENV, password_gate=gate,
    ))
    app.add_middleware(UnsafeRequestGuard, origin="https://testserver")
    return app


def seed_token(db, user_id, *, expires=NOW + timedelta(minutes=30), consumed=None):
    token = new_recovery_token()
    with db() as session, session.begin():
        session.add(AccountRecoveryToken(
            user_id=user_id, purpose="password_reset", token_hash=hash_recovery_token(token),
            created_at=NOW - timedelta(hours=1), expires_at=expires, consumed_at=consumed,
        ))
    return token


async def checkpoint():
    future = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(future.set_result, None)
    await future


def test_aggregate_one_across_all_current_argon2_paths(db, monkeypatch):
    real_user = add_user(db)
    rehash_user = add_user(db, "rehash@example.com")
    token = seed_token(db, real_user)
    with db() as session:
        rehash_value = session.get(User, rehash_user).password_hash
    assert accounts.PASSWORD_WORK_GATE is recovery.PASSWORD_WORK_GATE
    async def scenario():
        loop = asyncio.get_running_loop()
        entered, queued = asyncio.Event(), asyncio.Event()
        release = threading.Event()
        lock = threading.Lock()
        counts = Counter()
        active = peak = waiting = 0
        async def wait(future, *, timeout):
            nonlocal waiting
            assert timeout == 2.0
            waiting += 1
            if waiting == 4:
                queued.set()
            # Concurrency is controlled by barriers, independently of host
            # CPU speed. The production timeout path has its own timer test.
            return await future
        gate = PasswordWorkGate(wait_for=wait)
        original_hash, original_verify = accounts.hash_password, accounts.verify_password
        def observed(label, function, *args):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                counts[label] += 1
            loop.call_soon_threadsafe(entered.set)
            try:
                assert release.wait(10)
                return function(*args)
            finally:
                with lock:
                    active -= 1
        monkeypatch.setattr(accounts, "hash_password", lambda p: observed(
            "registration" if p == NEW_PASSWORD else "rehash", original_hash, p))
        monkeypatch.setattr(accounts, "verify_password", lambda h, p: observed(
            "dummy" if h is None else "verify", original_verify, h, p))
        monkeypatch.setattr(accounts, "needs_rehash", lambda h: h == rehash_value)
        monkeypatch.setattr(recovery, "hash_password", lambda p: observed("recovery", original_hash, p))
        app = app_for(db, gate=gate)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver",
                                     headers=ORIGIN) as client:
            first = asyncio.create_task(client.post("/api/account/register", json={
                "email": "registered@example.com", "password": NEW_PASSWORD}))
            tasks = [first]
            try:
                await asyncio.wait_for(entered.wait(), 5)
                for email in ("person@example.com", "unknown@example.com", "rehash@example.com"):
                    tasks.append(asyncio.create_task(client.post("/api/account/login", json={
                        "email": email, "password": PASSWORD})))
                tasks.append(asyncio.create_task(client.post("/api/account/recovery/confirm", json={
                    "token": token, "password": NEW_PASSWORD})))
                await asyncio.wait_for(queued.wait(), 5)
                assert peak == 1 and sum(counts.values()) == 1
            finally:
                release.set()
            responses = await asyncio.gather(*tasks)
            assert [r.status_code for r in responses] == [201, 200, 401, 200, 200]
        assert peak == 1 and active == 0
        assert counts == {"registration": 1, "verify": 2, "dummy": 1, "rehash": 1, "recovery": 1}
        assert await gate.run(lambda: True)
    asyncio.run(scenario())


@pytest.mark.parametrize("outcome,status", [("success", 201), ("duplicate", 409), ("database", 500)])
def test_registration_all_started_work_is_charged_without_success_reset(db, monkeypatch, outcome, status):
    stored_hash = hash_password(PASSWORD)
    if outcome == "duplicate":
        add_user(db)
    clock = [0.0]
    limiter = RegistrationRateLimiter(clock=lambda: clock[0])
    calls = []
    monkeypatch.setattr(accounts, "hash_password", lambda p: calls.append(p) or stored_hash)
    if outcome == "database":
        def fail(*args):
            raise RuntimeError("synthetic database failure")
        monkeypatch.setattr(accounts, "create_user", fail)
    with TestClient(app_for(db, limiter=limiter), base_url="https://testserver",
                    raise_server_exceptions=False, headers=ORIGIN) as client:
        for i in range(5):
            email = "person@example.com" if outcome == "duplicate" else f"new{i}@example.com"
            assert client.post("/api/account/register", json={"email": email, "password": PASSWORD}).status_code == status
        body = {"email": "person@example.com" if outcome == "duplicate" else "sixth@example.com", "password": PASSWORD}
        response = client.post("/api/account/register", json=body)
        assert response.status_code == 429 and response.json() == {"error": "too_many_attempts"}
        assert response.headers["retry-after"] == "900" and response.headers["cache-control"] == "no-store"
        clock[0] = 899.1
        assert client.post("/api/account/register", json=body).headers["retry-after"] == "1"
        clock[0] = 900.0
        assert client.post("/api/account/register", json=body).status_code == status
    assert len(calls) == 6
    assert list(limiter._store._buckets) == [hashlib.sha256(b"192.0.2.10").hexdigest()]


def test_registration_has_no_email_bucket_and_uses_existing_client_trust_contract(db, monkeypatch):
    add_user(db)
    calls = []
    stored_hash = hash_password(PASSWORD)
    monkeypatch.setattr(accounts, "hash_password", lambda p: calls.append(p) or stored_hash)
    limiter = RegistrationRateLimiter()
    with TestClient(app_for(db, limiter=limiter), base_url="https://testserver", headers=ORIGIN) as client:
        for i in range(8):
            response = client.post("/api/account/register", headers={"CF-Connecting-IP": f"192.0.2.{i+1}"},
                                   json={"email": "person@example.com", "password": PASSWORD})
            assert response.status_code == 409
    assert len(calls) == 8 and len(limiter._store._buckets) == 8


def test_registration_gate_timeouts_are_exact_busy_and_uncharged(db, monkeypatch):
    async def timeout(future, *, timeout):
        assert timeout == 2.0
        future.cancel()
        raise TimeoutError()
    gate = PasswordWorkGate(wait_for=timeout)
    limiter = RegistrationRateLimiter()
    calls = []
    stored_hash = hash_password(PASSWORD)
    monkeypatch.setattr(accounts, "hash_password", lambda p: calls.append(p) or stored_hash)
    async def scenario():
        await gate.acquire()
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(db, gate=gate, limiter=limiter)),
                                         base_url="https://testserver", headers=ORIGIN) as client:
                for _ in range(8):
                    response = await client.post("/api/account/register",
                        json={"email": "new@example.com", "password": PASSWORD})
                    assert response.status_code == 503 and response.json() == {"error": "busy"}
                    assert response.headers["cache-control"] == "no-store"
                assert calls == [] and rows(db, User) == []
        finally:
            gate.release()
    asyncio.run(scenario())
    decision, attempt = limiter.begin("192.0.2.10")
    assert decision.allowed
    attempt.release()


@pytest.mark.parametrize("started", [False, True])
def test_registration_cancellation_accounting_and_actual_worker_lifetime(db, monkeypatch, started):
    async def scenario():
        loop = asyncio.get_running_loop()
        entered, queued = asyncio.Event(), asyncio.Event()
        release = threading.Event()
        calls = []
        async def wait(future, *, timeout):
            queued.set()
            return await future
        gate = PasswordWorkGate(wait_for=wait)
        limiter = RegistrationRateLimiter()
        def hash_work(password):
            calls.append(password)
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5)
            return "synthetic-not-stored-hash"
        monkeypatch.setattr(accounts, "hash_password", hash_work)
        if not started:
            await gate.acquire()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(db, gate=gate, limiter=limiter)),
                                     base_url="https://testserver", headers=ORIGIN) as client:
            task = asyncio.create_task(client.post("/api/account/register",
                json={"email": "new@example.com", "password": PASSWORD}))
            try:
                await asyncio.wait_for((entered if started else queued).wait(), 5)
                task.cancel()
                await checkpoint()
                if started:
                    assert not task.done() and len(calls) == 1
                else:
                    assert calls == []
            finally:
                if started:
                    release.set()
                else:
                    gate.release()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert rows(db, User) == []
        buckets = list(limiter._store._buckets.values())
        assert len(buckets) == int(started)
        assert sum(bucket.in_flight for bucket in buckets) == 0
        assert sum(len(bucket.failures) for bucket in buckets) == int(started)
        assert await gate.run(lambda: True)
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["malformed", "unknown", "expired", "consumed", "reissued",
                                  "wrong-purpose", "inactive", "missing-account"])
def test_recovery_prevalidation_rejections_do_zero_password_work(db, monkeypatch, kind):
    user = add_user(db)
    token = seed_token(db, user)
    if kind == "malformed":
        token = "bad"
    elif kind == "unknown":
        token = new_recovery_token()
    else:
        with db() as session, session.begin():
            if kind == "expired":
                session.execute(update(AccountRecoveryToken).values(expires_at=NOW))
            elif kind in {"consumed", "reissued"}:
                session.execute(update(AccountRecoveryToken).values(consumed_at=NOW))
            elif kind == "wrong-purpose":
                session.connection().exec_driver_sql("PRAGMA ignore_check_constraints=ON")
                session.execute(update(AccountRecoveryToken).values(purpose="other"))
            elif kind == "inactive":
                session.execute(update(User).values(is_active=False))
            elif kind == "missing-account":
                session.execute(delete(User).where(User.id == user))
    before = {m: rows(db, m) for m in (User, AccountRecoveryToken, AccountSession)}
    def forbidden(*args, **kwargs):
        pytest.fail("prevalidation rejection reached password work")
    monkeypatch.setattr(recovery, "hash_password", forbidden)
    gate = PasswordWorkGate()
    monkeypatch.setattr(gate, "run", forbidden)
    gateway = FakeEmail()
    with TestClient(app_for(db, gate=gate, gateway=gateway), base_url="https://testserver", headers=ORIGIN) as client:
        response = client.post("/api/account/recovery/confirm", json={"token": token, "password": NEW_PASSWORD})
    assert response.status_code == 400 and response.json() == {"error": "invalid_or_expired_token"}
    assert {m: rows(db, m) for m in before} == before and gateway.changed == []


def test_recovery_precheck_transaction_ends_before_gate_wait_and_hash(db, monkeypatch):
    user = add_user(db)
    token = seed_token(db, user)
    active = set()
    engine = db.kw["bind"]
    @event.listens_for(engine, "begin")
    def begin(connection):
        active.add(id(connection))
    @event.listens_for(engine, "commit")
    @event.listens_for(engine, "rollback")
    def end(connection):
        active.discard(id(connection))
    order = []
    class CheckedGate(PasswordWorkGate):
        async def acquire(self):
            assert not active
            order.append("gate")
            await super().acquire()
    real = recovery.hash_password
    def checked(password):
        assert not active
        order.append("hash")
        return real(password)
    monkeypatch.setattr(recovery, "hash_password", checked)
    gateway = FakeEmail()
    with TestClient(app_for(db, gate=CheckedGate(), gateway=gateway), base_url="https://testserver", headers=ORIGIN) as client:
        response = client.post("/api/account/recovery/confirm", json={"token": token, "password": NEW_PASSWORD})
    assert response.status_code == 200 and order == ["gate", "hash"] and not active
    assert gateway.changed == ["person@example.com"]


@pytest.mark.parametrize("change", ["expiry", "consumed", "reissued", "inactive", "missing-account"])
def test_final_recovery_transaction_rechecks_changes_after_precheck(db, monkeypatch, change):
    user = add_user(db)
    add_sessions(db, user)
    token = seed_token(db, user)
    original = rows(db, User)[0]["password_hash"]
    sessions = rows(db, AccountSession)
    clock = [NOW]
    calls = []
    replacement = hash_password(NEW_PASSWORD)
    monkeypatch.setattr(recovery, "hash_password", lambda p: calls.append(p) or replacement)
    class ChangingGate(PasswordWorkGate):
        async def acquire(self):
            with db() as session, session.begin():
                if change == "expiry":
                    clock[0] = NOW + timedelta(minutes=30)
                elif change in {"consumed", "reissued"}:
                    session.execute(update(AccountRecoveryToken).values(consumed_at=NOW))
                elif change == "inactive":
                    session.execute(update(User).values(is_active=False))
                else:
                    session.execute(delete(User).where(User.id == user))
            await super().acquire()
    gateway = FakeEmail()
    with TestClient(app_for(db, gate=ChangingGate(), gateway=gateway, clock=lambda: clock[0]),
                    base_url="https://testserver", headers=ORIGIN) as client:
        response = client.post("/api/account/recovery/confirm", json={"token": token, "password": NEW_PASSWORD})
    assert response.status_code == 400 and response.json() == {"error": "invalid_or_expired_token"}
    assert calls == [NEW_PASSWORD] and gateway.changed == []
    if change != "missing-account":
        assert rows(db, User)[0]["password_hash"] == original
        assert rows(db, AccountSession) == sessions


@pytest.mark.parametrize("different", [False, True])
def test_route_recovery_concurrent_prechecks_do_not_replace_final_lock(db, different):
    user = add_user(db)
    add_sessions(db, user)
    first = seed_token(db, user)
    second = seed_token(db, user) if different else first
    gateway = FakeEmail()
    async def scenario():
        both = asyncio.Event()
        arrivals = 0
        class BarrierGate(PasswordWorkGate):
            async def run(self, *args, **kwargs):
                nonlocal arrivals
                arrivals += 1
                if arrivals == 2:
                    both.set()
                await asyncio.wait_for(both.wait(), 5)
                return await super().run(*args, **kwargs)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(db, gate=BarrierGate(), gateway=gateway)),
                                     base_url="https://testserver", headers=ORIGIN) as client:
            responses = await asyncio.gather(*[
                client.post("/api/account/recovery/confirm", json={"token": t, "password": NEW_PASSWORD+str(i)})
                for i, t in enumerate((first, second))
            ])
        assert sorted(r.status_code for r in responses) == [200, 400]
    asyncio.run(scenario())
    assert gateway.changed == ["person@example.com"]
    assert all(r["consumed_at"] is not None for r in rows(db, AccountRecoveryToken))
    assert all(r["revoked_at"] is not None for r in rows(db, AccountSession))


@pytest.mark.parametrize("path,body,status", [
    ("/api/account/register", {"email": "bad", "password": PASSWORD}, 422),
    ("/api/account/register", {"email": "new@example.com", "password": "short"}, 422),
    ("/api/account/login", {"email": "bad", "password": PASSWORD}, 422),
    ("/api/account/login", {"email": "person@example.com", "password": "x"*257}, 401),
    ("/api/account/recovery/confirm", {"token": "bad", "password": PASSWORD}, 400),
    ("/api/account/recovery/confirm", {"token": "A"*43, "password": "short"}, 422),
])
def test_cheap_handler_rejections_do_not_hash(db, monkeypatch, path, body, status):
    def forbidden(*args, **kwargs):
        pytest.fail("cheap rejection reached Argon2")
    for module in (accounts, recovery):
        monkeypatch.setattr(module, "hash_password", forbidden)
    monkeypatch.setattr(accounts, "verify_password", forbidden)
    with TestClient(app_for(db), base_url="https://testserver", headers=ORIGIN) as client:
        assert client.post(path, json=body).status_code == status


def test_login_rate_rejection_and_busy_reservations_preserve_accounting(db, monkeypatch):
    calls = []
    monkeypatch.setattr(accounts, "verify_password", lambda *a: calls.append(True) or False)
    with TestClient(app_for(db), base_url="https://testserver", headers=ORIGIN) as client:
        for _ in range(5):
            assert client.post("/api/account/login", json={"email": "unknown@example.com", "password": PASSWORD}).status_code == 401
        response = client.post("/api/account/login", json={"email": "unknown@example.com", "password": PASSWORD})
        assert response.status_code == 429 and len(calls) == 5


def test_busy_login_releases_limiter_reservations_without_password_work(db, monkeypatch):
    calls = []
    monkeypatch.setattr(accounts, "verify_password", lambda *a: calls.append(True) or False)
    async def timeout(future, *, timeout):
        assert timeout == 2.0
        future.cancel()
        raise TimeoutError()
    async def scenario():
        gate = PasswordWorkGate(wait_for=timeout)
        await gate.acquire()
        app = app_for(db, gate=gate)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="https://testserver", headers=ORIGIN) as client:
            body = {"email": "unknown@example.com", "password": PASSWORD}
            try:
                for _ in range(8):
                    response = await client.post("/api/account/login", json=body)
                    assert response.status_code == 503 and response.json() == {"error": "busy"}
                assert calls == []
            finally:
                gate.release()
            for _ in range(5):
                assert (await client.post("/api/account/login", json=body)).status_code == 401
            assert (await client.post("/api/account/login", json=body)).status_code == 429
            assert len(calls) == 5
    asyncio.run(scenario())


def test_busy_recovery_releases_confirmation_reservations_and_never_hashes(db, monkeypatch):
    user = add_user(db)
    token = seed_token(db, user)
    calls = []
    value = hash_password(NEW_PASSWORD)
    monkeypatch.setattr(recovery, "hash_password", lambda p: calls.append(True) or value)
    async def timeout(future, *, timeout):
        assert timeout == 2.0
        future.cancel()
        raise TimeoutError()
    async def scenario():
        gate = PasswordWorkGate(wait_for=timeout)
        await gate.acquire()
        gateway = FakeEmail()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(db, gate=gate, gateway=gateway)),
                                     base_url="https://testserver", headers=ORIGIN) as client:
            try:
                for _ in range(12):
                    response = await client.post("/api/account/recovery/confirm",
                        json={"token": token, "password": NEW_PASSWORD})
                    assert response.status_code == 503 and response.json() == {"error": "busy"}
                assert calls == [] and gateway.changed == []
                assert rows(db, AccountRecoveryToken)[0]["consumed_at"] is None
            finally:
                gate.release()
            assert (await client.post("/api/account/recovery/confirm",
                json={"token": token, "password": NEW_PASSWORD})).status_code == 200
            assert calls == [True] and gateway.changed == ["person@example.com"]
    asyncio.run(scenario())



def test_registration_capacity_refusal_never_hashes_or_allocates(db, monkeypatch):
    clock = [0.0]
    store = type(RegistrationRateLimiter()._store)(max_keys=2)
    limiter = RegistrationRateLimiter(clock=lambda: clock[0], store=store)
    value = hash_password(PASSWORD)
    calls = []
    monkeypatch.setattr(accounts, "hash_password", lambda password: calls.append(True) or value)
    with TestClient(app_for(db, limiter=limiter), base_url="https://testserver", headers=ORIGIN) as client:
        for index, address in enumerate(("192.0.2.1", "192.0.2.2")):
            response = client.post("/api/account/register",
                headers={"CF-Connecting-IP": address},
                json={"email": f"capacity{index}@example.com", "password": PASSWORD})
            assert response.status_code == 201
        before = set(store._buckets)
        for address in ("192.0.2.3", "192.0.2.4"):
            response = client.post("/api/account/register",
                headers={"CF-Connecting-IP": address},
                json={"email": "capacity-new@example.com", "password": PASSWORD})
            assert response.status_code == 429 and response.json() == {"error": "too_many_attempts"}
            assert response.headers["retry-after"] == "900"
            assert len(calls) == 2 and set(store._buckets) == before
        clock[0] = 900.0
        response = client.post("/api/account/register",
            headers={"CF-Connecting-IP": "192.0.2.3"},
            json={"email": "capacity-new@example.com", "password": PASSWORD})
        assert response.status_code == 201 and len(calls) == 3
