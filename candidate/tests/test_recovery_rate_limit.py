"""Deterministic recovery throttling, including in-flight races and client isolation."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from kalillac_accounts.recovery_rate_limit import RecoveryRateLimiter
from kalillac_accounts.recovery_tokens import new_recovery_token
from kalillac_db.models import Base
from account_test_db import make_session_factory, make_sqlite_engine
from recovery_test_support import FakeEmail, add_user, client_for, confirm, request_link


@pytest.fixture
def clock():
    value = [0.0]
    return value


def test_public_request_limit_and_retry_after(clock):
    limiter = RecoveryRateLimiter(clock=lambda: clock[0])
    for i in range(5):
        assert limiter.request("client", f"{i}@example.com").allowed
    blocked = limiter.request("client", "last@example.com")
    assert not blocked.allowed and blocked.retry_after_seconds == 900
    clock[0] = 899.1
    assert limiter.request("client", "last@example.com").retry_after_seconds == 1
    clock[0] = 900
    assert limiter.request("client", "last@example.com").allowed


def test_email_limit_silently_suppresses_across_clients(clock):
    limiter = RecoveryRateLimiter(clock=lambda: clock[0])
    for i in range(3):
        assert limiter.request(f"client-{i}", "same@example.com").send
    silent = limiter.request("another-client", "same@example.com")
    assert silent.allowed and not silent.send and silent.retry_after_seconds == 0
    clock[0] = 3600
    assert limiter.request("another-client", "same@example.com").send


def test_confirmation_limit_counts_invalid_only_and_expires(clock):
    limiter = RecoveryRateLimiter(clock=lambda: clock[0])
    for _ in range(30):
        decision, keys = limiter.begin_confirm("good-client")
        assert decision.allowed
        limiter.finish_confirm(keys, invalid=False)
    for _ in range(10):
        decision, keys = limiter.begin_confirm("client")
        assert decision.allowed
        limiter.finish_confirm(keys, invalid=True)
    decision, _ = limiter.begin_confirm("client")
    assert not decision.allowed and decision.retry_after_seconds == 900
    assert limiter.begin_confirm("unrelated-client")[0].allowed
    clock[0] = 900
    assert limiter.begin_confirm("client")[0].allowed


def test_parallel_confirmation_reservations_cannot_exceed_limit(clock):
    limiter = RecoveryRateLimiter(clock=lambda: clock[0])
    with ThreadPoolExecutor(max_workers=20) as pool:
        attempts = list(pool.map(lambda _: limiter.begin_confirm("one-client"), range(20)))
    assert sum(decision.allowed for decision, _ in attempts) == 10
    for decision, keys in attempts:
        if decision.allowed:
            limiter.finish_confirm(keys, invalid=False)
    assert limiter.begin_confirm("one-client")[0].allowed


def test_only_hashed_identifiers_are_kept(clock):
    limiter = RecoveryRateLimiter(clock=lambda: clock[0])
    limiter.request("192.0.2.123", "private@example.com")
    limiter.begin_confirm("192.0.2.123")
    stored = limiter._store._buckets
    assert len(stored) == 3
    assert all(len(key) == 64 and set(key) <= set("0123456789abcdef") for key in stored)
    assert "private@example.com" not in repr(stored) and "192.0.2.123" not in repr(stored)


@pytest.fixture
def db():
    engine = make_sqlite_engine()
    Base.metadata.create_all(engine)
    yield make_session_factory(engine)
    engine.dispose()


def test_request_http_public_limit_and_normalized_email_silent_limit(db, clock):
    gateway = FakeEmail()
    add_user(db)
    limiter = RecoveryRateLimiter(clock=lambda: clock[0])
    with client_for(db, gateway, limiter=limiter) as client:
        for i in range(5):
            response = client.post("/api/account/recovery/request", json={"email": " Person@Example.com "},
                                   headers={"cf-connecting-ip": "192.0.2.1"})
            assert response.status_code == 202 and response.json() == {"ok": True}
            assert response.headers["cache-control"] == "no-store"
        assert len(gateway.reset) == 3
        response = client.post("/api/account/recovery/request", json={"email": "person@example.com"},
                               headers={"cf-connecting-ip": "192.0.2.1"})
        assert response.status_code == 429 and response.headers["retry-after"] == "900"
        assert response.headers["cache-control"] == "no-store"
        # A different public client does not bypass the normalized-email budget.
        response = client.post("/api/account/recovery/request", json={"email": "person@example.com"},
                               headers={"cf-connecting-ip": "192.0.2.2"})
        assert response.status_code == 202 and len(gateway.reset) == 3


def test_valid_recovery_remains_possible_after_another_clients_failures(db, clock):
    gateway = FakeEmail()
    add_user(db)
    limiter = RecoveryRateLimiter(clock=lambda: clock[0])
    with client_for(db, gateway, limiter=limiter) as client:
        token = request_link(client, gateway)
        for _ in range(10):
            response = confirm(client, "malformed", peer="192.0.2.66")
            assert response.status_code == 400
        blocked = confirm(client, token, peer="192.0.2.66")
        assert blocked.status_code == 429 and blocked.headers["retry-after"] == "900"
        assert blocked.headers["cache-control"] == "no-store"
        assert confirm(client, token, peer="192.0.2.67").status_code == 200
