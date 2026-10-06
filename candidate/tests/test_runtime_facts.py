"""Regression tests for Kalillac V31 runtime facts."""

from pathlib import Path
import json
import sys


CANDIDATE_DIR = Path(__file__).resolve().parents[1]

if str(CANDIDATE_DIR) not in sys.path:
    sys.path.insert(0, str(CANDIDATE_DIR))


from kalillac_routing.runtime_facts import (
    RuntimeConfig,
    build_runtime_facts,
)


def make_config():
    return RuntimeConfig(
        primary_provider="OpenAI",
        primary_model="gpt-5.6-luna",
        reasoning_effort="low",
    )


def test_primary_configuration_is_reported():
    facts = build_runtime_facts(make_config())

    assert facts["configured_primary"] == {
        "provider": "OpenAI",
        "model": "gpt-5.6-luna",
        "reasoning_effort": "low",
    }


def test_there_is_no_automatic_model_fallback():
    facts = build_runtime_facts(make_config())

    assert facts["configured_fallback_chain"] == []
    assert facts["automatic_model_fallback"] is False
    assert "model-provider-unavailable" in facts["model_unavailable_behavior"]


def test_runtime_config_has_no_fallback_slots():
    import dataclasses

    names = {field.name for field in dataclasses.fields(RuntimeConfig)}

    assert names == {
        "primary_provider",
        "primary_model",
        "reasoning_effort",
        "web_search_provider",
    }


def test_runtime_facts_name_no_other_model_provider():
    serialized = json.dumps(build_runtime_facts(make_config())).lower()

    for name in ("groq", "workers ai", "gpt-oss", "cloudflare"):
        assert name not in serialized


def test_search_provider_is_tavily():
    facts = build_runtime_facts(make_config())

    assert facts["web_search_provider"] == "Tavily"
    assert "does not generate answers" in facts["web_search_provider_role"]


def test_per_message_provider_is_not_claimed():
    facts = build_runtime_facts(make_config())

    assert facts["per_message_provider_identity_available"] is False

    note = facts["per_message_provider_note"].lower()

    assert "does not yet preserve metadata" in note
    assert "particular completed response" in note


def test_runtime_facts_do_not_contain_credentials():
    facts = build_runtime_facts(make_config())

    serialized = json.dumps(facts).lower()

    forbidden = [
        "api_key",
        "api key",
        "token=",
        "authorization",
        "bearer ",
        "tvly-",
    ]

    for value in forbidden:
        assert value not in serialized