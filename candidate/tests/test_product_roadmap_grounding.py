"""Roadmap and commercial-direction grounding across the original conversation.

The real chat() pipeline runs for both the legacy path and the V31 native
path; only the OpenAI HTTP call and Tavily are faked. The tests assert what
authoritative grounding reaches the model, not the model's prose.
"""

import json
import os

import pytest

os.environ.setdefault("GROQ_API_KEY", "test-not-real")

import app_fastapi_candidate as app

from kalillac_failed_conversation_fixture import (
    BLUEPRINT_DIAGRAM_PROMPT,
    ENTITLEMENTS_PROMPT,
    MONETIZATION_PROMPT,
    TURNS,
    history_before,
    turn_index,
)


SAVED_MODE_PLANNED = "PLANNED, NOT LAUNCHED: Kalillac plans an optional Saved Mode"
ACCOUNT_NOT_PERSISTENT = "Having an account does NOT automatically make conversations persistent"
COMMERCIAL_DIRECTION = (
    "anonymous free access without an account + an optional account-based "
    "paid tier + business/API plans later"
)
TOKENS_NOT_CHOSEN = "are NOT Kalillac's chosen commercial architecture"


@pytest.fixture
def captured(monkeypatch):
    state = {"payloads": []}

    def fake_post(payload, timeout=90):
        state["payloads"].append(payload)
        return {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "Reply."}],
                }
            ],
        }

    def fake_search(query, include_domains=None):
        return "unavailable", []

    monkeypatch.setattr(app, "OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setattr(app, "_post_openai_responses", fake_post)
    monkeypatch.setattr(app, "run_web_search", fake_search)

    return state


def _sent_for(captured, monkeypatch, prompt, v31_native):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", v31_native)
    captured["payloads"].clear()

    index = turn_index(prompt)
    app.chat(
        prompt,
        history_before(index),
        session_id=f"roadmap-{v31_native}-{index}",
    )

    assert captured["payloads"], "model was not called"
    return json.dumps(captured["payloads"][0])


# --- authoritative facts --------------------------------------------------


def test_roadmap_facts_are_rendered_as_planned_not_launched():
    facts = app.render_kalillac_facts()

    assert SAVED_MODE_PLANNED in facts
    assert "Saved Mode does not exist today" in facts
    assert "no launch date is established" in facts
    assert ACCOUNT_NOT_PERSISTENT in facts
    assert "A signed-in or paying user may still use Private Session" in facts
    assert "separate from persistent cross-chat memory" in facts
    assert "separate opt-in capability" in facts


def test_commercial_direction_facts():
    facts = app.render_kalillac_facts()

    assert COMMERCIAL_DIRECTION in facts
    assert "entitlement recovery" in facts
    assert "subscription management" in facts
    assert "refunds and support" in facts
    assert "Billing identity is separate from chat persistence" in facts
    assert TOKENS_NOT_CHOSEN in facts


def test_v31_runtime_facts_include_roadmap_and_commercial_direction():
    facts = app._v31_runtime_facts()

    assert any(SAVED_MODE_PLANNED in item for item in facts["product_roadmap"])
    assert any(
        COMMERCIAL_DIRECTION in item
        for item in facts["commercial_direction"]
    )


# --- "is that going to change?" after temporary sessions ----------------------


@pytest.mark.parametrize("v31_native", [False, True])
def test_session_change_followup_is_grounded_in_roadmap(
    monkeypatch,
    captured,
    v31_native,
):
    sent = _sent_for(
        captured,
        monkeypatch,
        "is that going to change?",
        v31_native,
    )

    assert SAVED_MODE_PLANNED in sent
    assert ACCOUNT_NOT_PERSISTENT in sent
    assert "Private Session is intended to stay ephemeral" in sent
    # The temporary-session conversation is still the context.
    assert "what does temporary session mean?" in sent


# --- monetization conversation ----------------------------------------------


@pytest.mark.parametrize(
    "prompt",
    [MONETIZATION_PROMPT, ENTITLEMENTS_PROMPT, BLUEPRINT_DIAGRAM_PROMPT],
)
@pytest.mark.parametrize("v31_native", [False, True])
def test_monetization_turns_use_established_commercial_direction(
    monkeypatch,
    captured,
    prompt,
    v31_native,
):
    sent = _sent_for(captured, monkeypatch, prompt, v31_native)

    assert COMMERCIAL_DIRECTION in sent
    assert TOKENS_NOT_CHOSEN in sent
    assert "Billing identity is separate from chat persistence" in sent
    assert "only when explicitly labeled as alternatives" in sent


@pytest.mark.parametrize("v31_native", [False, True])
def test_profitability_diagram_keeps_context_and_commercial_grounding(
    monkeypatch,
    captured,
    v31_native,
):
    sent = _sent_for(
        captured,
        monkeypatch,
        BLUEPRINT_DIAGRAM_PROMPT,
        v31_native,
    )

    # Earlier monetization discussion plus authoritative direction.
    assert "optional paid entitlements" in sent
    assert COMMERCIAL_DIRECTION in sent
    # The fixed architecture diagram is not the answer.
    assert not app.is_architecture_diagram_request(BLUEPRINT_DIAGRAM_PROMPT)


def test_every_fixture_turn_completes_with_v31(monkeypatch, captured):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", True)

    for index, turn in enumerate(TURNS):
        if turn.get("stopped"):
            continue

        reply = app.chat(
            turn["user"],
            history_before(index),
            session_id="roadmap-full-replay",
        )

        assert reply.strip(), turn["user"]


# --- unrelated prompts stay unchanged ----------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "explain recursion in python",
        "how do I close my bank account",
        "what is kali linux",
    ],
)
def test_unrelated_prompt_gets_no_roadmap_block(message):
    assert not app.mentions_kalillac_product_topic(message)

    messages = app.build_messages(message, [], "general", [])
    prompt = "\n".join(str(m.content) for m in messages)

    assert "KALILLAC PRODUCT ROADMAP" not in prompt


@pytest.mark.parametrize(
    "message",
    [
        "what does temporary session mean?",
        "will there be a saved mode?",
        "how would you monetize this",
        "is there a free tier",
    ],
)
def test_product_topics_are_detected(message):
    assert app.mentions_kalillac_product_topic(message)
