"""Reasoning modes (QUICK / BALANCED / DEEP): configuration, permissions, the
payload actually sent, and their interaction with Financial Safeguards.

Efforts are synthetic test mappings, not product decisions. No network or
provider: the OpenAI seam ends in a local terminal.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys

from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import pytest

import finance_test_support as f
from kalillac_routing import reasoning_modes as modes
from kalillac_routing.reasoning_modes import ReasoningConfigError, ReasoningModeRefused, Selection
from test_finance_ledger import make, pool

CANDIDATE = Path(__file__).resolve().parents[1]
MODEL = "gpt-6-luna"
COMPLETE = {
    "KALILLAC_REASONING_MODES_ENABLED": "true",
    "KALILLAC_REASONING_MODE_QUICK_EFFORT": "none",
    "KALILLAC_REASONING_MODE_BALANCED_EFFORT": "low",
    "KALILLAC_REASONING_MODE_DEEP_EFFORT": "high",
    "KALILLAC_REASONING_MODE_QUICK_EXTRA_OUTPUT_TOKENS": "0",
    "KALILLAC_REASONING_MODE_BALANCED_EXTRA_OUTPUT_TOKENS": "0",
    "KALILLAC_REASONING_MODE_DEEP_EXTRA_OUTPUT_TOKENS": "8000",
    "KALILLAC_REASONING_MODE_DEFAULT": "balanced",
    "KALILLAC_REASONING_MODES_FREE": "quick,balanced",
    "KALILLAC_REASONING_MODES_PAID": "quick, balanced, deep",
}


def load(overrides=None, model=MODEL, default_effort="low"):
    env = {**COMPLETE, **(overrides or {})}
    return modes.load_reasoning_modes({k: v for k, v in env.items() if v is not None}, model, default_effort)


@pytest.fixture
def enabled(monkeypatch):
    config = load()
    monkeypatch.setattr(modes, "CONFIG", config)
    return config


# --- configuration ------------------------------------------------------------------------


def test_supported_efforts_are_exactly_the_verified_list_for_the_one_model():
    assert modes.SUPPORTED_MODEL == "gpt-6-luna"
    assert modes.SUPPORTED_EFFORTS == ("none", "low", "medium", "high", "xhigh", "max")
    assert modes.MODES == ("quick", "balanced", "deep")
    assert len(modes.REQUIRED) == 9 and len(set(modes.REQUIRED)) == 9


def test_disabled_reads_only_the_flag():
    for env in ({}, {"KALILLAC_REASONING_MODES_ENABLED": "false"},
                {"KALILLAC_REASONING_MODE_DEEP_EFFORT": "turbo", "KALILLAC_REASONING_MODES_FREE": "everything"}):
        assert modes.load_reasoning_modes(env, "another-model", "anything") is None
    with pytest.raises(ReasoningConfigError, match="must be true or false"):
        modes.load_reasoning_modes({"KALILLAC_REASONING_MODES_ENABLED": "maybe"}, MODEL, "low")


def test_complete_configuration_loads_exactly():
    config = load()
    assert dict(config.efforts) == {"quick": "none", "balanced": "low", "deep": "high"}
    assert dict(config.extra_output_tokens) == {"quick": 0, "balanced": 0, "deep": 8000}
    assert config.default == "balanced"
    assert config.free == {"quick", "balanced"} and config.paid == {"quick", "balanced", "deep"}


@pytest.mark.parametrize("name", modes.REQUIRED)
def test_every_setting_is_required_when_enabled(name):
    for value in (None, "", "   "):
        with pytest.raises(ReasoningConfigError, match=re.escape(name)):
            load({name: value})


@pytest.mark.parametrize("case, overrides, message", [
    ("unsupported effort", {"KALILLAC_REASONING_MODE_DEEP_EFFORT": "minimal"}, "KALILLAC_REASONING_MODE_DEEP_EFFORT"),
    ("made-up effort", {"KALILLAC_REASONING_MODE_QUICK_EFFORT": "turbo"}, "KALILLAC_REASONING_MODE_QUICK_EFFORT"),
    ("effort in capitals", {"KALILLAC_REASONING_MODE_DEEP_EFFORT": "High"}, "KALILLAC_REASONING_MODE_DEEP_EFFORT"),
    ("decreasing efforts", {"KALILLAC_REASONING_MODE_QUICK_EFFORT": "medium"}, "must not decrease"),
    ("deep below balanced", {"KALILLAC_REASONING_MODE_DEEP_EFFORT": "none",
                             "KALILLAC_REASONING_MODE_QUICK_EFFORT": "none"}, "must not decrease"),
    ("extra tokens not a count", {"KALILLAC_REASONING_MODE_DEEP_EXTRA_OUTPUT_TOKENS": "1e4"},
     "KALILLAC_REASONING_MODE_DEEP_EXTRA_OUTPUT_TOKENS"),
    ("negative extra tokens", {"KALILLAC_REASONING_MODE_DEEP_EXTRA_OUTPUT_TOKENS": "-1"},
     "KALILLAC_REASONING_MODE_DEEP_EXTRA_OUTPUT_TOKENS"),
    ("extra tokens above the model", {"KALILLAC_REASONING_MODE_DEEP_EXTRA_OUTPUT_TOKENS": "120001"},
     "KALILLAC_REASONING_MODE_DEEP_EXTRA_OUTPUT_TOKENS"),
    ("unknown default", {"KALILLAC_REASONING_MODE_DEFAULT": "turbo"}, "KALILLAC_REASONING_MODE_DEFAULT"),
    ("default closed to free", {"KALILLAC_REASONING_MODES_FREE": "quick"}, "default reasoning mode must be open"),
    ("free wider than paid", {"KALILLAC_REASONING_MODES_PAID": "balanced,deep"}, "open to paid requests"),
    ("unknown mode in a list", {"KALILLAC_REASONING_MODES_PAID": "quick,balanced,turbo"},
     "KALILLAC_REASONING_MODES_PAID"),
    ("repeated mode in a list", {"KALILLAC_REASONING_MODES_FREE": "quick,quick,balanced"},
     "KALILLAC_REASONING_MODES_FREE"),
    ("default adds output tokens", {"KALILLAC_REASONING_MODE_BALANCED_EXTRA_OUTPUT_TOKENS": "100"},
     "must add no output tokens"),
    ("mistyped variable", {"KALILLAC_REASONING_MODE_DEEP_EFORT": "high"}, "Unsupported reasoning-mode variable"),
    ("unsupported variable", {"KALILLAC_REASONING_MODES_ANONYMOUS": "quick"}, "Unsupported reasoning-mode variable"),
])
def test_inconsistent_configuration_is_refused(case, overrides, message):
    with pytest.raises(ReasoningConfigError, match=re.escape(message)):
        load(overrides)


def test_messages_never_echo_a_configured_value():
    with pytest.raises(ReasoningConfigError) as refused:
        load({"KALILLAC_REASONING_MODE_DEEP_EFFORT": "secret-looking-value"})
    assert "secret-looking-value" not in str(refused.value)


def test_default_mode_must_reproduce_the_configured_effort():
    with pytest.raises(ReasoningConfigError, match="must equal the configured OPENAI_REASONING_EFFORT"):
        load(default_effort="medium")
    assert load({"KALILLAC_REASONING_MODE_DEFAULT": "quick"}, default_effort="none").default == "quick"


def test_modes_are_refused_for_a_model_whose_efforts_were_not_verified():
    for model in ("gpt-6-sol", "gpt-6-luna-fast", "", None):
        with pytest.raises(ReasoningConfigError, match="verified for gpt-6-luna only"):
            load(model=model)


# --- resolution and permissions -----------------------------------------------------------


def test_resolution_by_tier(enabled):
    assert enabled.resolve(None, False) is None            # Nothing selected: unchanged request.
    assert enabled.resolve("balanced", False) is None      # The default: unchanged request.
    assert enabled.resolve("quick", False) == Selection("quick", "none", 0)
    assert enabled.resolve("deep", True) == Selection("deep", "high", 8000)
    assert enabled.resolve("quick", True) == Selection("quick", "none", 0)
    with pytest.raises(ReasoningModeRefused) as refused:
        enabled.resolve("deep", False)                     # Never silently downgraded.
    assert (refused.value.status, refused.value.code) == (403, "reasoning_mode_not_allowed")
    for paid in ("yes", 1, "paid", object(), None):        # Only a literal True is paid.
        with pytest.raises(ReasoningModeRefused):
            enabled.resolve("deep", paid)


@pytest.mark.parametrize("value", ["turbo", "DEEP", " deep", "", 3, True, ["deep"], {"mode": "deep"}, 1.5])
def test_unknown_or_malformed_mode_is_invalid(enabled, value):
    with pytest.raises(ReasoningModeRefused) as refused:
        enabled.resolve(value, True)
    assert (refused.value.status, refused.value.code) == (422, "invalid_request")


def test_begin_and_end_bind_one_request_only(enabled):
    assert modes.effective_effort("low") == "low"
    token = modes.begin("quick")
    try:
        assert modes.effective_effort("low") == "none"
    finally:
        modes.end(token)
    assert modes.effective_effort("low") == "low"
    token = modes.begin("balanced")
    assert modes.effective_effort("low") == "low"
    modes.end(token)
    with pytest.raises(ReasoningModeRefused):
        modes.begin("deep")  # paid defaults to False.
    modes.end(None)


def test_everything_is_inert_while_modes_are_off(monkeypatch):
    monkeypatch.setattr(modes, "CONFIG", None)
    for value in ("deep", "turbo", 7, None, {"anything": 1}):
        assert modes.begin(value) is None  # Ignored like any unknown field; never refused.
    seen = []
    wrapped = modes.apply(lambda payload: seen.append(payload) or payload)
    payload = dict(f.PAYLOAD)
    token = modes._SELECTED.set(Selection("deep", "max", 9000))  # Even a stale selection changes nothing.
    try:
        wrapped(payload)
        assert modes.effective_effort("low") == "low"
    finally:
        modes._SELECTED.reset(token)
    assert seen[0] is payload and payload == f.PAYLOAD


# --- the payload actually sent ------------------------------------------------------------


def test_selected_mode_changes_effort_and_output_allowance_without_mutating(enabled):
    seen = []
    wrapped = modes.apply(lambda payload, *rest: seen.append((payload, rest)) or "sent")
    payload = {**f.PAYLOAD, "reasoning": {"effort": "low", "summary": "auto"}}
    original = {**payload, "reasoning": dict(payload["reasoning"])}

    assert wrapped(payload, "extra") == "sent"             # Nothing selected.
    assert seen[-1][0] is payload and seen[-1][1] == ("extra",)

    token = modes.begin("deep", paid=True)
    try:
        wrapped(payload)
    finally:
        modes.end(token)
    sent = seen[-1][0]
    assert sent is not payload and payload == original     # The caller's payload is untouched.
    assert sent["reasoning"] == {"effort": "high", "summary": "auto"}
    assert sent["max_output_tokens"] == original["max_output_tokens"] + 8000
    assert {k: v for k, v in sent.items() if k not in ("reasoning", "max_output_tokens")} == \
        {k: v for k, v in original.items() if k not in ("reasoning", "max_output_tokens")}

    token = modes.begin("quick")
    try:
        wrapped(payload)
    finally:
        modes.end(token)
    assert seen[-1][0]["reasoning"]["effort"] == "none"
    assert seen[-1][0]["max_output_tokens"] == original["max_output_tokens"]

    token = modes.begin("balanced")                         # The default sends the caller's own object.
    try:
        wrapped(payload)
    finally:
        modes.end(token)
    assert seen[-1][0] is payload


def test_output_allowance_never_exceeds_the_model_maximum(monkeypatch):
    monkeypatch.setattr(modes, "CONFIG", load({"KALILLAC_REASONING_MODE_DEEP_EXTRA_OUTPUT_TOKENS": "120000"}))
    seen = []
    wrapped = modes.apply(lambda payload: seen.append(payload))
    token = modes.begin("deep", paid=True)
    try:
        wrapped({**f.PAYLOAD, "max_output_tokens": 100_000})
        wrapped({**f.PAYLOAD, "max_output_tokens": None})    # Unbounded payloads are left for the reservation to refuse.
    finally:
        modes.end(token)
    assert seen[0]["max_output_tokens"] == modes.MAX_OUTPUT_TOKENS == 128_000
    assert seen[1]["max_output_tokens"] is None and seen[1]["reasoning"]["effort"] == "high"


def test_application_applies_modes_outside_the_financial_reservation():
    import app_fastapi_candidate as app
    source = (CANDIDATE / "app_fastapi_candidate.py").read_text(encoding="utf-8")
    assert source.count("reasoning_modes.apply(provider_controls.financial_openai(") == 1
    assert source.count("reasoning_modes.apply(") == 1
    assert app._post_openai_for_attempt.__name__ == "_post_openai_for_attempt"
    # No request is treated as paid until something issues a paid entitlement.
    assert re.findall(r"reasoning_modes\.begin\(([^)]*)\)", source) == ["req.reasoning_mode, paid=False"]
    assert "issue_paid_entitlement" not in source
    # Kalillac's self-description reports the effort the request actually uses.
    assert source.count("reasoning_modes.effective_effort(OPENAI_REASONING_EFFORT)") == 2


# --- interaction with Financial Safeguards ------------------------------------------------


def _moded_attempt(service, mode, paid, traffic):
    """One model attempt through the application's wrapper order:
    reasoning mode -> service-tier pin -> protect -> observe -> terminal."""
    from kalillac_accounts.entitlements import PAID
    from kalillac_finance.traffic import issue_paid_entitlement
    from kalillac_routing import provider_controls, provider_usage
    from kalillac_routing.request_budget import RequestBudget, budget_scope
    seen = []

    def terminal(payload):
        seen.append(payload)
        provider_usage.dispatched()
        return f.model_response(usage=False)

    chain = modes.apply(provider_controls.financial_openai(
        provider_controls.protect(provider_usage.observe_model(terminal), "openai")))
    previous = provider_controls.SERVICE
    provider_controls.SERVICE = service
    state, token = provider_controls.begin_request()
    selected = modes.begin(mode, paid=paid)
    try:
        provider_controls.account_status(traffic != "anonymous",
                                         issue_paid_entitlement(state, PAID) if traffic == "paid" else None)
        with budget_scope(RequestBudget(duration_seconds=30, max_model_attempts=8, max_search_attempts=8)):
            try:
                chain(dict(f.PAYLOAD, reasoning={"effort": "low"}))
                outcome = "ok"
            except provider_controls.RequestCostCeiling:
                outcome = "request_ceiling"
            except provider_controls.CeilingBusy:
                outcome = "busy"
    finally:
        modes.end(selected)
        provider_controls.end_request(state, token)
        provider_controls.SERVICE = previous
    return outcome, seen


def _reservation(effort, max_output_tokens):
    """The worst case the ledger must reserve for the payload a mode sends."""
    from kalillac_finance.cost import reserve_openai
    sent = {**f.PAYLOAD, "reasoning": {"effort": effort}, "max_output_tokens": max_output_tokens,
            "service_tier": "default"}
    return reserve_openai(f.finance_config().prices.openai, sent).microusd


def test_a_mode_is_priced_by_the_reservation_it_actually_sends(tmp_path, enabled):
    data = make(tmp_path)
    try:
        outcome, seen = _moded_attempt(data.service, None, False, "anonymous")
        assert outcome == "ok" and seen[0]["reasoning"] == {"effort": "low"} and seen[0]["max_output_tokens"] == 3200
        outcome, seen = _moded_attempt(data.service, "deep", True, "paid")
        assert outcome == "ok"
        assert seen[0]["reasoning"] == {"effort": "high"} and seen[0]["max_output_tokens"] == 11200
        assert seen[0]["service_tier"] == "default"          # The tier pin still applies inside.
        default, deep = _reservation("low", 3200), _reservation("high", 11200)
        assert {r["scope"]: r["reserved"] for r in f.reservations(data.factory)} == {"free": default, "paid": deep}
        # 8,000 more output tokens at $0.50 per million (4,000 µUSD), plus two more body bytes.
        assert 4000 <= deep - default <= 4001
        f.check_invariants(data.factory, data.finance.limits)
    finally:
        data.engine.dispose()


def test_a_mode_cannot_pass_the_request_or_scope_ceilings(tmp_path, enabled):
    R, deep = f.openai_reservation(f.finance_config()), _reservation("high", 11200)
    day = f.scopes_for(100 * R, 100 * R)
    data = make(tmp_path, finance=f.finance_config(day=day, max_request=(day[1], deep - 1)))
    try:
        outcome, seen = _moded_attempt(data.service, "deep", True, "paid")
        assert (outcome, seen) == ("request_ceiling", [])     # One µUSD short: nothing reserved or sent.
        assert pool(data) == (0, 0) and f.reservations(data.factory) == []
        outcome, seen = _moded_attempt(data.service, "balanced", True, "paid")
        assert outcome == "ok" and seen[0]["max_output_tokens"] == 3200
    finally:
        data.engine.dispose()
    (tmp_path / "scope").mkdir()
    # The request ceiling (here the whole PAID day) admits one deep attempt; the
    # PAID day ceiling refuses it once a default attempt has been charged.
    data = make(tmp_path / "scope", finance=f.finance_config(day=f.scopes_for(R, deep + R - 1)))
    try:
        assert _moded_attempt(data.service, "balanced", True, "paid")[0] == "ok"
        assert _moded_attempt(data.service, "deep", True, "paid")[0] == "busy"
        assert _moded_attempt(data.service, "quick", True, "paid")[0] == "ok"
    finally:
        data.engine.dispose()


# --- the chat boundary --------------------------------------------------------------------


@pytest.fixture
def boundary(monkeypatch):
    """The real /api/chat boundary with the pipeline behind it replaced: the
    dispatch records what the request selected and answers."""
    import app_fastapi_candidate as app
    calls = []

    async def dispatch(request, req, history, session):
        calls.append((req.message, modes._SELECTED.get(), modes.effective_effort("low")))
        return JSONResponse(content={"reply": "synthetic", "session_id": "synthetic"})

    monkeypatch.setattr(app, "_api_chat_dispatch", dispatch)
    with TestClient(app.api) as client:
        yield client, calls


def test_boundary_ignores_the_field_while_modes_are_off(boundary, monkeypatch):
    client, calls = boundary
    monkeypatch.setattr(modes, "CONFIG", None)
    for value in ("deep", "turbo", 7, None, ["x"]):
        response = client.post("/api/chat", json={"message": "hello", "reasoning_mode": value})
        assert response.status_code == 200, response.text
    assert client.post("/api/chat", json={"message": "hello"}).status_code == 200
    assert [call[1:] for call in calls] == [(None, "low")] * 6


def test_boundary_binds_allowed_modes_and_refuses_the_rest_before_any_work(boundary, enabled):
    client, calls = boundary
    assert client.post("/api/chat", json={"message": "a"}).status_code == 200
    assert client.post("/api/chat", json={"message": "b", "reasoning_mode": "balanced"}).status_code == 200
    assert client.post("/api/chat", json={"message": "c", "reasoning_mode": "quick"}).status_code == 200
    assert calls == [("a", None, "low"), ("b", None, "low"), ("c", Selection("quick", "none", 0), "none")]

    refused = client.post("/api/chat", json={"message": "d", "reasoning_mode": "deep"})
    assert (refused.status_code, refused.json()) == (403, {"error": "reasoning_mode_not_allowed"})
    for value in ("turbo", "DEEP", 7, ["deep"]):
        invalid = client.post("/api/chat", json={"message": "e", "reasoning_mode": value})
        assert (invalid.status_code, invalid.json()) == (422, {"error": "invalid_request"})
    assert len(calls) == 3                                  # A refused request never reaches the pipeline.
    assert modes._SELECTED.get() is None                    # Nothing leaks past the request.


def test_a_failing_pipeline_still_releases_the_selection(monkeypatch, enabled):
    import app_fastapi_candidate as app

    async def dispatch(request, req, history, session):
        raise app.SessionEnded()

    monkeypatch.setattr(app, "_api_chat_dispatch", dispatch)
    with TestClient(app.api) as client:
        response = client.post("/api/chat", json={"message": "a", "reasoning_mode": "quick"})
    assert response.status_code == 409
    assert modes._SELECTED.get() is None


# --- startup ------------------------------------------------------------------------------


def _start(overrides):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("KALILLAC_", "OPENAI_", "BRAVE_", "TAVILY_", "STRIPE_", "RESEND_", "GROQ_"))}
    env.update(overrides, PYTHONDONTWRITEBYTECODE="1")
    script = ("import app_fastapi_candidate as app; from kalillac_routing import reasoning_modes as m; "
              "print('MODES', m.CONFIG is not None)")
    return subprocess.run([sys.executable, "-c", script], cwd=CANDIDATE, env=env, capture_output=True, text=True,
                          timeout=180)


def test_startup_is_unchanged_without_the_flag_and_refuses_a_bad_mapping():
    result = _start({})
    assert result.returncode == 0, result.stderr[-1500:]
    assert result.stdout.strip().splitlines()[-1] == "MODES False"
    result = _start(COMPLETE)                               # OPENAI_REASONING_EFFORT defaults to "low".
    assert result.returncode == 0, result.stderr[-1500:]
    assert result.stdout.strip().splitlines()[-1] == "MODES True"
    for overrides, message in (
            ({**COMPLETE, "OPENAI_REASONING_EFFORT": "medium"}, "must equal the configured OPENAI_REASONING_EFFORT"),
            ({**COMPLETE, "OPENAI_MODEL": "gpt-6-sol"}, "verified for gpt-6-luna only"),
            ({"KALILLAC_REASONING_MODES_ENABLED": "true"}, "KALILLAC_REASONING_MODE_QUICK_EFFORT"),
            ({"KALILLAC_REASONING_MODES_ENABLED": "sometimes"}, "must be true or false")):
        result = _start(overrides)
        assert result.returncode != 0 and message in result.stderr, result.stderr[-1500:]
