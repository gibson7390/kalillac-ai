"""Deterministic tests for joining a cut-off answer with its continuation.

Live staging: the router answer hit max_output_tokens; the continuation
re-opened a ```python fence and restarted the interrupted line. Blind
concatenation produced "-> Gro```python" plus repeated lines. Inside a
docstring that still parsed (duplicated text shipped); inside code it was a
SyntaxError, the same-cap repair was cut off, and chat returned the
deterministic rejection after four long calls.
"""

import ast
import json
import os

import pytest

os.environ.setdefault("GROQ_API_KEY", "test-not-real")

import app_fastapi_candidate as app

from kalillac_failed_conversation_fixture import (
    TURNS,
    history_before,
    turn_index,
)
from kalillac_router_generation_fixture import (
    ROUTER_PROMPT,
    ROUTER_REFERENCE_CODE,
)
from kalillac_routing.openai_tool_loop import (
    run_tool_loop,
    stitch_continuation,
)


CODE = ROUTER_REFERENCE_CODE
CUT_MARKER = '            "created_at": ti'
REJECTION_MARKER = "I couldn't safely return that generated backend"


def _cut_index():
    return CODE.index(CUT_MARKER) + len(CUT_MARKER)


def _line_start(index, lines_back=0):
    """Start of the line containing index, moved back lines_back lines."""
    start = CODE.rfind("\n", 0, index) + 1

    for _ in range(lines_back):
        start = CODE.rfind("\n", 0, start - 1) + 1

    return start


def _partial():
    return "```python\n" + CODE[: _cut_index()]


def _fenced_body(stitched):
    code = app.extract_fenced_code(stitched)
    ast.parse(code)
    return code


def _assert_clean(stitched):
    assert "```python" not in stitched[len("```python"):]
    assert stitched.count("```") == 2
    assert _fenced_body(stitched) == CODE.strip()


# --- stitch_continuation ------------------------------------------------------


def test_continuation_without_fence_or_overlap_is_appended():
    stitched = stitch_continuation(_partial(), CODE[_cut_index():] + "```")

    _assert_clean(stitched)


def test_continuation_reopening_python_fence_is_unwrapped():
    continuation = "```python\n" + CODE[_cut_index():] + "```"

    _assert_clean(stitch_continuation(_partial(), continuation))


@pytest.mark.parametrize("lines_back", [0, 1, 3, 8])
def test_continuation_repeating_previous_lines_is_deduplicated(lines_back):
    restart = _line_start(_cut_index(), lines_back)
    continuation = "```python\n" + CODE[restart:] + "```"

    _assert_clean(stitch_continuation(_partial(), continuation))


def test_continuation_restarting_whole_block_replaces_partial():
    continuation = "```python\n" + CODE + "```"

    _assert_clean(stitch_continuation(_partial(), continuation))


def test_provider_chain_docstring_cut_has_no_nested_fence():
    # The shape seen on live staging.
    docstring = (
        '"""Provider chain:\n'
        "    OpenAI gpt-5.6-luna\n"
        "    -> Groq openai/gpt-oss-120b\n"
        "    -> Cloudflare Workers AI @cf/openai/gpt-oss-120b\n"
        "    -> Groq openai/gpt-oss-20b\n"
        '"""\n'
    )
    code = f"def call_model():\n    {docstring}    return None\n"
    cut = code.index("    -> Groq openai/gpt-oss-20b") + len("    -> Gro")
    partial = "```python\n" + code[:cut]
    continuation = (
        "```python\n"
        + code[code.index("    -> Groq openai/gpt-oss-120b"):]
        + "```"
    )

    stitched = stitch_continuation(partial, continuation)

    assert "Gro```" not in stitched
    assert stitched.count("-> Groq openai/gpt-oss-20b") == 1
    assert stitched.count("-> Cloudflare Workers AI") == 1
    assert _fenced_body(stitched) == code.strip()


def test_prose_continuation_is_joined_exactly():
    assert (
        stitch_continuation("The answer starts here and", " finishes here.")
        == "The answer starts here and finishes here."
    )


def test_short_coincidental_prefix_is_not_trimmed():
    # Fewer than MIN_CONTINUATION_OVERLAP repeated characters are kept.
    assert stitch_continuation("x = 1\ny", "y = 2\n") == "x = 1\nyy = 2\n"


def test_empty_sides():
    assert stitch_continuation("", "abc") == "abc"
    assert stitch_continuation("abc", "") == "abc"


def test_still_incomplete_continuation_stays_unfenced_and_unparsed():
    # Second cut: continuation stops before the end of the file.
    second_cut = CODE.index("def is_arithmetic_request")
    restart = _line_start(_cut_index(), 2)
    continuation = "```python\n" + CODE[restart:second_cut]

    stitched = stitch_continuation(_partial(), continuation)

    assert stitched.count("```") == 1
    assert stitched == "```python\n" + CODE[:second_cut]


