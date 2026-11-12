"""Shared, content-free helpers for the Financial Safeguards ledger tests.

Prices are the owner-approved official values (as explicit operator values);
every ceiling is a synthetic test value. Provider terminals are local mocks:
nothing leaves the process.
"""
from __future__ import annotations

from datetime import date
import os
import time

from kalillac_finance.config import load_financial_config
from kalillac_finance.cost import reserve_openai

OFFICIAL_PRICES = {
    "KALILLAC_FINANCIAL_SAFEGUARDS_ENABLED": "true",
    "KALILLAC_PRICE_OPENAI_MODEL": "gpt-6-luna",
    "KALILLAC_PRICE_OPENAI_INPUT_USD_PER_MTOK": "0.10",
    "KALILLAC_PRICE_OPENAI_CACHED_INPUT_USD_PER_MTOK": "0.01",
    "KALILLAC_PRICE_OPENAI_CACHE_WRITE_USD_PER_MTOK": "0.125",
    "KALILLAC_PRICE_OPENAI_OUTPUT_USD_PER_MTOK": "0.50",
    "KALILLAC_PRICE_OPENAI_LONG_CONTEXT_ABOVE_INPUT_TOKENS": "272000",
    "KALILLAC_PRICE_OPENAI_LONG_CONTEXT_INPUT_USD_PER_MTOK": "0.20",
    "KALILLAC_PRICE_OPENAI_LONG_CONTEXT_CACHED_INPUT_USD_PER_MTOK": "0.02",
    "KALILLAC_PRICE_OPENAI_LONG_CONTEXT_CACHE_WRITE_USD_PER_MTOK": "0.25",
    "KALILLAC_PRICE_OPENAI_LONG_CONTEXT_OUTPUT_USD_PER_MTOK": "0.75",
    "KALILLAC_FINANCE_OPENAI_INPUT_OVERHEAD_TOKENS": "8192",
    "KALILLAC_PRICE_BRAVE_USD_PER_REQUEST": "0.005",
    "KALILLAC_PRICE_TAVILY_USD_PER_CREDIT": "0.008",
    "KALILLAC_FINANCE_TAVILY_MAX_CREDITS_SEARCH_BASIC": "1",
    "KALILLAC_FINANCE_TAVILY_MAX_CREDITS_SEARCH_ADVANCED": "2",
    "KALILLAC_FINANCE_TAVILY_MAX_CREDITS_EXTRACT_BASIC": "1",
    "KALILLAC_FINANCE_TAVILY_MAX_CREDITS_EXTRACT_ADVANCED": "2",
}

# The fixed synthetic model payload every finance test attempt sends.
PAYLOAD = {"model": "gpt-6-luna", "instructions": "synthetic", "input": [{"role": "user", "content": "synthetic"}],
           "store": False, "reasoning": {"effort": "medium"}, "max_output_tokens": 3200}
BIG = 10**12  # A ceiling large enough never to bind ($1,000,000).


def usd(microusd):
    return f"{microusd // 1_000_000}.{microusd % 1_000_000:06d}"


def limits_env(day, month, percent=10):
    """day/month: (global, free, paid) in µUSD."""
    env = {"KALILLAC_FINANCE_MIN_HEADROOM_PERCENT": str(percent)}
    for period, values in (("DAY", day), ("MONTH", month)):
        for scope, value in zip(("GLOBAL", "FREE", "PAID"), values):
            env[f"KALILLAC_FINANCE_{period}_{scope}_LIMIT_USD"] = usd(value)
    return env


