"""Production registration reservations, exact rolling boundaries and privacy."""
import hashlib
from concurrent.futures import ThreadPoolExecutor

from kalillac_accounts.rate_limit import InMemoryFailureStore
from kalillac_accounts.registration_rate_limit import RegistrationRateLimiter


def test_five_charged_attempts_sixth_refused_and_rolling_expiry():
    clock = [1000.0]
    limiter = RegistrationRateLimiter(clock=lambda: clock[0])
    for _ in range(5):
        decision, attempt = limiter.begin("192.0.2.1")
        assert decision.allowed
        attempt.charge()
        attempt.release()  # Success cannot reset a charged attempt.
    decision, attempt = limiter.begin("192.0.2.1")
    assert not decision.allowed and attempt is None and decision.retry_after_seconds == 900
    clock[0] += 899.1
    assert limiter.begin("192.0.2.1")[0].retry_after_seconds == 1
    clock[0] = 1900.0
    decision, attempt = limiter.begin("192.0.2.1")
    assert decision.allowed
    attempt.release()


def test_inflight_reservations_are_atomic_and_uncharged_release_is_idempotent():
    limiter = RegistrationRateLimiter()
    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lambda _: limiter.begin("192.0.2.1"), range(20)))
    admitted = [attempt for decision, attempt in results if decision.allowed]
    assert len(admitted) == 5
    for attempt in admitted:
        attempt.release()
        attempt.release()
    for _ in range(5):
        decision, attempt = limiter.begin("192.0.2.1")
        assert decision.allowed
        attempt.charge()
    assert not limiter.begin("192.0.2.1")[0].allowed


def test_only_client_sha256_digest_is_retained_and_clients_are_independent():
    store = InMemoryFailureStore()
    limiter = RegistrationRateLimiter(store=store)
    for _ in range(5):
        _, attempt = limiter.begin("192.0.2.1")
        attempt.charge()
    assert list(store._buckets) == [hashlib.sha256(b"192.0.2.1").hexdigest()]
    assert "192.0.2.1" not in repr(store._buckets)
    assert all("email" not in key for key in store._buckets)
    assert not limiter.begin("192.0.2.1")[0].allowed
    decision, attempt = limiter.begin("192.0.2.2")
    assert decision.allowed
    attempt.release()


def test_window_is_charged_at_actual_work_start():
    clock = [0.0]
    limiter = RegistrationRateLimiter(clock=lambda: clock[0])
    attempts = [limiter.begin("client")[1] for _ in range(5)]
    clock[0] = 100.0
    for attempt in attempts:
        attempt.charge()
    clock[0] = 900.0
    assert limiter.begin("client")[0].retry_after_seconds == 100
    clock[0] = 1000.0
    decision, attempt = limiter.begin("client")
    assert decision.allowed
    attempt.release()


def _capacity_limiter(*, clock=lambda: 0.0):
    # Use the actual default store class, with only its capacity reduced.
    store = type(RegistrationRateLimiter()._store)(max_keys=2)
    return RegistrationRateLimiter(clock=clock, store=store)


def test_capacity_churn_preserves_live_charges_and_reclaims_empty_buckets():
    clock = [0.0]
    limiter = _capacity_limiter(clock=lambda: clock[0])
    for _ in range(5):
        decision, attempt = limiter.begin("A")
        assert decision.allowed
        attempt.charge()
    for client in ("B", "C"):
        decision, attempt = limiter.begin(client)
        assert decision.allowed
        attempt.release()
    assert not limiter.begin("A")[0].allowed
    assert len(limiter._store._buckets) == 1
    clock[0] = 899.1
    assert limiter.begin("A")[0].retry_after_seconds == 1
    clock[0] = 900.0
    decision, attempt = limiter.begin("A")
    assert decision.allowed
    attempt.release()
    assert not limiter._store._buckets and not limiter._store._expiry


def test_simultaneous_distinct_reservations_never_exceed_capacity():
    import threading
    limiter = _capacity_limiter()
    barrier = threading.Barrier(20)
    def reserve(index):
        barrier.wait(timeout=5)
        return limiter.begin(str(index))
    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(reserve, range(20)))
    admitted = [attempt for decision, attempt in results if decision.allowed]
    refused = [decision for decision, _ in results if not decision.allowed]
    assert len(admitted) == 2 and len(refused) == 18
    assert len(limiter._store._buckets) == 2
    assert all(decision.retry_after_seconds == 1 for decision in refused)
    for attempt in admitted:
        attempt.release()
    assert not limiter._store._buckets
    decision, attempt = limiter.begin("replacement")
    assert decision.allowed
    attempt.release()


def test_full_charged_capacity_refuses_until_last_charge_expiry():
    clock = [0.0]
    limiter = _capacity_limiter(clock=lambda: clock[0])
    for client in ("A", "B"):
        _, attempt = limiter.begin(client)
        attempt.charge()
    clock[0] = 100.0
    _, attempt = limiter.begin("A")
    attempt.charge()
    clock[0] = 899.1
    decision, attempt = limiter.begin("C")
    assert not decision.allowed and attempt is None and decision.retry_after_seconds == 1
    assert len(limiter._store._buckets) == 2
    clock[0] = 900.0
    decision, attempt = limiter.begin("C")
    assert decision.allowed
    attempt.release()
    # A's later charge survives; B expired and C's empty state was reclaimed.
    assert list(limiter._store._buckets) == [hashlib.sha256(b"A").hexdigest()]
    clock[0] = 1000.0
    decision, attempt = limiter.begin("D")
    assert decision.allowed
    attempt.release()
    assert not limiter._store._buckets


def test_reservations_survive_charge_expiry_and_cleanup_stays_bounded():
    clock = [0.0]
    limiter = _capacity_limiter(clock=lambda: clock[0])
    _, charged = limiter.begin("A")
    charged.charge()
    _, held_a = limiter.begin("A")
    _, held_b = limiter.begin("B")
    clock[0] = 900.0
    assert not limiter.begin("C")[0].allowed
    assert len(limiter._store._buckets) == 2
    held_a.charge()
    held_b.release()
    _, accepted = limiter.begin("C")
    accepted.release()
    assert len(limiter._store._buckets) == len(limiter._store._expiry) == 1
