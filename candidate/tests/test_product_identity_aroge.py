"""Kalillac's own developer relationship (Aroge Tech) is application self-knowledge.

Owner-verified facts: Aroge Tech is the technology business developing Kalillac
AI; Kalillac AI is an independently branded product developed under Aroge Tech;
Robert Casey created Kalillac AI; the official websites are arogetech.com and
kalillac.ai; OpenAI provides the underlying language model. Aroge Tech's legal
formation is not established.

These questions must be answered from those facts on every model path, without
a web search and without any search source attached to them. Similarly named
businesses stay ordinary questions and keep the normal search behaviour.

Everything is deterministic: providers are scripted, the model is a stub and
conftest forbids external sockets. No AI or search provider is called.
"""
from __future__ import annotations

import re

import pytest

import app_fastapi_candidate as app
from test_source_trust_behavior import (  # noqa: F401  (world is a fixture)
    answer_turn, brave, footer, reply_with, search_turn, world,
)


AROGE_QUESTIONS = [
    "What is Aroge Tech?",
    "Is Kalillac AI developed by Aroge Tech?",
    "What is Aroge Tech's official website?",
    "Does Aroge Tech operate independently of Kalillac AI?",
    "Who founded Aroge Tech?",
    "Tell me about arogetech.com",
]
KALILLAC_QUESTIONS = [
    "Who created Kalillac AI?",
    "Who provides Kalillac's underlying language model?",
]
UNRELATED_QUESTIONS = [
    "What is Aroga Technologies?",
    "Tell me about Argo-Tech",
    "What does Aroga Tech make?",
]

DEVELOPER_FACTS = [
    "the technology business developing Kalillac AI",
    "independently branded AI product developed under Aroge Tech",
    "https://arogetech.com",
    "https://kalillac.ai",
    "OpenAI provides Kalillac's underlying language model",
    "not Kalillac's developer",
    "Aroga Technologies",
]


def _facts_in(text):
    return [fact for fact in DEVELOPER_FACTS if fact not in text]


# --- 1. routing: the developer is self-knowledge, similar names are not ----------------------


@pytest.mark.parametrize("question", AROGE_QUESTIONS + KALILLAC_QUESTIONS)
def test_developer_and_kalillac_questions_are_self_knowledge(question):
    assert app.classify_request(question, []) == "self_knowledge"
    assert app.kalillac_self_knowledge_context_active(question, [])


@pytest.mark.parametrize("question", UNRELATED_QUESTIONS)
def test_similarly_named_businesses_are_not_kalillac_self_knowledge(question):
    assert app.classify_request(question, []) != "self_knowledge"
    assert not app.is_self_knowledge_request(question)
    assert not app.kalillac_self_knowledge_context_active(question, [])


def test_an_explicit_search_for_aroge_tech_still_searches():
    assert app.classify_request("Search the web for Aroge Tech", []) == "web_search"


def test_a_follow_up_keeps_the_developer_facts_in_context():
    history = [{"role": "user", "content": "What is Aroge Tech?"},
               {"role": "assistant", "content": "Aroge Tech is the technology business developing Kalillac AI."}]
    assert app.kalillac_self_knowledge_context_active("What's their website?", history)


# --- 2. the facts reach every model path ------------------------------------------------------


def test_system_prompt_states_the_developer_relationship():
    # SYSTEM_PROMPT leads the V31 native instructions and every legacy model route.
    assert _facts_in(app.SYSTEM_PROMPT) == []
    assert "Created by Robert Casey." in app.SYSTEM_PROMPT
    assert "not a generic frontend for another company's model or a model marketplace" in app.SYSTEM_PROMPT


def test_self_knowledge_facts_carry_a_labelled_developer_section():
    facts = app.render_kalillac_facts()
    section = facts[facts.index("DEVELOPER - VERIFIED APPLICATION FACTS, NOT SEARCH RESULTS:"):]
    section = section[:section.index("\n\n")]
    assert _facts_in(section) == []
    assert "Kalillac AI was created by Robert Casey." in facts


def test_native_policy_forbids_searching_for_the_developer():
    policy = app.V31_NATIVE_TOOL_POLICY
    assert "Do not use public web search to determine Kalillac's developer" in policy
    assert "never attribute them\n  to a search result" in policy


def test_creator_attribution_is_unchanged():
    assert app.KALILLAC_SELF_KNOWLEDGE["identity"] == [
        "Kalillac AI is a privacy-first public AI assistant.",
        "Kalillac AI was created by Robert Casey.",
    ]
    assert app.KALILLAC_CANONICAL_CREATOR == "Kalillac AI was created by Robert Casey."
    assert app.get_canonical_self_knowledge_response("who created you") == (
        "creator", "Kalillac AI was created by Robert Casey.")


# --- 3. no fabricated company or legal claims -------------------------------------------------


def _sentences(text):
    return re.split(r"(?<=[.;:])\s+", text)


