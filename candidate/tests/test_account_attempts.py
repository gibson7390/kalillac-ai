"""Per-account daily provider attempts (Codex finding F3), through the real ProviderControls and protect seam.

A failed response never counts against the customer's successful-response allowance, so before this a
signed-in customer whose requests kept failing could keep the providers working until only the service-wide
ceilings stopped them. Now every attempt a signed-in account's request starts is counted on that account's
UTC day, successful or not, and refused at the account's limit before the provider is called. A never-
dispatched attempt is refunded. The global and per-provider ceilings are unchanged.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import threading
from types import SimpleNamespace
import uuid

import pytest

from account_test_db import make_session_factory, make_sqlite_file_engine
from kalillac_routing.request_budget import RequestBudget, budget_scope

NOW = datetime(2026, 11, 18, 12, tzinfo=timezone.utc)
LIMITS = ("KALILLAC_ACCOUNT_DAILY_PROVIDER_ATTEMPTS_FREE", "KALILLAC_ACCOUNT_DAILY_PROVIDER_ATTEMPTS_PAID")


def create_service(tmp_path, free=3, paid=5, total=1000, accounts=True):
    from kalillac_db.models import Base, User
    from kalillac_routing.provider_controls import ProviderControls
    from kalillac_routing.provider_limits import AccountAttemptLimit, ControlConfig, ProviderLimit
    engine = make_sqlite_file_engine(str(tmp_path))
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    users = [uuid.uuid4() for _ in range(3)]
    with factory() as session, session.begin():
        for n, user in enumerate(users):
            session.add(User(id=user, email=f"synthetic{n}@example.invalid", password_hash="x"))
    clock = {"now": NOW}
    config = ControlConfig({p: ProviderLimit(total, total, 86400) for p in ("openai", "brave", "tavily")}, None,
                           AccountAttemptLimit(free, paid) if accounts else None)
    service = ProviderControls(config, session_factory=lambda: factory, clock=lambda: clock["now"])
    return SimpleNamespace(service=service, factory=factory, engine=engine, clock=clock, users=users, config=config)


def attempts(data, user, day=NOW.date()):
    from kalillac_db.models import AccountUsageDaily
    with data.factory() as session:
        row = session.get(AccountUsageDaily, (user, day))
        return None if row is None else (row.provider_attempts, row.successful_chats)


def pool(data, provider="openai"):
    from kalillac_db.models import ProviderUsagePool
    with data.factory() as session:
        row = session.get(ProviderUsagePool, provider)
        return 0 if row is None else row.total_attempts


# --- configuration -------------------------------------------------------------------------------------

@pytest.fixture
def ceilings(monkeypatch):
    for name, value in {"KALILLAC_PROVIDER_CEILINGS_ENABLED": "true", "KALILLAC_DB_ENABLED": "true",
                        "KALILLAC_REQUEST_BUDGET_ENABLED": "true", "KALILLAC_USAGE_METERING_ENABLED": "true",
                        "KALILLAC_ACCOUNTS_ENABLED": "true",
                        **{f"KALILLAC_{p}_{k}": v for p in ("OPENAI", "BRAVE", "TAVILY")
                           for k, v in (("TOTAL_ATTEMPTS", "100"), ("ANONYMOUS_ATTEMPTS", "10"),
                                        ("PERIOD_SECONDS", "60"))}}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("KALILLAC_ANONYMOUS_ABUSE_ENABLED", raising=False)
    for name in LIMITS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.mark.parametrize("missing", LIMITS)
def test_with_accounts_both_account_limits_are_required_and_never_defaulted(ceilings, missing):
    from kalillac_routing.provider_limits import ControlConfigError, load_controls
    for name in LIMITS:
        if name != missing:
            ceilings.setenv(name, "10")
    with pytest.raises(ControlConfigError, match=missing):
        load_controls()
    for bad in ("", "-1", "ten", "1.5", "1000001"):
        ceilings.setenv(missing, bad)
        with pytest.raises(ControlConfigError, match=missing):
            load_controls()


def test_the_limits_are_read_exactly_and_ignored_without_accounts(ceilings):
    from kalillac_routing.provider_limits import AccountAttemptLimit, load_controls
    ceilings.setenv(LIMITS[0], "0")
    ceilings.setenv(LIMITS[1], "250")
    assert load_controls().accounts == AccountAttemptLimit(free=0, paid=250)
    ceilings.delenv("KALILLAC_ACCOUNTS_ENABLED")
    ceilings.delenv("KALILLAC_USAGE_METERING_ENABLED")
    ceilings.setenv(LIMITS[0], "not-read")
    assert load_controls().accounts is None          # Anonymous-only deployments (production today) unchanged.


# --- the limit -------------------------------------------------------------------------------------------

def test_each_account_is_refused_at_its_own_tiers_limit_and_others_are_untouched(tmp_path):
    from kalillac_routing.provider_controls import AccountAttemptLimitReached
    data = create_service(tmp_path, free=3, paid=5)
    free, paid, other = data.users
    try:
        for _ in range(3):
            data.service.reserve("openai", False, account=(free, False))
        with pytest.raises(AccountAttemptLimitReached) as refused:
            data.service.reserve("brave", False, account=(free, False))   # Every provider counts.
        assert (refused.value.status_code, refused.value.code) == (429, "account_processing_limit_reached")
        for _ in range(5):
            data.service.reserve("openai", False, account=(paid, True))
        with pytest.raises(AccountAttemptLimitReached):
            data.service.reserve("openai", False, account=(paid, True))
        assert attempts(data, free) == (3, 0) and attempts(data, paid) == (5, 0) and attempts(data, other) is None
        data.service.reserve("openai", False, account=(other, False))   # Another account: its own count.
        data.service.reserve("openai", True)                              # Anonymous: the pool only.
        assert pool(data) == 3 + 5 + 1 + 1 and attempts(data, other) == (1, 0)
        # The next UTC day starts again at zero; yesterday's count stays as it was.
        data.clock["now"] += timedelta(days=1)
        data.service.reserve("openai", False, account=(free, False))
        assert attempts(data, free, (NOW + timedelta(days=1)).date()) == (1, 0) and attempts(data, free) == (3, 0)
    finally:
        data.engine.dispose()


def test_a_never_dispatched_attempt_is_refunded_and_a_dispatched_one_is_not(tmp_path):
    data = create_service(tmp_path, free=2)
    user = data.users[0]
    try:
        lease = data.service.reserve("openai", False, account=(user, False))
        assert data.service.release(lease) is True and attempts(data, user) == (0, 0)
        lease = data.service.reserve("openai", False, account=(user, False))
        lease.dispatched = True                                           # The provider was called.
        assert data.service.release(lease) is False and attempts(data, user) == (1, 0)
    finally:
        data.engine.dispose()


def test_a_signed_in_request_without_an_account_is_refused_never_unbounded(tmp_path):
    from kalillac_routing.provider_controls import CeilingUnavailable
    data = create_service(tmp_path)
    try:
        for account in (None, ("not-a-uuid", False), (data.users[0], "yes"), (data.users[0],)):
            with pytest.raises(CeilingUnavailable):
                data.service.reserve("openai", False, account=account)
        assert pool(data) == 0
    finally:
        data.engine.dispose()


def test_without_account_limits_nothing_is_counted_per_account(tmp_path):
    data = create_service(tmp_path, accounts=False)
    try:
        data.service.reserve("openai", False)
        assert pool(data) == 1 and attempts(data, data.users[0]) is None
    finally:
        data.engine.dispose()


def test_concurrent_attempts_for_one_account_admit_exactly_its_limit(tmp_path):
    from kalillac_routing.provider_controls import AccountAttemptLimitReached, CeilingUnavailable, ProviderControls
    data = create_service(tmp_path, free=4)
    user = data.users[0]
    barrier = threading.Barrier(12)

    def race(_):
        service = ProviderControls(data.config, session_factory=lambda: data.factory, clock=lambda: NOW)
        barrier.wait(5)
        for _ in range(20):
            try:
                return service.reserve("openai", False, account=(user, False)) is not None
            except AccountAttemptLimitReached:
                return False
            except CeilingUnavailable:   # SQLite's single writer timed out: try again, as a new request would.
                continue
        raise AssertionError("never decided")
    try:
        with ThreadPoolExecutor(max_workers=12) as executor:
            admitted = list(executor.map(race, range(12)))
        assert sum(admitted) == 4 and attempts(data, user) == (4, 0) and pool(data) == 4
    finally:
        data.engine.dispose()


# --- Codex's scenario, through protect(): failing responses no longer run without bound ------------------

def test_failing_requests_stop_reaching_the_provider_at_the_accounts_limit(tmp_path, monkeypatch):
    from kalillac_finance.scopes import TrafficClass
    from kalillac_routing import provider_controls
    data = create_service(tmp_path, free=3)
    user = data.users[0]
    monkeypatch.setattr(provider_controls, "SERVICE", data.service)
    calls = []

    def provider(payload):
        provider_controls.dispatch()                    # What the bounded transport does before sending.
        calls.append(payload)
        raise ConnectionError("the provider failed after the attempt was sent")
    guarded = provider_controls.protect(provider, "openai")
    outcomes = []
    try:
        for n in range(6):                              # Six failing requests from one free account.
            control, token = provider_controls.begin_request()
            try:
                provider_controls.account_status(True, None, account_id=str(user))
                assert control.traffic_class is TrafficClass.FREE_ACCOUNT and control.account_id == user
                budget = RequestBudget(duration_seconds=60, max_model_attempts=5, max_search_attempts=1)
                with budget_scope(budget):
                    try:
                        guarded({"n": n})
                    except provider_controls.AccountAttemptLimitReached as refused:
                        outcomes.append(refused.code)
                    except ConnectionError:
                        outcomes.append("provider_failed")
            finally:
                provider_controls.end_request(control, token)
        # The first three reached the provider and failed; none counted as a successful response; the rest
        # were refused before the provider was called.
        assert outcomes == ["provider_failed"] * 3 + ["account_processing_limit_reached"] * 3
        assert len(calls) == 3 and attempts(data, user) == (3, 0) and pool(data) == 3
    finally:
        data.engine.dispose()


def test_without_the_account_limit_failing_requests_reach_the_provider_every_time(tmp_path, monkeypatch):
    """The original failure (Codex F3), kept as evidence: without the account limit, as before this change,
    every failing request from one account reaches the provider, and none uses the response allowance."""
    from kalillac_routing import provider_controls
    data = create_service(tmp_path, accounts=False)
    monkeypatch.setattr(provider_controls, "SERVICE", data.service)
    calls = []

    def provider(payload):
        provider_controls.dispatch()
        calls.append(payload)
        raise ConnectionError("the provider failed after the attempt was sent")
    guarded = provider_controls.protect(provider, "openai")
    try:
        for n in range(6):
            control, token = provider_controls.begin_request()
            try:
                provider_controls.account_status(True, None, account_id=str(data.users[0]))
                with budget_scope(RequestBudget(duration_seconds=60, max_model_attempts=5, max_search_attempts=1)):
                    with pytest.raises(ConnectionError):
                        guarded({"n": n})
            finally:
                provider_controls.end_request(control, token)
        assert len(calls) == 6 and attempts(data, data.users[0]) is None and pool(data) == 6
    finally:
        data.engine.dispose()


def test_a_request_classified_anonymous_carries_no_account(tmp_path, monkeypatch):
    from kalillac_routing import provider_controls
    data = create_service(tmp_path)
    monkeypatch.setattr(provider_controls, "SERVICE", data.service)
    control, token = provider_controls.begin_request()
    try:
        provider_controls.account_status(False, None, account_id=str(data.users[0]))
        assert control.anonymous is True and control.account_id is None
        assert provider_controls._attempt_account(control) is None
    finally:
        provider_controls.end_request(control, token)
        data.engine.dispose()
