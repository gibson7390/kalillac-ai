"""Anonymous daily allowance repository and service: offline SQLite tests.

SQLite file databases with BEGIN IMMEDIATE stand in for PostgreSQL row
locks; they are not evidence of PostgreSQL locking.
"""
import ast
import asyncio
import base64
from datetime import date, datetime, timedelta, timezone
import hashlib
from pathlib import Path
import subprocess
import sys
import threading

import pytest
from sqlalchemy import select

from kalillac_accounts.anonymous_quota import (
    VISITOR_COOKIE, VISITOR_MAX_AGE, AnonymousQuota, day_digest, new_visitor_token, parse_visitor_token,
)
from kalillac_db.models import AnonymousUsageDay, AnonymousUsageSlot, Base
from kalillac_db.repositories import anonymous_usage as repo
from kalillac_db.repositories.anonymous_usage import AnonymousLease, AnonymousLimitReached
from kalillac_db.repositories.quota import QuotaUnavailable
from account_test_db import make_session_factory, make_sqlite_file_engine
import kalillac_accounts.anonymous_quota as service_module

NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
DAY = NOW.date()
RAW = bytes(range(1, 33))
COOKIE_VALUE = base64.urlsafe_b64encode(RAW).rstrip(b"=").decode("ascii")


@pytest.fixture
def db(tmp_path):
    engine = make_sqlite_file_engine(str(tmp_path))
    Base.metadata.create_all(engine)
    data = {"engine": engine, "factory": make_session_factory(engine), "now": NOW}
    yield data
    engine.dispose()


def quota(db, limit=10, **options):
    return AnonymousQuota(limit, session_factory=lambda: db["factory"], clock=lambda: db["now"], **options)


def reserved(service, token=RAW):
    request = service.new_request(token)
    request.reserve_now()
    return request


def slots(db):
    with db["factory"]() as session:
        return [(r.visitor_digest, r.usage_date, r.slot, r.state, r.completed_date) for r in session.scalars(
            select(AnonymousUsageSlot).order_by(AnonymousUsageSlot.usage_date, AnonymousUsageSlot.slot))]


def states(db, day=DAY):
    return [state for _, usage_date, _, state, _ in slots(db) if usage_date == day]


def days(db):
    with db["factory"]() as session:
        return sorted((r.usage_date, r.visitor_digest) for r in session.scalars(select(AnonymousUsageDay)))


# --- tokens and digests ---------------------------------------------------------------------

def test_new_tokens_are_random_unpadded_base64url_of_32_bytes():
    tokens = {new_visitor_token() for _ in range(64)}
    assert len(tokens) == 64
    for token in tokens:
        assert len(token) == 43 and "=" not in token
        assert parse_visitor_token(token) is not None and len(parse_visitor_token(token)) == 32
    assert VISITOR_COOKIE == "__Host-kalillac_visitor" and VISITOR_MAX_AGE == 48 * 3600


@pytest.mark.parametrize("value", [
    None, "", COOKIE_VALUE[:-1], COOKIE_VALUE + "A", COOKIE_VALUE + "=", " " + COOKIE_VALUE,
    COOKIE_VALUE[:-1] + "+", COOKIE_VALUE[:-1] + "/", COOKIE_VALUE[:-1] + "\u00e9", COOKIE_VALUE[:-1] + "\u0661",
    COOKIE_VALUE.encode("ascii"), 12345, "A" * 42 + "B",  # "B" sets unused low bits: non-canonical.
])
def test_malformed_tokens_are_rejected(value):
    assert parse_visitor_token(value) is None


def test_canonical_token_round_trips():
    assert parse_visitor_token(COOKIE_VALUE) == RAW
    assert parse_visitor_token("A" * 43) == bytes(32)


