"""Failed-login abuse protection.

Deterministic: time comes from an injected fake clock.
"""

from __future__ import annotations

import os
import threading

import pytest

os.environ.setdefault("GROQ_API_KEY", "test-not-real")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kalillac_accounts.rate_limit import (
    DEFAULT_RULES,
    InMemoryFailureStore,
    LimitRule,
    LoginRateLimiter,
    client_identifier,
)
from kalillac_accounts.router import AccountSettings, build_account_router
from kalillac_db.models import Base

from account_test_db import make_session_factory, make_sqlite_engine


PASSWORD = "correct horse battery staple"
WRONG = "definitely the wrong password"
WINDOW = 15 * 60
PAIR_LIMIT = 5
CLIENT_LIMIT = 30
EMAIL_LIMIT = 50


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_default_rules_are_the_documented_policy():
    assert [(r.scope, r.max_failures, r.window_seconds) for r in DEFAULT_RULES] == [
        ("client_email", PAIR_LIMIT, WINDOW),
        ("client", CLIENT_LIMIT, WINDOW),
        ("email", EMAIL_LIMIT, WINDOW),
    ]


# --- limiter unit tests --------------------------------------------------------


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def limiter(clock):
    return LoginRateLimiter(store=InMemoryFailureStore(), clock=clock)


def _fail(limiter, client, email, times):
    keys = limiter.keys_for(client, email)

    for _ in range(times):
        assert limiter.begin(keys).allowed
        limiter.record_failure(keys)


def test_repeated_failures_block_the_pair(limiter):
    _fail(limiter, "1.1.1.1", "a@example.com", PAIR_LIMIT)

    decision = limiter.begin(limiter.keys_for("1.1.1.1", "a@example.com"))

    assert not decision.allowed
    assert decision.retry_after_seconds == WINDOW


def test_block_expires_with_the_sliding_window(limiter, clock):
    _fail(limiter, "1.1.1.1", "a@example.com", PAIR_LIMIT)
    keys = limiter.keys_for("1.1.1.1", "a@example.com")

    clock.advance(WINDOW - 1)
    assert not limiter.begin(keys).allowed
    assert limiter.begin(keys).retry_after_seconds == 1

    clock.advance(1)
    assert limiter.begin(keys).allowed


def test_success_resets_account_keys_but_not_client_key(limiter):
    keys = limiter.keys_for("1.1.1.1", "a@example.com")
    _fail(limiter, "1.1.1.1", "a@example.com", PAIR_LIMIT - 1)

    assert limiter.begin(keys).allowed
    limiter.record_success(keys)

    # The pair starts over: a full new allowance of failures.
    _fail(limiter, "1.1.1.1", "a@example.com", PAIR_LIMIT)
    assert not limiter.begin(keys).allowed

    # The client-wide count kept every failure (4 + 5), so logging in to
    # one's own account cannot launder a spray against other accounts.
    for index in range(CLIENT_LIMIT - 9 - 1):
        _fail(limiter, "1.1.1.1", f"spray{index}@example.com", 1)

    # One failure short of the client limit: still allowed...
    assert limiter.begin(limiter.keys_for("1.1.1.1", "y@example.com")).allowed
    limiter.record_failure(limiter.keys_for("1.1.1.1", "y@example.com"))

    # ...and at 4 + 5 + 21 = 30 the client is blocked for every email.
    assert not limiter.begin(limiter.keys_for("1.1.1.1", "z@example.com")).allowed


def test_other_emails_from_same_client_are_not_blocked(limiter):
    _fail(limiter, "1.1.1.1", "a@example.com", PAIR_LIMIT)

    assert not limiter.begin(limiter.keys_for("1.1.1.1", "a@example.com")).allowed
    assert limiter.begin(limiter.keys_for("1.1.1.1", "b@example.com")).allowed


