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
        first_fallback_provider="Groq",
        first_fallback_model="openai/gpt-oss-120b",
        second_fallback_provider="Cloudflare Workers AI",
        second_fallback_model="@cf/openai/gpt-oss-120b",
        final_fallback_provider="Groq",
        final_fallback_model="openai/gpt-oss-20b",
    )


def test_primary_configuration_is_reported():
    facts = build_runtime_facts(make_config())

    assert facts["configured_primary"] == {
        "provider": "OpenAI",
        "model": "gpt-5.6-luna",
        "reasoning_effort": "low",
    }


def test_fallback_chain_preserves_order():
    facts = build_runtime_facts(make_config())

    assert facts["configured_fallback_chain"] == [
        {
            "provider": "Groq",
            "model": "openai/gpt-oss-120b",
        },
        {
            "provider": "Cloudflare Workers AI",
            "model": "@cf/openai/gpt-oss-120b",
        },
        {
            "provider": "Groq",
            "model": "openai/gpt-oss-20b",
        },
    ]


def test_search_provider_is_tavily():
    facts = build_runtime_facts(make_config())

    assert facts["web_search_provider"] == "Tavily"


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