def test_digest_is_stable_per_day_and_differs_across_days_and_tokens():
    first = day_digest(RAW, DAY)
    assert first == day_digest(RAW, DAY) and len(first) == 64 and first == first.lower()
    assert first != day_digest(RAW, DAY + timedelta(days=1))
    assert first != day_digest(bytes(32), DAY)
    # Domain-separated: not a plain hash of the token and date.
    assert first != hashlib.sha256(RAW + DAY.isoformat().encode()).hexdigest()
    assert first == hashlib.sha256(
        b"kalillac.anonymous-daily-allowance.v1\x00" + RAW + b"\x00" + b"2026-10-18").hexdigest()
    for token, day in ((RAW[:31], DAY), (COOKIE_VALUE, DAY), (RAW, NOW), (RAW, "2026-10-18")):
        with pytest.raises(ValueError) as error:
            day_digest(token, day)
        assert COOKIE_VALUE not in str(error.value)


def test_service_rejects_bad_limits_ttl_and_tokens(db):
    for limit in (0, -1, 10.0, True, "10"):
        with pytest.raises(ValueError):
            AnonymousQuota(limit)
    with pytest.raises(ValueError):
        AnonymousQuota(10, ttl=timedelta(0))
    for token in (COOKIE_VALUE, RAW[:31], None):
        with pytest.raises(ValueError):
            quota(db).new_request(token)


def test_repository_accepts_digests_only(db):
    with db["factory"]() as session, session.begin():
        for value in (COOKIE_VALUE, RAW.hex().upper(), "g" * 64, day_digest(RAW, DAY)[:63]):
            with pytest.raises(QuotaUnavailable):
                repo.lock_visitor_day(session, value, DAY, create=True)
    assert days(db) == []


# --- lifecycle --------------------------------------------------------------------------------

def test_success_lifecycle_is_idempotent(db):
    service = quota(db)
    request = reserved(service)
    lease = request.lease
    assert lease.visitor_digest == day_digest(RAW, DAY) and lease.usage_date == DAY and lease.slot == 0
    request.before_work()
    assert request.complete_now() == DAY
    assert request.lease is lease
    assert service.complete(lease, RAW, request) is lease  # Already prepared: unchanged.
    assert states(db) == ["ready"]
    assert request.record_sent_now() == DAY
    assert request.record_sent_now() == DAY
    assert slots(db) == [(lease.visitor_digest, DAY, 0, "completed", DAY)]
    request.close(True)
    service.release(lease)  # A completed lease is never refunded.
    assert states(db) == ["completed"]


def test_expired_ready_is_recovered_to_uncertain_by_reservation(db):
    service = quota(db, ttl=timedelta(seconds=5))
    crashed = reserved(service)
    crashed.complete_now()
    db["now"] += timedelta(seconds=5)
    reserved(service)
    assert states(db) == ["uncertain", "pending"]
    service.release(crashed.lease)
    assert states(db) == ["uncertain", "pending"]


def test_known_failures_before_send_refund_pending_and_ready(db):
    service = quota(db, limit=1)
    pending = reserved(service)
    assert pending.close(False) is True
    pending.cleanup()
    assert states(db) == ["released"]
    ready = reserved(service)
    ready.complete_now()
    assert states(db) == ["ready"]
    service.release(ready.lease)  # Failed send before the final body chunk.
    assert states(db) == ["released"]
    service.release(ready.lease)  # Idempotent.
    reserved(service)  # Capacity was refunded.


def test_uncertain_and_completed_are_never_refunded(db):
    service = quota(db, limit=2, ttl=timedelta(seconds=5))
    first, second = reserved(service), reserved(service)
    first.complete_now()
    second.complete_now()
    second.record_sent_now()
    db["now"] += timedelta(seconds=5)
    service.release(first.lease)  # Expired ready: becomes uncertain, not refunded.
    service.release(second.lease)
    assert states(db) == ["uncertain", "completed"]
    service.release(first.lease)
    assert states(db) == ["uncertain", "completed"]
    with pytest.raises(AnonymousLimitReached):
        reserved(service)
    assert first.record_sent_now() == DAY  # A late confirmed send settles it.
    assert states(db) == ["completed", "completed"]


