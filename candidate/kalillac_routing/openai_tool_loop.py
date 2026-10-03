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


# Responses API incomplete_details.reason for an output-token cutoff.
OUTPUT_TOKEN_LIMIT_REASON = "max_output_tokens"

# The only continuation instruction Kalillac sends after an output-token
# cutoff. The partial answer is replayed as assistant output before it.
CONTINUATION_INSTRUCTION = (
    "Your previous response was cut off because it reached the output "
    "length limit. Continue exactly where it stopped. Do not repeat any "
    "earlier text, do not restart, and do not add a preamble."
)


def response_incomplete_reason(
    response: Mapping[str, Any],
) -> str | None:
    """Return why a Responses API result is incomplete, or None.

    A response whose status is "incomplete" is never a normal completed
    answer, even when it carries visible text.
    """

    if response.get("status") != "incomplete":
        return None

    details = response.get("incomplete_details")

    if isinstance(details, Mapping):
        reason = details.get("reason")

        if isinstance(reason, str) and reason:
            return reason

    return "unknown"


@dataclass(frozen=True)
class ToolLoopResult:
    """Native-tool turn result.

    incomplete is True when the visible text is still cut off after the
    bounded continuation attempt; incomplete_reason carries the provider's
    reason so callers can surface it instead of presenting broken output
    as a finished answer.
    """

    text: str
    model_calls: int
    tool_calls: tuple[ValidatedToolCall, ...]
    incomplete: bool = False
    incomplete_reason: str | None = None


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

    return _raw_output_text(response).strip()


def _raw_output_text(
    response: Mapping[str, Any],
) -> str:
    """Visible text without stripping, so a continuation can be joined
    exactly at the point where the previous output stopped."""

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

    return "\n".join(pieces)


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
    max_continuations: int = 1,
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

    A final answer cut off by the output-token limit gets at most
    max_continuations continuation calls. If it is still incomplete, the
    result is returned with incomplete=True rather than as a finished answer.
    """

    if max_tool_rounds < 0:
        raise ValueError(
            "max_tool_rounds must be zero or greater."
        )

    if max_tool_calls < 0:
        raise ValueError(
            "max_tool_calls must be zero or greater."
        )

    if max_continuations < 0:
        raise ValueError(
            "max_continuations must be zero or greater."
        )

    input_items = [
        deepcopy(dict(item))
        for item in initial_input
    ]

    validated_history: list[ValidatedToolCall] = []
    tool_rounds = 0
    model_calls = 0
    continuations = 0
    partial_text = ""

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
        incomplete_reason = response_incomplete_reason(response)

        function_calls = [
            item
            for item in output
            if item.get("type") == "function_call"
        ]

        if function_calls and incomplete_reason is not None:
            # A cut-off response may carry truncated tool arguments.
            # Never execute tools requested by an incomplete response.
            raise ToolLoopError(
                "Model response was incomplete during tool selection "
                f"({incomplete_reason})."
            )

        if not function_calls:
            partial_text += _raw_output_text(response)

            if (
                incomplete_reason == OUTPUT_TOKEN_LIMIT_REASON
                and continuations < max_continuations
            ):
                continuations += 1

                # With store=False, replay the cut-off output before
                # asking for the remainder, as with tool continuation.
                input_items.extend(
                    deepcopy(output)
                )
                input_items.append(
                    {
                        "role": "user",
                        "content": CONTINUATION_INSTRUCTION,
                    }
                )

                continue

            text = partial_text.strip()

            if not text:
                raise ToolLoopError(
                    "Model completed without visible output text."
                )

            return ToolLoopResult(
                text=text,
                model_calls=model_calls,
                tool_calls=tuple(validated_history),
                incomplete=incomplete_reason is not None,
                incomplete_reason=incomplete_reason,
            )

        if partial_text:
            raise ToolLoopError(
                "Model requested a tool while continuing a cut-off answer."
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