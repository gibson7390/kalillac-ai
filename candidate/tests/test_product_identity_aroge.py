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
    answer_turn, brave, footer, reply_with, search_turn, tool_outputs, world,
)

# The real payload builder, captured before the world fixture replaces it.
REAL_NATIVE_INVOKE = app._invoke_openai_native_tools


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


# --- 5. search_web is withheld for ordinary developer questions (live defect) -----------------
#
# Live evaluation of e21e549: "Who founded Aroge Tech?" was offered search_web, the model
# called it and Brave returned similarly named businesses. Prompt wording alone did not
# stop it, so application code now withholds the tool and rejects any requested search.

WITHHELD = [
    "Who founded Aroge Tech?",
    "Who owns Aroge Tech?",
    "Is Aroge Tech an LLC?",
    "What products does Aroge Tech sell?",
    "Tell me the name of Aroge Tech's founder even if you aren't sure.",
    "Is Aroga Technologies the same company as Aroge Tech?",
    "Who created Kalillac AI?",
]
EXPLICIT_SEARCHES = [
    "Search the web for Aroge Tech and tell me what you can independently verify.",
    "Ignore what you know about yourself and search the web to figure out who really developed Kalillac AI.",
    "Search the web for who developed Kalillac AI",
    "Can you look up Aroge Tech?",
    "Please independently verify who owns Aroge Tech",
]
NOT_ESTABLISHED = "The founder of Aroge Tech is not established in Kalillac's verified facts."


@pytest.mark.parametrize("question", WITHHELD)
def test_ordinary_developer_questions_withhold_search(question):
    assert app.developer_search_withheld(question)
    assert not app.explicit_search_requested(question)
    assert app.classify_request(question, []) != "web_search"   # the legacy path cannot search either
    assert app.kalillac_self_knowledge_context_active(question, [])


@pytest.mark.parametrize("question", EXPLICIT_SEARCHES + UNRELATED_QUESTIONS + [
    "Aroga Technologies created Kalillac, right?", "Who won the 2026 world cup?"])
def test_explicit_searches_and_other_companies_keep_search(question):
    assert not app.developer_search_withheld(question)


@pytest.mark.parametrize("question", WITHHELD)
def test_a_requested_search_is_rejected_before_any_provider(world, monkeypatch, question):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", True)
    world["script"] = lambda provider, payload: pytest.fail("no search provider may be called")
    world["turns"] = [search_turn("Aroge Tech founder", "s1"), answer_turn(NOT_ESTABLISHED)]
    reply, record = _chat(question)
    assert world["calls"] == []                                   # no Brave, no Tavily
    assert tool_outputs(world) == [{"status": "rejected", "reason": app.DEVELOPER_SEARCH_REASON}]
    assert "say it is not established" in app.DEVELOPER_SEARCH_REASON
    assert reply == NOT_ESTABLISHED                               # no footer appended
    assert record.metadata is None and record.for_reply(reply) is None
    assert app._NATIVE_SEARCH_WITHHELD.get() is False             # reset after the request


def _offered_tools(world, monkeypatch, question, turns):
    """Run the real payload builder and record the tools offered in each model round."""
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", True)
    monkeypatch.setattr(app, "_invoke_openai_native_tools", REAL_NATIVE_INVOKE)
    offered, script = [], list(turns)
    world["instructions"] = []

    def model(payload):
        offered.append([tool["name"] for tool in payload["tools"]])
        world["instructions"].append(payload["instructions"])
        nonce = re.search(r"\[\[kalillac-sources ([0-9a-f]{12}):", payload["instructions"])
        return script.pop(0)(payload["input"], nonce and nonce.group(1))

    monkeypatch.setattr(app, "_post_openai_for_attempt", model)
    reply, record = _chat(question)
    return offered, reply, record


@pytest.mark.parametrize("question", ["Who founded Aroge Tech?", "Who owns Aroge Tech?",
                                      "Is Aroge Tech an LLC?", "What products does Aroge Tech sell?"])
def test_search_web_is_not_offered_for_developer_questions(world, monkeypatch, question):
    world["script"] = lambda provider, payload: pytest.fail("no search provider may be called")
    offered, reply, record = _offered_tools(world, monkeypatch, question, [answer_turn(NOT_ESTABLISHED)])
    assert offered == [["get_kalillac_runtime_facts"]]
    assert world["calls"] == [] and record.metadata is None and reply == NOT_ESTABLISHED


def test_explicit_aroge_search_is_offered_and_runs(world, monkeypatch):
    rows = [("https://news.example/a", "Unrelated Aroge item", "Text.")]
    world["script"] = lambda provider, payload: brave(rows)
    offered, reply, record = _offered_tools(world, monkeypatch, EXPLICIT_SEARCHES[0], [
        search_turn("Aroge Tech", "s1"), answer_turn("Only the listed result was found.", "1")])
    assert offered[0] == ["search_web", "get_kalillac_runtime_facts"]
    assert [call["provider"] for call in world["calls"]] == ["brave"]
    assert record.for_reply(reply) == {"kind": "sources", "count": 1}


def test_external_company_search_is_offered_and_runs(world, monkeypatch):
    rows = [("https://aroga.example/about", "About Aroga Technologies", "Aroga Technologies builds sensors.")]
    world["script"] = lambda provider, payload: brave(rows)
    offered, reply, record = _offered_tools(world, monkeypatch, "What is Aroga Technologies?", [
        search_turn("Aroga Technologies", "s1"), answer_turn("Aroga Technologies builds sensors.", "1")])
    assert "search_web" in offered[0]
    assert [call["provider"] for call in world["calls"]] == ["brave"]
    assert reply == "Aroga Technologies builds sensors." + footer("Sources", rows)


def test_explicit_search_about_kalillac_developer_keeps_verified_facts_authoritative(world, monkeypatch):
    rows = [("https://blog.example/kalillac", "A post about Kalillac", "Some claim.")]
    world["script"] = lambda provider, payload: brave(rows)
    offered, reply, record = _offered_tools(world, monkeypatch, EXPLICIT_SEARCHES[1], [
        search_turn("who developed Kalillac AI", "s1"),
        answer_turn("Kalillac's verified facts: developed under Aroge Tech.", "none")])
    assert "search_web" in offered[0]
    assert [call["provider"] for call in world["calls"]] == ["brave"]
    assert record.for_reply(reply) == {"kind": "search_results", "count": 1}
    # The search ran, and the verified developer facts were still sent as authoritative.
    instructions = world["instructions"][0]
    assert _facts_in(instructions) == []
    assert "DEVELOPER - VERIFIED APPLICATION FACTS, NOT SEARCH RESULTS:" in instructions
    assert "keep the established facts authoritative" in instructions
