"""Financial Safeguards V1, batch 2: ledger, reservation, settlement, latches.

SQLite stands in here for single-process semantics, configuration, traffic
classification and randomized invariant checks; real multi-process
PostgreSQL concurrency is in test_provider_controls_postgres.py. Ceilings are
synthetic; prices are the owner-approved values. No network or provider.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import os
from pathlib import Path
import random
import re
import subprocess
import sys
from types import SimpleNamespace

import pytest

import finance_test_support as f
from account_test_db import make_session_factory, make_sqlite_file_engine
from kalillac_finance import config as finance_config_module, limits as limits_module, pricing
from kalillac_finance.pricing import PricingConfigError
from kalillac_finance.scopes import FinancialScope, TrafficClass

CANDIDATE = Path(__file__).resolve().parents[1]
NOW = datetime(2030, 3, 14, 12, tzinfo=timezone.utc)
DAY, MONTH = NOW.date(), date(2030, 3, 1)


def make(tmp_path, finance=None, total=1000, anonymous=1000, now=NOW):
    from kalillac_db.models import Base
    from kalillac_routing.provider_controls import ProviderControls
    from kalillac_routing.provider_limits import ControlConfig, ProviderLimit
    engine = make_sqlite_file_engine(str(tmp_path))
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    clock = {"now": now}
    config = ControlConfig({p: ProviderLimit(total, anonymous, 400 * 86400) for p in ("openai", "brave", "tavily")})
    finance = finance if finance is not None else f.finance_config()
    data = SimpleNamespace(engine=engine, factory=factory, clock=clock, config=config, finance=finance)
    data.service = ProviderControls(config, session_factory=lambda: factory, clock=lambda: clock["now"],
                                    finance=finance)
    data.restart = lambda: ProviderControls(config, session_factory=lambda: factory, clock=lambda: clock["now"],
                                            finance=finance)
    return data


@pytest.fixture
def ledger(tmp_path):
    data = make(tmp_path)
    yield data
    data.engine.dispose()


def pool(data, provider="openai"):
    from kalillac_db.models import ProviderUsagePool
    with data.factory() as session:
        row = session.get(ProviderUsagePool, provider)
        return (row.total_attempts, row.anonymous_attempts) if row else (0, 0)


# --- configuration ------------------------------------------------------------------------


def test_official_prices_load_exactly_as_explicit_values():
    config = f.finance_config()
    openai = config.prices.openai
    assert openai.model == "gpt-6-luna" and openai.input_overhead_tokens == 8192
    assert (openai.standard.input, openai.standard.cached_input, openai.standard.cache_write,
            openai.standard.output) == (100_000, 10_000, 125_000, 500_000)
    assert openai.long_context.above_input_tokens == 272_000
    long = openai.long_context.rates
    assert (long.input, long.cached_input, long.cache_write, long.output) == (200_000, 20_000, 250_000, 750_000)
    assert config.prices.brave.per_request == 5_000 and config.prices.tavily.per_credit == 8_000
    assert [config.prices.tavily.max_credits_for(*op) for op in pricing.TAVILY_OPERATIONS] == [1, 2, 1, 2]


@pytest.mark.parametrize("name", limits_module.REQUIRED)
def test_every_ceiling_is_required_when_enabled(name):
    env = {**f.OFFICIAL_PRICES, **f.limits_env(f.scopes_for(400, 400), f.scopes_for(4000, 4000))}
    assert finance_config_module.load_financial_config(env) is not None
    del env[name]
    with pytest.raises(PricingConfigError, match=re.escape(name)):
        finance_config_module.load_financial_config(env)


@pytest.mark.parametrize("day, month, percent", [
    ((800, 400, 400), (8000, 4000, 3000), 10),       # FREE + PAID == GLOBAL (day)
    ((801, 400, 400), (8000, 3000, 3000), 10),       # 1 µUSD of headroom < 10%
    ((1000, 400, 400), (10000, 4000, 4000), 30),     # 20% headroom < 30% minimum
    ((1000, 400, 400), (900, 300, 300), 10),         # day global above month global
    ((1000, 400, 400), (10000, 4000, 4000), 0),      # minimum headroom must be at least 1%
    ((1000, 400, 400), (10000, 4000, 4000), 91),
])
def test_inconsistent_ceilings_are_refused(day, month, percent):
    with pytest.raises(PricingConfigError):
        finance_config_module.load_financial_config({**f.OFFICIAL_PRICES, **f.limits_env(day, month, percent)})


def test_disabled_reads_only_the_flag_and_needs_no_ceiling():
    assert finance_config_module.load_financial_config({}) is None
    assert finance_config_module.load_financial_config({"KALILLAC_FINANCE_DAY_FREE_LIMIT_USD": "1e999"}) is None


def test_ceiling_text_refuses_exponents_and_negative_values():
    env = {**f.OFFICIAL_PRICES, **f.limits_env(f.scopes_for(400, 400), f.scopes_for(4000, 4000))}
    for value in ("1e3", "-1", "nan", "inf", "１"):
        with pytest.raises(PricingConfigError):
            finance_config_module.load_financial_config(dict(env, KALILLAC_FINANCE_DAY_FREE_LIMIT_USD=value))


def _start(env_overrides):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("KALILLAC_", "OPENAI_", "BRAVE_", "TAVILY_", "STRIPE_", "RESEND_", "GROQ_"))}
    env.update(env_overrides, PYTHONDONTWRITEBYTECODE="1")
    script = ("import app_fastapi_candidate as app; from kalillac_routing import provider_controls as c; "
              "print('SERVICE', c.SERVICE is not None, getattr(c.SERVICE, 'finance', None) is not None)")
    return subprocess.run([sys.executable, "-c", script], cwd=CANDIDATE, env=env, capture_output=True, text=True,
                          timeout=180)


from test_spaces_config import REQUEST_BUDGET  # noqa: E402  (shared test-only request limits)

CEILINGS = {**REQUEST_BUDGET, "KALILLAC_PROVIDER_CEILINGS_ENABLED": "true", "KALILLAC_DB_ENABLED": "true",
            **{f"KALILLAC_{p}_{s}": v for p in ("OPENAI", "BRAVE", "TAVILY")
               for s, v in (("TOTAL_ATTEMPTS", "10"), ("ANONYMOUS_ATTEMPTS", "5"), ("PERIOD_SECONDS", "86400"))}}
COMPLETE = {**f.OFFICIAL_PRICES, **f.limits_env(f.scopes_for(400, 400), f.scopes_for(4000, 4000))}


def test_startup_without_the_flag_is_unchanged():
    result = _start({})
    assert result.returncode == 0, result.stderr[-1500:]
    assert result.stdout.strip().splitlines()[-1] == "SERVICE False False"
    result = _start(CEILINGS)
    assert result.returncode == 0, result.stderr[-1500:]
    assert result.stdout.strip().splitlines()[-1] == "SERVICE True False"


@pytest.mark.parametrize("case, overrides, message", [
    ("prices only", {**CEILINGS, **f.OFFICIAL_PRICES}, "KALILLAC_FINANCE_MIN_HEADROOM_PERCENT"),
    ("no attempt ceilings", COMPLETE, "requires KALILLAC_PROVIDER_CEILINGS_ENABLED"),
    ("model mismatch", {**CEILINGS, **COMPLETE, "OPENAI_MODEL": "gpt-5.6-luna"}, "KALILLAC_PRICE_OPENAI_MODEL must"),
    ("ambiguous flag", {**CEILINGS, "KALILLAC_FINANCIAL_SAFEGUARDS_ENABLED": "maybe"}, "must be true or false"),
])
def test_enabled_but_incomplete_or_inconsistent_refuses_startup(case, overrides, message):
    result = _start(overrides)
    assert result.returncode != 0 and message in result.stderr, (case, result.stderr[-1500:])


def test_complete_configuration_starts_with_both_ledgers():
    result = _start({**CEILINGS, **COMPLETE})
    assert result.returncode == 0, result.stderr[-1500:]
    assert result.stdout.strip().splitlines()[-1] == "SERVICE True True"


# --- traffic classification ---------------------------------------------------------------


def test_traffic_class_never_infers_paid():
    from kalillac_accounts.entitlements import FREE, PAID, EffectiveEntitlement
    from kalillac_finance.traffic import PaidEntitlement, issue_paid_entitlement, resolve_traffic_class
    request, other = object(), object()
    assert resolve_traffic_class(authenticated=False) is TrafficClass.ANONYMOUS
    assert resolve_traffic_class(authenticated=False, request=request,
                                 paid_entitlement=issue_paid_entitlement(request, PAID)) is TrafficClass.ANONYMOUS
    assert resolve_traffic_class(authenticated=True, request=request) is TrafficClass.FREE_ACCOUNT
    for value in (True, "paid", {"tier": "paid"}, PAID, 1, "user-id", object()):
        assert resolve_traffic_class(authenticated=True, request=request, paid_entitlement=value) \
            is TrafficClass.FREE_ACCOUNT
    assert issue_paid_entitlement(request, FREE) is None
    assert issue_paid_entitlement(request, None) is None
    assert issue_paid_entitlement(None, PAID) is None
    lookalike = EffectiveEntitlement(tier="paid", saved_mode_access=True, higher_usage_access=False)
    assert issue_paid_entitlement(request, lookalike) is None
    with pytest.raises(TypeError):
        PaidEntitlement(request)
    proof = issue_paid_entitlement(request, PAID)
    assert resolve_traffic_class(authenticated=True, request=request, paid_entitlement=proof) is TrafficClass.PAID_ACCOUNT
    # Evidence from another (for example an earlier) request is stale here.
    assert resolve_traffic_class(authenticated=True, request=other, paid_entitlement=proof) is TrafficClass.FREE_ACCOUNT
    assert resolve_traffic_class(authenticated="yes", request=request, paid_entitlement=proof) is TrafficClass.ANONYMOUS


def test_the_application_classifies_signed_in_users_as_free(ledger):
    from kalillac_routing import provider_controls
    provider_controls_service = provider_controls.SERVICE
    provider_controls.SERVICE = ledger.service
    try:
        state, token = provider_controls.begin_request()
        provider_controls.account_status(True)
        assert state.traffic_class is TrafficClass.FREE_ACCOUNT and state.anonymous is False
        provider_controls.end_request(state, token)
        state, token = provider_controls.begin_request()
        assert state.traffic_class is TrafficClass.ANONYMOUS
        provider_controls.end_request(state, token)
    finally:
        provider_controls.SERVICE = provider_controls_service
    source = (CANDIDATE / "app_fastapi_candidate.py").read_text(encoding="utf-8")
    assert re.findall(r"provider_controls\.account_status\(([^)]*)\)", source) == ["meter_user_id is not None"]
    assert "issue_paid_entitlement" not in source


# --- reservation, release, settlement -----------------------------------------------------


def test_reservation_is_committed_before_dispatch_with_both_leases(ledger):
    R = f.openai_reservation(ledger.finance)
    seen = {}

    def on_event(name):
        if name == "entered":  # Inside the provider call, before dispatch, on another connection.
            seen["buckets"] = f.buckets(ledger.factory)
            seen["rows"] = f.reservations(ledger.factory)
            seen["pool"] = pool(ledger)
    result = f.attempt(ledger.service, value=f.model_response(100, 10), on_event=on_event)
    assert result["outcome"] == "ok"
    assert seen["pool"] == (1, 1)
    assert [(r["state"], r["reserved"], r["scope"]) for r in seen["rows"]] == [("reserved", R, "free")]
    assert {k: v[0] for k, v in seen["buckets"].items()} == {
        ("day", DAY, "global"): R, ("day", DAY, "free"): R, ("month", MONTH, "global"): R, ("month", MONTH, "free"): R}


def test_failure_before_dispatch_releases_both_leases(ledger):
    result = f.attempt(ledger.service, behavior="reject_before_dispatch")
    assert result == {"entered": 1, "dispatched": 0, "outcome": "local_reject"}
    assert pool(ledger) == (0, 0)
    assert [r["state"] for r in f.reservations(ledger.factory)] == ["released"]
    assert all(v == (0, 0, 0) for v in f.buckets(ledger.factory).values())


def test_dispatched_failure_settles_at_the_full_reservation(ledger):
    R = f.openai_reservation(ledger.finance)
    assert f.attempt(ledger.service, behavior="fail_after_dispatch")["outcome"] == "provider_failure"
    [row] = f.reservations(ledger.factory)
    assert (row["state"], row["charged"], row["basis"]) == ("settled", R, "unknown")
    assert f.bucket(ledger.factory, "day", DAY, "free") == (0, R, 0)


@pytest.mark.parametrize("value, basis", [
    (f.model_response(usage=False), "missing"),
    (f.model_response(-1, 10), "invalid"),
    ({"usage": {"input_tokens": 10}}, "partial"),
    ("not-a-response", "missing"),
])
def test_unpriceable_usage_keeps_the_full_reservation(ledger, value, basis):
    R = f.openai_reservation(ledger.finance)
    assert f.attempt(ledger.service, value=value)["outcome"] == "ok"
    [row] = f.reservations(ledger.factory)
    assert (row["charged"], row["basis"], row["overrun"]) == (R, basis, 0)
    assert f.latches(ledger.factory) == {}


def test_usage_below_reservation_releases_the_difference(ledger):
    R = f.openai_reservation(ledger.finance)
    f.attempt(ledger.service, value=f.model_response(1000, 200, cached=400))
    # 600 uncached at the cache-write rate (split not reported) + 400 cached + 200 output.
    expected = -(-(600 * 125_000 + 400 * 10_000 + 200 * 500_000) // 1_000_000)
    [row] = f.reservations(ledger.factory)
    assert (row["charged"], row["basis"]) == (expected, "usage") and expected < R
    assert f.bucket(ledger.factory, "month", MONTH, "global") == (0, expected, 0)


def test_brave_and_tavily_settlement(ledger):
    assert f.attempt(ledger.service, provider="brave")["outcome"] == "ok"
    assert f.attempt(ledger.service, provider="tavily", depth="advanced", value={"results": []})["outcome"] == "ok"
    assert f.attempt(ledger.service, provider="tavily", value={"usage": {"credits": 1}})["outcome"] == "ok"
    rows = sorted((r["provider"], r["reserved"], r["charged"], r["basis"]) for r in f.reservations(ledger.factory))
    assert rows == [("brave", 5000, 5000, "usage"), ("tavily", 8000, 8000, "usage"), ("tavily", 16000, 16000, "missing")]
    assert f.attempt(ledger.service, provider="tavily", depth="fast")["outcome"] == "unavailable"
    assert pool(ledger, "tavily") == (2, 2)  # The unpriced depth never reserved an attempt either.


def test_unboundable_payload_reserves_nothing_and_never_dispatches(ledger):
    for payload in ({**f.PAYLOAD, "model": "gpt-5.6-luna"}, {**f.PAYLOAD, "max_output_tokens": None}):
        assert f.attempt(ledger.service, payload=payload) == {"entered": 0, "dispatched": 0, "outcome": "unavailable"}
    assert pool(ledger) == (0, 0) and f.reservations(ledger.factory) == []


def test_crash_after_dispatch_stays_charged_across_restart(ledger):
    R = f.openai_reservation(ledger.finance)
    # Simulate death between dispatch and any cleanup: neither release nor settle runs.
    ledger.service.release = lambda lease: None
    ledger.service.settle = lambda lease, value, returned: None
    f.attempt(ledger.service, value=f.model_response(1, 1))
    restarted = ledger.restart()
    assert f.bucket(ledger.factory, "day", DAY, "free") == (R, 0, 0)
    assert [r["state"] for r in f.reservations(ledger.factory)] == ["reserved"]
    f.attempt(restarted, value=f.model_response(usage=False))
    assert f.bucket(ledger.factory, "day", DAY, "free") == (R, R, 0)


def test_settlement_database_failure_stays_charged(ledger):
    R = f.openai_reservation(ledger.finance)
    original = ledger.service._factory

    def on_event(name):
        if name == "dispatched":
            ledger.service._factory = lambda: (_ for _ in ()).throw(RuntimeError("database outage"))
    assert f.attempt(ledger.service, value=f.model_response(1, 1), on_event=on_event)["outcome"] == "ok"
    ledger.service._factory = original
    [row] = f.reservations(ledger.factory)
    assert row["state"] == "reserved"
    assert f.bucket(ledger.factory, "day", DAY, "global") == (R, 0, 0)


def test_database_unavailable_means_no_dispatch(ledger):
    ledger.service._factory = lambda: (_ for _ in ()).throw(RuntimeError("database outage"))
    assert f.attempt(ledger.service) == {"entered": 0, "dispatched": 0, "outcome": "unavailable"}


def test_both_leases_are_required_for_dispatch(ledger):
    from kalillac_routing import provider_controls
    from kalillac_routing.provider_controls import CeilingUnavailable, Lease
    with pytest.raises(CeilingUnavailable):
        ledger.service.reserve("openai", True)  # Finance on: no attempt lease without a charge.
    previous = provider_controls.SERVICE
    provider_controls.SERVICE = ledger.service
    state, token = provider_controls.begin_request()
    lease_token = provider_controls._LEASE.set(Lease("openai", 0, True, float("inf")))
    try:
        with pytest.raises(CeilingUnavailable):
            provider_controls.dispatch()  # An attempt lease without a financial lease.
    finally:
        provider_controls._LEASE.reset(lease_token)
        provider_controls.end_request(state, token)
        provider_controls.SERVICE = previous


def test_attempt_ceiling_and_financial_ceiling_are_both_enforced(tmp_path):
    R = f.openai_reservation(f.finance_config())
    data = make(tmp_path, finance=f.finance_config(day=f.scopes_for(2 * R, 2 * R)), total=3, anonymous=3)
    try:
        assert [f.attempt(data.service, value=f.model_response(usage=False))["outcome"] for _ in range(3)] == \
            ["ok", "ok", "busy"]  # FREE money ceiling first; its refusal consumed no attempt.
        assert pool(data) == (2, 2)
        assert [f.attempt(data.service, traffic="paid", value=f.model_response(usage=False))["outcome"]
                for _ in range(2)] == ["ok", "busy"]  # Then the attempt ceiling (3 total).
        assert pool(data) == (3, 2)
    finally:
        data.engine.dispose()


def test_free_exhaustion_never_consumes_paid_capacity(tmp_path):
    R = f.openai_reservation(f.finance_config())
    data = make(tmp_path, finance=f.finance_config(day=f.scopes_for(3 * R, 2 * R)))
    try:
        outcomes = [f.attempt(data.service, traffic=t, value=f.model_response(usage=False))["outcome"]
                    for t in ("anonymous", "free", "anonymous", "free")]
        assert outcomes == ["ok", "ok", "ok", "busy"]
        assert [f.attempt(data.service, traffic="paid", value=f.model_response(usage=False))["outcome"]
                for _ in range(3)] == ["ok", "ok", "busy"]
        f.check_invariants(data.factory, data.finance.limits)
    finally:
        data.engine.dispose()


# --- overruns and latches -----------------------------------------------------------------


def test_free_overrun_latches_free_and_keeps_safe_paid_capacity(ledger):
    assert f.attempt(ledger.service, value=f.model_response(10**6, 3200))["outcome"] == "ok"
    [row] = f.reservations(ledger.factory)
    actual = -(-(10**6 * 250_000 + 3200 * 750_000) // 1_000_000)  # long-context rates, not capped
    assert (row["charged"], row["overrun"], row["bound_exceeded"]) == (actual, actual - row["reserved"], True)
    assert f.bucket(ledger.factory, "day", DAY, "free")[2] == actual - row["reserved"]
    assert f.latches(ledger.factory) == {"free": "overrun"}
    for traffic in ("anonymous", "free"):
        assert f.attempt(ledger.service, traffic=traffic)["entered"] == 0
    assert f.attempt(ledger.service, traffic="paid", value=f.model_response(1, 1))["outcome"] == "ok"
    restarted = ledger.restart()  # A new process: the latch is durable, never auto-cleared.
    assert f.attempt(restarted, traffic="free")["entered"] == 0
    assert f.latches(ledger.factory) == {"free": "overrun"}
    f.check_invariants(ledger.factory, ledger.finance.limits)


def test_bound_exceeded_without_money_overrun_still_latches(tmp_path):
    data = make(tmp_path)
    try:
        R = f.openai_reservation(data.finance)
        reservation_tokens = len(__import__("json").dumps(f.PAYLOAD)) + 8192
        f.attempt(data.service, value=f.model_response(reservation_tokens + 1, 0, cached=reservation_tokens + 1))
        [row] = f.reservations(data.factory)
        assert row["overrun"] == 0 and row["bound_exceeded"] and row["charged"] < R
        assert f.latches(data.factory) == {"free": "bound_exceeded"}
    finally:
        data.engine.dispose()


def test_overrun_exhausting_global_headroom_stops_everything(tmp_path):
    data = make(tmp_path, finance=f.finance_config(day=(100_000, 40_000, 40_000)))
    try:
        f.attempt(data.service, value=f.model_response(2 * 10**5, 3200))  # overrun > 20,000 µUSD headroom
        assert f.latches(data.factory) == {"free": "overrun", "global": "headroom_exhausted"}
        for traffic in ("anonymous", "free", "paid"):
            assert f.attempt(data.service, traffic=traffic)["entered"] == 0
        f.check_invariants(data.factory, data.finance.limits)
    finally:
        data.engine.dispose()


def test_paid_overrun_is_charged_to_paid_and_latches_paid(ledger):
    f.attempt(ledger.service, traffic="paid", value=f.model_response(10**6, 3200))
    [row] = f.reservations(ledger.factory)
    assert row["scope"] == "paid" and row["overrun"] > 0
    assert f.bucket(ledger.factory, "day", DAY, "free") == (0, 0, 0)
    assert f.latches(ledger.factory) == {"paid": "overrun"}
    assert f.attempt(ledger.service, traffic="paid")["entered"] == 0
    assert f.attempt(ledger.service, traffic="anonymous", value=f.model_response(1, 1))["outcome"] == "ok"


# --- periods ------------------------------------------------------------------------------


def test_utc_day_rollover_keeps_old_reservations_in_their_own_day(ledger):
    R = f.openai_reservation(ledger.finance)
    ledger.clock["now"] = datetime(2030, 3, 14, 23, 59, 59, tzinfo=timezone.utc)

    def on_event(name):
        if name == "dispatched":
            ledger.clock["now"] = datetime(2030, 3, 15, 0, 0, 1, tzinfo=timezone.utc)
    f.attempt(ledger.service, value=f.model_response(usage=False), on_event=on_event)
    f.attempt(ledger.service, value=f.model_response(usage=False))
    assert f.bucket(ledger.factory, "day", date(2030, 3, 14), "free") == (0, R, 0)
    assert f.bucket(ledger.factory, "day", date(2030, 3, 15), "free") == (0, R, 0)
    assert f.bucket(ledger.factory, "month", MONTH, "free") == (0, 2 * R, 0)


def test_utc_month_rollover_and_month_ceiling(tmp_path):
    R = f.openai_reservation(f.finance_config())
    data = make(tmp_path, finance=f.finance_config(day=f.scopes_for(2 * R, 2 * R), month=f.scopes_for(3 * R, 3 * R)),
                now=datetime(2030, 3, 31, 23, 0, tzinfo=timezone.utc))
    try:
        assert [f.attempt(data.service, value=f.model_response(usage=False))["outcome"] for _ in range(3)] == \
            ["ok", "ok", "busy"]  # day ceiling
        data.clock["now"] = datetime(2030, 4, 1, 0, 0, 1, tzinfo=timezone.utc)
        assert [f.attempt(data.service, value=f.model_response(usage=False))["outcome"] for _ in range(3)] == \
            ["ok", "ok", "busy"]  # new day AND new month
        data.clock["now"] = datetime(2030, 4, 2, 9, tzinfo=timezone.utc)
        assert [f.attempt(data.service, value=f.model_response(usage=False))["outcome"] for _ in range(2)] == \
            ["ok", "busy"]  # day room left, month ceiling (3 R) reached
        assert f.bucket(data.factory, "month", date(2030, 3, 1), "free") == (0, 2 * R, 0)
        assert f.bucket(data.factory, "month", date(2030, 4, 1), "free") == (0, 3 * R, 0)
    finally:
        data.engine.dispose()


# --- randomized adversarial invariants ----------------------------------------------------


def test_randomized_operations_preserve_every_ledger_invariant(tmp_path):
    rng = random.Random(20301)
    R = f.openai_reservation(f.finance_config())
    data = make(tmp_path, finance=f.finance_config(day=f.scopes_for(6 * R, 6 * R), month=f.scopes_for(40 * R, 40 * R)))
    try:
        for step in range(220):
            if rng.random() < 0.08:
                data.clock["now"] += timedelta(hours=rng.choice([1, 13, 30, 24 * 20]))
            traffic = rng.choice(["anonymous", "free", "paid"])
            provider = rng.choice(["openai", "openai", "brave", "tavily"])
            behavior = rng.choice(["ok", "ok", "ok", "reject_before_dispatch", "fail_after_dispatch"])
            value = rng.choice([f.model_response(usage=False), f.model_response(rng.randrange(0, 9000),
                                rng.randrange(0, 3200)), {"results": []}, {"usage": {"credits": rng.randrange(0, 3)}}])
            if rng.random() < 0.02:
                value = f.model_response(10**6, 3200)  # adversarial overrun
            latched = f.latches(data.factory)
            scope = "paid" if traffic == "paid" else "free"
            result = f.attempt(data.service, provider=provider, traffic=traffic, behavior=behavior, value=value,
                               depth=rng.choice(["basic", "advanced"]))
            if "global" in latched or scope in latched:
                assert result["entered"] == 0, (step, latched, traffic)
            assert result["dispatched"] <= result["entered"] <= 1
            f.check_invariants(data.factory, data.finance.limits)
        assert {r["state"] for r in f.reservations(data.factory)} <= {"released", "settled"}
    finally:
        data.engine.dispose()


# --- disabled: production behaviour unchanged ---------------------------------------------


def test_disabled_finance_leaves_the_attempt_ceiling_path_untouched(tmp_path):
    data = make(tmp_path)
    try:
        data.service.finance = None
        assert f.attempt(data.service, value=f.model_response(10**6, 3200))["outcome"] == "ok"
        assert pool(data) == (1, 1)
        assert f.buckets(data.factory) == {} and f.reservations(data.factory) == [] and f.latches(data.factory) == {}
    finally:
        data.engine.dispose()


@pytest.mark.parametrize("enabled", [False, True])
def test_service_tier_is_pinned_only_when_enabled(monkeypatch, tmp_path, enabled):
    from kalillac_routing import provider_controls
    data = make(tmp_path)
    try:
        data.service.finance = f.finance_config() if enabled else None
        monkeypatch.setattr(provider_controls, "SERVICE", data.service)
        seen = []
        wrapped = provider_controls.financial_openai(lambda payload: seen.append(payload) or payload)
        payload = dict(f.PAYLOAD)
        wrapped(payload)
        if enabled:
            assert seen[0] == {**payload, "service_tier": "default"} and seen[0] is not payload
            assert "service_tier" not in payload
        else:
            assert seen[0] is payload and "service_tier" not in payload
        monkeypatch.setattr(provider_controls, "SERVICE", None)
        seen.clear()
        wrapped(payload)
        assert seen[0] is payload
    finally:
        data.engine.dispose()


def test_application_wraps_only_the_openai_seam_with_the_service_tier_pin():
    import app_fastapi_candidate as app
    from kalillac_routing import provider_controls
    source = (CANDIDATE / "app_fastapi_candidate.py").read_text(encoding="utf-8")
    assert source.count("provider_controls.financial_openai(") == 1
    assert "service_tier" not in source.replace('"service_tier": "default"', "")
    assert app._post_openai_for_attempt.__wrapped__.__wrapped__.__wrapped__  # financial -> protect -> observe
    assert app._post_openai_for_attempt.__name__ == "_post_openai_for_attempt"
