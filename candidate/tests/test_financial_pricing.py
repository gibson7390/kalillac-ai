"""Financial Safeguards V1, batch 1: money, pricing, usage, cost, scopes.

Every price here is a synthetic test value, not a production price. Property
checks use fixed seeds so every run is identical. No network, database or
provider is used.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import random
import re
import subprocess
import sys
import time

import pytest

from kalillac_finance import cost, money, pricing, scopes, usage
from kalillac_finance.cost import ReservationError
from kalillac_finance.money import MoneyError
from kalillac_finance.pricing import (BravePricing, LongContext, OpenAIPricing, PricingConfigError, TavilyPricing,
                                      TokenRates)
from kalillac_finance.scopes import FinancialScope, ScopeCeilings, TrafficClass
from kalillac_finance.usage import OpenAIUsage
from kalillac_routing.provider_usage import MAX_TOKENS

CANDIDATE = Path(__file__).resolve().parents[1]
MTOK = 1_000_000

SYNTHETIC_ENV = {
    "KALILLAC_FINANCIAL_SAFEGUARDS_ENABLED": "true",
    "KALILLAC_PRICE_OPENAI_MODEL": "gpt-6-luna",
    "KALILLAC_PRICE_OPENAI_INPUT_USD_PER_MTOK": "1.25",
    "KALILLAC_PRICE_OPENAI_CACHED_INPUT_USD_PER_MTOK": "0.125",
    "KALILLAC_PRICE_OPENAI_CACHE_WRITE_USD_PER_MTOK": "1.25",
    "KALILLAC_PRICE_OPENAI_OUTPUT_USD_PER_MTOK": "10",
    "KALILLAC_PRICE_OPENAI_LONG_CONTEXT_ABOVE_INPUT_TOKENS": "none",
    "KALILLAC_FINANCE_OPENAI_INPUT_OVERHEAD_TOKENS": "512",
    "KALILLAC_PRICE_BRAVE_USD_PER_REQUEST": "0.005",
    "KALILLAC_PRICE_TAVILY_USD_PER_CREDIT": "0.008",
    "KALILLAC_FINANCE_TAVILY_MAX_CREDITS_SEARCH_BASIC": "1",
    "KALILLAC_FINANCE_TAVILY_MAX_CREDITS_SEARCH_ADVANCED": "2",
    "KALILLAC_FINANCE_TAVILY_MAX_CREDITS_EXTRACT_BASIC": "1",
    "KALILLAC_FINANCE_TAVILY_MAX_CREDITS_EXTRACT_ADVANCED": "2",
}
LONG_RATES = {
    "KALILLAC_PRICE_OPENAI_LONG_CONTEXT_ABOVE_INPUT_TOKENS": "272000",
    "KALILLAC_PRICE_OPENAI_LONG_CONTEXT_INPUT_USD_PER_MTOK": "2.5",
    "KALILLAC_PRICE_OPENAI_LONG_CONTEXT_CACHED_INPUT_USD_PER_MTOK": "0.25",
    "KALILLAC_PRICE_OPENAI_LONG_CONTEXT_CACHE_WRITE_USD_PER_MTOK": "2.5",
    "KALILLAC_PRICE_OPENAI_LONG_CONTEXT_OUTPUT_USD_PER_MTOK": "15",
}
HOSTILE_AMOUNTS = ["", " ", "-1", "+1", "-0", "1e3", "1E3", "1e999999999", "9e9999", "inf", "Infinity", "nan",
                   "NaN", "1.", ".5", "01", "00", "1.0000001", "1_000", "1,5", "0x10", "0b1", "1 000", "１",
                   "٣", "1 ", "1.2.3", "1e-3", "1000000000", "9" * 17, "9" * 1_000_000]


def openai_pricing(rates=(1_250_000, 125_000, 1_250_000, 10_000_000), long_context=None, overhead=512):
    return OpenAIPricing("gpt-6-luna", TokenRates(*rates), long_context, overhead)


def tavily_pricing(per_credit=8_000, credits=(1, 2, 1, 2)):
    return TavilyPricing(per_credit, tuple((*op, n) for op, n in zip(pricing.TAVILY_OPERATIONS, credits)))


def payload(text="Hello", max_output_tokens=3200, model="gpt-6-luna"):
    return {"model": model, "instructions": "Be helpful.", "input": [{"role": "user", "content": text}],
            "store": False, "reasoning": {"effort": "medium"}, "max_output_tokens": max_output_tokens}


def response(**usage_fields):
    return {"model": "gpt-6-luna", "status": "completed", "usage": usage_fields}


# --- 1. exact integer money --------------------------------------------------------------


@pytest.mark.parametrize("text, expected", [
    ("0", 0), ("1", 1_000_000), ("0.000001", 1), ("0.1", 100_000), ("1.25", 1_250_000),
    ("10", 10_000_000), ("0.005", 5_000), ("123456789.123456", 123_456_789_123_456),
])
def test_parse_usd_is_exact(text, expected):
    assert money.parse_usd(text, maximum_microusd=money.MAX_MICROUSD) == expected


def test_parse_usd_maximum_text_and_bounds():
    assert money.parse_usd("999999999.999999", maximum_microusd=money.MAX_MICROUSD) == 999_999_999_999_999
    with pytest.raises(MoneyError):
        money.parse_usd("2", maximum_microusd=1_999_999)
    with pytest.raises(MoneyError):
        money.parse_usd("0", maximum_microusd=10, minimum_microusd=1)


@pytest.mark.parametrize("text", HOSTILE_AMOUNTS, ids=range(len(HOSTILE_AMOUNTS)))
def test_parse_usd_rejects_hostile_text_quickly(text):
    started = time.perf_counter()
    with pytest.raises(MoneyError) as error:
        money.parse_usd(text, maximum_microusd=money.MAX_MICROUSD)
    assert time.perf_counter() - started < 0.05
    assert text.strip() == "" or text[:8] not in str(error.value)


@pytest.mark.parametrize("value", [None, 1, 1.25, b"1", True, float("nan"), ["1"]])
def test_parse_usd_rejects_non_text(value):
    with pytest.raises(MoneyError):
        money.parse_usd(value, maximum_microusd=money.MAX_MICROUSD)


def test_ceil_div_rounds_up_exactly():
    assert money.ceil_div(0, 7) == 0
    assert money.ceil_div(1, MTOK) == 1
    assert money.ceil_div(MTOK, MTOK) == 1
    assert money.ceil_div(MTOK + 1, MTOK) == 2
    rng = random.Random(1)
    for _ in range(5000):
        n, d = rng.randrange(0, 10**20), rng.randrange(1, 10**12)
        q = money.ceil_div(n, d)
        assert q * d >= n and (q - 1) * d < n
    for bad in ((-1, 3), (1, 0), (1, -2), (True, 3), (1.0, 3), (1, 3.0)):
        with pytest.raises(MoneyError):
            money.ceil_div(*bad)


def test_amount_and_count_reject_bool_float_and_out_of_range():
    for bad in (True, False, 1.0, -1, money.MAX_MICROUSD + 1, "1", None):
        with pytest.raises(MoneyError):
            money.amount(bad)
    assert money.amount(money.MAX_MICROUSD) == money.MAX_MICROUSD
    assert money.MAX_MICROUSD < 2**63 - 1


# --- 2. explicit price configuration -----------------------------------------------------


class RecordingEnv(dict):
    def __init__(self, *args):
        super().__init__(*args)
        self.read = []

    def get(self, key, default=None):
        self.read.append(key)
        return super().get(key, default)

    def __getitem__(self, key):
        self.read.append(key)
        return super().__getitem__(key)

    def __contains__(self, key):
        self.read.append(key)
        return super().__contains__(key)

    def __iter__(self):
        self.read.append("<iterate>")
        return super().__iter__()


@pytest.mark.parametrize("flag", [None, "", "false", "0", "off", "no", " FALSE "])
def test_disabled_pricing_reads_only_the_flag(flag):
    env = RecordingEnv({k: v for k, v in SYNTHETIC_ENV.items() if k != pricing.ENABLE})
    env["KALILLAC_PRICE_OPENAI_INPUT_USD_PER_MTOK"] = "1e999999999"  # would be refused if read
    if flag is not None:
        env[pricing.ENABLE] = flag
    assert pricing.load_pricing(env) is None
    assert env.read == [pricing.ENABLE]


def test_disabled_pricing_needs_no_price_settings():
    assert pricing.load_pricing({}) is None


@pytest.mark.parametrize("flag", ["maybe", "ture", "2", "enabled"])
def test_ambiguous_enable_flag_is_refused(flag):
    with pytest.raises(PricingConfigError):
        pricing.load_pricing({pricing.ENABLE: flag})


def test_full_synthetic_configuration_loads_exactly():
    table = pricing.load_pricing(dict(SYNTHETIC_ENV))
    assert table.openai == OpenAIPricing("gpt-6-luna", TokenRates(1_250_000, 125_000, 1_250_000, 10_000_000), None, 512)
    assert table.brave == BravePricing(5_000)
    assert table.tavily.per_credit == 8_000
    assert {(op, depth): table.tavily.max_credits_for(op, depth) for op, depth in pricing.TAVILY_OPERATIONS} == {
        ("search", "basic"): 1, ("search", "advanced"): 2, ("extract", "basic"): 1, ("extract", "advanced"): 2}


@pytest.mark.parametrize("name", pricing.REQUIRED)
def test_every_required_price_must_be_explicit(name):
    env = dict(SYNTHETIC_ENV)
    del env[name]
    with pytest.raises(PricingConfigError, match=re.escape(name)):
        pricing.load_pricing(env)
    env[name] = "   "
    with pytest.raises(PricingConfigError, match=re.escape(name)):
        pricing.load_pricing(env)


PRICE_NAMES = [*pricing.OPENAI_RATES.values(), pricing.BRAVE_REQUEST, pricing.TAVILY_CREDIT]


@pytest.mark.parametrize("name", PRICE_NAMES)
@pytest.mark.parametrize("value", ["-1", "1e3", "nan", "inf", "1.0000001", "0x1", "１", "1_0", "9" * 5000])
def test_invalid_price_text_is_refused_without_echo(name, value):
    env = dict(SYNTHETIC_ENV, **{name: value})
    started = time.perf_counter()
    with pytest.raises(PricingConfigError) as error:
        pricing.load_pricing(env)
    assert time.perf_counter() - started < 0.05
    assert value[:6] not in str(error.value).replace(name, "")


@pytest.mark.parametrize("name", [pricing.OPENAI_RATES["input"], pricing.OPENAI_RATES["output"],
                                  pricing.BRAVE_REQUEST, pricing.TAVILY_CREDIT])
def test_zero_is_refused_for_billable_units(name):
    with pytest.raises(PricingConfigError, match=re.escape(name)):
        pricing.load_pricing(dict(SYNTHETIC_ENV, **{name: "0"}))


def test_zero_cached_and_cache_write_rates_are_explicitly_allowed():
    table = pricing.load_pricing(dict(SYNTHETIC_ENV, **{pricing.OPENAI_RATES["cached_input"]: "0",
                                                       pricing.OPENAI_RATES["cache_write"]: "0.000000"}))
    assert (table.openai.standard.cached_input, table.openai.standard.cache_write) == (0, 0)


@pytest.mark.parametrize("name, value", [
    (pricing.OPENAI_RATES["input"], "100000.000001"), (pricing.BRAVE_REQUEST, "1000.000001"),
    (pricing.OPENAI_OVERHEAD, "1000001"), (pricing.TAVILY_MAX_CREDITS[("search", "basic")], "0"),
    (pricing.TAVILY_MAX_CREDITS[("search", "basic")], "1001"), (pricing.OPENAI_OVERHEAD, "1e3"),
    (pricing.OPENAI_OVERHEAD, "-1"), (pricing.OPENAI_OVERHEAD, "01"), (pricing.OPENAI_MODEL, "GPT 6"),
    (pricing.OPENAI_MODEL, "gpt-6-luna/x"), (pricing.OPENAI_MODEL, "x" * 65), (pricing.LONG_CONTEXT_ABOVE, "0"),
    (pricing.LONG_CONTEXT_ABOVE, "16777217"), (pricing.LONG_CONTEXT_ABOVE, "1e5"),
])
def test_out_of_range_configuration_is_refused(name, value):
    env = dict(SYNTHETIC_ENV, **{name: value})
    if name == pricing.LONG_CONTEXT_ABOVE:
        env.update({k: v for k, v in LONG_RATES.items() if k != name})
    with pytest.raises(PricingConfigError):
        pricing.load_pricing(env)


@pytest.mark.parametrize("name", ["KALILLAC_PRICE_OPENAI_REASONING_USD_PER_MTOK",
                                  "KALILLAC_PRICE_OPENAI_OUTPUT_USD_PER_MTOKEN", "KALILLAC_FINANCE_ANYTHING"])
def test_unrecognized_pricing_variables_are_refused(name):
    with pytest.raises(PricingConfigError, match=re.escape(name)):
        pricing.load_pricing(dict(SYNTHETIC_ENV, **{name: "1"}))


def test_long_context_rates_are_all_or_nothing():
    table = pricing.load_pricing(dict(SYNTHETIC_ENV, **LONG_RATES))
    assert table.openai.long_context == LongContext(272_000, TokenRates(2_500_000, 250_000, 2_500_000, 15_000_000))
    for name in list(LONG_RATES)[1:]:
        partial = dict(SYNTHETIC_ENV, **LONG_RATES)
        del partial[name]
        with pytest.raises(PricingConfigError, match=re.escape(name)):
            pricing.load_pricing(partial)
        stray = dict(SYNTHETIC_ENV, **{name: LONG_RATES[name]})
        with pytest.raises(PricingConfigError, match=re.escape(name)):
            pricing.load_pricing(stray)


def test_pricing_objects_validate_direct_construction():
    for rates in ((0, 0, 0, 1), (1, 0, 0, 0), (True, 0, 0, 1), (1.0, 0, 0, 1), (1, -1, 0, 1),
                  (pricing.MAX_TOKEN_RATE + 1, 0, 0, 1)):
        with pytest.raises(MoneyError):
            TokenRates(*rates)
    with pytest.raises(MoneyError):
        OpenAIPricing("gpt-6-luna", TokenRates(1, 0, 0, 1), None, -1)
    with pytest.raises(MoneyError):
        BravePricing(0)
    with pytest.raises(MoneyError):
        tavily_pricing(credits=(1, 2, 1, 0))
    with pytest.raises(MoneyError):
        TavilyPricing(1, (("search", "basic", 1),) * 4)


# --- 3. usage normalization ---------------------------------------------------------------


def test_openai_usage_with_cached_and_reasoning_split():
    status, parsed = usage.normalize_openai_usage(response(
        input_tokens=1000, output_tokens=400, total_tokens=1400,
        input_tokens_details={"cached_tokens": 600}, output_tokens_details={"reasoning_tokens": 300}))
    assert status == usage.USABLE
    assert parsed == OpenAIUsage(1000, 400, 600, None, 300)


def test_openai_usage_zero_is_usable_zero():
    status, parsed = usage.normalize_openai_usage(response(input_tokens=0, output_tokens=0))
    assert (status, parsed) == (usage.USABLE, OpenAIUsage(0, 0))


@pytest.mark.parametrize("data, status", [
    (None, usage.MISSING), ("text", usage.MISSING), ({}, usage.MISSING), ({"usage": None}, usage.MISSING),
    ({"usage": []}, usage.INVALID), ({"usage": "1"}, usage.INVALID),
    ({"usage": {"input_tokens": -1, "output_tokens": 1}}, usage.INVALID),
    ({"usage": {"input_tokens": True, "output_tokens": 1}}, usage.INVALID),
    ({"usage": {"input_tokens": 1.0, "output_tokens": 1}}, usage.INVALID),
    ({"usage": {"input_tokens": "1", "output_tokens": 1}}, usage.INVALID),
    ({"usage": {"input_tokens": MAX_TOKENS + 1, "output_tokens": 1}}, usage.INVALID),
    ({"usage": {"input_tokens": 10**100000, "output_tokens": 1}}, usage.INVALID),
    ({"usage": {"input_tokens": 5, "output_tokens": 1, "input_tokens_details": {"cached_tokens": 6}}}, usage.INVALID),
    ({"usage": {"input_tokens": 5, "output_tokens": 1,
                "input_tokens_details": {"cached_tokens": 3, "cache_write_tokens": 3}}}, usage.INVALID),
    ({"usage": {"input_tokens": 5, "output_tokens": 1, "output_tokens_details": {"reasoning_tokens": 2}}}, usage.INVALID),
    ({"usage": {"input_tokens": 5, "output_tokens": 1, "total_tokens": 7}}, usage.INVALID),
    ({"usage": {"input_tokens": 5}}, usage.PARTIAL), ({"usage": {"output_tokens": 5}}, usage.PARTIAL),
    ({"usage": {"total_tokens": 5}}, usage.PARTIAL),
])
def test_unpriceable_openai_usage_yields_no_usage(data, status):
    started = time.perf_counter()
    assert usage.normalize_openai_usage(data) == (status, None)
    assert time.perf_counter() - started < 0.05


@pytest.mark.parametrize("data, expected", [
    ({"usage": {"credits": 2}}, (usage.USABLE, 2)), ({"usage": {"credits": 0}}, (usage.USABLE, 0)),
    ({"results": []}, (usage.MISSING, None)), ({"usage": {}}, (usage.MISSING, None)), (None, (usage.MISSING, None)),
    ({"usage": {"credits": 1.0}}, (usage.INVALID, None)), ({"usage": {"credits": True}}, (usage.INVALID, None)),
    ({"usage": {"credits": "1"}}, (usage.INVALID, None)), ({"usage": {"credits": -1}}, (usage.INVALID, None)),
    ({"usage": {"credits": 1001}}, (usage.INVALID, None)), ({"usage": [1]}, (usage.INVALID, None)),
    ({"usage": {"credits": 10**100000}}, (usage.INVALID, None)),
])
def test_tavily_credits_normalization(data, expected):
    assert usage.normalize_tavily_credits(data) == expected


# --- 4. actual cost -----------------------------------------------------------------------


def test_openai_cost_exact_examples():
    p = openai_pricing()
    # 400 uncached x 1.25 + 600 cached x 0.125 + 400 output x 10, per 1M tokens.
    units = 400 * 1_250_000 + 600 * 125_000 + 400 * 10_000_000
    assert cost.openai_cost(p, OpenAIUsage(1000, 400, 600, 0, 300)) == money.ceil_div(units, MTOK) == 4_575
    assert cost.openai_cost(p, OpenAIUsage(0, 0)) == 0
    assert cost.openai_cost(p, OpenAIUsage(10, 20, 0, 0, 5)) == 213  # 212.5 rounded up


def test_openai_cost_rounds_up_per_call():
    p = openai_pricing(rates=(1, 0, 0, 1))
    assert cost.openai_cost(p, OpenAIUsage(1, 0)) == 1
    assert cost.openai_cost(p, OpenAIUsage(MTOK, 0)) == 1
    assert cost.openai_cost(p, OpenAIUsage(MTOK + 1, 0)) == 2
    p = openai_pricing(rates=(333_333, 0, 0, 1))
    assert cost.openai_cost(p, OpenAIUsage(3, 0)) == 1  # 999,999 rate units


def test_reasoning_tokens_are_billed_once_within_output():
    p = openai_pricing()
    assert cost.openai_cost(p, OpenAIUsage(100, 500, 0, 0, 499)) == cost.openai_cost(p, OpenAIUsage(100, 500, 0, 0, None))


def test_cache_write_tokens_use_their_own_rate():
    p = openai_pricing(rates=(1_000_000, 100_000, 3_000_000, 2_000_000))
    assert cost.openai_cost(p, OpenAIUsage(MTOK, 0, 0, MTOK)) == 3_000_000
    assert cost.openai_cost(p, OpenAIUsage(MTOK, 0, MTOK, 0)) == 100_000
    assert cost.openai_cost(p, OpenAIUsage(MTOK, 0, 0, 0)) == 1_000_000


def test_unreported_input_split_is_billed_at_the_highest_possible_rate():
    rng = random.Random(2)
    for _ in range(3000):
        rates = (rng.randrange(1, 10**8), rng.randrange(0, 10**8), rng.randrange(0, 10**8), rng.randrange(1, 10**8))
        p = openai_pricing(rates=rates)
        i, o = rng.randrange(0, 10**6), rng.randrange(0, 10**5)
        c = rng.randrange(0, i + 1)
        w = rng.randrange(0, i - c + 1)
        concrete = cost.openai_cost(p, OpenAIUsage(i, o, c, w))
        assert cost.openai_cost(p, OpenAIUsage(i, o, None, None)) >= concrete
        assert cost.openai_cost(p, OpenAIUsage(i, o, c, None)) >= concrete
        assert cost.openai_cost(p, OpenAIUsage(i, o, None, w)) >= concrete


def test_long_context_threshold_applies_to_the_whole_request():
    long_rates = TokenRates(2_000_000, 0, 2_000_000, 4_000_000)
    p = openai_pricing(rates=(1_000_000, 0, 1_000_000, 2_000_000), long_context=LongContext(MTOK, long_rates))
    assert cost.openai_cost(p, OpenAIUsage(MTOK, 0, 0, 0)) == 1_000_000
    assert cost.openai_cost(p, OpenAIUsage(MTOK + 1, 0, 0, 0)) == money.ceil_div((MTOK + 1) * 2_000_000, MTOK)
    assert cost.openai_cost(p, OpenAIUsage(MTOK + 1, MTOK, 0, 0)) == money.ceil_div((MTOK + 1) * 2_000_000, MTOK) + 4_000_000


def test_maximum_supported_values_are_exact_and_within_bigint():
    top = pricing.MAX_TOKEN_RATE
    p = openai_pricing(rates=(top, top, top, top))
    result = cost.openai_cost(p, OpenAIUsage(MAX_TOKENS, MAX_TOKENS, 0, 0, MAX_TOKENS))
    assert result == (MAX_TOKENS * top * 2) // MTOK == 400_000_000_000
    assert result <= money.MAX_MICROUSD < 2**63 - 1
    t = tavily_pricing(per_credit=pricing.MAX_OPERATION_PRICE, credits=(1000,) * 4)
    assert cost.tavily_cost(t, 1000) == 1000 * pricing.MAX_OPERATION_PRICE <= money.MAX_MICROUSD


def test_brave_and_tavily_cost():
    assert cost.brave_cost(BravePricing(5_000)) == 5_000
    t = tavily_pricing()
    assert [cost.tavily_cost(t, n) for n in (0, 1, 2)] == [0, 8_000, 16_000]
    for bad in (-1, 1001, True, 1.0, None):
        with pytest.raises(MoneyError):
            cost.tavily_cost(t, bad)


def test_cost_functions_refuse_wrong_types():
    with pytest.raises(MoneyError):
        cost.openai_cost(openai_pricing(), {"input_tokens": 1, "output_tokens": 1})
    with pytest.raises(MoneyError):
        cost.openai_cost(None, OpenAIUsage(1, 1))
    with pytest.raises(MoneyError):
        OpenAIUsage(True, 1)


# --- 5. worst-case reservation ------------------------------------------------------------


def test_openai_input_bound_is_the_transport_body_plus_overhead():
    from kalillac_routing.bounded_transport import _encode_payload
    body = payload("naïve 文字 😀 \ud800 \"quoted\" \\ \n")
    encoded = _encode_payload(body)
    assert encoded == json.dumps(body).encode("ascii")  # ASCII-only body
    assert cost.openai_input_token_bound(body, 512) == len(encoded) + 512


def test_ascii_json_never_uses_fewer_bytes_than_utf8():
    alphabet = ["a", " ", "\"", "\\", "\n", "\x00", "\x7f", "é", "ß", "߿", "ࠀ", "文", "￿",
                "😀", "\U0010ffff", "\ud800", "\udfff"]
    rng = random.Random(3)
    for _ in range(3000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 40)))
        # surrogatepass: a lone surrogate's 3 bytes, the same as U+FFFD.
        assert len(json.dumps(text)) - 2 >= len(text.encode("utf-8", "surrogatepass"))


def test_reservation_exact_example():
    p = openai_pricing(overhead=0)
    body = payload("hi")
    bound = len(json.dumps(body).encode())
    reservation = cost.reserve_openai(p, body)
    assert reservation.input_token_bound == bound and reservation.output_token_bound == 3200
    assert reservation.microusd == money.ceil_div(bound * 1_250_000 + 3200 * 10_000_000, MTOK)


@pytest.mark.parametrize("bad", [
    payload(model="gpt-5.6-luna"), payload(max_output_tokens=None), payload(max_output_tokens=0),
    payload(max_output_tokens=True), payload(max_output_tokens=3200.0), payload(max_output_tokens=MAX_TOKENS + 1),
    payload(text=float("nan")), payload(text={1, 2}), {"model": "gpt-6-luna"}, [], None,
])
def test_unboundable_payloads_are_refused(bad):
    with pytest.raises(ReservationError):
        cost.reserve_openai(openai_pricing(), bad)


def test_reservation_refuses_input_beyond_the_reservable_bound():
    p = openai_pricing(overhead=1000)
    base = len(json.dumps(payload("")).encode())
    fill = pricing.MAX_INPUT_BOUND_TOKENS - base - 1000
    assert cost.reserve_openai(p, payload("x" * fill)).input_token_bound == pricing.MAX_INPUT_BOUND_TOKENS
    with pytest.raises(ReservationError):
        cost.reserve_openai(p, payload("x" * (fill + 1)))


def test_largest_reservation_fits_the_money_bound():
    top = pricing.MAX_TOKEN_RATE
    p = openai_pricing(rates=(top, top, top, top), overhead=pricing.MAX_OVERHEAD_TOKENS)
    base = len(json.dumps(payload("", max_output_tokens=MAX_TOKENS)).encode())
    big = payload("x" * (pricing.MAX_INPUT_BOUND_TOKENS - base - pricing.MAX_OVERHEAD_TOKENS), MAX_TOKENS)
    reservation = cost.reserve_openai(p, big)
    expected = money.ceil_div((pricing.MAX_INPUT_BOUND_TOKENS + MAX_TOKENS) * top, MTOK)
    assert reservation.microusd == expected <= money.MAX_MICROUSD


def _random_pricing(rng):
    def rates():
        return TokenRates(rng.randrange(1, 10**9), rng.choice([0, rng.randrange(0, 10**9)]),
                          rng.randrange(0, 10**9), rng.randrange(1, 10**9))
    long_context = rng.choice([None, LongContext(rng.randrange(1, 4000), rates())])
    return OpenAIPricing("gpt-6-luna", rates(), long_context, rng.randrange(0, 2000))


def _random_text(rng):
    pieces = ["a", "Kalillac ", "文字", "😀", "\"", "\\", "\n", "é", "\ud800", "{}", " "]
    return "".join(rng.choice(pieces) for _ in range(rng.randrange(0, 300)))


def _usages_within(rng, input_bound, output_bound):
    i = rng.choice([0, input_bound, rng.randrange(0, input_bound + 1)])
    o = rng.choice([0, output_bound, rng.randrange(0, output_bound + 1)])
    c = rng.randrange(0, i + 1)
    w = rng.randrange(0, i - c + 1)
    r = rng.randrange(0, o + 1)
    return [OpenAIUsage(i, o, c, w, r), OpenAIUsage(i, o, None, None, None), OpenAIUsage(i, o, c, None),
            OpenAIUsage(i, o, None, w), OpenAIUsage(i, o, i, 0), OpenAIUsage(i, o, 0, i), OpenAIUsage(i, o, 0, 0)]


def test_reservation_covers_every_valid_usage_within_its_bounds():
    rng = random.Random(4)
    for _ in range(1500):
        p = _random_pricing(rng)
        body = payload(_random_text(rng), max_output_tokens=rng.randrange(1, 7000))
        reservation = cost.reserve_openai(p, body)
        for item in _usages_within(rng, min(reservation.input_token_bound, MAX_TOKENS), reservation.output_token_bound):
            charged = cost.openai_cost(p, item)
            assert charged <= reservation.microusd
            settled = cost.settle_openai(p, reservation, response(
                input_tokens=item.input_tokens, output_tokens=item.output_tokens,
                input_tokens_details={"cached_tokens": item.cached_input_tokens or 0}))
            assert settled.charged <= settled.reserved and settled.overrun == 0 and not settled.bound_exceeded


def test_reservation_covers_long_context_boundary_on_both_sides():
    long_rates = TokenRates(9_000_000, 9_000_000, 9_000_000, 90_000_000)
    for overhead in (0, 1, 2):
        body = payload("abc")
        size = len(json.dumps(body).encode())
        for above in (size - 1, size, size + 1, size + 2):
            p = openai_pricing(rates=(1, 1, 1, 1), long_context=LongContext(above, long_rates), overhead=overhead)
            reservation = cost.reserve_openai(p, body)
            for i in range(max(0, above - 2), reservation.input_token_bound + 1):
                assert cost.openai_cost(p, OpenAIUsage(i, 3200, 0, 0)) <= reservation.microusd


def test_tavily_and_brave_reservations_cover_their_operations():
    t = tavily_pricing()
    for op, depth in pricing.TAVILY_OPERATIONS:
        reserved = cost.reserve_tavily(t, op, depth)
        for credits in range(0, t.max_credits_for(op, depth) + 1):
            assert cost.tavily_cost(t, credits) <= reserved
    assert cost.reserve_brave(BravePricing(5_000)) == cost.brave_cost(BravePricing(5_000)) == 5_000
    for op, depth in (("search", "fast"), ("crawl", "basic"), ("search", None), (None, "basic")):
        with pytest.raises(MoneyError):
            cost.reserve_tavily(t, op, depth)


# --- settlement ---------------------------------------------------------------------------


@pytest.mark.parametrize("data, basis", [
    ({}, usage.MISSING), ({"usage": {"input_tokens": -1, "output_tokens": 1}}, usage.INVALID),
    ({"usage": {"input_tokens": 5}}, usage.PARTIAL), ("not-json", usage.MISSING),
])
def test_unpriceable_openai_response_keeps_the_full_reservation(data, basis):
    p = openai_pricing()
    reservation = cost.reserve_openai(p, payload())
    settled = cost.settle_openai(p, reservation, data)
    assert (settled.charged, settled.basis) == (reservation.microusd, basis)


def test_usage_below_reservation_settles_to_actual():
    p = openai_pricing()
    reservation = cost.reserve_openai(p, payload())
    settled = cost.settle_openai(p, reservation, response(input_tokens=10, output_tokens=20))
    assert (settled.charged, settled.basis, settled.overrun) == (213, "usage", 0)


def test_usage_beyond_the_bound_is_charged_in_full_and_flagged():
    p = openai_pricing(overhead=0)
    reservation = cost.reserve_openai(p, payload(max_output_tokens=10))
    data = response(input_tokens=reservation.input_token_bound + 100, output_tokens=10)
    settled = cost.settle_openai(p, reservation, data)
    actual = cost.openai_cost(p, OpenAIUsage(reservation.input_token_bound + 100, 10))
    assert settled.charged == actual > reservation.microusd
    assert settled.overrun == actual - reservation.microusd and settled.bound_exceeded
    # Above the bound but still below the reserved money: flagged, never capped.
    p = openai_pricing(rates=(1_000_000, 0, 0, 1_000_000), overhead=0)
    reservation = cost.reserve_openai(p, payload(max_output_tokens=1000))
    settled = cost.settle_openai(p, reservation, response(input_tokens=reservation.input_token_bound + 1, output_tokens=0))
    assert settled.bound_exceeded and settled.overrun == 0 and settled.charged < reservation.microusd


def test_tavily_and_brave_settlement():
    t = tavily_pricing()
    reserved = cost.reserve_tavily(t, "search", "advanced")
    assert cost.settle_tavily(t, reserved, {"results": []}).charged == reserved
    assert cost.settle_tavily(t, reserved, {"usage": {"credits": 1.5}}).basis == usage.INVALID
    assert cost.settle_tavily(t, reserved, {"usage": {"credits": 1}}).charged == 8_000
    over = cost.settle_tavily(t, reserved, {"usage": {"credits": 5}})
    assert (over.charged, over.overrun, over.bound_exceeded) == (40_000, 24_000, True)
    assert cost.settle_brave(BravePricing(5_000), 5_000).charged == 5_000


def test_settlement_never_charges_below_actual_or_reservation_when_unknown():
    rng = random.Random(5)
    for _ in range(5000):
        reserved, actual = rng.randrange(0, 10**12), rng.randrange(0, 10**12)
        known = cost.settle(reserved, actual)
        assert known.charged == actual and known.overrun == max(0, actual - reserved)
        assert cost.settle(reserved, None, usage.INVALID).charged == reserved


# --- 6. ledger scopes ---------------------------------------------------------------------


def test_scope_identifiers_are_distinct_and_stable():
    assert [s.value for s in FinancialScope] == ["global", "free", "paid"]
    assert len({s.value for s in FinancialScope}) == 3


def test_every_charge_lands_in_global_and_exactly_one_of_free_or_paid():
    for traffic in TrafficClass:
        charged = scopes.charged_scopes(traffic)
        assert charged[0] is FinancialScope.GLOBAL and len(charged) == 2
        assert (FinancialScope.FREE in charged) != (FinancialScope.PAID in charged)
    assert scopes.charged_scopes(TrafficClass.ANONYMOUS)[1] is FinancialScope.FREE
    assert scopes.charged_scopes(TrafficClass.FREE_ACCOUNT)[1] is FinancialScope.FREE
    assert scopes.charged_scopes(TrafficClass.PAID_ACCOUNT)[1] is FinancialScope.PAID
    for bad in ("anonymous", "paid_account", None, FinancialScope.PAID):
        with pytest.raises(MoneyError):
            scopes.charged_scopes(bad)


def test_scope_ceilings_must_reserve_paid_capacity_inside_global():
    ScopeCeilings(1000, 600, 400)
    for bad in ((1000, 601, 400), (1000, 600, 401), (-1, 0, 0), (10, True, 0), (10, 1.0, 0)):
        with pytest.raises(MoneyError):
            ScopeCeilings(*bad)


def test_paid_exposure_is_its_own_scope_never_global_minus_free():
    ceilings = ScopeCeilings(1000, 600, 400)
    with pytest.raises(MoneyError):
        scopes.admits(ceilings, {FinancialScope.GLOBAL: 0, FinancialScope.FREE: 0}, TrafficClass.PAID_ACCOUNT, 1)
    # Global usage from outside both scopes does not count as paid usage.
    exposure = {FinancialScope.GLOBAL: 600, FinancialScope.FREE: 600, FinancialScope.PAID: 0}
    assert scopes.admits(ceilings, exposure, TrafficClass.PAID_ACCOUNT, 400)
    assert not scopes.admits(ceilings, exposure, TrafficClass.PAID_ACCOUNT, 401)


def test_free_traffic_can_never_exhaust_paid_capacity():
    rng = random.Random(6)
    for _ in range(300):
        paid_limit, free_limit = rng.randrange(0, 5000), rng.randrange(0, 5000)
        ceilings = ScopeCeilings(paid_limit + free_limit + rng.randrange(0, 100), free_limit, paid_limit)
        exposure = dict.fromkeys(FinancialScope, 0)
        for _ in range(200):
            traffic = rng.choice(list(TrafficClass))
            amount = rng.randrange(0, 400)
            scope = scopes.charged_scopes(traffic)[1]
            admitted = scopes.admits(ceilings, exposure, traffic, amount)
            if traffic is TrafficClass.PAID_ACCOUNT:
                # Whatever free traffic did, paid is refused only by its own ceiling.
                assert admitted == (exposure[FinancialScope.PAID] + amount <= paid_limit)
            if admitted:
                exposure[FinancialScope.GLOBAL] += amount
                exposure[scope] += amount
            assert exposure[FinancialScope.FREE] <= free_limit
            assert exposure[FinancialScope.PAID] <= paid_limit
            assert exposure[FinancialScope.GLOBAL] <= ceilings.global_limit


# --- 7. behaviour neutrality --------------------------------------------------------------


def test_only_the_provider_control_seam_and_startup_use_the_financial_package():
    # Batch 2 wires the ledger in at exactly these two places (see test_finance_ledger.py).
    users = []
    for path in CANDIDATE.rglob("*.py"):
        relative = path.relative_to(CANDIDATE).parts
        if relative[0] in ("tests", "kalillac_finance") or "__pycache__" in relative:
            continue
        if "kalillac_finance" in path.read_text(encoding="utf-8", errors="replace"):
            users.append("/".join(relative))
    assert sorted(users) == ["app_fastapi_candidate.py", "kalillac_routing/provider_controls.py"]


def test_application_starts_unchanged_when_the_flag_is_unset():
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("KALILLAC_", "OPENAI_", "BRAVE_", "TAVILY_", "STRIPE_", "RESEND_", "GROQ_"))}
    env.update({"PYTHONDONTWRITEBYTECODE": "1"})
    script = ("import app_fastapi_candidate as app; from kalillac_routing import provider_controls as c; "
              "print('OFF', app._financial_config is None and c.SERVICE is None)")
    result = subprocess.run([sys.executable, "-c", script], cwd=CANDIDATE, env=env, capture_output=True,
                            text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().splitlines()[-1] == "OFF True"