def test_invalid_transitions_fail_closed(db):
    service = quota(db, ttl=timedelta(seconds=5))
    request = reserved(service)
    lease = request.lease
    with pytest.raises(QuotaUnavailable):
        service.record_sent(lease)  # Never sent before preparation.
    db["now"] += timedelta(seconds=5)
    with pytest.raises(QuotaUnavailable):
        request.before_work()  # Expired pending lease.
    with pytest.raises(QuotaUnavailable):
        service.complete(lease, RAW, request)
    service.release(lease)
    with pytest.raises(QuotaUnavailable):
        service.validate(lease, request)
    missing = AnonymousLease(day_digest(bytes(32), DAY), DAY, 0, NOW)
    with pytest.raises(QuotaUnavailable):
        service.validate(missing, request)
    service.release(missing)  # No visitor-day row: nothing to release.


def test_eleventh_reservation_is_refused_with_the_fixed_code(db):
    service = quota(db)
    for _ in range(10):
        reserved(service)
    with pytest.raises(AnonymousLimitReached) as refused:
        reserved(service)
    assert (refused.value.status_code, refused.value.code) == (429, "anonymous_daily_limit_reached")
    assert str(refused.value) == ""
    assert len(slots(db)) == 10
    reserved(service, bytes(32))  # Another browser has its own allowance.


def test_released_slots_are_reused_and_fenced(db):
    service = quota(db)
    old = reserved(service)
    service.release(old.lease)
    new = reserved(service)  # Same clock instant.
    assert new.lease.slot == old.lease.slot and new.lease.expires_at > old.lease.expires_at
    service.release(old.lease)  # Stale cleanup cannot free the new lease.
    with pytest.raises(QuotaUnavailable):
        service.complete(old.lease, RAW, old)
    assert states(db) == ["pending"]
    for _ in range(40):
        request = reserved(service)
        service.release(request.lease)
    assert len(slots(db)) == 2  # Slot 0 is still pending; slot 1 is reused 40 times.
    for _ in range(9):
        reserved(service)
    assert len(slots(db)) == 10
    with pytest.raises(AnonymousLimitReached):
        reserved(service)
    assert len(slots(db)) == 10


def test_pending_crash_is_recovered_and_ready_crash_stays_charged(db):
    service = quota(db, ttl=timedelta(seconds=5))
    crashed = [reserved(service) for _ in range(10)]
    for request in crashed[:4]:
        request.complete_now()  # Crashes after preparation, before settlement.
    db["now"] += timedelta(seconds=5)
    for _ in range(6):
        reserved(quota(db))  # A new worker recovers the six expired pending leases.
    with pytest.raises(AnonymousLimitReached):
        reserved(quota(db))
    assert sorted(states(db)) == ["pending"] * 6 + ["uncertain"] * 4
    assert len(slots(db)) == 10


def test_closed_request_hands_cleanup_to_late_worker(db):
    service = quota(db)
    request = service.new_request(RAW)
    original = service.reserve
    def reserve_then_close(token, state):
        lease = original(token, state)
        assert state.close(False) is False  # Still in work: the worker owns cleanup.
        return lease
    service.reserve = reserve_then_close
    request.reserve_now()
    assert states(db) == ["released"]


def test_async_wrappers_run_the_same_lifecycle(db):
    service = quota(db)
    request = service.new_request(RAW)
    async def run():
        await request.reserve()
        day = await request.complete()
        await request.record_sent()
        return day
    assert asyncio.run(run()) == DAY
    assert states(db) == ["completed"]


# --- concurrency -------------------------------------------------------------------------------

def _race(db, count, before=0):
    service = quota(db)
    for _ in range(before):
        reserved(service)
    barrier, results, lock = threading.Barrier(count), [], threading.Lock()
    def worker():
        request = service.new_request(RAW)
        barrier.wait()
        try:
            request.reserve_now()
            outcome = "reserved"
        except AnonymousLimitReached:
            outcome = "limit"
        with lock:
            results.append(outcome)
    threads = [threading.Thread(target=worker) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads)
    return sorted(results)


