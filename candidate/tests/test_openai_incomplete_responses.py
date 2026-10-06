"""Responses API status/incomplete_details handling in Kalillac's OpenAI paths.

Every Responses API result here is fake; the HTTP layer is replaced, so no
network access occurs.
"""

import json
import os

import pytest

os.environ.setdefault("GROQ_API_KEY", "test-not-real")

import app_fastapi_candidate as app
from langchain_core.messages import HumanMessage, SystemMessage


def _response(text, status="completed", reason=None):
    response = {
        "status": status,
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
    }

    if reason is not None:
        response["incomplete_details"] = {"reason": reason}

    return response


def _cut_off(text):
    return _response(
        text,
        status="incomplete",
        reason=app.OUTPUT_TOKEN_LIMIT_REASON,
    )


MESSAGES = [
    SystemMessage(content="system"),
    HumanMessage(content="write something long"),
]


@pytest.fixture
def openai_calls(monkeypatch):
    """Queue fake Responses API results; record every payload sent."""
    state = {"queue": [], "payloads": []}

    def fake_post(payload, timeout=90):
        state["payloads"].append(payload)
        result = state["queue"].pop(0)

        if isinstance(result, Exception):
            raise result

        return result

    monkeypatch.setattr(app, "OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setattr(app, "_post_openai_responses", fake_post)

    return state


# --- _invoke_openai ---------------------------------------------------------


def test_completed_response_is_not_incomplete(openai_calls):
    openai_calls["queue"] = [_response("Done.")]

    response = app._invoke_openai(MESSAGES)

    assert response.content == "Done."
    assert response.incomplete is False
    assert response.incomplete_reason is None
    assert len(openai_calls["payloads"]) == 1


def test_token_cutoff_is_continued_once_and_joined(openai_calls):
    openai_calls["queue"] = [
        _cut_off("def handler():\n    return \"keep that available during"),
        _response(" the session\""),
    ]

    response = app._invoke_openai(MESSAGES, max_tokens=4000)

    assert response.content == (
        "def handler():\n    return \"keep that available during the session\""
    )
    assert response.incomplete is False
    assert len(openai_calls["payloads"]) == 2

    continuation = openai_calls["payloads"][1]
    # Same output cap: the continuation is not a hidden cap increase.
    assert continuation["max_output_tokens"] == 4000
    assert continuation["input"][-2]["role"] == "assistant"
    assert continuation["input"][-1] == {
        "role": "user",
        "content": app.CONTINUATION_INSTRUCTION,
    }


def test_token_cutoff_still_incomplete_is_typed(openai_calls):
    openai_calls["queue"] = [_cut_off("part one"), _cut_off(" part two")]

    response = app._invoke_openai(MESSAGES)

    assert response.content == "part one part two"
    assert response.incomplete is True
    assert response.incomplete_reason == app.OUTPUT_TOKEN_LIMIT_REASON
    assert app.is_incomplete_model_response(response)
    # Bounded: one continuation only.
    assert len(openai_calls["payloads"]) == 2


def test_failed_continuation_keeps_partial_as_incomplete(openai_calls):
    openai_calls["queue"] = [_cut_off("partial"), TimeoutError("slow")]

    response = app._invoke_openai(MESSAGES)

    assert response.content == "partial"
    assert response.incomplete is True


def test_non_token_incomplete_reason_is_not_continued(openai_calls):
    openai_calls["queue"] = [
        _response("partial", status="incomplete", reason="content_filter"),
    ]

    response = app._invoke_openai(MESSAGES)

    assert response.incomplete is True
    assert response.incomplete_reason == "content_filter"
    assert len(openai_calls["payloads"]) == 1


def test_invoke_llm_returns_typed_incomplete_without_fallback(
    monkeypatch,
    openai_calls,
):
    openai_calls["queue"] = [_cut_off("part one"), _cut_off(" part two")]

    def no_fallback(*args, **kwargs):
        raise AssertionError("fallback chain must not run")

    # OpenAI is the only provider: any other outbound request fails.
    monkeypatch.setattr(app.urllib.request, "urlopen", no_fallback)

    response = app.invoke_llm(MESSAGES)

    assert app.is_incomplete_model_response(response)
    assert response.content == "part one part two"


# --- notice helpers -----------------------------------------------------------


def test_mark_incomplete_reply_closes_open_fence_and_round_trips():
    partial = "```python\ndef f():\n    return \"during"

    marked = app.mark_incomplete_reply(partial)

    assert marked.endswith(app.INCOMPLETE_RESPONSE_NOTICE)
    assert marked.count("```") % 2 == 0
    assert app.has_incomplete_notice(marked)
    assert app.strip_incomplete_notice(marked) == partial
    assert app.previous_code_answer_looks_incomplete(marked)


def test_unvalidated_code_notice_round_trips():
    partial = "```python\ndef f():\n    return \"during"

    marked = app.mark_incomplete_reply(partial, unvalidated_code=True)

    assert app.UNVALIDATED_CODE_NOTICE in marked
    assert marked.endswith(app.INCOMPLETE_RESPONSE_NOTICE)
    assert app.strip_incomplete_notice(marked) == partial
    assert app.previous_code_answer_looks_incomplete(marked)


def test_marked_prose_is_not_treated_as_incomplete_code():
    marked = app.mark_incomplete_reply("A long prose answer that stops")

    assert not app.previous_code_answer_looks_incomplete(marked)


# --- HTML repair ------------------------------------------------------------


def test_cut_off_html_repair_keeps_pre_repair_document(monkeypatch):
    # An undefined class needs a model repair; no local fix applies.
    original = (
        "<!DOCTYPE html><html><head><style>body{}</style></head>"
        "<body><div class=\"navbar\">x</div></body></html>"
    )
    repair_calls = []

    def fake_invoke_llm(messages, max_tokens=None):
        repair_calls.append(messages)
        return app.SimpleNamespace(
            content="```html\n<!DOCTYPE html><html><body><p>cut",
            incomplete=True,
            incomplete_reason=app.OUTPUT_TOKEN_LIMIT_REASON,
        )

    monkeypatch.setattr(app, "invoke_llm", fake_invoke_llm)

    reply = app.enforce_code_quality(
        "make me a simple html page with a link",
        f"```html\n{original}\n```",
        "code",
    )

    assert len(repair_calls) == 1

    # Non-full-page repair carries the user-scope override.
    assert "USER-SCOPE OVERRIDE" in str(repair_calls[0][1].content)

    # The incomplete repair is discarded rather than returned.
    assert "<p>cut" not in reply
    assert "class=\"navbar\"" in reply
    assert "</html>" in reply


def test_incomplete_generation_skips_html_repair(monkeypatch):
    def no_repair(*args, **kwargs):
        raise AssertionError("cut-off HTML must not be repaired")

    monkeypatch.setattr(app, "repair_html_output", no_repair)

    partial = "```html\n<!DOCTYPE html><html><head><title>x"

    assert app.enforce_code_quality(
        "make me an html page",
        partial,
        "code",
        incomplete=True,
    ) == partial


# --- V31 native path ----------------------------------------------------------


def _native_chat(monkeypatch, responses):
    payloads = []

    def fake_post(payload, timeout=90):
        payloads.append(payload)
        return responses.pop(0)

    monkeypatch.setattr(app, "OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setattr(app, "_post_openai_responses", fake_post)

    state = app.get_session_state_by_id("native-incomplete")
    reply = app._run_v31_native_tool_chat(
        "explain temporary sessions in detail",
        [],
        state,
    )

    return reply, payloads


def test_v31_native_cutoff_is_continued_once(monkeypatch):
    reply, payloads = _native_chat(
        monkeypatch,
        [_cut_off("Temporary sessions keep"), _response(" context briefly.")],
    )

    assert reply == "Temporary sessions keep context briefly."
    assert len(payloads) == 2
    assert app.CONTINUATION_INSTRUCTION in json.dumps(payloads[1]["input"])
    assert not app.has_incomplete_notice(reply)


def test_v31_native_still_incomplete_is_surfaced(monkeypatch):
    reply, payloads = _native_chat(
        monkeypatch,
        [_cut_off("Temporary sessions keep"), _cut_off(" context")],
    )

    assert len(payloads) == 2
    assert reply.startswith("Temporary sessions keep context")
    assert reply.endswith(app.INCOMPLETE_RESPONSE_NOTICE)


def test_reasoning_only_cutoff_continuation_replays_no_empty_message(
    openai_calls,
):
    openai_calls["queue"] = [_cut_off(""), _response("Full answer.")]

    response = app._invoke_openai(MESSAGES)

    assert response.content == "Full answer."
    assert response.incomplete is False

    continuation_input = openai_calls["payloads"][1]["input"]
    assert all(
        item.get("content") for item in continuation_input
    )
