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
        primary_model="gpt-6-luna",
        reasoning_effort="low",
    )


def test_primary_configuration_is_reported():
    facts = build_runtime_facts(make_config())

    assert facts["configured_primary"] == {
        "provider": "OpenAI",
        "model": "gpt-6-luna",
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

# Migration controls inspect configuration and scripted provider payloads, not
# real-model response quality or deployed configuration.
import pytest


@pytest.mark.parametrize("configured,expected", [
    (None, "gpt-6-luna"),
    ("gpt-5.6-luna", "gpt-5.6-luna"),
    ("configured-evaluation-model", "configured-evaluation-model"),
])
def test_process_model_default_and_environment_override(configured, expected):
    import os
    import subprocess
    env = os.environ.copy()
    for name in ("OPENAI_MODEL", "OPENAI_REASONING_EFFORT", "OPENAI_API_KEY",
                 "BRAVE_API_KEY", "TAVILY_API_KEY", "RUN_LIVE_TESTS"):
        env.pop(name, None)
    if configured is not None:
        env["OPENAI_MODEL"] = configured
    script = (
        "import sys,json; "
        "sys.path.insert(0," + repr(str(CANDIDATE_DIR / "tests")) + "); "
        "sys.path.insert(0," + repr(str(CANDIDATE_DIR)) + "); "
        "import conftest; import app_fastapi_candidate as app; "
        "print('CONFIG_RESULT='+json.dumps({'model':app.OPENAI_MODEL,"
        "'effort':app.OPENAI_REASONING_EFFORT,'facts':app._v31_runtime_facts(),"
        "'external_attempts':len(conftest._BLOCKED)}))"
    )
    run = subprocess.run([sys.executable, "-B", "-c", script], env=env,
                         cwd=CANDIDATE_DIR.parent, capture_output=True, text=True,
                         encoding="utf-8", timeout=30, check=True)
    data = json.loads(next(line.split("=", 1)[1] for line in run.stdout.splitlines()
                           if line.startswith("CONFIG_RESULT=")))
    assert data["model"] == expected
    assert data["effort"] == "low"
    assert data["facts"]["configured_primary"]["model"] == expected
    assert data["facts"]["configured_primary"]["reasoning_effort"] == "low"
    assert data["facts"]["configured_fallback_chain"] == []
    assert data["facts"]["automatic_model_fallback"] is False
    assert data["external_attempts"] == 0


@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
@pytest.mark.parametrize("model", ["gpt-6-luna", "configured-evaluation-model"])
def test_runtime_model_reaches_actual_provider_and_self_knowledge(monkeypatch, native, model):
    import copy
    import app_fastapi_candidate as app
    from kalillac_routing.search_providers import SearchProviderPolicy
    monkeypatch.setattr(app, "OPENAI_MODEL", model)
    monkeypatch.setattr(app, "OPENAI_REASONING_EFFORT", "low")
    monkeypatch.setattr(app, "OPENAI_API_KEY", "offline-synthetic-key")
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    monkeypatch.setattr(app, "SEARCH_PROVIDER_POLICY", SearchProviderPolicy("brave", "tavily"))
    monkeypatch.setattr(app, "SESSION_STATE", type(app.SESSION_STATE)())
    payloads = []
    def post(payload):
        payloads.append(copy.deepcopy(payload))
        return {"status": "completed", "output": [{"type": "message",
                "content": [{"type": "output_text", "text": "A scripted response."}]}]}
    monkeypatch.setattr(app, "_post_openai_for_attempt", post)
    def no_search(*args, **kwargs):
        raise AssertionError("Self-knowledge does not need external search")
    monkeypatch.setattr(app, "run_web_search", no_search)
    question = "What model do you use and write a poem about calculus?"
    assert app.chat(question, [], session_id="migration-payload") == "A scripted response."
    assert len(payloads) == 1
    payload = payloads[0]
    assert payload["model"] == model
    assert payload["reasoning"] == {"effort": "low"}
    assert payload["store"] is False
    assert ("instructions" in payload) is native
    prompt = payload.get("instructions", "") + "\n" + "\n".join(
        item.get("content", "") for item in payload["input"])
    assert question in prompt and model in prompt
    assert "gpt-5.6-luna" not in prompt
    if model != "gpt-6-luna":
        assert "gpt-6-luna" not in prompt
    assert prompt.count("CURRENT PUBLIC CHAT CAPABILITIES:") == 1
    facts = app._v31_runtime_facts()
    assert facts["configured_primary"]["model"] == model
    assert facts["configured_fallback_chain"] == []
    assert facts["automatic_model_fallback"] is False
    assert facts["web_search_policy"]["primary"] == "Brave"
    assert facts["web_search_policy"]["fallback"] == "Tavily"
    reply = app.chat("What model do you use?", [], session_id="migration-facts")
    assert model in reply and "gpt-5.6-luna" not in reply
    assert "no automatic fallback" in reply
    assert len(payloads) == 1



@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
@pytest.mark.parametrize("primary,fallback", [
    ("tavily", "none"), ("brave", "none"), ("brave", "tavily"),
])
def test_generic_search_disclosures_and_precise_runtime_policy(monkeypatch, native, primary, fallback):
    """Scripted payload evidence; this does not prove live-model adherence."""
    import copy
    import app_fastapi_candidate as app
    from kalillac_routing.search_providers import SearchProviderPolicy
    monkeypatch.setattr(app, "SEARCH_PROVIDER_POLICY", SearchProviderPolicy(primary, fallback))
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    monkeypatch.setattr(app, "OPENAI_API_KEY", "offline-synthetic-key")
    monkeypatch.setattr(app, "SESSION_STATE", type(app.SESSION_STATE)())
    payloads = []
    def post(payload):
        payloads.append(copy.deepcopy(payload))
        return {"status": "completed", "output": [{"type": "message",
                "content": [{"type": "output_text", "text": "A scripted explanation."}]}]}
    monkeypatch.setattr(app, "_post_openai_for_attempt", post)
    def no_search(*args, **kwargs):
        raise AssertionError("Provider disclosures do not require live search")
    monkeypatch.setattr(app, "run_web_search", no_search)
    question = "Explain your configuration in detail, including privacy limits and recorded deployment facts."
    assert app.chat(question, [], session_id="neutral-provider-disclosure") == "A scripted explanation."
    assert len(payloads) == 1
    payload = payloads[0]
    assert payload["model"] == app.OPENAI_MODEL == "gpt-6-luna"
    assert payload["reasoning"] == {"effort": "low"}
    assert payload["store"] is False
    assert ("instructions" in payload) is native
    delivered = payload.get("instructions", "") + "\n" + "\n".join(
        item.get("content", "") for item in payload["input"])
    disclosures = (
        "When live search runs, the search provider used receives the query text needed to perform that search. "
        "Retrieved evidence can be supplied to OpenAI for the answer. "
        "Search providers do not generate Kalillac\u2019s answer. "
        "Do not claim that a search provider receives the entire conversation, and do not claim that it receives nothing.",
        "Never state OpenAI's or any search provider's retention, deletion, logging, storage, training, or analytics as fact",
        "Browser developer tools show only browser-to-Kalillac requests, not server-side calls to OpenAI or search providers",
    )
    for disclosure in disclosures:
        assert disclosure in app.SYSTEM_PROMPT
        assert disclosure in delivered
        assert "Tavily" not in disclosure and "Brave" not in disclosure
    for obsolete in (
        "When live search runs, Tavily receives",
        "Never state OpenAI's or Tavily's",
        "not server-side calls to OpenAI or Tavily",
    ):
        assert obsolete not in app.SYSTEM_PROMPT
        assert obsolete not in delivered
    assert delivered.count("CURRENT PUBLIC CHAT CAPABILITIES:") == 1
    assert "Configured search primary: " + primary.title() + "; fallback:" in delivered
    facts = app._v31_runtime_facts()
    policy = facts["web_search_policy"]
    assert policy["primary"] == primary.title()
    assert policy["fallback"] == (None if fallback == "none" else "Tavily")
    assert policy["fallback_max_attempts"] == (0 if fallback == "none" else 1)
    assert policy["fallback_for_quality_or_age"] is False
    followup = policy["native_evidence_followup"]
    assert followup["enabled_in_current_process"] is (native and primary == "brave" and fallback == "tavily")
    assert followup["max_attempts"] == (1 if primary == "brave" and fallback == "tavily" else 0)
    assert facts["configured_primary"]["model"] == "gpt-6-luna"
    assert facts["configured_fallback_chain"] == []
    assert facts["automatic_model_fallback"] is False