def test_concurrent_first_row_creation_admits_exactly_the_limit(db):
    assert _race(db, 16) == ["limit"] * 6 + ["reserved"] * 10
    assert days(db) == [(DAY, day_digest(RAW, DAY))]
    assert sorted(slot for _, _, slot, _, _ in slots(db)) == list(range(10))


def test_concurrent_last_slot_admits_exactly_one(db):
    assert _race(db, 8, before=9) == ["limit"] * 7 + ["reserved"]
    assert len(slots(db)) == 10


# --- failures ----------------------------------------------------------------------------------

def test_rollback_leaves_no_reservation(db, monkeypatch, capsys):
    real = service_module.reserve_anonymous_slot
    def reserve_then_fail(*args):
        real(*args)
        raise RuntimeError(f"synthetic failure {args[1]}")  # Would quote the digest.
    monkeypatch.setattr(service_module, "reserve_anonymous_slot", reserve_then_fail)
    request = quota(db).new_request(RAW)
    with pytest.raises(QuotaUnavailable) as failure:
        request.reserve_now()
    assert request.lease is None and slots(db) == [] and days(db) == []
    output = capsys.readouterr().out
    assert output == "WARN: ANONYMOUS_QUOTA_DATABASE_FAILED RuntimeError\n"
    assert failure.value.__cause__ is None and failure.value.__suppress_context__
    assert str(failure.value) == "" and day_digest(RAW, DAY) not in repr(failure.value)


@pytest.mark.parametrize("factory", [lambda: None, lambda: (_ for _ in ()).throw(OSError("down"))])
def test_unavailable_database_fails_closed(factory):
    service = AnonymousQuota(10, session_factory=factory, clock=lambda: NOW)
    request = service.new_request(RAW)
    with pytest.raises(QuotaUnavailable):
        request.reserve_now()
    lease = AnonymousLease(day_digest(RAW, DAY), DAY, 0, NOW)
    for operation in (lambda: service.validate(lease, request), lambda: service.complete(lease, RAW, request),
                      lambda: service.record_sent(lease), lambda: service.release(lease)):
        with pytest.raises(QuotaUnavailable):
            operation()


def test_failed_cleanup_logs_only_a_fixed_warning(db, monkeypatch, capsys):
    service = quota(db)
    request = reserved(service)
    monkeypatch.setattr(service, "_factory", lambda: None)
    request.close(False)
    request.cleanup()
    assert capsys.readouterr().out == "WARN: ANONYMOUS_QUOTA_RELEASE_FAILED QuotaUnavailable\n"
    assert states(db) == ["pending"]  # Charged until lazy expiry recovery.


# --- UTC midnight (completion-day accounting) ----------------------------------------------------

def _before_midnight(db):
    db["now"] = datetime(2026, 10, 18, 23, 59, 59, tzinfo=timezone.utc)
    return date(2026, 10, 18), date(2026, 10, 19)


def test_midnight_rollover_releases_old_day_and_prepares_new_day(db):
    old_day, new_day = _before_midnight(db)
    service = quota(db)
    request = reserved(service)
    old = request.lease
    db["now"] = datetime(2026, 10, 19, 0, 0, 1, tzinfo=timezone.utc)
    assert request.complete_now() == new_day
    new = request.lease
    assert new is not old and not request.holds_token
    assert (new.visitor_digest, new.usage_date) == (day_digest(RAW, new_day), new_day)
    assert new.visitor_digest != old.visitor_digest
    assert slots(db) == [(old.visitor_digest, old_day, 0, "released", None),
                         (new.visitor_digest, new_day, 0, "ready", new_day)]
    assert service.complete(new, RAW, request) is new  # Idempotent on the new day.
    assert request.record_sent_now() == new_day
    assert states(db, new_day) == ["completed"] and states(db, old_day) == ["released"]