def finance_config(day=(BIG, BIG * 4 // 10, BIG * 4 // 10), month=None, percent=10):
    month = month or tuple(v * 31 for v in day)
    return load_financial_config({**OFFICIAL_PRICES, **limits_env(day, month, percent)})


def scopes_for(free, paid):
    """(global, free, paid) day ceilings with exactly 20% global headroom."""
    return ((free + paid) * 5 // 4 + 1, free, paid)


def openai_reservation(config):
    return reserve_openai(config.prices.openai, PAYLOAD).microusd


def model_response(input_tokens=None, output_tokens=None, cached=0, usage=True):
    data = {"status": "completed", "model": "gpt-6-luna", "service_tier": "default",
            "output": [{"type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": "synthetic"}]}]}
    if usage:
        data["usage"] = {"input_tokens": input_tokens, "output_tokens": output_tokens,
                         "input_tokens_details": {"cached_tokens": cached},
                         "output_tokens_details": {"reasoning_tokens": 0}}
    return data


class LocalReject(Exception):
    """A confirmed local rejection after admission, before dispatch."""


class ProviderFailure(Exception):
    """A provider failure after dispatch."""


def attempt(service, provider="openai", traffic="anonymous", behavior="ok", value=None, depth="basic",
            on_event=None, payload=None):
    """One attempt through protect() exactly as the application wraps it.

    traffic: "anonymous", "free" (signed in, no paid entitlement) or "paid"
    (signed in WITH an authoritative paid entitlement for this request).
    """
    from kalillac_accounts.entitlements import PAID
    from kalillac_finance.traffic import issue_paid_entitlement
    from kalillac_routing import provider_controls, provider_usage
    from kalillac_routing.request_budget import RequestBudget, budget_scope
    ledger = {"entered": 0, "dispatched": 0}

    def terminal(*_args, **_kwargs):
        ledger["entered"] += 1
        if on_event:
            on_event("entered")
        if behavior == "reject_before_dispatch":
            raise LocalReject()
        if behavior == "hang_before_dispatch":
            while True:
                time.sleep(1)
        provider_usage.dispatched()
        ledger["dispatched"] += 1
        if on_event:
            on_event("dispatched")
        if behavior == "hang_after_dispatch":
            while True:
                time.sleep(1)
        if behavior == "exit_after_dispatch":
            os._exit(0)  # A real process death: no finally, release or settlement runs.
        if behavior == "fail_after_dispatch":
            raise ProviderFailure()
        return value

    previous = provider_controls.SERVICE
    provider_controls.SERVICE = service
    state, token = provider_controls.begin_request()
    budget = RequestBudget(duration_seconds=30, max_model_attempts=8, max_search_attempts=8)
    try:
        proof = issue_paid_entitlement(state, PAID) if traffic == "paid" else None
        provider_controls.account_status(traffic != "anonymous", proof)
        with budget_scope(budget):
            if provider == "openai":
                provider_controls.protect(provider_usage.observe_model(terminal), "openai")(
                    dict(payload or PAYLOAD))
            elif provider == "brave":
                provider_controls.protect(provider_usage.observe_single_search(terminal))("brave", {})
            elif provider == "tavily":
                key = "search_depth"
                provider_controls.protect(provider_usage.observe_tavily(terminal), "tavily")(
                    budget, "search", {key: depth})
            else:
                raise ValueError(provider)
        outcome = "ok"
    except provider_controls.CeilingBusy:
        outcome = "busy"
    except provider_controls.CeilingUnavailable:
        outcome = "unavailable"
    except LocalReject:
        outcome = "local_reject"
    except ProviderFailure:
        outcome = "provider_failure"
    finally:
        provider_controls.end_request(state, token)
        provider_controls.SERVICE = previous
    return dict(ledger, outcome=outcome)


# --- ledger inspection --------------------------------------------------------------------


def buckets(factory):
    from kalillac_db.models import FinanceLedgerBucket
    with factory() as session:
        return {(r.period_kind, r.period_start, r.scope): (r.reserved_microusd, r.settled_microusd, r.overrun_microusd)
                for r in session.query(FinanceLedgerBucket).all()}


def bucket(factory, kind, start, scope):
    return buckets(factory).get((kind, start, scope), (0, 0, 0))


def reservations(factory):
    from kalillac_db.models import FinanceReservation
    with factory() as session:
        return [dict(id=r.id, provider=r.provider, scope=r.scope, day=r.day, month=r.month,
                     reserved=r.reserved_microusd, state=r.state, charged=r.charged_microusd,
                     overrun=r.overrun_microusd, bound_exceeded=r.bound_exceeded, basis=r.basis)
                for r in session.query(FinanceReservation).all()]


def latches(factory):
    from kalillac_db.models import FinanceUnsafeLatch
    with factory() as session:
        return {r.scope: r.reason for r in session.query(FinanceUnsafeLatch).all()}


def check_invariants(factory, limits, periods=None):
    """Every bucket equals the sum of its reservations; GLOBAL = FREE + PAID;
    no bucket's exposure exceeds its ceiling except by its recorded overrun."""
    rows, held = buckets(factory), reservations(factory)
    for (kind, start, scope), (reserved, settled, overrun) in rows.items():
        if periods is not None and start not in periods:
            continue
        mine = [r for r in held if (r["day"] if kind == "day" else r["month"]) == start
                and (scope == "global" or r["scope"] == scope)]
        assert reserved == sum(r["reserved"] for r in mine if r["state"] == "reserved"), (kind, start, scope)
        assert settled == sum(r["charged"] for r in mine if r["state"] == "settled"), (kind, start, scope)
        assert overrun == sum(r["overrun"] for r in mine if r["state"] == "settled"), (kind, start, scope)
        assert reserved + settled <= limits.limit(kind, scope) + overrun, (kind, start, scope)
        if scope == "global":
            parts = [rows.get((kind, start, s), (0, 0, 0)) for s in ("free", "paid")]
            assert (reserved, settled, overrun) == tuple(sum(p[i] for p in parts) for i in range(3))
    return rows


def first_of_month(day):
    return date(day.year, day.month, 1)
