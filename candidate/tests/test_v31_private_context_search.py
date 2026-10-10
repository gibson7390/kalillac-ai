"""V31 native-tool path: private context never reaches external search.

When the user's request (or the model-generated search_web query) asks
Kalillac to inspect the user's own session, memory or conversation, the
application rejects the tool call locally, before the one-search allowance,
the session search allowance, domain selection or Tavily. The mocked model
receives the rejection and answers from the conversation. Public searches keep
the existing path.

The model, Tavily and every network destination except loopback are fakes or
refused.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import socket
import sys
from types import SimpleNamespace

import pytest

CANDIDATE_DIR = Path(__file__).resolve().parents[1]
if str(CANDIDATE_DIR) not in sys.path:
    sys.path.insert(0, str(CANDIDATE_DIR))

import app_fastapi_candidate as app


PRIVATE_REQUESTS = [
    "Search my memory for groq.com.",
    "Look through this conversation for what I said about OpenAI.",
    "Search our chat history for the pricing number I mentioned.",
    "What do you remember about my business?",
    "Earlier in this conversation, what did I tell you?",
    "Search this session for the link I pasted.",
]
# References to the user's own earlier messages, caught by the composed
# is_private_search_target() predicate (existing recall/reference checks plus
# the narrow message-reference patterns).
CONVERSATION_REFERENCES = [
    "What did you say about my plan?",
    "What did I tell you earlier?",
    "Look at my previous message.",
    "Check what I pasted above.",
    "Search what I sent earlier for groq.com.",
    "Search my previous chat for the link.",
]
# The same references as a model might phrase them in a search_web query.
REFERENCE_QUERIES = [
    "what you said about my plan",
    "what I told you earlier",
    "my previous message",
    "what I pasted above",
    "what I sent earlier groq.com",
    "my previous chat link",
]
PUBLIC_REQUESTS = [
    "Search the web for Groq pricing.",
    "Look up OpenAI’s latest release notes.",
    "Search for current information about example.com.",
    "What did OpenAI announce in its latest release?",
    "What did OpenAI say in its latest announcement?",
    "Find OpenAI’s previous release notes.",
    "Search the web for the latest Groq pricing.",
    "Look up the previous Python release.",
]
PRIVATE_QUERY = "search my memory for the user's business details"
ANSWER = "[mocked Luna answer]"


@pytest.fixture
def harness(monkeypatch):
    """A scripted native model, spies on every search step, a fake Tavily and
    a socket guard. The model emits the queued search_web queries in order,
    then answers; every tool result it receives is recorded."""
    seen = {"tool_results": [], "searches": [], "allowance": 0, "domains": 0, "network": [], "queries": []}

    def fake_model(input_items, instructions):
        outputs = [item for item in input_items if isinstance(item, dict) and item.get("type") == "function_call_output"]
        seen["tool_results"] = [json.loads(item["output"]) for item in outputs]
        if len(outputs) < len(seen["queries"]):
            query = seen["queries"][len(outputs)]
            return {"output": [{"type": "function_call", "name": "search_web",
                                "arguments": json.dumps({"query": query}), "call_id": f"search-{len(outputs)}"}]}
        return {"output": [{"type": "message", "role": "assistant",
                            "content": [{"type": "output_text", "text": ANSWER}]}]}

    def fake_search(query, include_domains=None):
        seen["searches"].append(query)
        return "ok", [{"title": "Public result", "url": "https://example.com/public",
                       "published": "2026-10-01", "content": "Networkless test result."}]

    real_allowed = app.session_search_allowed
    real_explicit = app.get_search_domain_filters
    real_authoritative = app.get_authoritative_search_domains

    def counted_allowed(state):
        seen["allowance"] += 1
        return real_allowed(state)

    def counted_explicit(raw_message):
        seen["domains"] += 1
        return real_explicit(raw_message)

    def counted_authoritative(*args, **kwargs):
        seen["domains"] += 1
        return real_authoritative(*args, **kwargs)

    def refuse(name):
        def _refuse(*args, **kwargs):
            raise AssertionError(f"{name} must not be called")
        return _refuse

    real_connect = socket.socket.connect

    def guarded_connect(sock, address):
        host = str(address[0] if isinstance(address, tuple) else address)
        if host in ("127.0.0.1", "::1", "localhost") or host.startswith("127."):
            return real_connect(sock, address)
        seen["network"].append(host)
        raise AssertionError("no outbound network is allowed")

    monkeypatch.setattr(app, "_invoke_openai_native_tools", fake_model)
    monkeypatch.setattr(app, "run_web_search", fake_search)
    monkeypatch.setattr(app, "session_search_allowed", counted_allowed)
    monkeypatch.setattr(app, "get_search_domain_filters", counted_explicit)
    monkeypatch.setattr(app, "get_authoritative_search_domains", counted_authoritative)
    for name in ("_post_tavily_for_attempt", "invoke_llm", "_invoke_openai", "_post_openai_for_attempt"):
        monkeypatch.setattr(app, name, refuse(name))
    monkeypatch.setattr(app.urllib.request, "urlopen", refuse("urlopen"))
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", True)
    monkeypatch.setattr(app, "SESSION_STATE", type(app.SESSION_STATE)())
    return seen


def run_v31(message, queries, session_id):
    """Run the V31 native path directly with the model emitting `queries`."""
    state = app.get_session_state_by_id(session_id)
    reply = app._run_v31_native_tool_chat(message, [], state)
    return reply, state


def assert_rejected_privately(result):
    assert result["status"] == "rejected"
    assert "private session, memory or conversation context" in result["reason"]


# --- private requests -----------------------------------------------------------------------------


@pytest.mark.parametrize("message", PRIVATE_REQUESTS)
def test_private_requests_never_reach_search(harness, message):
    # The model asks for a public-looking search; the user's private intent still blocks it.
    harness["queries"] = ["groq.com OpenAI pricing"]
    reply, state = run_v31(message, harness["queries"], f"private-{abs(hash(message))}")

    assert reply == ANSWER                                  # the model answered after the rejection
    assert len(harness["tool_results"]) == 1
    assert_rejected_privately(harness["tool_results"][0])
    assert harness["searches"] == []                        # no Tavily / run_web_search
    assert harness["allowance"] == 0                        # session_search_allowed not called
    assert harness["domains"] == 0                          # no domain-filter selection
    assert state["search_times"] == []                      # no session search recorded
    assert harness["network"] == []


def test_a_domain_inside_a_private_request_does_not_override_the_boundary(harness):
    harness["queries"] = ["site:groq.com"]
    reply, state = run_v31("Search my memory for groq.com.", harness["queries"], "private-domain")

    assert_rejected_privately(harness["tool_results"][0])
    assert harness["searches"] == [] and harness["domains"] == 0 and state["search_times"] == []


def test_every_search_attempt_in_a_private_request_is_rejected(harness):
    harness["queries"] = ["groq pricing", "openai pricing"]
    reply, state = run_v31("Search our chat history for the pricing number I mentioned.",
                           harness["queries"], "private-repeat")

    assert reply == ANSWER
    assert [r["status"] for r in harness["tool_results"]] == ["rejected", "rejected"]
    assert all("private" in r["reason"] for r in harness["tool_results"])
    assert harness["searches"] == [] and harness["allowance"] == 0 and state["search_times"] == []


@pytest.mark.parametrize("message", CONVERSATION_REFERENCES)
def test_conversation_references_never_reach_search(harness, message):
    harness["queries"] = ["groq.com OpenAI pricing"]
    reply, state = run_v31(message, harness["queries"], f"reference-{abs(hash(message))}")

    assert reply == ANSWER
    assert len(harness["tool_results"]) == 1
    assert_rejected_privately(harness["tool_results"][0])
    assert harness["searches"] == [] and harness["allowance"] == 0 and harness["domains"] == 0
    assert state["search_times"] == [] and harness["network"] == []


@pytest.mark.parametrize("query", REFERENCE_QUERIES)
def test_model_generated_conversation_references_are_rejected(harness, query):
    message = "Can you help me with my pricing page?"
    assert not app.is_private_search_target(message)
    harness["queries"] = [query]
    reply, state = run_v31(message, harness["queries"], f"reference-query-{abs(hash(query))}")

    assert reply == ANSWER
    assert_rejected_privately(harness["tool_results"][0])
    assert harness["searches"] == [] and harness["allowance"] == 0 and harness["domains"] == 0
    assert state["search_times"] == [] and harness["network"] == []


@pytest.mark.parametrize("query", REFERENCE_QUERIES)
def test_a_rejected_reference_query_leaves_the_one_search_for_a_public_query(harness, query):
    harness["queries"] = [query, "Groq pricing"]
    reply, state = run_v31("Can you help me with my pricing page?", harness["queries"],
                           f"reference-then-public-{abs(hash(query))}")

    assert [r["status"] for r in harness["tool_results"]] == ["rejected", "ok"]
    assert harness["searches"] == ["Groq pricing"]
    assert harness["allowance"] == 1 and len(state["search_times"]) == 1


def test_conversation_reference_rejections_do_not_log_the_message_or_query(harness, monkeypatch):
    monkeypatch.setattr(app, "DEBUG_MODE", True)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        for message in CONVERSATION_REFERENCES:
            harness["queries"] = ["secret plan 5521"]
            run_v31(message, harness["queries"], f"reference-log-{abs(hash(message))}")
        for query in REFERENCE_QUERIES:
            harness["queries"] = [query]
            run_v31("Can you help me with my pricing page?", harness["queries"], f"reference-qlog-{abs(hash(query))}")
    logged = (out.getvalue() + err.getvalue()).lower()

    assert logged.count("v31 search: rejected (private context)") == len(CONVERSATION_REFERENCES) + len(REFERENCE_QUERIES)
    for private_text in ("5521", "secret plan", "my plan", "pasted above", "sent earlier", "previous message",
                         "previous chat", "told you", "pricing page"):
        assert private_text not in logged, private_text


# --- model-generated private queries --------------------------------------------------------------


def test_a_private_model_generated_query_is_rejected_locally(harness):
    message = "Can you help me with my pricing page?"
    assert not app.is_private_context_request(app.normalize_for_router(message))
    harness["queries"] = [PRIVATE_QUERY]
    reply, state = run_v31(message, harness["queries"], "private-query")

    assert reply == ANSWER
    assert_rejected_privately(harness["tool_results"][0])
    assert harness["searches"] == [] and harness["allowance"] == 0 and harness["domains"] == 0
    assert state["search_times"] == []


def test_a_rejected_query_does_not_consume_the_one_search_allowance(harness):
    # After the private query is rejected, a public query in the same request
    # still gets the request's single search.
    harness["queries"] = [PRIVATE_QUERY, "Groq pricing"]
    reply, state = run_v31("Can you help me with my pricing page?", harness["queries"], "private-then-public")

    assert reply.startswith(ANSWER)
    assert [r["status"] for r in harness["tool_results"]] == ["rejected", "ok"]
    assert harness["searches"] == ["Groq pricing"]
    assert harness["allowance"] == 1 and len(state["search_times"]) == 1


# --- logging --------------------------------------------------------------------------------------


def test_rejections_do_not_log_the_message_or_query(harness, monkeypatch):
    monkeypatch.setattr(app, "DEBUG_MODE", True)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        harness["queries"] = ["secret pricing number 4417"]
        run_v31("Search our chat history for the pricing number I mentioned.", harness["queries"], "log-1")
        harness["queries"] = [PRIVATE_QUERY]
        run_v31("Can you help me with my pricing page?", harness["queries"], "log-2")
    logged = (out.getvalue() + err.getvalue()).lower()

    assert logged.count("v31 search: rejected (private context)") == 2
    for private_text in ("4417", "chat history", "pricing number", "business details", "pricing page"):
        assert private_text not in logged, private_text


# --- public searches are unchanged ----------------------------------------------------------------


@pytest.mark.parametrize("message", PUBLIC_REQUESTS)
def test_public_requests_still_search(harness, message):
    harness["queries"] = ["OpenAI Groq public query"]
    reply, state = run_v31(message, harness["queries"], f"public-{abs(hash(message))}")

    assert harness["tool_results"][0]["status"] == "ok"
    assert harness["searches"] == ["OpenAI Groq public query"]
    assert harness["allowance"] == 1 and harness["domains"] >= 1
    assert len(state["search_times"]) == 1
    assert "**Sources**" in reply and "https://example.com/public" in reply
    assert harness["network"] == []


def test_explicit_public_domain_still_filters_the_search(harness, monkeypatch):
    captured = {}

    def fake_search(query, include_domains=None):
        captured["domains"] = include_domains
        return "ok", []

    monkeypatch.setattr(app, "run_web_search", fake_search)
    harness["queries"] = ["current information"]
    run_v31("Search for current information about example.com.", harness["queries"], "public-domain")

    assert captured["domains"] == ["example.com"]


# --- through chat() -------------------------------------------------------------------------------


CONVERSATION = [{"role": "user", "content": "Our pricing number is 4417 and the link is example.org/plan."},
                {"role": "assistant", "content": "Got it."}]


@pytest.mark.parametrize("message", [m for m in PRIVATE_REQUESTS
                                     if app.classify_request(m, CONVERSATION) in app.V31_NATIVE_TOOL_ROUTES])
def test_private_requests_routed_to_v31_by_chat_are_blocked(harness, message):
    harness["queries"] = ["groq.com OpenAI pricing"]
    session_id = f"chat-private-{abs(hash(message))}"
    reply = app.chat(message, CONVERSATION, session_id=session_id)

    assert reply == ANSWER
    assert_rejected_privately(harness["tool_results"][0])
    assert harness["searches"] == [] and harness["allowance"] == 0
    assert app.get_session_state_by_id(session_id)["search_times"] == []
    assert app.get_session_state_by_id(session_id)["memory"] == []


@pytest.mark.parametrize("message", CONVERSATION_REFERENCES)
def test_conversation_references_with_active_history_never_search_through_chat(harness, monkeypatch, message):
    # With active history the earlier chat guards let these through. Those
    # routed to V31 (5 of 6, including one the classifier labels web_search)
    # must have search_web refused; "What did I tell you earlier?" takes the
    # legacy memory route, which has no search.
    monkeypatch.setattr(app, "invoke_llm", lambda *a, **k: SimpleNamespace(
        content="[legacy reply]", incomplete=False, incomplete_reason=None))
    harness["queries"] = ["groq.com OpenAI pricing"]
    session_id = f"chat-reference-{abs(hash(message))}"
    route = app.classify_request(message, CONVERSATION)
    reply = app.chat(message, CONVERSATION, session_id=session_id)

    if message == "What did I tell you earlier?":
        assert route == "memory" and harness["tool_results"] == []
    else:
        assert route in app.V31_NATIVE_TOOL_ROUTES
        assert reply == ANSWER
        assert_rejected_privately(harness["tool_results"][0])
    assert harness["searches"] == [] and harness["allowance"] == 0 and harness["domains"] == 0
    assert app.get_session_state_by_id(session_id)["search_times"] == []
    assert harness["network"] == []


def test_the_crisis_guard_still_runs_before_v31(harness, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("V31 must not run for a crisis message")

    monkeypatch.setattr(app, "_run_v31_native_tool_chat", refuse)
    reply = app.chat("I want to kill myself. Search my memory for what I said.", [], session_id="crisis-first")

    assert reply == app.CRISIS_SELF_RESPONSE
    assert harness["searches"] == []
