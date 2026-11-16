"""Financial Safeguards V1 completion: provider budgets, the per-request
dollar ceiling, exactly-once resolution and the operator reconciliation.

SQLite stands in for single-process semantics; real multi-process PostgreSQL
concurrency is in test_provider_controls_postgres.py. Ceilings are synthetic;
prices are the owner-approved values. No network or provider.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import importlib.util
import io
import json
from pathlib import Path
import re
import threading

import pytest

import finance_test_support as f
from kalillac_finance import config as finance_config_module, limits as limits_module
from kalillac_finance.pricing import PricingConfigError
from kalillac_finance.scopes import TrafficClass
from test_finance_ledger import DAY, MONTH, NOW, make, pool

CANDIDATE = Path(__file__).resolve().parents[1]
MISSING = f.model_response(usage=False)  # Unpriceable: settles at the full reservation.


def _r():
    return f.openai_reservation(f.finance_config())


def _roomy(units=100):
    """(global, free, paid) day ceilings that never bind in these tests."""
    return f.scopes_for(units * _r(), units * _r())


@pytest.fixture
def ledger(tmp_path):
    data = make(tmp_path)
    yield data
    data.engine.dispose()


def _admin_tool():
    spec = importlib.util.spec_from_file_location("finance_ledger_admin", CANDIDATE / "tools" / "finance_ledger_admin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- configuration ------------------------------------------------------------------------


def test_budget_variable_names_are_exact_and_cover_every_provider():
    from kalillac_routing.provider_limits import PROVIDERS
    assert limits_module.PROVIDERS == PROVIDERS
    assert sorted(limits_module.PROVIDER_LIMIT_NAMES.values()) == sorted(
        f"KALILLAC_FINANCE_{period}_{provider}_LIMIT_USD"
        for period in ("DAY", "MONTH") for provider in ("OPENAI", "BRAVE", "TAVILY"))
    assert sorted(limits_module.MAX_REQUEST_NAMES.values()) == [
        "KALILLAC_FINANCE_MAX_REQUEST_FREE_USD", "KALILLAC_FINANCE_MAX_REQUEST_PAID_USD"]
    assert set(limits_module.PROVIDER_LIMIT_NAMES.values()) | set(limits_module.MAX_REQUEST_NAMES.values()) \
        <= set(limits_module.REQUIRED)
    assert len(set(limits_module.REQUIRED)) == len(limits_module.REQUIRED) == 15


@pytest.mark.parametrize("case, overrides, message", [
    ("provider day above global day", {"provider_day": (1001, 10, 10)}, "KALILLAC_FINANCE_DAY_OPENAI_LIMIT_USD"),
    ("provider month above global month", {"provider_month": (10, 10, 10001)},
     "KALILLAC_FINANCE_MONTH_TAVILY_LIMIT_USD"),
    ("provider day above provider month", {"provider_day": (10, 500, 10), "provider_month": (10, 499, 10)},
     "KALILLAC_FINANCE_DAY_BRAVE_LIMIT_USD"),
    ("free request above free day", {"max_request": (401, 400)}, "KALILLAC_FINANCE_MAX_REQUEST_FREE_USD"),
    ("paid request above paid day", {"max_request": (400, 401)}, "KALILLAC_FINANCE_MAX_REQUEST_PAID_USD"),
])
def test_inconsistent_provider_and_request_ceilings_are_refused(case, overrides, message):
    with pytest.raises(PricingConfigError, match=re.escape(message)):
        f.finance_config(day=(1000, 400, 400), month=(10000, 4000, 4000), **overrides)


def test_budget_text_refuses_exponents_signs_and_blank_values():
    env = {**f.OFFICIAL_PRICES, **f.limits_env(f.scopes_for(400, 400), f.scopes_for(4000, 4000))}
    assert finance_config_module.load_financial_config(env) is not None
    for name in ("KALILLAC_FINANCE_DAY_OPENAI_LIMIT_USD", "KALILLAC_FINANCE_MAX_REQUEST_PAID_USD"):
        for value in ("1e3", "-1", "nan", " ", "1,000", "１"):
            with pytest.raises(PricingConfigError, match=re.escape(name)):
                finance_config_module.load_financial_config({**env, name: value})


def test_limits_expose_the_configured_budgets():
    config = f.finance_config(day=(1000, 400, 400), month=(10000, 4000, 4000), provider_day=(700, 20, 0),
                              provider_month=(7000, 200, 0), max_request=(40, 400))
    limits = config.limits
    assert [limits.provider_limit("day", p) for p in ("openai", "brave", "tavily")] == [700, 20, 0]
    assert [limits.provider_limit("month", p) for p in ("openai", "brave", "tavily")] == [7000, 200, 0]
    assert (limits.max_request("free"), limits.max_request("paid")) == (40, 400)
    for bad in (lambda: limits.provider_limit("day", "groq"), lambda: limits.provider_limit("week", "openai"),
                lambda: limits.max_request("global")):
        with pytest.raises(ValueError):
            bad()


# --- provider budgets ---------------------------------------------------------------------


def test_provider_day_ceiling_refuses_only_that_provider(tmp_path):
    R, day = _r(), _roomy()
    data = make(tmp_path, finance=f.finance_config(day=day, provider_day=(2 * R + R // 2, day[0], day[0])))
    try:
        assert [f.attempt(data.service, value=MISSING)["outcome"] for _ in range(3)] == ["ok", "ok", "busy"]
        # The provider ceiling is across scopes: a paying subscriber is refused too.
        refused = f.attempt(data.service, traffic="paid", value=MISSING)
        assert refused == {"entered": 0, "dispatched": 0, "outcome": "busy"}
        assert pool(data) == (2, 2)  # A refused reservation consumed no attempt.
        assert len(f.reservations(data.factory)) == 2
        assert f.provider_bucket(data.factory, "day", DAY, "openai") == (0, 2 * R, 0)
        # The other providers keep their own budgets.
        assert f.attempt(data.service, provider="brave", value={"results": []})["outcome"] == "ok"
        assert f.attempt(data.service, provider="tavily", value={"results": []})["outcome"] == "ok"
        assert f.provider_bucket(data.factory, "day", DAY, "brave") == (0, 5_000, 0)
        assert f.provider_bucket(data.factory, "day", DAY, "tavily") == (0, 8_000, 0)
        f.check_invariants(data.factory, data.finance.limits)
    finally:
        data.engine.dispose()


def test_zero_provider_ceiling_stops_that_provider_entirely(tmp_path):
    day = _roomy()
    data = make(tmp_path, finance=f.finance_config(day=day, provider_day=(day[0], day[0], 0)))
    try:
        assert f.attempt(data.service, provider="tavily") == {"entered": 0, "dispatched": 0, "outcome": "busy"}
        assert f.attempt(data.service, provider="brave", value={"results": []})["outcome"] == "ok"
        assert pool(data, "tavily") == (0, 0) and len(f.reservations(data.factory)) == 1
    finally:
        data.engine.dispose()


def test_provider_month_ceiling_spans_days(tmp_path):
    R, day = _r(), _roomy()
    data = make(tmp_path, finance=f.finance_config(
        day=day, provider_day=(2 * R, day[0], day[0]), provider_month=(3 * R, day[0] * 31, day[0] * 31)))
    try:
        assert [f.attempt(data.service, value=MISSING)["outcome"] for _ in range(3)] == ["ok", "ok", "busy"]
        data.clock["now"] = NOW + timedelta(days=1)
        assert [f.attempt(data.service, value=MISSING)["outcome"] for _ in range(2)] == ["ok", "busy"]
        assert f.provider_bucket(data.factory, "month", MONTH, "openai") == (0, 3 * R, 0)
        data.clock["now"] = datetime(2030, 4, 1, 0, 0, 1, tzinfo=timezone.utc)  # A new month: room again.
        assert f.attempt(data.service, value=MISSING)["outcome"] == "ok"
        f.check_invariants(data.factory, data.finance.limits)
    finally:
        data.engine.dispose()


def test_provider_buckets_follow_release_settlement_and_overrun(ledger):
    R = f.openai_reservation(ledger.finance)
    seen = {}
    f.attempt(ledger.service, behavior="reject_before_dispatch",
              on_event=lambda name: seen.setdefault(name, f.provider_bucket(ledger.factory, "day", DAY, "openai")))
    assert seen["entered"] == (R, 0, 0)  # Committed before the provider call was entered.
    assert f.provider_bucket(ledger.factory, "day", DAY, "openai") == (0, 0, 0)  # Released in full.
    f.attempt(ledger.service, value=f.model_response(1000, 200, cached=400))
    charged = -(-(600 * 125_000 + 400 * 10_000 + 200 * 500_000) // 1_000_000)
    assert f.provider_bucket(ledger.factory, "day", DAY, "openai") == (0, charged, 0)
    f.attempt(ledger.service, traffic="paid", value=f.model_response(10**6, 3200))  # Overrun, never capped.
    overrun = [r for r in f.reservations(ledger.factory) if r["overrun"]][0]
    assert f.provider_bucket(ledger.factory, "day", DAY, "openai") == (
        0, charged + overrun["charged"], overrun["overrun"])
    assert f.provider_bucket(ledger.factory, "month", MONTH, "openai") == (
        0, charged + overrun["charged"], overrun["overrun"])
    f.check_invariants(ledger.factory, ledger.finance.limits)


def test_released_provider_capacity_is_reusable(tmp_path):
    R, day = _r(), _roomy()
    data = make(tmp_path, finance=f.finance_config(day=day, provider_day=(R, day[0], day[0])))
    try:
        assert f.attempt(data.service, behavior="reject_before_dispatch")["outcome"] == "local_reject"
        assert f.attempt(data.service, value=MISSING)["outcome"] == "ok"
        assert f.attempt(data.service, value=MISSING)["outcome"] == "busy"
    finally:
        data.engine.dispose()


# --- the per-request dollar ceiling -------------------------------------------------------


def test_request_ceiling_is_a_processing_limit_not_a_capacity_failure():
    from kalillac_routing import provider_controls as c
    assert issubclass(c.RequestCostCeiling, c.CeilingStop)
    assert not issubclass(c.RequestCostCeiling, (c.CeilingBusy, c.CeilingUnavailable))
    assert (c.RequestCostCeiling.status_code, c.RequestCostCeiling.code) == (422, "processing_limit_reached")
    # Usage observation counts it as Kalillac's own allowance, never as a provider failure.
    from kalillac_routing import provider_usage
    assert provider_usage._failure(c.RequestCostCeiling()) == provider_usage._failure(c.CeilingBusy()) == "allowance"
    source = (CANDIDATE / "app_fastapi_candidate.py").read_text(encoding="utf-8")
    # Both chat handlers answer a CeilingStop with its own status and code.
    assert source.count("_budget_error(stop.status_code, stop.code)") == 1
    assert source.count("_budget_error(defect.status_code, defect.code)") >= 1


def test_request_ceiling_bounds_all_attempts_of_one_request(tmp_path):
    R, day = _r(), _roomy()
    data = make(tmp_path, finance=f.finance_config(day=day, max_request=(2 * R + R // 2, day[2])))
    try:
        results = f.request(data.service, [dict(value=MISSING)] * 4)
        assert [r["outcome"] for r in results] == ["ok", "ok", "request_ceiling", "request_ceiling"]
        assert [r["authorized"] for r in results] == [R, 2 * R, 2 * R, 2 * R]
        assert [(r["entered"], r["dispatched"]) for r in results[2:]] == [(0, 0), (0, 0)]
        assert pool(data) == (2, 2) and len(f.reservations(data.factory)) == 2  # Refused before either ledger.
        # The ceiling is per request: the next request starts from zero.
        assert f.attempt(data.service, value=MISSING)["outcome"] == "ok"
        f.check_invariants(data.factory, data.finance.limits)
    finally:
        data.engine.dispose()


def test_one_attempt_above_the_request_ceiling_touches_no_ledger(tmp_path):
    R, day = _r(), _roomy()
    data = make(tmp_path, finance=f.finance_config(day=day, max_request=(R - 1, R - 1)))
    try:
        for traffic in ("anonymous", "free", "paid"):
            assert f.attempt(data.service, traffic=traffic) == {
                "entered": 0, "dispatched": 0, "outcome": "request_ceiling"}
        assert pool(data) == (0, 0)
        assert f.reservations(data.factory) == [] and f.buckets(data.factory) == {}
        assert f.provider_buckets(data.factory) == {}
    finally:
        data.engine.dispose()


def test_free_and_paid_requests_have_their_own_ceilings(tmp_path):
    R, day = _r(), _roomy()
    data = make(tmp_path, finance=f.finance_config(day=day, max_request=(R, 3 * R)))
    try:
        for traffic in ("anonymous", "free"):
            assert [r["outcome"] for r in f.request(data.service, [dict(value=MISSING)] * 2, traffic)] == \
                ["ok", "request_ceiling"]
        assert [r["outcome"] for r in f.request(data.service, [dict(value=MISSING)] * 4, "paid")] == \
            ["ok", "ok", "ok", "request_ceiling"]
    finally:
        data.engine.dispose()


def test_every_provider_counts_toward_the_same_request(tmp_path):
    R, day = _r(), _roomy()
    data = make(tmp_path, finance=f.finance_config(day=day, max_request=(R + 5_000, day[2])))
    try:
        results = f.request(data.service, [dict(value=MISSING), dict(provider="brave", value={"results": []}),
                                           dict(provider="tavily", value={"results": []})])
        assert [r["outcome"] for r in results] == ["ok", "ok", "request_ceiling"]
        assert [r["authorized"] for r in results] == [R, R + 5_000, R + 5_000]
        assert pool(data, "tavily") == (0, 0)
    finally:
        data.engine.dispose()


def test_confirmed_settlement_below_the_reservation_returns_the_difference(tmp_path):
    R, day = _r(), _roomy()
    data = make(tmp_path, finance=f.finance_config(day=day, max_request=(R + R // 2, day[2])))
    try:
        charged = -(-(1000 * 125_000 + 200 * 500_000) // 1_000_000)
        assert 5 * charged < R // 2
        results = f.request(data.service, [dict(value=f.model_response(1000, 200))] * 5)
        assert [r["outcome"] for r in results] == ["ok"] * 5
        assert [r["authorized"] for r in results] == [charged * n for n in range(1, 6)]
    finally:
        data.engine.dispose()


def test_released_attempt_returns_its_whole_authorization(tmp_path):
    R, day = _r(), _roomy()
    data = make(tmp_path, finance=f.finance_config(day=day, max_request=(R, day[2])))
    try:
        results = f.request(data.service, [dict(behavior="reject_before_dispatch"), dict(value=MISSING),
                                           dict(value=MISSING)])
        assert [r["outcome"] for r in results] == ["local_reject", "ok", "request_ceiling"]
        assert [r["authorized"] for r in results] == [0, R, R]
    finally:
        data.engine.dispose()


def test_refused_reservation_returns_its_authorization(tmp_path):
    R = _r()
    day = f.scopes_for(R, 10 * R)
    data = make(tmp_path, finance=f.finance_config(day=day, max_request=(R, 10 * R)))
    try:
        assert f.attempt(data.service, value=MISSING)["outcome"] == "ok"  # FREE's day ceiling is now used up.
        [result] = f.request(data.service, [dict(value=MISSING)])
        assert (result["outcome"], result["authorized"]) == ("busy", 0)
    finally:
        data.engine.dispose()


def test_unconfirmed_settlement_and_dispatched_failure_stay_authorized(tmp_path):
    R, day = _r(), _roomy()
    data = make(tmp_path, finance=f.finance_config(day=day, max_request=(R + R // 2, day[2])))
    try:
        original = data.service._factory

        def outage(name):
            if name == "dispatched":
                data.service._factory = lambda: (_ for _ in ()).throw(RuntimeError("database outage"))
        results = f.request(data.service, [dict(value=f.model_response(1, 1), on_event=outage), dict(value=MISSING)])
        data.service._factory = original
        # The cheap response could not be settled, so its full reservation still counts.
        assert [(r["outcome"], r["authorized"]) for r in results] == [("ok", R), ("request_ceiling", R)]
        results = f.request(data.service, [dict(behavior="fail_after_dispatch"), dict(value=MISSING)])
        assert [(r["outcome"], r["authorized"]) for r in results] == [("provider_failure", R), ("request_ceiling", R)]
    finally:
        data.engine.dispose()


def test_request_authorization_is_exact_under_concurrent_attempts():
    from kalillac_routing import provider_controls as c
    state = c.ControlRequest(service=None)
    admitted, refused = [], []
    barrier = threading.Barrier(40)

    def worker():
        barrier.wait(timeout=10)
        try:
            state.authorize_cost(7, 100)
            admitted.append(1)
        except c.RequestCostCeiling:
            refused.append(1)
    threads = [threading.Thread(target=worker) for _ in range(40)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert (len(admitted), len(refused), state.authorized_microusd) == (14, 26, 98)
    for bad in (-1, True, 1.0, None, "1"):
        with pytest.raises(c.RequestCostCeiling):
            state.authorize_cost(bad, 100)
    state.adjust_cost(-10**9)
    assert state.authorized_microusd == 0
    state.close()
    with pytest.raises(c.CeilingUnavailable):
        state.authorize_cost(1, 100)


def test_disabled_finance_never_counts_request_authorization(tmp_path):
    data = make(tmp_path)
    try:
        data.service.finance = None
        results = f.request(data.service, [dict(value=MISSING)] * 3)
        assert [(r["outcome"], r["authorized"]) for r in results] == [("ok", 0)] * 3
        assert f.provider_buckets(data.factory) == {} and f.reservations(data.factory) == []
    finally:
        data.engine.dispose()


# --- exactly-once resolution --------------------------------------------------------------


def _reserve(data, traffic=TrafficClass.ANONYMOUS):
    from kalillac_db.repositories import finance_ledger
    from kalillac_finance.runtime import charge_for
    charge = charge_for(data.finance.prices, "openai", (dict(f.PAYLOAD),), traffic)
    with data.factory() as session, session.begin():
        return finance_ledger.reserve(session, charge, NOW, data.finance.limits)


def _apply(data, operation):
    with data.factory() as session, session.begin():
        return operation(session)


def test_duplicate_and_conflicting_resolutions_apply_exactly_once(ledger):
    from kalillac_db.repositories import finance_ledger
    from kalillac_finance.cost import settle as settlement
    R = f.openai_reservation(ledger.finance)
    limits = ledger.finance.limits
    first, second = _reserve(ledger), _reserve(ledger)
    assert f.bucket(ledger.factory, "day", DAY, "free") == (2 * R, 0, 0)

    def settle(lease, actual):
        return _apply(ledger, lambda s: finance_ledger.settle(s, lease, settlement(R, actual), NOW, limits))

    def release(lease):
        return _apply(ledger, lambda s: finance_ledger.release(s, lease, NOW))

    assert settle(first, 100) is True
    after = (f.buckets(ledger.factory), f.provider_buckets(ledger.factory), f.reservations(ledger.factory))
    assert f.bucket(ledger.factory, "day", DAY, "free") == (R, 100, 0)
    # A duplicate settlement, a different later amount and a late release change nothing.
    assert settle(first, 100) is False and settle(first, 999) is False and release(first) is False
    assert (f.buckets(ledger.factory), f.provider_buckets(ledger.factory), f.reservations(ledger.factory)) == after

    assert release(second) is True
    after = (f.buckets(ledger.factory), f.provider_buckets(ledger.factory), f.reservations(ledger.factory))
    assert f.bucket(ledger.factory, "day", DAY, "free") == (0, 100, 0)
    assert release(second) is False and settle(second, 100) is False
    assert (f.buckets(ledger.factory), f.provider_buckets(ledger.factory), f.reservations(ledger.factory)) == after
    assert f.latches(ledger.factory) == {}
    f.check_invariants(ledger.factory, limits)


def test_settlement_for_another_reservation_amount_is_refused_and_rolled_back(ledger):
    from kalillac_db.repositories import finance_ledger
    from kalillac_finance.cost import settle as settlement
    R = f.openai_reservation(ledger.finance)
    lease = _reserve(ledger)
    with pytest.raises(ValueError):
        _apply(ledger, lambda s: finance_ledger.settle(s, lease, settlement(R + 1, 10), NOW, ledger.finance.limits))
    assert [r["state"] for r in f.reservations(ledger.factory)] == ["reserved"]
    assert f.bucket(ledger.factory, "day", DAY, "free") == (R, 0, 0)


def test_unknown_provider_or_scope_reserves_nothing(ledger):
    from dataclasses import replace
    from kalillac_db.repositories import finance_ledger
    from kalillac_finance.runtime import charge_for
    from kalillac_finance.scopes import FinancialScope
    from kalillac_routing.provider_controls import CeilingUnavailable
    charge = charge_for(ledger.finance.prices, "openai", (dict(f.PAYLOAD),), TrafficClass.ANONYMOUS)
    for bad in (replace(charge, provider="groq"), replace(charge, scope=FinancialScope.GLOBAL),
                replace(charge, reserved=-1), replace(charge, reserved=1.5)):
        with pytest.raises(CeilingUnavailable):
            _apply(ledger, lambda s: finance_ledger.reserve(s, bad, NOW, ledger.finance.limits))
    assert f.reservations(ledger.factory) == [] and f.provider_buckets(ledger.factory) == {}


# --- operator audit and reconciliation ----------------------------------------------------


def _crash_after_dispatch(data):
    """A process death between dispatch and cleanup: neither release nor settle runs."""
    release, settle = data.service.release, data.service.settle
    data.service.release = lambda lease: None
    data.service.settle = lambda lease, value, returned: None
    try:
        f.attempt(data.service, value=f.model_response(1, 1))
    finally:
        data.service.release, data.service.settle = release, settle


def test_verify_recomputes_every_bucket_and_detects_tampering(ledger):
    from sqlalchemy import text
    from kalillac_db.repositories import finance_admin
    f.attempt(ledger.service, value=f.model_response(1000, 200))
    f.attempt(ledger.service, traffic="paid", provider="brave", value={"results": []})
    f.attempt(ledger.service, behavior="reject_before_dispatch")
    _crash_after_dispatch(ledger)
    assert _apply(ledger, finance_admin.verify) == []
    report = _apply(ledger, lambda s: finance_admin.summary(s, NOW, ledger.finance.limits))
    R = f.openai_reservation(ledger.finance)
    assert report["violations"] == [] and report["latches"] == {}
    assert report["outstanding"]["count"] == 1 and report["outstanding"]["microusd"] == R
    assert report["periods"] == {"day": "2030-03-14", "month": "2030-03-01"}
    assert report["providers"]["day"]["brave"] == {"reserved": 0, "settled": 5_000, "overrun": 0,
                                                    "limit": ledger.finance.limits.provider_limit("day", "brave")}
    assert report["scopes"]["day"]["paid"]["settled"] == 5_000
    json.dumps(report)  # Plain data only.

    for statement, key in (
            ("UPDATE kalillac.finance_ledger_buckets SET settled_microusd = settled_microusd + 1 "
             "WHERE period_kind = 'day' AND scope = 'free'", ("scope", "day", "2030-03-14", "free")),
            ("UPDATE kalillac.finance_provider_buckets SET reserved_microusd = reserved_microusd + 7 "
             "WHERE period_kind = 'month' AND provider = 'openai'", ("provider", "month", "2030-03-01", "openai"))):
        with ledger.engine.begin() as conn:
            conn.execute(text(statement))
        violations = _apply(ledger, finance_admin.verify)
        assert [v[:4] for v in violations] == [key]
        with ledger.engine.begin() as conn:
            conn.execute(text(statement.replace("+ 1", "- 1").replace("+ 7", "- 7")))
    assert _apply(ledger, finance_admin.verify) == []


def test_resolve_stale_charges_in_full_exactly_once_and_records_the_action(ledger):
    from kalillac_db.repositories import finance_admin
    R = f.openai_reservation(ledger.finance)
    _crash_after_dispatch(ledger)
    f.attempt(ledger.service, value=MISSING)  # A settled reservation is never stale.
    [stale] = [r for r in f.reservations(ledger.factory) if r["state"] == "reserved"]
    hour = finance_admin.MIN_STALE_SECONDS

    def ids(now):
        return _apply(ledger, lambda s: finance_admin.stale_ids(s, now, hour))

    def resolve(now):
        return _apply(ledger, lambda s: finance_admin.resolve_stale(s, stale["id"], now, hour))

    assert ids(NOW + timedelta(minutes=59)) == []
    assert resolve(NOW + timedelta(minutes=59)) is None  # Still young: nothing changes.
    assert f.operator_actions(ledger.factory) == []
    later = NOW + timedelta(hours=2)
    assert ids(later) == [stale["id"]]
    assert resolve(later) == R
    [row] = [r for r in f.reservations(ledger.factory) if r["id"] == stale["id"]]
    assert (row["state"], row["charged"], row["basis"], row["overrun"]) == ("settled", R, "unknown", 0)
    assert f.bucket(ledger.factory, "day", DAY, "free") == (0, 2 * R, 0)  # Exposure unchanged, nothing refunded.
    assert f.provider_bucket(ledger.factory, "month", MONTH, "openai") == (0, 2 * R, 0)
    assert f.operator_actions(ledger.factory) == [
        dict(action="resolve_stale", scope="free", reason=None, reservation_id=stale["id"], amount=R)]
    assert resolve(later) is None and ids(later) == []  # A second run finds nothing.
    assert len(f.operator_actions(ledger.factory)) == 1
    assert f.latches(ledger.factory) == {}
    f.check_invariants(ledger.factory, ledger.finance.limits)


def test_resolve_stale_refuses_a_window_under_one_hour(ledger):
    from kalillac_db.repositories import finance_admin
    for bad in (0, 3599, -1, 3600.0, None, True):
        with pytest.raises(ValueError):
            _apply(ledger, lambda s: finance_admin.stale_ids(s, NOW, bad))
        with pytest.raises(ValueError):
            _apply(ledger, lambda s: finance_admin.resolve_stale(s, None, NOW, bad))
    with pytest.raises(ValueError):
        _apply(ledger, lambda s: finance_admin.stale_ids(s, NOW, 3600, batch=0))


def test_clear_latch_requires_the_named_reason_and_is_recorded(ledger):
    from kalillac_db.repositories import finance_admin
    f.attempt(ledger.service, value=f.model_response(10**6, 3200))  # FREE overrun.
    assert f.latches(ledger.factory) == {"free": "overrun"}
    [overrun] = f.reservations(ledger.factory)

    def clear(scope, reason):
        return _apply(ledger, lambda s: finance_admin.clear_latch(s, scope, reason, NOW))

    assert clear("free", "bound_exceeded") is False and clear("paid", "overrun") is False
    assert f.latches(ledger.factory) == {"free": "overrun"} and f.operator_actions(ledger.factory) == []
    assert f.attempt(ledger.service)["entered"] == 0  # Still stopped.
    for bad in (("free", "anything"), ("everyone", "overrun")):
        with pytest.raises(ValueError):
            clear(*bad)
    assert clear("free", "overrun") is True
    assert f.latches(ledger.factory) == {}
    assert f.operator_actions(ledger.factory) == [
        dict(action="clear_latch", scope="free", reason="overrun", reservation_id=overrun["id"], amount=0)]
    assert clear("free", "overrun") is False and len(f.operator_actions(ledger.factory)) == 1
    assert f.attempt(ledger.service, value=MISSING)["outcome"] == "ok"  # Admission resumes.
    # The recorded overrun itself is history: clearing the latch does not erase it.
    assert f.bucket(ledger.factory, "day", DAY, "free")[2] == overrun["overrun"]
    f.check_invariants(ledger.factory, ledger.finance.limits)


# --- the operator command -----------------------------------------------------------------


def _run(tool, data, *argv):
    out = io.StringIO()
    code = tool.run(list(argv), factory=data.factory, out=out)
    return code, out.getvalue()


def test_admin_command_reports_resolves_and_refuses(tmp_path, monkeypatch):
    from sqlalchemy import text
    tool = _admin_tool()
    monkeypatch.delenv("KALILLAC_FINANCIAL_SAFEGUARDS_ENABLED", raising=False)
    # The command reads the real clock, so the stranded reservation is three hours old.
    data = make(tmp_path, now=datetime.now(timezone.utc) - timedelta(hours=3))
    try:
        R = f.openai_reservation(data.finance)
        _crash_after_dispatch(data)
        f.attempt(data.service, provider="brave", value={"results": []})

        code, output = _run(tool, data, "report")
        assert code == tool.EXIT_OK and output.strip().endswith("finance_ledger_consistent")
        assert "financial safeguards configuration: off" in output
        assert f"outstanding reservations: 1 holding {tool._usd(R)}" in output and "unsafe latches: none" in output
        code, output = _run(tool, data, "report", "--json")
        assert code == tool.EXIT_OK
        parsed = json.loads(output[:output.rindex("finance_ledger_consistent")])
        assert parsed["outstanding"]["count"] == 1 and parsed["configuration"] == "off"

        for argv in (("resolve-stale", "--older-than-hours", "0"), ("resolve-stale", "--older-than-hours", "9000"),
                     ("resolve-stale",), ("clear-latch", "--scope", "free", "--expect-reason", "overrun"),
                     ("clear-latch", "--scope", "everyone", "--expect-reason", "overrun", "--apply"),
                     ("report", "--all"), ("resolve-all",), ()):
            code, _output = _run(tool, data, *argv)
            assert code == tool.EXIT_REFUSED, argv
        assert [r["state"] for r in f.reservations(data.factory)].count("reserved") == 1

        code, output = _run(tool, data, "resolve-stale", "--older-than-hours", "1")  # Dry run by default.
        assert code == tool.EXIT_OK and output.startswith("stale_reservations_found 1 ")
        assert f.operator_actions(data.factory) == []
        code, output = _run(tool, data, "resolve-stale", "--older-than-hours", "4", "--apply")
        assert (code, output.strip()) == (tool.EXIT_OK, "stale_reservations_resolved 0 charged $0.000000")
        code, output = _run(tool, data, "resolve-stale", "--older-than-hours", "1", "--apply")
        assert (code, output.strip()) == (tool.EXIT_OK, f"stale_reservations_resolved 1 charged {tool._usd(R)}")
        assert {r["state"] for r in f.reservations(data.factory)} == {"settled"}
        assert [a["action"] for a in f.operator_actions(data.factory)] == ["resolve_stale"]
        code, output = _run(tool, data, "resolve-stale", "--older-than-hours", "1", "--apply")
        assert output.strip() == "stale_reservations_resolved 0 charged $0.000000"

        code, output = _run(tool, data, "clear-latch", "--scope", "free", "--expect-reason", "overrun", "--apply")
        assert (code, output.strip()) == (tool.EXIT_NO_LATCH, "latch_not_found")
        f.attempt(data.service, value=f.model_response(10**6, 3200))
        code, output = _run(tool, data, "clear-latch", "--scope", "free", "--expect-reason", "overrun", "--apply")
        assert (code, output.strip()) == (tool.EXIT_OK, "latch_cleared free overrun")
        assert f.latches(data.factory) == {}

        with data.engine.begin() as conn:
            conn.execute(text("UPDATE kalillac.finance_provider_buckets SET settled_microusd = settled_microusd + 3 "
                              "WHERE period_kind = 'day' AND provider = 'brave'"))
        code, output = _run(tool, data, "report")
        assert code == tool.EXIT_INCONSISTENT and "INCONSISTENT provider day" in output
        assert output.strip().endswith("finance_ledger_inconsistent")
    finally:
        data.engine.dispose()


def test_admin_command_reports_failures_without_details(tmp_path):
    tool = _admin_tool()

    def broken():
        raise RuntimeError("postgresql+psycopg://user:secret@host/db")
    out = io.StringIO()
    assert tool.run(["report"], factory=broken, out=out) == tool.EXIT_FAILED
    assert out.getvalue().strip() == "unexpected_error RuntimeError"


def test_admin_command_is_refused_without_a_database(monkeypatch):
    tool = _admin_tool()
    monkeypatch.delenv("KALILLAC_DB_ENABLED", raising=False)
    out = io.StringIO()
    assert tool.run(["report"], out=out) == tool.EXIT_REFUSED
    assert out.getvalue().strip() == "refused database_not_enabled"


def test_no_application_route_or_module_reaches_the_admin_functions():
    source = (CANDIDATE / "app_fastapi_candidate.py").read_text(encoding="utf-8")
    assert "finance_admin" not in source and "finance_ledger_admin" not in source
    for path in (CANDIDATE / "kalillac_routing").glob("*.py"):
        assert "finance_admin" not in path.read_text(encoding="utf-8"), path.name
    assert date(2030, 3, 14) == DAY  # The shared fixture clock these tests assume.


def test_0016_rendered_offline_carries_its_empty_ledger_refusal(monkeypatch):
    """Codex audit D1: `alembic upgrade --sql` cannot read the ledger, so the refusal must be SQL the
    database evaluates when the script is applied, ahead of the new tables (verified against a
    populated ledger in test_provider_controls_postgres)."""
    import io
    from pathlib import Path
    from alembic import command
    from alembic.config import Config
    from kalillac_db import migration_roles
    for name, value in ((migration_roles.OWNER_ROLE_ENV, "kalillac_owner"), (migration_roles.APP_ROLE_ENV, "kalillac_app"),
                        ("KALILLAC_MIGRATION_REGISTRATION_ROLE", "kalillac_registration"),
                        (migration_roles.V1_SECURITY_SERVICE_ROLE_ENV, "kalillac_v1_service"),
                        (migration_roles.V1_SECURITY_EXECUTOR_ROLE_ENV, "kalillac_v1_executor")):
        monkeypatch.setenv(name, value)
    candidate = Path(__file__).resolve().parents[1]
    config = Config(str(candidate / "alembic.ini"))
    config.set_main_option("script_location", str(candidate / "migrations"))
    config.set_main_option("sqlalchemy.url", "postgresql+psycopg://offline@127.0.0.1/offline")
    config.output_buffer = io.StringIO()
    command.upgrade(config, "0015_finance_ledger:0016_finance_provider_budgets", sql=True)
    script = config.output_buffer.getvalue()
    lock = script.index("LOCK TABLE kalillac.finance_reservations IN SHARE MODE")
    guard = script.index("IF EXISTS (SELECT 1 FROM kalillac.finance_reservations) THEN")
    assert lock < guard < script.index("CREATE TABLE kalillac.finance_provider_buckets")
    assert "finance_reservations is not empty: provider buckets cannot start at zero." in script
