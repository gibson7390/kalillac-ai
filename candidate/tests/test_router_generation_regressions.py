"""Regressions for the Kalillac router-generation rejection.

Live V31 staging returned Kalillac's deterministic rejection message for the
router prompt. The cause, observed on real model output for that prompt:
kalillac_python_fidelity_errors() required every verified model id to appear
as a literal, so valid illustrative code that abstracts the provider call was
rejected, and the whole-file repair at the same output cap could not fix it.
"""

import json
import os

import pytest

os.environ.setdefault("GROQ_API_KEY", "test-not-real")

import app_fastapi_candidate as app

from kalillac_router_generation_fixture import (
    ROUTER_PROMPT,
    ROUTER_REFERENCE_CODE,
)


REJECTION_MARKER = "I couldn't safely return that generated backend"


def _completed(text):
    return {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
    }


@pytest.fixture
def openai_calls(monkeypatch):
    state = {"replies": [], "payloads": []}

    def fake_post(payload, timeout=90):
        state["payloads"].append(payload)
        return _completed(state["replies"].pop(0))

    def no_fallback(messages, invoke_kwargs):
        raise AssertionError("fallback chain must not run")

    monkeypatch.setattr(app, "OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setattr(app, "_post_openai_responses", fake_post)
    monkeypatch.setattr(app, "_invoke_existing_provider_chain", no_fallback)

    return state


def _fenced(code):
    return f"```python\n{code}```"


# --- the original failure -----------------------------------------------------


def test_router_prompt_routes_to_kalillac_python_reference():
    reply = _fenced(ROUTER_REFERENCE_CODE)

    assert app.classify_request(ROUTER_PROMPT, []) == "code"
    assert app.is_kalillac_python_reference(ROUTER_PROMPT, reply)


def test_real_router_output_passes_safety_and_fidelity():
    # Safety: AST check for eval/exec. This router uses an allowlisted
    # ast.parse(..., mode="eval") evaluator, which is not an eval() call.
    assert app.python_code_quality_errors(ROUTER_REFERENCE_CODE) == []

    # Fidelity: the router names no model id, so it contradicts nothing.
    assert app.kalillac_python_fidelity_errors(ROUTER_REFERENCE_CODE) == []


def test_router_prompt_returns_router_without_repair(openai_calls):
    openai_calls["replies"] = [_fenced(ROUTER_REFERENCE_CODE)]

    reply = app.chat(ROUTER_PROMPT, [], session_id="router-generation")

    assert REJECTION_MARKER not in reply
    assert "def classify_request(message: str) -> RouteDecision:" in reply
    assert not app.has_incomplete_notice(reply)
    # One generation; no repair pass.
    assert len(openai_calls["payloads"]) == 1


def test_router_prompt_states_model_id_rule():
    messages = app.build_messages(ROUTER_PROMPT, [], "code", [])
    prompt = "\n".join(str(m.content) for m in messages)

    assert "Model ids are optional in the code" in prompt
    assert "AUTHORITATIVE KALILLAC REFERENCE" in prompt
    assert "never claim it is Kalillac's exact/private source" in prompt


# --- fidelity still rejects contradictions -----------------------------------


@pytest.mark.parametrize(
    "code, expected",
    [
        ('MODEL = "gpt-oss-120b"\n', "Short model id gpt-oss-120b"),
        ('MODEL = "gpt-oss-20b"\n', "Short model id gpt-oss-20b"),
        ('MODEL = "gpt-4o"\n', "Model id gpt-4o is not in Kalillac"),
        ('MODEL = "gpt-5.6"\n', "Model id gpt-5.6 is not in Kalillac"),
        (
            'CHAIN = ["openai/gpt-oss-20b", "gpt-5.6-luna"]\n',
            "Provider chain order contradicts",
        ),
        (
            'CHAIN = [\n'
            '    {"provider": "groq", "model": "openai/gpt-oss-120b"},\n'
            '    {"provider": "openai", "model": "gpt-5.6-luna"},\n'
            ']\n',
            "Provider chain order contradicts",
        ),
    ],
)
def test_fidelity_rejects_contradicting_model_facts(code, expected):
    errors = app.kalillac_python_fidelity_errors(code)

    assert any(expected in error for error in errors), errors


@pytest.mark.parametrize(
    "code",
    [
        'import os\nMODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")\n',
        (
            "CHAIN = [\n"
            '    ("openai", "gpt-5.6-luna"),\n'
            '    ("groq", "openai/gpt-oss-120b"),\n'
            '    ("cloudflare", "@cf/openai/gpt-oss-120b"),\n'
            '    ("groq", "openai/gpt-oss-20b"),\n'
            "]\n"
        ),
        'PRIMARY = "gpt-5.6-luna"\n',
        "def call_model(messages):\n    return provider.complete(messages)\n",
    ],
)
def test_fidelity_accepts_accurate_or_abstracted_models(code):
    assert app.kalillac_python_fidelity_errors(code) == []


# --- AST safety is unchanged ---------------------------------------------------


def test_complete_router_with_eval_is_still_blocked(openai_calls):
    unsafe = ROUTER_REFERENCE_CODE + (
        "\n\ndef quick_calculate(text: str) -> float:\n"
        "    return eval(text)\n"
    )
    # Generation, then the repair returns the same unsafe code.
    openai_calls["replies"] = [_fenced(unsafe), _fenced(unsafe)]

    reply = app.chat(ROUTER_PROMPT, [], session_id="router-unsafe-complete")

    assert REJECTION_MARKER in reply
    assert "eval(text)" not in reply
    assert len(openai_calls["payloads"]) == 2
    assert "Disallowed eval() call" in json.dumps(openai_calls["payloads"][1])


# --- code survives cleanup and extraction -------------------------------------


def test_clean_ai_reply_does_not_edit_fenced_code():
    code_reply = (
        "```python\nfor chunk in stream:\n    yield chunk\n```"
    )

    assert app.clean_ai_reply(code_reply) == code_reply


def test_clean_ai_reply_still_cleans_prose_outside_code():
    reply = (
        "Here is the answer from document chunk 3.\n\n"
        "```python\nfor chunk in stream:\n    yield chunk\n```"
    )

    cleaned = app.clean_ai_reply(reply)

    assert "document chunk 3" not in cleaned
    assert "for chunk in stream:\n    yield chunk" in cleaned


def test_extract_fenced_code_keeps_inner_backticks():
    code = (
        "def strip_fences(text):\n"
        '    if text.startswith("```"):\n'
        "        return text[3:]\n"
        "    return text"
    )

    assert app.extract_fenced_code(f"```python\n{code}\n```") == code


def test_router_with_fence_handling_code_is_returned(openai_calls):
    code = ROUTER_REFERENCE_CODE + (
        "\n\ndef strip_fences(text: str) -> str:\n"
        '    if text.startswith("```") and text.endswith("```"):\n'
        "        return text[3:-3]\n"
        "    return text\n"
    )
    openai_calls["replies"] = [_fenced(code)]

    reply = app.chat(ROUTER_PROMPT, [], session_id="router-fences")

    assert REJECTION_MARKER not in reply
    assert "def strip_fences(text: str) -> str:" in reply
    assert len(openai_calls["payloads"]) == 1
