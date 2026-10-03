"""Regressions for the original failed Kalillac conversation.

The real chat() pipeline runs; only the OpenAI HTTP request and Tavily are
faked, so the router, prompt construction, HTML quality checks, canonical
self-knowledge, and Responses API status handling are exercised for real.
Assertions target behavior, not exact model prose.
"""

import json
import os

import pytest

os.environ.setdefault("GROQ_API_KEY", "test-not-real")

import app_fastapi_candidate as app

from kalillac_failed_conversation_fixture import (
    BASIC_HTML_PROMPT,
    BLUEPRINT_DIAGRAM_PROMPT,
    BLUEPRINT_DIAGRAM_PROMPT_SINGLE_SPACE,
    ROUTER_PROMPT,
    TURNS,
    history_before,
    turn_index,
)


BASIC_HTML_DOCUMENT = (
    "<!DOCTYPE html>\n<html>\n<head>\n  <title>My Page</title>\n</head>\n"
    "<body>\n  <h1>Hello</h1>\n  <p>This is my page.</p>\n</body>\n</html>"
)

ROUTER_PARTIAL = (
    "```python\n# router.py\n\ndef classify_request(message):\n"
    "    return \"I’ll keep that available during"
)

# Distinctive lines of the fixed backend architecture diagram.
CANNED_DIAGRAM_MARKERS = ("Uvicorn / FastAPI", "/api/chat")


def _response(text, incomplete=False):
    response = {
        "status": "incomplete" if incomplete else "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
    }

    if incomplete:
        response["incomplete_details"] = {
            "reason": app.OUTPUT_TOKEN_LIMIT_REASON,
        }

    return response


def _is_canned_diagram(reply):
    return (
        reply.strip() == app.kalillac_ascii_diagram().strip()
        or all(marker in reply for marker in CANNED_DIAGRAM_MARKERS)
    )