def test_victim_email_not_lockable_from_one_client(limiter):
    # An attacker exhausting the pair limit does not lock the victim out
    # from their own client.
    _fail(limiter, "6.6.6.6", "victim@example.com", PAIR_LIMIT)

    assert not limiter.begin(limiter.keys_for("6.6.6.6", "victim@example.com")).allowed
    assert limiter.begin(limiter.keys_for("2.2.2.2", "victim@example.com")).allowed


def test_one_client_spraying_many_emails_is_blocked(limiter):
    for index in range(CLIENT_LIMIT):
        _fail(limiter, "6.6.6.6", f"user{index}@example.com", 1)

    assert not limiter.begin(limiter.keys_for("6.6.6.6", "new@example.com")).allowed
    # Other clients are unaffected.
    assert limiter.begin(limiter.keys_for("2.2.2.2", "new@example.com")).allowed


def test_many_clients_guessing_one_email_hit_the_email_limit(limiter):
    for index in range(EMAIL_LIMIT):
        _fail(limiter, f"10.0.0.{index}", "victim@example.com", 1)

    assert not limiter.begin(limiter.keys_for("2.2.2.2", "victim@example.com")).allowed
    assert limiter.begin(limiter.keys_for("2.2.2.2", "other@example.com")).allowed


def test_concurrent_attempts_cannot_exceed_the_limit(clock):
    limiter = LoginRateLimiter(
        store=InMemoryFailureStore(),
        rules=(LimitRule("client_email", 5, WINDOW),),
        clock=clock,
    )
    keys = limiter.keys_for("1.1.1.1", "a@example.com")
    barrier = threading.Barrier(20)
    allowed = []

    def attempt():
        barrier.wait()
        if limiter.begin(keys).allowed:
            allowed.append(1)

    threads = [threading.Thread(target=attempt) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    # In-flight attempts count against the limit, so a parallel burst gets
    # exactly the allowance, not one slot per thread.
    assert len(allowed) == 5

    for _ in allowed:
        limiter.record_failure(keys)

    assert not limiter.begin(keys).allowed


def test_release_frees_a_slot_without_counting_a_failure(limiter):
    keys = limiter.keys_for("1.1.1.1", "a@example.com")

    for _ in range(PAIR_LIMIT * 3):
        assert limiter.begin(keys).allowed
        limiter.release(keys)

    assert limiter.begin(keys).allowed


def test_store_keeps_no_raw_identifiers(limiter):
    store = InMemoryFailureStore()
    limiter = LoginRateLimiter(store=store, clock=FakeClock())
    _fail(limiter, "203.0.113.9", "private.person@example.com", 2)

    dumped = repr(list(store._buckets))

    assert "private.person" not in dumped
    assert "example.com" not in dumped
    assert "203.0.113.9" not in dumped


def test_store_memory_is_bounded(clock):
    store = InMemoryFailureStore(max_keys=10)
    limiter = LoginRateLimiter(store=store, clock=clock)

    for index in range(50):
        _fail(limiter, f"10.0.{index}.1", f"user{index}@example.com", 1)

    assert len(store._buckets) <= 10


@pytest.mark.parametrize(
    "peer, headers, trust, expected",
    [
        ("127.0.0.1", {"cf-connecting-ip": "198.51.100.7"}, False, "127.0.0.1"),
        ("127.0.0.1", {"cf-connecting-ip": "198.51.100.7"}, True, "198.51.100.7"),
        ("127.0.0.1", {"cf-connecting-ip": "2001:db8::1"}, True, "2001:db8::1"),
        ("127.0.0.1", {"cf-connecting-ip": "not-an-ip"}, True, "127.0.0.1"),
        ("127.0.0.1", {}, True, "127.0.0.1"),
        (None, {}, False, "unknown"),
    ],
)
def test_client_identifier(peer, headers, trust, expected):
    assert client_identifier(peer, headers, trust) == expected


# --- login endpoint ----------------------------------------------------------------


@pytest.fixture
def api(clock):
    engine = make_sqlite_engine()
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)

    app = FastAPI()
    app.include_router(
        build_account_router(
            session_factory=lambda: factory,
            settings=AccountSettings(
                cookie_secure=True,
                session_days=14,
                trust_cloudflare_client_ip=True,
            ),
            limiter=LoginRateLimiter(store=InMemoryFailureStore(), clock=clock),
        )
    )

    with TestClient(app, base_url="https://testserver") as client:
        client.post(
            "/api/account/register",
            json={"email": "person@example.com", "password": PASSWORD},
        )
        yield client

    engine.dispose()