def test_midnight_rollover_enforces_new_day_capacity(db):
    old_day, new_day = _before_midnight(db)
    service = quota(db)
    request = reserved(service)
    old = request.lease
    db["now"] = datetime(2026, 10, 19, 0, 0, 1, tzinfo=timezone.utc)
    for _ in range(10):
        reserved(service)
    with pytest.raises(AnonymousLimitReached):
        request.complete_now()
    assert request.lease is old and not request.holds_token
    assert states(db, old_day) == ["pending"]  # The rollback kept the old lease intact.
    assert states(db, new_day) == ["pending"] * 10
    request.close(False)
    request.cleanup()
    assert states(db, old_day) == ["released"]


def test_new_day_expired_leases_are_recovered_during_rollover(db):
    _, new_day = _before_midnight(db)
    request = reserved(quota(db, ttl=timedelta(seconds=60)))  # Valid until 00:00:59.
    db["now"] = datetime(2026, 10, 19, 0, 0, 1, tzinfo=timezone.utc)
    short = quota(db, ttl=timedelta(seconds=5))
    crashed = [reserved(short) for _ in range(10)]
    for item in crashed[:3]:
        item.complete_now()  # Ready, then never settled.
    db["now"] = datetime(2026, 10, 19, 0, 0, 10, tzinfo=timezone.utc)
    assert request.complete_now() == new_day
    assert sorted(states(db, new_day)) == ["ready"] + ["released"] * 6 + ["uncertain"] * 3
    assert len([row for row in slots(db) if row[1] == new_day]) == 10


def test_midnight_decision_uses_fresh_time_after_the_old_day_lock(db, monkeypatch):
    # The clock reads 23:59:59.9 before preparation starts, and midnight
    # passes while the old visitor-day lock is awaited.
    old_day, new_day = date(2026, 10, 18), date(2026, 10, 19)
    db["now"] = datetime(2026, 10, 18, 23, 59, 59, 900000, tzinfo=timezone.utc)
    request = reserved(quota(db))
    old = request.lease
    readings = []
    service = request.service
    real_now, real_lock = service._now, service_module.lock_visitor_day
    def recording_now(session):
        readings.append(real_now(session))
        return readings[-1]
    def lock_then_midnight(session, digest, day, create=False):
        locked = real_lock(session, digest, day, create)
        if (digest, day) == (old.visitor_digest, old.usage_date):
            db["now"] = datetime(2026, 10, 19, 0, 0, 0, 100000, tzinfo=timezone.utc)
        return locked
    monkeypatch.setattr(service, "_now", recording_now)
    monkeypatch.setattr(service_module, "lock_visitor_day", lock_then_midnight)
    assert recording_now(None).date() == old_day  # Reading before the lock.
    assert request.complete_now() == new_day
    assert [reading.date() for reading in readings] == [old_day, new_day, new_day]
    new = request.lease
    assert (new.visitor_digest, new.usage_date) == (day_digest(RAW, new_day), new_day)
    assert slots(db) == [(old.visitor_digest, old_day, 0, "released", None),
                         (new.visitor_digest, new_day, 0, "ready", new_day)]
    assert not request.holds_token


def test_replacement_lease_is_adopted_only_after_commit(db, monkeypatch):
    old_day, new_day = _before_midnight(db)
    request = reserved(quota(db))
    old = request.lease
    real = service_module.roll_over_anonymous_slot
    def roll_over_then_fail(*args):
        real(*args)
        raise RuntimeError("synthetic commit failure")
    monkeypatch.setattr(service_module, "roll_over_anonymous_slot", roll_over_then_fail)
    db["now"] = datetime(2026, 10, 19, 0, 0, 1, tzinfo=timezone.utc)
    with pytest.raises(QuotaUnavailable):
        request.complete_now()
    assert request.lease is old and not request.holds_token
    assert states(db, old_day) == ["pending"] and states(db, new_day) == []
    assert [day for day, _ in days(db)] == [old_day]
    request.close(False)
    request.cleanup()
    assert states(db, old_day) == ["released"]