@pytest.fixture
def fake_openai(monkeypatch):
    """Fake Responses API keyed on the prompt currently being sent."""
    state = {"prompt": None, "payloads": [], "fallbacks": 0}

    def fake_post(payload, timeout=90):
        state["payloads"].append(payload)
        prompt = state["prompt"]
        is_continuation = app.CONTINUATION_INSTRUCTION in json.dumps(payload)

        if prompt == ROUTER_PROMPT:
            # Cut off by the output cap, and still cut off after the one
            # bounded continuation.
            return _response(
                " still" if is_continuation else ROUTER_PARTIAL,
                incomplete=True,
            )

        if prompt == BASIC_HTML_PROMPT:
            return _response(f"```html\n{BASIC_HTML_DOCUMENT}\n```")

        if prompt in {
            BLUEPRINT_DIAGRAM_PROMPT,
            BLUEPRINT_DIAGRAM_PROMPT_SINGLE_SPACE,
        }:
            return _response(
                "```text\nFree anonymous tier\n   |\n   v\n"
                "Optional paid entitlement\n```"
            )

        return _response("Fixture reply.")

    def fake_search(query, include_domains=None):
        return "ok", [
            {
                "title": "Fixture source",
                "url": "https://example.com/fixture",
                "content": "Fixture search content.",
                "published": None,
            }
        ]

    def no_fallback(messages, invoke_kwargs):
        state["fallbacks"] += 1
        raise AssertionError("fallback provider chain must not run")

    monkeypatch.setattr(app, "OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setattr(app, "_post_openai_responses", fake_post)
    monkeypatch.setattr(app, "run_web_search", fake_search)
    monkeypatch.setattr(app, "_invoke_existing_provider_chain", no_fallback)

    return state


def _run_turn(fake_openai, index, session_id):
    prompt = TURNS[index]["user"]
    fake_openai["prompt"] = prompt
    fake_openai["payloads"].clear()

    return app.chat(
        prompt,
        history_before(index),
        session_id=session_id,
    )


@pytest.mark.parametrize("v31_native", [False, True])
def test_original_conversation_replay(monkeypatch, fake_openai, v31_native):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", v31_native)
    session_id = f"failed-conversation-{v31_native}"

    for index, turn in enumerate(TURNS):
        if turn.get("stopped"):
            continue

        reply = _run_turn(fake_openai, index, session_id)

        assert isinstance(reply, str) and reply.strip(), turn["user"]
        assert fake_openai["fallbacks"] == 0, turn["user"]

        # No turn of this conversation asks for Kalillac's architecture.
        assert not _is_canned_diagram(reply), turn["user"]


def test_basic_html_stays_basic(fake_openai):
    index = turn_index(BASIC_HTML_PROMPT)

    reply = _run_turn(fake_openai, index, "basic-html")

    assert app.classify_request(BASIC_HTML_PROMPT, []) == "code"

    # One generation, no landing-page "repair" pass.
    assert len(fake_openai["payloads"]) == 1
    assert BASIC_HTML_DOCUMENT in reply

    lowered = reply.lower()
    for landing_marker in ("<nav", "hero", "<footer", "@media", "cta"):
        assert landing_marker not in lowered

    prompt = json.dumps(fake_openai["payloads"][0])
    assert "USER-SCOPE RULES" in prompt
    assert "Build a visually complete page" not in prompt


def test_basic_html_document_passes_non_full_page_quality_check():
    assert app.requests_minimal_code_scope(BASIC_HTML_PROMPT)
    assert app.html_quality_errors(
        BASIC_HTML_DOCUMENT,
        full_page=False,
    ) == []


def test_full_page_quality_check_still_requires_landing_structure():
    errors = app.html_quality_errors(BASIC_HTML_DOCUMENT, full_page=True)

    for expected in (
        "Missing required section: nav.",
        "Missing required section: hero.",
        "Missing required section: cta.",
        "Missing required section: footer.",
        "Missing responsive media queries.",
        "Missing overflow-x: hidden protection on body or layout.",
    ):
        assert expected in errors


@pytest.mark.parametrize(
    "message",
    [
        "produce very basic html code",
        "make me a simple html page",
        "make me a minimal landing page",
        "give me a barebones starter template",
        "create a starter html file",
        "write a plain html page",
        "just a snippet please",
        "show me an html snippet",
        "a basic website for my bakery",
        "make basic html",
        "build a landing page but keep it simple",
        "make a signup form as simple as possible",
    ],
)
def test_explicit_minimal_scope_overrides_landing_page_default(message):
    assert app.requests_minimal_code_scope(message)

    messages = app.build_messages(message, [], "code", [])
    prompt = "\n".join(str(m.content) for m in messages)

    assert "USER-SCOPE RULES" in prompt
    assert "Build a visually complete page" not in prompt


@pytest.mark.parametrize(
    "message",
    [
        "make a landing page with a simple color scheme",
        "build a website with a plain white background",
        "create a dashboard with basic analytics charts",
        "make a landing page for my starter plan pricing",
        "a portfolio page that shows code snippets",
        "build a landing page with simple navigation and a minimal footer",
    ],
)
def test_scope_word_inside_page_detail_keeps_full_page(message):
    assert not app.requests_minimal_code_scope(message)

    messages = app.build_messages(message, [], "code", [])
    prompt = "\n".join(str(m.content) for m in messages)

    assert "Build a visually complete page" in prompt


def test_detail_scope_word_keeps_full_page_quality_check(monkeypatch):
    repairs = []

    def fake_repair(message, code, errors, grounding_context="", full_page=True):
        repairs.append(full_page)
        return f"```html\n{code}\n```"

    monkeypatch.setattr(app, "repair_html_output", fake_repair)

    app.enforce_code_quality(
        "make a landing page with a simple color scheme",
        f"```html\n{BASIC_HTML_DOCUMENT}\n```",
        "code",
    )

    # Held to the full-page structure, so a full-page repair was requested.
    assert repairs == [True]


def test_unscoped_landing_page_keeps_polished_rules():
    message = "make me an html landing page"

    assert not app.requests_minimal_code_scope(message)

    messages = app.build_messages(message, [], "code", [])
    prompt = "\n".join(str(m.content) for m in messages)

    assert "Build a visually complete page" in prompt


@pytest.mark.parametrize(
    "prompt",
    [BLUEPRINT_DIAGRAM_PROMPT, BLUEPRINT_DIAGRAM_PROMPT_SINGLE_SPACE],
)
@pytest.mark.parametrize("v31_native", [False, True])
def test_blueprint_diagram_is_model_generated_with_context(
    monkeypatch,
    fake_openai,
    prompt,
    v31_native,
):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", v31_native)
    index = turn_index(BLUEPRINT_DIAGRAM_PROMPT)
    fake_openai["prompt"] = prompt

    assert not app.is_architecture_diagram_request(prompt)

    reply = app.chat(
        prompt,
        history_before(index),
        session_id=f"blueprint-{v31_native}",
    )

    assert not _is_canned_diagram(reply)
    assert "Optional paid entitlement" in reply

    # The model saw the monetization conversation it was asked to diagram.
    assert fake_openai["payloads"]
    sent = json.dumps(fake_openai["payloads"][0])
    assert "optional paid entitlements" in sent


@pytest.mark.parametrize(
    "prompt",
    [
        "Draw an ASCII diagram of your architecture",
        "create an ASCII of exactly how Kalillac AI works behind the scenes",
        "show me a kalillac architecture diagram",
        "can you make a flowchart of how kalillac works",
        "ascii diagram of kalillac ai's request flow",
    ],
)
def test_explicit_architecture_diagram_keeps_fixed_diagram(prompt):
    family, reply = app.get_canonical_self_knowledge_response(prompt)

    assert family == "architecture_diagram"
    assert reply == app.kalillac_ascii_diagram()


@pytest.mark.parametrize(
    "prompt",
    [
        "draw a diagram of kalillac ai's monetization plan",
        "create an ascii diagram of the kalillac ai roadmap",
        "make a flowchart of how kalillac ai could make money",
        "diagram the business model for kalillac",
    ],
)
def test_non_architecture_kalillac_diagrams_are_not_fixed(prompt):
    assert not app.is_architecture_diagram_request(prompt)


def test_cut_off_router_answer_is_marked_incomplete(fake_openai):
    index = turn_index(ROUTER_PROMPT)

    reply = _run_turn(fake_openai, index, "router")

    # Original request plus exactly one bounded continuation.
    assert len(fake_openai["payloads"]) == 2
    assert app.CONTINUATION_INSTRUCTION in json.dumps(
        fake_openai["payloads"][1]
    )

    assert reply.rstrip().endswith(app.INCOMPLETE_RESPONSE_NOTICE)
    assert reply.count("```") % 2 == 0
    assert "available during still" in reply

    # Cut-off Kalillac-reference Python skipped the AST gate and says so.
    assert app.UNVALIDATED_CODE_NOTICE in reply

    # A cut-off answer is never "repaired" at the same output cap.
    assert fake_openai["fallbacks"] == 0

    # The marked answer still routes "continue" to code continuation.
    history = history_before(index) + [
        {"role": "user", "content": ROUTER_PROMPT},
        {"role": "assistant", "content": reply},
    ]
    assert app.classify_request("continue", history) == "code_continuation"


def test_cut_off_kalillac_python_is_never_presented_as_validated(
    monkeypatch,
    fake_openai,
):
    partial = (
        "```python\n# router.py\n\ndef calculate(text):\n"
        "    return eval(text)\n\ndef route(message):\n    return \""
    )
    gate_calls = []

    def fake_post(payload, timeout=90):
        fake_openai["payloads"].append(payload)
        return _response(partial, incomplete=True)

    def record_gate(code):
        gate_calls.append(code)
        return []

    monkeypatch.setattr(app, "_post_openai_responses", fake_post)
    # If the partial entered the complete-code path, these would run and
    # (with no errors reported) let it through as validated.
    monkeypatch.setattr(app, "python_code_quality_errors", record_gate)
    monkeypatch.setattr(app, "kalillac_python_fidelity_errors", record_gate)
    fake_openai["prompt"] = ROUTER_PROMPT

    reply = app.chat(ROUTER_PROMPT, [], session_id="router-unvalidated")

    assert gate_calls == []
    # Initial request plus one continuation; no same-cap repair call.
    assert len(fake_openai["payloads"]) == 2

    assert app.UNVALIDATED_CODE_NOTICE in reply
    assert reply.rstrip().endswith(app.INCOMPLETE_RESPONSE_NOTICE)
    assert reply.count("```") % 2 == 0


def test_complete_kalillac_python_still_uses_ast_gate(monkeypatch):
    code = (
        "def calculate(text):\n    return eval(text)\n"
    )
    repairs = []

    def fake_repair(message, code, errors):
        repairs.append(errors)
        return code

    monkeypatch.setattr(app, "repair_python_output", fake_repair)

    reply = app.enforce_code_quality(
        ROUTER_PROMPT,
        f"```python\n{code}```",
        "code",
    )

    assert repairs and any("eval()" in e for e in repairs[0])
    assert "eval(text)" not in reply
    assert app.UNVALIDATED_CODE_NOTICE not in reply
