"""Lean V31 router, phase 1: current-information keyword forcing and the
obsolete Ollama debug instruction are removed; deterministic boundaries stay.

- Words such as "latest", "current", "today" or "price" no longer force the
  web_search route. With V31 on, the model decides whether to call
  search_web; with V31 off, those prompts take the normal model path.
- Explicit search instructions, explicit domains, identity verification,
  the one-search cap, session search limits, private-context blocking, the
  calculator and the crisis guard are unchanged.
- The dedicated logic route is intentionally unchanged in this checkpoint
  (inspection only), and so is normalize_for_router.

The model and Tavily are fakes; every other network destination is refused.
"""

from __future__ import annotations

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


ANSWER = "[mocked Luna answer]"
LEGACY = "[legacy model reply]"
RESULT = {"title": "Public result", "url": "https://example.com/result", "published": "2026-10-09",
          "content": "Networkless test result."}

# Prompts that the removed keyword lists used to force onto web_search.
FORMERLY_FORCED = [
    "who is the current president of the United States",
    "what is the weather in Terre Haute today",
    "find current iPhone prices",
    "what happened in the news today",
    "Who is Apple's CEO?",
    "What time does Walmart close?",
    "Is Python 3.14 released?",
    "Who runs Microsoft?",
    "Is version 4.0 available yet?",
    "What is the latest Kali Linux release?",
    "Is Mike still the CEO?",
    "What's the forecast for tomorrow?",
]
NON_FACTUAL_LATEST = "Write a short poem titled The Latest Thing."
QUOTED_CURRENT_PRICE = 'Rewrite this sentence more formally: "the current price is too high for us"'


@pytest.fixture
def harness(monkeypatch):
    """Scripted native model (emits the queued search_web queries, then
    answers), a fake Tavily, a fake legacy model and a socket guard."""
    seen = {"queries": [], "searches": [], "tool_results": [], "legacy": [], "network": []}

    def fake_native_model(input_items, instructions):
        outputs = [i for i in input_items if isinstance(i, dict) and i.get("type") == "function_call_output"]
        seen["tool_results"] = [json.loads(i["output"]) for i in outputs]
        if len(outputs) < len(seen["queries"]):
            return {"output": [{"type": "function_call", "name": "search_web", "call_id": f"s{len(outputs)}",
                                "arguments": json.dumps({"query": seen["queries"][len(outputs)]})}]}
        return {"output": [{"type": "message", "role": "assistant",
                            "content": [{"type": "output_text", "text": ANSWER}]}]}

    def fake_search(query, include_domains=None):
        seen["searches"].append(query)
        return "ok", [dict(RESULT)]

    def fake_legacy(messages, max_tokens=None, **kwargs):
        seen["legacy"].append(messages)
        return SimpleNamespace(content=LEGACY, incomplete=False, incomplete_reason=None)

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

    monkeypatch.setattr(app, "_invoke_openai_native_tools", fake_native_model)
    monkeypatch.setattr(app, "run_web_search", fake_search)
    monkeypatch.setattr(app, "invoke_llm", fake_legacy)
    for name in ("_post_tavily_for_attempt", "_invoke_openai", "_post_openai_for_attempt"):
        monkeypatch.setattr(app, name, refuse(name))
    monkeypatch.setattr(app.urllib.request, "urlopen", refuse("urlopen"))
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", True)
    monkeypatch.setattr(app, "SESSION_STATE", type(app.SESSION_STATE)())
    return seen


def chat(message, session_id, history=None):
    return app.chat(message, history or [], session_id=session_id)


# --- current-information words no longer force search ---------------------------------------------


@pytest.mark.parametrize("message", FORMERLY_FORCED)
def test_current_information_words_no_longer_select_web_search(message):
    assert not app.is_web_search_request(message, [])
    assert app.classify_request(message, []) in app.V31_NATIVE_TOOL_ROUTES


def test_a_non_factual_latest_prompt_does_not_search(harness):
    assert app.classify_request(NON_FACTUAL_LATEST, []) != "web_search"
    assert chat(NON_FACTUAL_LATEST, "latest-poem") == ANSWER
    assert harness["searches"] == [] and harness["tool_results"] == []
    assert app.get_session_state_by_id("latest-poem")["search_times"] == []


def test_a_quoted_current_price_rewrite_does_not_search(harness):
    assert app.classify_request(QUOTED_CURRENT_PRICE, []) != "web_search"
    chat(QUOTED_CURRENT_PRICE, "quoted-price")
    assert harness["searches"] == []
    assert app.get_session_state_by_id("quoted-price")["search_times"] == []


@pytest.mark.parametrize("message", [NON_FACTUAL_LATEST, QUOTED_CURRENT_PRICE, FORMERLY_FORCED[0]])
def test_with_v31_off_formerly_forced_prompts_take_the_normal_model_path(harness, monkeypatch, message):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", False)
    chat(message, f"legacy-{abs(hash(message))}")
    assert harness["searches"] == []
    assert harness["legacy"], "the normal legacy model path still answers"


