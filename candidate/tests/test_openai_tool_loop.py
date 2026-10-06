"""Unit tests for the V31 native-tool loop.

Every model response and tool result here is fake.
No network access occurs.
"""

from pathlib import Path
import sys

import pytest


CANDIDATE_DIR = Path(__file__).resolve().parents[1]

if str(CANDIDATE_DIR) not in sys.path:
    sys.path.insert(0, str(CANDIDATE_DIR))


from kalillac_routing.openai_tool_loop import (
    CONTINUATION_INSTRUCTION,
    ToolLoopError,
    ToolLoopOutputError,
    ToolLoopProtocolError,
    response_incomplete_reason,
    run_tool_loop,
)
from kalillac_routing.tool_contract import (
    ToolValidationError,
)


def message(text):
    return {
        "type": "message",
        "content": [
            {
                "type": "output_text",
                "text": text,
            }
        ],
    }


def function_call(call_id, name, arguments):
    return {
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
    }


def test_direct_response_executes_no_tool():
    model_inputs = []

    def call_model(items):
        model_inputs.append(items)

        return {
            "output": [
                message("Direct answer."),
            ]
        }

    def execute_tool(_call):
        raise AssertionError(
            "Tool executor must not run."
        )

    result = run_tool_loop(
        user_message="Explain TCP port 443",
        initial_input=[
            {
                "role": "user",
                "content": "Explain TCP port 443",
            }
        ],
        call_model=call_model,
        execute_tool=execute_tool,
    )

    assert result.text == "Direct answer."
    assert result.model_calls == 1
    assert result.tool_calls == ()
    assert len(model_inputs) == 1


def test_search_call_is_validated_executed_and_replayed():
    captured_inputs = []
    responses = [
        {
            "output": [
                {
                    "type": "reasoning",
                    "id": "reasoning_1",
                    "encrypted_content": "opaque-test-value",
                },
                function_call(
                    "call_search_1",
                    "search_web",
                    '{"query":"AI news today March 2026"}',
                ),
            ]
        },
        {
            "output": [
                message("Here is today's AI news."),
            ]
        },
    ]

    def call_model(items):
        captured_inputs.append(items)
        return responses.pop(0)

    executed = []

    def execute_tool(call):
        executed.append(call)

        return {
            "status": "ok",
            "results": [
                {
                    "title": "Example",
                    "url": "https://example.com",
                }
            ],
        }

    result = run_tool_loop(
        user_message="AI news today",
        initial_input=[
            {
                "role": "user",
                "content": "AI news today",
            }
        ],
        call_model=call_model,
        execute_tool=execute_tool,
    )

    assert result.text == "Here is today's AI news."
    assert result.model_calls == 2
    assert len(result.tool_calls) == 1

    assert executed[0].name == "search_web"
    assert executed[0].arguments == {
        "query": "AI news today",
    }

    second_input = captured_inputs[1]

    assert second_input[1]["type"] == "reasoning"
    assert second_input[2]["type"] == "function_call"

    tool_output = second_input[3]

    assert tool_output["type"] == "function_call_output"
    assert tool_output["call_id"] == "call_search_1"
    assert '"status":"ok"' in tool_output["output"]


def test_runtime_tool_can_complete_then_return_text():
    responses = [
        {
            "output": [
                function_call(
                    "runtime_1",
                    "get_kalillac_runtime_facts",
                    '{"topic":"current primary model"}',
                ),
            ]
        },
        {
            "output": [
                message(
                    "Kalillac's configured primary model is Luna."
                ),
            ]
        },
    ]

    def call_model(_items):
        return responses.pop(0)

    def execute_tool(call):
        assert call.name == "get_kalillac_runtime_facts"

        return {
            "configured_primary": {
                "provider": "OpenAI",
                "model": "gpt-5.6-luna",
            }
        }

    result = run_tool_loop(
        user_message="What model do you use?",
        initial_input=[
            {
                "role": "user",
                "content": "What model do you use?",
            }
        ],
        call_model=call_model,
        execute_tool=execute_tool,
    )

    assert result.model_calls == 2
    assert len(result.tool_calls) == 1
    assert "Luna" in result.text


def test_invalid_tool_call_is_rejected_before_execution():
    executed = []

    def call_model(_items):
        return {
            "output": [
                function_call(
                    "bad_search",
                    "search_web",
                    '{"query":"search the web"}',
                )
            ]
        }

    def execute_tool(call):
        executed.append(call)
        return {}

    with pytest.raises(
        ToolValidationError,
        match="meaningful search target",
    ):
        run_tool_loop(
            user_message="search",
            initial_input=[
                {
                    "role": "user",
                    "content": "search",
                }
            ],
            call_model=call_model,
            execute_tool=execute_tool,
        )

    assert executed == []


