"""The gated live runner never calls a provider by default and refuses to run
live without the owner gate and bounds. Only its dry-run estimate is exercised.
"""
from __future__ import annotations

import pytest

from tests.safety_suite import live_runner


def test_dry_run_makes_no_provider_call_and_estimates(monkeypatch):
    monkeypatch.delenv("SAFETY_LIVE_EVAL", raising=False)
    plan = live_runner.dry_run()
    assert plan["mode"].startswith("dry-run")
    assert plan["would_run_live"] is False
    assert plan["live_cases"] > 0
    assert plan["est_input_tokens"] > 0 and plan["est_output_tokens"] > 0
    assert any("SAFETY_LIVE_EVAL" in b for b in plan["blockers"])


def test_run_live_refuses_without_the_owner_gate(monkeypatch):
    monkeypatch.delenv("SAFETY_LIVE_EVAL", raising=False)
    with pytest.raises(RuntimeError) as caught:
        live_runner.run_live()
    assert "gated" in str(caught.value)


def test_run_live_requires_caps_even_when_enabled(monkeypatch):
    monkeypatch.setenv("SAFETY_LIVE_EVAL", "enabled")
    monkeypatch.delenv("SAFETY_LIVE_MAX_REQUESTS", raising=False)
    monkeypatch.delenv("SAFETY_LIVE_MAX_USD", raising=False)
    with pytest.raises(RuntimeError) as caught:
        live_runner.run_live()
    msg = str(caught.value)
    assert "SAFETY_LIVE_MAX_REQUESTS" in msg and "SAFETY_LIVE_MAX_USD" in msg


def test_run_live_with_gate_and_bounds_is_not_implemented_this_phase(monkeypatch):
    monkeypatch.setenv("SAFETY_LIVE_EVAL", "enabled")
    monkeypatch.setenv("SAFETY_LIVE_MAX_REQUESTS", "100000")
    monkeypatch.setenv("SAFETY_LIVE_MAX_USD", "1000")
    monkeypatch.setenv("SAFETY_LIVE_INPUT_USD_PER_1K", "0")
    monkeypatch.setenv("SAFETY_LIVE_OUTPUT_USD_PER_1K", "0")
    with pytest.raises(NotImplementedError):
        live_runner.run_live()