def _login(client, email="person@example.com", password=WRONG, ip="198.51.100.1"):
    return client.post(
        "/api/account/login",
        json={"email": email, "password": password},
        headers={"CF-Connecting-IP": ip},
    )


def test_endpoint_repeated_failures_then_rate_limited(api):
    for _ in range(PAIR_LIMIT):
        response = _login(api)
        assert response.status_code == 401
        assert response.json() == {"error": "invalid_credentials"}

    limited = _login(api)

    assert limited.status_code == 429
    assert limited.json() == {"error": "too_many_attempts"}
    assert limited.headers["retry-after"] == str(WINDOW)
    assert limited.headers["cache-control"] == "no-store"

    # While blocked, even the right password is refused, so the block cannot
    # be used to confirm a correct guess.
    blocked_correct = _login(api, password=PASSWORD)
    assert blocked_correct.status_code == 429
    assert "set-cookie" not in blocked_correct.headers


def test_endpoint_block_lifts_after_window(api, clock):
    for _ in range(PAIR_LIMIT):
        _login(api)

    assert _login(api, password=PASSWORD).status_code == 429

    clock.advance(WINDOW)

    assert _login(api, password=PASSWORD).status_code == 200


def test_endpoint_success_resets_failures(api):
    for _ in range(PAIR_LIMIT - 1):
        assert _login(api).status_code == 401

    assert _login(api, password=PASSWORD).status_code == 200

    # A full fresh allowance after the successful sign-in.
    for _ in range(PAIR_LIMIT):
        assert _login(api).status_code == 401

    assert _login(api).status_code == 429


def test_endpoint_lockout_not_shared_across_clients_or_emails(api):
    for _ in range(PAIR_LIMIT):
        _login(api, ip="203.0.113.66")

    assert _login(api, ip="203.0.113.66").status_code == 429

    # Same email from another client: the real owner can still sign in.
    assert _login(api, password=PASSWORD, ip="198.51.100.2").status_code == 200

    # Another email from the blocked client is not locked by the pair limit.
    assert _login(api, email="someone@example.com", ip="203.0.113.66").status_code == 401


def test_endpoint_no_user_enumeration(api):
    # A registered and an unregistered email walk the same status sequence
    # with identical bodies, including the rate-limit transition.
    def sequence(email, ip):
        return [
            (response.status_code, response.json())
            for response in (
                _login(api, email=email, ip=ip) for _ in range(PAIR_LIMIT + 2)
            )
        ]

    registered = sequence("person@example.com", "198.51.100.10")
    unregistered = sequence("nobody@example.com", "198.51.100.11")

    assert registered == unregistered
    assert registered[: PAIR_LIMIT] == [
        (401, {"error": "invalid_credentials"})
    ] * PAIR_LIMIT
    assert registered[PAIR_LIMIT:] == [(429, {"error": "too_many_attempts"})] * 2


def test_validation_errors_are_not_counted(api):
    for _ in range(PAIR_LIMIT * 2):
        response = api.post(
            "/api/account/login",
            json={"email": "not-an-email", "password": WRONG},
            headers={"CF-Connecting-IP": "198.51.100.20"},
        )
        assert response.status_code == 422

    assert _login(api, password=PASSWORD, ip="198.51.100.20").status_code == 200