def test_entire_batch_validates_before_any_execution():
    executed = []

    def call_model(_items):
        return {
            "output": [
                function_call(
                    "valid_1",
                    "get_kalillac_runtime_facts",
                    '{"topic":"primary model"}',
                ),
                function_call(
                    "invalid_2",
                    "search_web",
                    '{"query":"search the web"}',
                ),
            ]
        }

    def execute_tool(call):
        executed.append(call)
        return {}

    with pytest.raises(ToolValidationError):
        run_tool_loop(
            user_message="Tell me about yourself and search",
            initial_input=[
                {
                    "role": "user",
                    "content": "Tell me about yourself and search",
                }
            ],
            call_model=call_model,
            execute_tool=execute_tool,
        )

    assert executed == []


def test_maximum_tool_rounds_stops_repeated_tool_requests():
    responses = [
        {
            "output": [
                function_call(
                    "one",
                    "get_kalillac_runtime_facts",
                    '{"topic":"primary model"}',
                )
            ]
        },
        {
            "output": [
                function_call(
                    "two",
                    "get_kalillac_runtime_facts",
                    '{"topic":"fallback model"}',
                )
            ]
        },
    ]

    def call_model(_items):
        return responses.pop(0)

    executions = []

    def execute_tool(call):
        executions.append(call)
        return {"status": "ok"}

    with pytest.raises(
        ToolLoopError,
        match="Maximum tool rounds exceeded",
    ):
        run_tool_loop(
            user_message="What model do you use?",
            initial_input=[
                {
                    "role": "user",
                    "content": "What model do you use?",
                }
            ],
            call_model=call_model,
            execute_tool=execute_tool,
            max_tool_rounds=1,
        )

    assert len(executions) == 1


def test_maximum_tool_calls_rejects_batch_before_execution():
    calls = [
        function_call(
            f"call_{number}",
            "get_kalillac_runtime_facts",
            '{"topic":"runtime"}',
        )
        for number in range(5)
    ]

    def call_model(_items):
        return {"output": calls}

    executions = []

    def execute_tool(call):
        executions.append(call)
        return {"status": "ok"}

    with pytest.raises(
        ToolLoopError,
        match="Maximum tool calls exceeded",
    ):
        run_tool_loop(
            user_message="Tell me about Kalillac",
            initial_input=[
                {
                    "role": "user",
                    "content": "Tell me about Kalillac",
                }
            ],
            call_model=call_model,
            execute_tool=execute_tool,
            max_tool_calls=4,
        )

    assert executions == []


def test_missing_final_text_is_rejected():
    def call_model(_items):
        return {
            "output": [
                {
                    "type": "reasoning",
                    "id": "reasoning_only",
                }
            ]
        }

    def execute_tool(_call):
        raise AssertionError(
            "Tool executor must not run."
        )

    with pytest.raises(
        ToolLoopError,
        match="without visible output text",
    ):
        run_tool_loop(
            user_message="hello",
            initial_input=[
                {
                    "role": "user",
                    "content": "hello",
                }
            ],
            call_model=call_model,
            execute_tool=execute_tool,
        )


# ---------------------------------------------------------------------------
# Incomplete Responses API results
# ---------------------------------------------------------------------------


def incomplete(text, reason="max_output_tokens"):
    return {
        "status": "incomplete",
        "incomplete_details": {"reason": reason},
        "output": [message(text)],
    }


def completed(text):
    return {
        "status": "completed",
        "output": [message(text)],
    }


def no_tools(_call):
    raise AssertionError("Tool executor must not run.")


USER_INPUT = [{"role": "user", "content": "write a long answer"}]


def test_response_incomplete_reason():
    assert response_incomplete_reason(completed("x")) is None
    assert response_incomplete_reason({"output": []}) is None
    assert (
        response_incomplete_reason(incomplete("x"))
        == "max_output_tokens"
    )
    assert (
        response_incomplete_reason({"status": "incomplete", "output": []})
        == "unknown"
    )


def test_token_cutoff_gets_one_continuation_that_completes():
    model_inputs = []
    responses = [
        incomplete("The answer starts here and"),
        completed(" finishes here."),
    ]

    def call_model(items):
        model_inputs.append(items)
        return responses.pop(0)

    result = run_tool_loop(
        user_message="write a long answer",
        initial_input=USER_INPUT,
        call_model=call_model,
        execute_tool=no_tools,
    )

    assert result.text == "The answer starts here and finishes here."
    assert result.incomplete is False
    assert result.incomplete_reason is None
    assert result.model_calls == 2

    continuation_input = model_inputs[1]
    assert continuation_input[-1] == {
        "role": "user",
        "content": CONTINUATION_INSTRUCTION,
    }
    # The cut-off output is replayed before the continuation request.
    assert continuation_input[-2]["type"] == "message"