# --- _invoke_openai ------------------------------------------------------------


def _response(text, incomplete=False):
    response = {
        "status": "incomplete" if incomplete else "completed",
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
    }

    if incomplete:
        response["incomplete_details"] = {
            "reason": app.OUTPUT_TOKEN_LIMIT_REASON,
        }

    return response


@pytest.fixture
def scripted_openai(monkeypatch):
    state = {"responses": [], "payloads": []}

    def fake_post(payload, timeout=90):
        state["payloads"].append(payload)
        return state["responses"].pop(0)

    def no_fallback(messages, invoke_kwargs):
        raise AssertionError("fallback chain must not run")

    monkeypatch.setattr(app, "OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setattr(app, "_post_openai_responses", fake_post)
    monkeypatch.setattr(app, "_invoke_existing_provider_chain", no_fallback)

    return state


def test_invoke_openai_stitches_fenced_restart(scripted_openai):
    restart = _line_start(_cut_index(), 1)
    scripted_openai["responses"] = [
        _response(_partial(), incomplete=True),
        _response("```python\n" + CODE[restart:] + "```"),
    ]

    response = app._invoke_openai([app.HumanMessage(content="write it")])

    assert response.incomplete is False
    _assert_clean(response.content)


def test_invoke_openai_still_incomplete_is_typed_and_unduplicated(
    scripted_openai,
):
    second_cut = CODE.index("def is_arithmetic_request")
    restart = _line_start(_cut_index(), 1)
    scripted_openai["responses"] = [
        _response(_partial(), incomplete=True),
        _response("```python\n" + CODE[restart:second_cut], incomplete=True),
    ]

    response = app._invoke_openai([app.HumanMessage(content="write it")])

    assert response.incomplete is True
    assert response.content == ("```python\n" + CODE[:second_cut]).strip()
    assert len(scripted_openai["payloads"]) == 2


# --- the exact multi-turn staging failure -------------------------------------


def test_router_as_turn_five_returns_router_not_rejection(scripted_openai):
    index = turn_index(ROUTER_PROMPT)
    history = history_before(index)

    # The first four completed turns of the original conversation.
    assert index == 4
    assert [turn["role"] for turn in history] == ["user", "assistant"] * 4
    assert history[0]["content"] == TURNS[0]["user"]

    restart = _line_start(_cut_index(), 1)
    scripted_openai["responses"] = [
        _response(_partial(), incomplete=True),
        _response("```python\n" + CODE[restart:] + "```"),
    ]

    reply = app.chat(ROUTER_PROMPT, history, session_id="turn-five-router")

    assert REJECTION_MARKER not in reply
    assert not app.has_incomplete_notice(reply)
    # Generation plus one continuation; no repair pass.
    assert len(scripted_openai["payloads"]) == 2
    _assert_clean(reply)

    # The continuation request carried the fence-aware instruction.
    continuation_input = json.dumps(scripted_openai["payloads"][1]["input"])
    assert "do not open a new code fence" in continuation_input


def test_router_code_prompt_does_not_depend_on_history():
    # The four prior turns do not enter this code prompt, so the live
    # isolated-vs-replay difference came from where the output cap cut the
    # answer, not from conversation context.
    assert not app.code_request_needs_recent_context(ROUTER_PROMPT)

    def prompt(history):
        return "\n".join(
            str(m.content)
            for m in app.build_messages(ROUTER_PROMPT, history, "code", [])
        )

    assert prompt([]) == prompt(history_before(turn_index(ROUTER_PROMPT)))


# --- V31 native tool loop -------------------------------------------------------


def test_tool_loop_stitches_fenced_restart():
    restart = _line_start(_cut_index(), 2)
    responses = [
        _response(_partial(), incomplete=True),
        _response("```python\n" + CODE[restart:] + "```"),
    ]

    result = run_tool_loop(
        user_message="write it",
        initial_input=[{"role": "user", "content": "write it"}],
        call_model=lambda items: responses.pop(0),
        execute_tool=lambda call: None,
    )

    assert result.incomplete is False
    _assert_clean(result.text)


# --- budget and prompt ---------------------------------------------------------


def test_code_budget_is_code_specific_and_bounded():
    assert app.MAX_RESPONSE_TOKENS == 1600
    assert app.CODE_RESPONSE_TOKENS == 6000


def test_router_prompt_separates_verified_and_example_values():
    messages = app.build_messages(ROUTER_PROMPT, [], "code", [])
    prompt = "\n".join(str(m.content) for m in messages)

    assert "VERIFIED VALUES VS EXAMPLE VALUES" in prompt
    assert (
        f"{app.SESSION_SEARCH_LIMIT} searches per rolling "
        f"{app.SESSION_SEARCH_WINDOW}-second window"
    ) in prompt
    assert "no verified time-based TTL" in prompt
    assert "# Example value, not a verified Kalillac setting" in prompt
