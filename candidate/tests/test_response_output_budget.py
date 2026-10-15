"""Output-token ceilings sent to the model, per route and path.

Normal responses (legacy and V31 native) use MAX_RESPONSE_TOKENS = 3200. The
ceiling covers reasoning tokens as well as the visible answer and is an
upper bound, not a target length. Specialized limits are unchanged:

- legacy model-backed self-knowledge: 2,000;
- native model-backed self-knowledge: the normal ceiling, because the native
  call always uses MAX_RESPONSE_TOKENS;
- code generation and revision: 6,000;
- code continuation: 2,400;
- deterministic routes such as the calculator: no model call.

Every payload is captured from the real chat flow with a scripted model and
scripted search; any other network access fails the test.
"""

from __future__ import annotations

import json

import pytest

import app_fastapi_candidate as app
from test_self_knowledge_voice import chat, completed, pipeline, prompt  # noqa: F401  (fixture)


def user(text):
    return {"role": "user", "content": text}


def assistant(text):
    return {"role": "assistant", "content": text}


def cut_off(text):
    response = completed(text)
    response["status"] = "incomplete"
    response["incomplete_details"] = {"reason": app.OUTPUT_TOKEN_LIMIT_REASON}
    return response


def search_call(query):
    return {"status": "completed", "output": [{"type": "function_call", "name": "search_web",
                                               "arguments": json.dumps({"query": query}), "call_id": "search-1"}]}


def caps(state):
    return [payload["max_output_tokens"] for payload in state["payloads"]]


DELIVERED_CODE = [user("Write a Python function that adds two numbers."),
                  assistant("```python\ndef add(a, b):\n    return a + b\n```")]
SEARCH_RESULT = ("ok", [{"title": "FastAPI release notes", "url": "https://example.org/fastapi",
                         "content": "Release notes for the latest version.", "published": None}])


def test_the_normal_ceiling_is_3200_and_specialized_limits_are_unchanged():
    assert app.MAX_RESPONSE_TOKENS == 3200
    assert app.SELF_KNOWLEDGE_RESPONSE_TOKENS == 2000
    assert app.CODE_RESPONSE_TOKENS == 6000
    assert app.CODE_CONTINUATION_RESPONSE_TOKENS == 2400


@pytest.mark.parametrize("message,history,route", [
    ("Explain recursion.", [], "general"),
    ("Teach me how TCP/IP networking works, in depth.", [], "general"),
    ("My laptop won't connect to Wi-Fi.", [], "general"),
    ("I'm torn between two job offers and feel anxious. Help me decide.", [], "personal"),
    ("Why?", [user("Is Python interpreted?"),
              assistant("Mostly: CPython compiles to bytecode, then interprets it.")], "followup"),
    ("Simplify the Boolean expression A AND (A OR B).", [], "logic"),
])
def test_normal_responses_send_the_3200_token_ceiling(pipeline, message, history, route):
    assert app.classify_request(message, history) == route
    pipeline["text"] = "Scripted answer."
    assert chat(pipeline, message, history) == "Scripted answer."
    assert caps(pipeline) == [3200]


def test_a_web_search_answer_sends_the_3200_token_ceiling(pipeline, monkeypatch):
    searches = []
    monkeypatch.setattr(app, "run_web_search", lambda *a, **k: searches.append(a) or SEARCH_RESULT)
    message = "Search the web for the latest FastAPI release."
    assert app.classify_request(message, []) == "web_search"
    if pipeline["native"]:
        pipeline["responses"] = [search_call("latest FastAPI release"), completed("Scripted answer.")]
    chat(pipeline, message)
    assert len(searches) == 1                                          # scripted search, no provider
    if pipeline["native"]:
        assert caps(pipeline) == [3200, 3200]                          # every native tool round
    else:
        assert caps(pipeline) == [3200]
        assert "https://example.org/fastapi" in prompt(pipeline)


def test_self_knowledge_keeps_its_existing_selection(pipeline):
    message = "Explain your configuration in detail, including privacy limits and recorded deployment facts."
    assert app.classify_request(message, []) == "self_knowledge"
    assert app.get_canonical_self_knowledge_response(message) == (None, None)
    chat(pipeline, message)
    expected = app.MAX_RESPONSE_TOKENS if pipeline["native"] else app.SELF_KNOWLEDGE_RESPONSE_TOKENS
    assert caps(pipeline) == [expected]
    assert expected == (3200 if pipeline["native"] else 2000)


@pytest.mark.parametrize("message,history,route,limit", [
    ("Write Python code to parse JSON.", [], "code", 6000),
    ("Add type hints to that function.", DELIVERED_CODE, "revision", 6000),
])
def test_code_generation_and_revision_keep_6000(pipeline, message, history, route, limit):
    assert app.classify_request(message, history) == route
    pipeline["text"] = "```python\ndef add(a: int, b: int) -> int:\n    return a + b\n```"
    chat(pipeline, message, history)
    assert caps(pipeline) == [limit]


def test_code_continuation_keeps_2400(pipeline):
    history = [user("Write Python code for a bit mask and print it."),
               assistant(app.mark_incomplete_reply("```python\nmask = 1 ^ 2"))]
    assert app.classify_request("continue", history) == "code_continuation"
    pipeline["text"] = "```python\nprint(mask)\n```"
    chat(pipeline, "continue", history)
    assert caps(pipeline) == [2400]


@pytest.mark.parametrize("message", ["What is 2 + 2?", "How does Kalillac work?"])
def test_deterministic_answers_make_no_model_call(pipeline, message):
    reply = chat(pipeline, message)
    assert reply
    assert pipeline["payloads"] == []


def test_a_cut_off_normal_answer_is_continued_once_at_the_same_ceiling(pipeline):
    pipeline["responses"] = [cut_off("Recursion is when a function calls"),
                             completed(" itself on a smaller input.")]
    reply = chat(pipeline, "Explain recursion.")
    assert all(cap == 3200 for cap in caps(pipeline))                  # no hidden cap increase
    assert 1 <= len(pipeline["payloads"]) <= 2
    if not pipeline["native"]:
        assert len(pipeline["payloads"]) == 2
        assert pipeline["payloads"][1]["input"][-1] == {"role": "user", "content": app.CONTINUATION_INSTRUCTION}
        assert reply == "Recursion is when a function calls itself on a smaller input."


def test_the_runtime_facts_report_the_normal_ceiling():
    facts = app._v31_runtime_facts()
    assert facts["limits"]["default_output_tokens"] == 3200
    assert facts["limits"]["legacy_self_knowledge_output_tokens"] == 2000
    assert facts["limits"]["code_output_tokens"] == 6000
    assert facts["limits"]["code_continuation_output_tokens"] == 2400