def test_a_current_information_prompt_searches_once_when_the_model_asks(harness):
    harness["queries"] = ["weather Terre Haute today"]
    reply = chat("What is the weather in Terre Haute today?", "current-weather")

    assert harness["searches"] == ["weather Terre Haute today"]
    assert harness["tool_results"][0]["status"] == "ok"
    assert reply.startswith(ANSWER) and "https://example.com/result" in reply   # application-owned Sources
    assert len(app.get_session_state_by_id("current-weather")["search_times"]) == 1


# --- explicit search, domains and verification are kept ------------------------------------------


@pytest.mark.parametrize("message", ["Search the web for the latest Groq pricing.", "look up the Python 3.13 changelog",
                                     "search for the session musician Steve Gadd"])
def test_explicit_search_instructions_still_select_web_search(message):
    assert app.is_web_search_request(message, [])
    assert app.classify_request(message, []) == "web_search"


def test_an_explicit_search_still_runs_through_v31(harness):
    harness["queries"] = ["Groq pricing"]
    reply = chat("Search the web for the latest Groq pricing.", "explicit-v31")
    assert harness["searches"] == ["Groq pricing"] and "https://example.com/result" in reply


def test_an_explicit_search_is_still_forced_with_v31_off(harness, monkeypatch):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", False)
    chat("Search the web for the latest Groq pricing.", "explicit-legacy")
    assert len(harness["searches"]) == 1


def test_an_explicit_domain_still_selects_web_search():
    assert app.classify_request("tell me about groq.com", []) == "web_search"
    assert app.get_search_domain_filters("tell me about groq.com") == ["groq.com"]


def test_identity_verification_is_still_required_and_enforced(harness, monkeypatch):
    assert app.requires_web_verification("Who is Skeeter Jean?", [])
    assert app.classify_request("Who is Skeeter Jean?", []) == "web_search"

    # Legacy path: results that do not name the subject are not accepted.
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", False)
    reply = chat("Who is Skeeter Jean?", "verify-identity")
    assert len(harness["searches"]) == 1
    assert "couldn't reliably verify" in reply and harness["legacy"] == []


# --- search limits are unchanged ------------------------------------------------------------------


def test_the_one_search_cap_still_holds(harness):
    harness["queries"] = ["first query", "second query"]
    chat("What is the weather in Terre Haute today?", "one-search")

    assert harness["searches"] == ["first query"]
    assert [r["status"] for r in harness["tool_results"]] == ["ok", "rejected"]
    assert "Only one external search" in harness["tool_results"][1]["reason"]


def test_the_session_search_limit_still_holds(harness):
    state = app.get_session_state_by_id("session-limit")
    import time
    state["search_times"] = [time.time()] * app.SESSION_SEARCH_LIMIT
    harness["queries"] = ["weather today"]
    chat("What is the weather in Terre Haute today?", "session-limit")

    assert harness["searches"] == []
    assert harness["tool_results"][0]["status"] == "limited"


# --- deterministic boundaries are unchanged -------------------------------------------------------


@pytest.mark.parametrize("message,expected", [("what is 60 + 70", "130"), ("How much is 15% of 240?", "36")])
def test_safe_arithmetic_still_uses_the_calculator(harness, message, expected):
    assert app.classify_request(message, []) == "calculator"
    reply = chat(message, f"calc-{abs(hash(message))}")
    assert expected in reply and harness["searches"] == []


@pytest.mark.parametrize("message", ["What does NAND do?", "Create a truth table for XNOR", "Simplify A + A'B",
                                     "what is a NOR gate?", "explain NOR"])
def test_the_logic_route_is_unchanged_in_this_checkpoint(message):
    assert app.classify_request(message, []) == "logic"


def test_the_crisis_guard_still_runs_first(harness):
    reply = chat("I want to kill myself. What is the latest news today?", "crisis-first")
    assert reply == app.CRISIS_SELF_RESPONSE
    assert harness["searches"] == [] and harness["legacy"] == []


def test_private_context_search_blocking_is_unchanged(harness):
    harness["queries"] = ["groq.com pricing"]
    chat("Search my memory for groq.com.", "private-search")
    assert harness["searches"] == []
    assert harness["tool_results"][0]["status"] == "rejected"


# --- obsolete Ollama instruction ------------------------------------------------------------------


@pytest.mark.parametrize("message", ["ModelNotFound error with langchain_chroma",
                                     "ModuleNotFoundError: No module named 'langchain_chroma'"])
def test_debug_messages_contain_no_ollama_instruction(message):
    messages = app.build_messages(message, [], "debug", [])
    text = " ".join(str(getattr(m, "content", m)) for m in messages)

    assert "ollama" not in text.lower()
    assert "pip install langchain-chroma" in text                    # general Python guidance is kept


def test_no_active_prompt_mentions_ollama():
    source = Path(app.__file__).read_text(encoding="utf-8")
    assert "ollama" not in source.lower()
