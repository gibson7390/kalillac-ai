"""Email-verification throttling: independent, hashed, deterministic sliding windows."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from account_test_db import make_session_factory, make_sqlite_engine
from verification_test_support import (
    COOKIE, EMAIL, FakeVerificationEmail, add_session, add_user, client_for, confirm,
    request_link, request_verification,
)
from kalillac_accounts.email_verification import new_verification_token
from kalillac_accounts.verification_rate_limit import VerificationRateLimiter
from kalillac_db.models import Base


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


def test_request_client_limit_and_retry_after(clock):
    limiter = VerificationRateLimiter(clock)
    assert all(limiter.request_client("client-a").allowed for _ in range(5))
    blocked = limiter.request_client("client-a")
    assert not blocked.allowed and blocked.retry_after_seconds == 900
    assert limiter.request_client("client-b").allowed
    clock.now += 900
    assert limiter.request_client("client-a").allowed


def test_account_limit_is_three_per_hour_across_clients(clock):
    limiter = VerificationRateLimiter(clock)
    assert [limiter.request_account("user-1", EMAIL) for _ in range(4)] == [True, True, True, False]
    assert limiter.request_account("user-2", "other@example.com")
    clock.now += 3599
    assert not limiter.request_account("user-1", EMAIL)
    clock.now += 1
    assert limiter.request_account("user-1", EMAIL)


def test_confirmation_counts_invalid_attempts_only_and_expires(clock):
    limiter = VerificationRateLimiter(clock)
    for _ in range(20):
        decision, keys = limiter.begin_confirm("client-a")
        assert decision.allowed
        limiter.finish_confirm(keys, invalid=False)
    for _ in range(10):
        decision, keys = limiter.begin_confirm("client-a")
        assert decision.allowed
        limiter.finish_confirm(keys, invalid=True)
    decision, _ = limiter.begin_confirm("client-a")
    assert not decision.allowed and decision.retry_after_seconds == 900
    assert limiter.begin_confirm("client-b")[0].allowed
    clock.now += 900
    assert limiter.begin_confirm("client-a")[0].allowed


def test_in_flight_confirmations_reserve_capacity(clock):
    limiter = VerificationRateLimiter(clock)
    barrier = threading.Barrier(16)
    def attempt():
        barrier.wait(timeout=10)
        return limiter.begin_confirm("client-a")[0].allowed
    with ThreadPoolExecutor(max_workers=16) as pool:
        allowed = [future.result(timeout=10) for future in [pool.submit(attempt) for _ in range(16)]]
    assert sum(allowed) == 10


def test_only_hashed_identifiers_are_kept(clock):
    limiter = VerificationRateLimiter(clock)
    limiter.request_client("198.51.100.7")
    limiter.request_account("user-1", EMAIL)
    limiter.begin_confirm("198.51.100.7")
    stored = repr(list(limiter._store._buckets))
    assert "198.51.100.7" not in stored and EMAIL not in stored and "user-1" not in stored
    assert all(len(key) == 64 for key in limiter._store._buckets)


def test_independent_from_recovery_and_login_limiters(clock):
    from kalillac_accounts.rate_limit import LoginRateLimiter
    from kalillac_accounts.recovery_rate_limit import RecoveryRateLimiter
    verification, recovery, login = VerificationRateLimiter(clock), RecoveryRateLimiter(clock), LoginRateLimiter(clock=clock)
    for _ in range(5):
        recovery.request("client-a", EMAIL)
    assert verification.request_client("client-a").allowed
    for _ in range(4):
        verification.request_client("client-a")
    assert not verification.request_client("client-a").allowed
    assert recovery._store is not verification._store and login._store is not verification._store
    assert login.begin(login.keys_for("client-a", EMAIL)).allowed


@pytest.fixture
def db():
    engine = make_sqlite_engine()
    Base.metadata.create_all(engine)
    yield make_session_factory(engine)
    engine.dispose()


def test_http_client_limit_returns_429_and_account_limit_is_silent(db, clock):
    gateway = FakeVerificationEmail()
    user_id = add_user(db)
    cookie = add_session(db, user_id)
    with client_for(db, gateway, limiter=VerificationRateLimiter(clock)) as client:
        for _ in range(3):
            request_link(client, gateway, cookie)
        silent = request_verification(client, cookie)
        assert silent.status_code == 202 and silent.json() == {"ok": True}
        assert len(gateway.sent) == 3
        assert request_verification(client, cookie).status_code == 202
        limited = request_verification(client, cookie)
        assert limited.status_code == 429 and limited.json() == {"error": "too_many_attempts"}
        assert limited.headers["retry-after"] == "900" and limited.headers["cache-control"] == "no-store"
        # Unauthenticated requests from a throttled client also stop at the client limit.
        client.cookies.clear()
        assert client.post("/api/account/email-verification/request", json={},
                           headers={"cf-connecting-ip": "192.0.2.10"}).status_code == 429
        # Another client is unaffected.
        assert request_verification(client, cookie, peer="192.0.2.99").status_code == 202
    assert len(gateway.sent) == 3


def test_http_confirmation_limit_counts_invalid_attempts_per_client(db, clock):
    gateway = FakeVerificationEmail()
    user_id = add_user(db)
    cookie = add_session(db, user_id)
    with client_for(db, gateway, limiter=VerificationRateLimiter(clock)) as client:
        token = request_link(client, gateway, cookie)
        for _ in range(10):
            assert confirm(client, new_verification_token(), peer="192.0.2.66").status_code == 400
        limited = confirm(client, token, peer="192.0.2.66")
        assert limited.status_code == 429 and limited.headers["retry-after"] == "900"
        # The other client's valid confirmation is unaffected, and valid attempts don't count.
        assert confirm(client, token, peer="192.0.2.10").status_code == 200
        for _ in range(12):
            assert confirm(client, "malformed", peer="192.0.2.77").status_code in (400, 429)
        assert confirm(client, "malformed", peer="192.0.2.77").status_code == 429