@pytest.mark.parametrize("name", ["SYSTEM_PROMPT", "facts"])
def test_legal_formation_is_never_asserted(name):
    text = app.SYSTEM_PROMPT if name == "SYSTEM_PROMPT" else app.render_kalillac_facts()
    assert "Aroge Tech LLC" not in text
    for sentence in _sentences(text):
        if re.search(r"\bLLC\b|\bincorporated\b|\bregistered\b", sentence) and "Aroge" in sentence:
            assert "not" in sentence, sentence


def test_only_the_verified_aroge_tech_web_address_is_stated():
    text = app.SYSTEM_PROMPT + app.render_kalillac_facts() + app.V31_NATIVE_TOOL_POLICY
    assert set(re.findall(r"[\w.+-]*arogetech\.com[\w/.-]*", text)) == {"arogetech.com"}
    assert "@arogetech" not in text


def test_planned_services_are_not_presented_as_available():
    developer = " ".join(app.KALILLAC_SELF_KNOWLEDGE["developer"])
    assert "commercially available services are established" in developer
    assert "no other Aroge Tech products" in developer


# --- 4. end to end: answered from facts, no search, no source list ----------------------------


def _chat(message, history=()):
    record = app.SourceFooterRecord()
    token = app._SOURCE_FOOTER.set(record)
    try:
        reply = app.chat(message, list(history), session_id="aroge-identity")
    finally:
        app._SOURCE_FOOTER.reset(token)
    return reply, record


ANSWER = "Aroge Tech is the technology business developing Kalillac AI."


@pytest.mark.parametrize("question", AROGE_QUESTIONS + KALILLAC_QUESTIONS)
def test_native_path_answers_from_facts_without_search_or_sources(world, monkeypatch, question):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", True)
    world["script"] = lambda provider, payload: pytest.fail("no search provider may be called")
    world["turns"] = [answer_turn(ANSWER)]
    reply, record = _chat(question)
    assert reply == ANSWER
    assert world["calls"] == []
    assert record.metadata is None and record.for_reply(reply) is None
    (items, instructions), = world["native"]
    assert _facts_in(instructions) == []
    assert "DEVELOPER - VERIFIED APPLICATION FACTS, NOT SEARCH RESULTS:" in instructions


@pytest.mark.parametrize("question", AROGE_QUESTIONS + ["Who provides Kalillac's underlying language model?"])
def test_legacy_path_answers_from_facts_without_search_or_sources(world, monkeypatch, question):
    world["script"] = lambda provider, payload: pytest.fail("no search provider may be called")
    world["replies"] = [reply_with(ANSWER)]
    reply, record = _chat(question)
    assert reply == ANSWER
    assert world["calls"] == []
    assert record.metadata is None
    combined = world["systems"][-1] + world["prompts"][-1]
    assert _facts_in(combined) == []
    assert "DEVELOPER - VERIFIED APPLICATION FACTS, NOT SEARCH RESULTS:" in combined
    assert "answer from the DEVELOPER facts only" in combined


def test_legacy_creator_question_keeps_the_deterministic_attribution(world):
    world["script"] = lambda provider, payload: pytest.fail("no search provider may be called")
    reply, record = _chat("Who created Kalillac AI?")
    assert reply == "Kalillac AI was created by Robert Casey."
    assert world["calls"] == [] and world["systems"] == [] and world["native"] == []


def test_an_unrelated_company_keeps_normal_search_and_sources(world, monkeypatch):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", True)
    rows = [("https://aroga.example/about", "About Aroga Technologies", "Aroga Technologies builds sensors.")]
    world["script"] = lambda provider, payload: brave(rows)
    world["turns"] = [search_turn("Aroga Technologies", "s1"),
                      answer_turn("Aroga Technologies builds sensors.", "1")]
    reply, record = _chat("What is Aroga Technologies?")
    assert [call["provider"] for call in world["calls"]] == ["brave"]
    assert reply == "Aroga Technologies builds sensors." + footer("Sources", rows)
    assert record.for_reply(reply) == {"kind": "sources", "count": 1}
    instructions = world["native"][0][1]
    # The distinction travels with SYSTEM_PROMPT; the full Kalillac fact block does not.
    assert "Aroge Tech is not Aroga Technologies" in instructions
    assert "DEVELOPER - VERIFIED APPLICATION FACTS" not in instructions


def test_an_explicit_aroge_search_lists_only_returned_results(world, monkeypatch):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", True)
    rows = [("https://news.example/a", "Unrelated Aroge item", "Text.")]
    world["script"] = lambda provider, payload: brave(rows)
    world["turns"] = [search_turn("Aroge Tech", "s1"), answer_turn(ANSWER + " https://arogetech.com", "none")]
    reply, record = _chat("Search the web for Aroge Tech")
    # The verified website is not a returned result: existing link enforcement
    # keeps it as readable literal text (no link), and the footer lists only
    # what search returned. Self-knowledge is never turned into a source.
    assert reply == ANSWER + " https&#58;&#47;&#47;arogetech&#46;com" + footer("Search results", rows)
    assert record.for_reply(reply) == {"kind": "search_results", "count": 1}
    assert "arogetech.com" not in footer("Search results", rows)