def test_backwards_clock_and_expired_old_lease_fail_closed(db):
    old_day, _ = _before_midnight(db)
    service = quota(db, ttl=timedelta(seconds=1))
    request = reserved(service)
    db["now"] = datetime(2026, 10, 17, 23, 0, tzinfo=timezone.utc)
    with pytest.raises(QuotaUnavailable):
        service.complete(request.lease, RAW, request)
    db["now"] = datetime(2026, 10, 19, 0, 0, 1, tzinfo=timezone.utc)  # Old lease expired at 00:00:00.
    with pytest.raises(QuotaUnavailable):
        service.complete(request.lease, RAW, request)
    assert states(db, old_day) == ["pending"] and len(days(db)) == 1


def test_token_is_dropped_after_preparation_and_on_close(db, monkeypatch):
    service = quota(db)
    request = reserved(service)
    assert request.holds_token
    request.complete_now()
    assert not request.holds_token and "_token" in vars(request) and vars(request)["_token"] is None
    failed = reserved(service)
    monkeypatch.setattr(service, "_factory", lambda: None)
    with pytest.raises(QuotaUnavailable):
        failed.complete_now()
    assert not failed.holds_token
    closed = service.new_request(RAW)
    closed.close(False)
    assert not closed.holds_token
    with pytest.raises(QuotaUnavailable):
        closed.reserve_now()


# --- independence from accounts -------------------------------------------------------------

ANONYMOUS_MODULES = (
    Path(__file__).resolve().parents[1] / "kalillac_accounts" / "anonymous_quota.py",
    Path(__file__).resolve().parents[1] / "kalillac_db" / "repositories" / "anonymous_usage.py",
)
ACCOUNT_MODULES = (
    "kalillac_accounts.quota", "kalillac_accounts.router", "kalillac_accounts.tokens",
    "kalillac_accounts.entitlements", "kalillac_db.repositories.entitlements",
    "kalillac_db.repositories.accounts", "argon2",
)


def _imported_names(path):
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = ("." * node.level) + (node.module or "")
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names)
    return names


def test_anonymous_modules_do_not_import_account_modules():
    forbidden = ACCOUNT_MODULES + (".quota.load_account_settings", "kalillac_accounts.router.load_account_settings")
    for path in ANONYMOUS_MODULES:
        names = _imported_names(path)
        assert not any(name == bad or name.startswith(bad + ".") for name in names for bad in forbidden), path.name
        assert not any("load_account_settings" in name or "entitlement" in name or "tokens" in name
                       for name in names), path.name
    # Shared primitives come from their defining repository module.
    assert "kalillac_db.repositories.quota.QuotaUnavailable" in _imported_names(ANONYMOUS_MODULES[0])
    assert ".quota.QuotaUnavailable" in _imported_names(ANONYMOUS_MODULES[1])