def test_token_cutoff_still_incomplete_after_continuation_is_typed():
    calls = []

    def call_model(items):
        calls.append(items)
        return incomplete("part one" if len(calls) == 1 else " part two")

    result = run_tool_loop(
        user_message="write a long answer",
        initial_input=USER_INPUT,
        call_model=call_model,
        execute_tool=no_tools,
    )

    # Bounded: exactly one continuation attempt.
    assert len(calls) == 2
    assert result.text == "part one part two"
    assert result.incomplete is True
    assert result.incomplete_reason == "max_output_tokens"


def test_non_token_incomplete_reason_is_not_continued():
    calls = []

    def call_model(items):
        calls.append(items)
        return incomplete("Partial", reason="content_filter")

    result = run_tool_loop(
        user_message="write a long answer",
        initial_input=USER_INPUT,
        call_model=call_model,
        execute_tool=no_tools,
    )

    assert len(calls) == 1
    assert result.incomplete is True
    assert result.incomplete_reason == "content_filter"


def test_continuations_can_be_disabled():
    calls = []

    def call_model(items):
        calls.append(items)
        return incomplete("Partial")

    result = run_tool_loop(
        user_message="write a long answer",
        initial_input=USER_INPUT,
        call_model=call_model,
        execute_tool=no_tools,
        max_continuations=0,
    )

    assert len(calls) == 1
    assert result.incomplete is True


def test_incomplete_response_with_tool_call_never_executes_tool():
    def call_model(_items):
        return {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [
                function_call(
                    "call_1",
                    "search_web",
                    '{"query": "trunc',
                ),
            ],
        }

    with pytest.raises(ToolLoopError, match="incomplete during tool"):
        run_tool_loop(
            user_message="search for something",
            initial_input=USER_INPUT,
            call_model=call_model,
            execute_tool=no_tools,
        )


def test_incomplete_response_without_visible_text_is_an_error():
    def call_model(_items):
        return {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [{"type": "reasoning", "id": "r1"}],
        }

    with pytest.raises(ToolLoopError, match="without visible output text"):
        run_tool_loop(
            user_message="write a long answer",
            initial_input=USER_INPUT,
            call_model=call_model,
            execute_tool=no_tools,
        )


# ---------------------------------------------------------------------------
# Failure classification (OpenAI-only provider policy)
# ---------------------------------------------------------------------------


def _run(responses, execute_tool=no_tools, **limits):
    queue = list(responses)

    return run_tool_loop(
        user_message="hello",
        initial_input=[{"role": "user", "content": "hello"}],
        call_model=lambda _items: queue.pop(0),
        execute_tool=execute_tool,
        **limits,
    )


def test_subclasses_keep_the_tool_loop_error_contract():
    assert issubclass(ToolLoopOutputError, ToolLoopError)
    assert issubclass(ToolLoopProtocolError, ToolLoopError)
    assert not issubclass(ToolLoopOutputError, ToolLoopProtocolError)
    assert not issubclass(ToolLoopProtocolError, ToolLoopOutputError)


@pytest.mark.parametrize(
    "response",
    [
        "not an object",
        {"status": "completed"},
        {"output": ["not an object item"]},
        completed(""),
    ],
    ids=["non_object", "missing_output_list", "non_object_item", "no_visible_text"],
)
def test_unusable_model_output_is_an_output_error(response):
    with pytest.raises(ToolLoopOutputError):
        _run([response])


def _runtime_call(call_id="call_1"):
    return function_call(call_id, "get_kalillac_runtime_facts", '{"topic": "models"}')


@pytest.mark.parametrize(
    "responses, limits",
    [
        ([{"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
           "output": [_runtime_call()]}], {}),
        ([incomplete("partial"), {"output": [_runtime_call()]}], {}),
        ([{"output": [_runtime_call()]}], {"max_tool_rounds": 0}),
        ([{"output": [_runtime_call("a"), _runtime_call("b")]}], {"max_tool_calls": 1}),
        ([{"output": [{"type": "function_call", "call_id": "c", "arguments": "{}"}]}], {}),
        ([{"output": [{"type": "function_call", "name": "get_kalillac_runtime_facts",
                       "arguments": "{}"}]}], {}),
    ],
    ids=["incomplete_tool_selection", "tool_during_continuation", "max_rounds",
         "max_calls", "missing_name", "missing_call_id"],
)
def test_tool_protocol_violations_are_protocol_errors(responses, limits):
    with pytest.raises(ToolLoopProtocolError):
        _run(responses, execute_tool=lambda call: {"status": "ok"}, **limits)


def test_unserializable_tool_result_is_a_plain_loop_error():
    # A Kalillac-side defect: neither model output nor protocol.
    with pytest.raises(ToolLoopError) as caught:
        _run([{"output": [_runtime_call()]}], execute_tool=lambda call: object())

    assert type(caught.value) is ToolLoopError
