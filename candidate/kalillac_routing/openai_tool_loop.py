"""Kalillac V31 stateless native-tool orchestration.

This module performs no network calls.

Network/model access and tool execution are injected by the caller so this
control flow can be unit-tested without OpenAI, Tavily, or production state.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Any, Callable, Mapping, Sequence

from .tool_contract import (
    ValidatedToolCall,
    validate_tool_call,
)


class ToolLoopError(RuntimeError):
    """Raised when the model/tool loop violates Kalillac's control contract."""


@dataclass(frozen=True)
class ToolLoopResult:
    """Completed native-tool turn."""

    text: str
    model_calls: int
    tool_calls: tuple[ValidatedToolCall, ...]


ModelCaller = Callable[
    [list[dict[str, Any]]],
    Mapping[str, Any],
]

ToolExecutor = Callable[
    [ValidatedToolCall],
    Any,
]


def _response_output(
    response: Mapping[str, Any],
) -> list[dict[str, Any]]:
    output = response.get("output")

    if not isinstance(output, list):
        raise ToolLoopError(
            "Model response is missing an output item list."
        )

    normalized = []

    for item in output:
        if not isinstance(item, Mapping):
            raise ToolLoopError(
                "Model output contains a non-object item."
            )

        normalized.append(dict(item))

    return normalized


def extract_output_text(
    response: Mapping[str, Any],
) -> str:
    """Extract assistant-visible text from Responses API output items."""

    pieces = []

    for item in _response_output(response):
        if item.get("type") != "message":
            continue

        content = item.get("content")

        if not isinstance(content, list):
            continue

        for part in content:
            if not isinstance(part, Mapping):
                continue

            if part.get("type") != "output_text":
                continue

            text = part.get("text")

            if isinstance(text, str) and text:
                pieces.append(text)

    return "\n".join(pieces).strip()


def _serialize_tool_result(result: Any) -> str:
    if isinstance(result, str):
        return result

    try:
        return json.dumps(
            result,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ToolLoopError(
            "Tool result is not JSON serializable."
        ) from exc


def run_tool_loop(
    *,
    user_message: str,
    initial_input: Sequence[Mapping[str, Any]],
    call_model: ModelCaller,
    execute_tool: ToolExecutor,
    max_tool_rounds: int = 3,
    max_tool_calls: int = 4,
) -> ToolLoopResult:
    """Run one stateless model/tool turn.

    The model never executes tools directly.

    Flow:
        input
          -> model
          -> validate requested tools
          -> application executes approved tools
          -> replay prior output + tool results
          -> model
          -> final visible text

    Limits prevent an accidental or adversarial infinite tool loop.
    """

    if max_tool_rounds < 0:
        raise ValueError(
            "max_tool_rounds must be zero or greater."
        )

    if max_tool_calls < 0:
        raise ValueError(
            "max_tool_calls must be zero or greater."
        )

    input_items = [
        deepcopy(dict(item))
        for item in initial_input
    ]

    validated_history: list[ValidatedToolCall] = []
    tool_rounds = 0
    model_calls = 0

    while True:
        model_calls += 1

        response = call_model(
            deepcopy(input_items)
        )

        if not isinstance(response, Mapping):
            raise ToolLoopError(
                "Model caller returned a non-object response."
            )

        output = _response_output(response)

        function_calls = [
            item
            for item in output
            if item.get("type") == "function_call"
        ]

        if not function_calls:
            text = extract_output_text(response)

            if not text:
                raise ToolLoopError(
                    "Model completed without visible output text."
                )

            return ToolLoopResult(
                text=text,
                model_calls=model_calls,
                tool_calls=tuple(validated_history),
            )

        if tool_rounds >= max_tool_rounds:
            raise ToolLoopError(
                "Maximum tool rounds exceeded."
            )

        if (
            len(validated_history)
            + len(function_calls)
            > max_tool_calls
        ):
            raise ToolLoopError(
                "Maximum tool calls exceeded."
            )

        # With store=False, preserve the model's complete output sequence
        # before supplying tool results for continuation.
        input_items.extend(
            deepcopy(output)
        )

        validated_batch = []

        # Validate the WHOLE batch before executing anything. If one call is
        # malformed, no earlier call in the same model batch is partially run.
        for call in function_calls:
            name = call.get("name")
            call_id = call.get("call_id")

            if not isinstance(name, str) or not name:
                raise ToolLoopError(
                    "Function call is missing a valid name."
                )

            if not isinstance(call_id, str) or not call_id:
                raise ToolLoopError(
                    "Function call is missing a valid call_id."
                )

            validated = validate_tool_call(
                name,
                call.get("arguments"),
                user_message=user_message,
            )

            validated_batch.append(
                (call_id, validated)
            )

        tool_rounds += 1

        for call_id, validated in validated_batch:
            result = execute_tool(validated)

            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": _serialize_tool_result(result),
                }
            )

            validated_history.append(validated)