def test_importing_the_anonymous_service_loads_no_account_modules():
    probe = ("import sys, json; import kalillac_accounts.anonymous_quota; "
             "print(json.dumps(sorted(sys.modules)))")
    result = subprocess.run([sys.executable, "-B", "-c", probe], cwd=str(ANONYMOUS_MODULES[0].parents[1]),
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    loaded = set(__import__("json").loads(result.stdout))
    assert "kalillac_accounts.anonymous_quota" in loaded
    assert not {name for name in loaded for bad in ACCOUNT_MODULES if name == bad or name.startswith(bad + ".")}


# --- privacy -----------------------------------------------------------------------------------

def test_no_token_or_digest_leaks_into_logs_exceptions_or_stored_values(db, monkeypatch, capsys):
    old_day, new_day = _before_midnight(db)
    service = quota(db, limit=1)
    request = reserved(service)
    errors = []
    try:
        reserved(service)
    except AnonymousLimitReached as error:
        errors.append(error)
    db["now"] = datetime(2026, 10, 19, 0, 0, 1, tzinfo=timezone.utc)
    request.complete_now()
    request.record_sent_now()
    monkeypatch.setattr(service, "_factory", lambda: (_ for _ in ()).throw(RuntimeError(COOKIE_VALUE)))
    try:
        service.release(request.lease)
    except QuotaUnavailable as error:
        errors.append(error)
    digests = {day_digest(RAW, old_day), day_digest(RAW, new_day)}
    secrets_ = {COOKIE_VALUE, RAW.hex(), RAW.hex().upper(), base64.b64encode(RAW).decode()} | digests
    text = capsys.readouterr().out + "".join(f"{e!s}{e!r}" for e in errors) + repr(request) + repr(request.lease)
    assert not any(secret in text for secret in secrets_)
    stored = [value for row in slots(db) for value in row] + [value for row in days(db) for value in row]
    stored_text = " ".join(str(value) for value in stored)
    assert not any(secret in stored_text for secret in secrets_ - digests)
    # Each stored digest is its own day's digest; no row carries another day's.
    for digest, usage_date, *_ in slots(db):
        assert digest == day_digest(RAW, usage_date)
    for usage_date, digest in days(db):
        assert digest == day_digest(RAW, usage_date)


# --- pruning ------------------------------------------------------------------------------------

def _seed_days(db, dates):
    with db["factory"]() as session, session.begin():
        for index, day in enumerate(dates):
            digest = day_digest(bytes([index]) * 32, day)
            session.add(AnonymousUsageDay(visitor_digest=digest, usage_date=day))
            session.flush()
            session.add(AnonymousUsageSlot(visitor_digest=digest, usage_date=day, slot=0, state="completed",
                                           expires_at=NOW, completed_date=day))


def test_pruning_keeps_today_and_yesterday_only(db):
    _seed_days(db, [DAY - timedelta(days=n) for n in (0, 1, 2, 3, 30)])
    assert quota(db).prune() == 3
    assert sorted({day for day, _ in days(db)}) == [DAY - timedelta(days=1), DAY]
    assert sorted({row[1] for row in slots(db)}) == [DAY - timedelta(days=1), DAY]


def test_pruning_is_bounded_per_batch(db):
    _seed_days(db, [DAY - timedelta(days=2 + n) for n in range(7)])
    counts = []
    for _ in range(4):
        with db["factory"]() as session, session.begin():
            counts.append(repo.prune_anonymous_days(session, DAY, batch=3))
    assert counts == [3, 3, 1, 0] and days(db) == [] and slots(db) == []


def test_reservation_prunes_lazily_at_most_once_per_interval(db, monkeypatch):
    clock = {"t": 1000.0}
    calls = []
    real = service_module.prune_anonymous_days
    monkeypatch.setattr(service_module, "prune_anonymous_days", lambda *args: calls.append(args[1]) or real(*args))
    service = quota(db, monotonic=lambda: clock["t"])
    _seed_days(db, [DAY - timedelta(days=5)])
    reserved(service)
    reserved(service)
    clock["t"] += 60
    reserved(service)
    assert calls == [DAY, DAY]
    assert sorted({day for day, _ in days(db)}) == [DAY]


def test_pruning_failure_warns_without_failing_the_reservation(db, monkeypatch, capsys):
    def broken(session, today):
        raise RuntimeError(day_digest(RAW, today))
    monkeypatch.setattr(service_module, "prune_anonymous_days", broken)
    request = reserved(quota(db))
    assert request.lease is not None and states(db) == ["pending"]
    output = capsys.readouterr().out
    assert output == "WARN: ANONYMOUS_QUOTA_DATABASE_FAILED RuntimeError\nWARN: ANONYMOUS_QUOTA_PRUNE_FAILED\n"
