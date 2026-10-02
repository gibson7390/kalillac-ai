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
    ToolLoopError,
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