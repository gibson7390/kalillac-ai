"""V31 native-tool schemas and deterministic validation.

This module performs no network calls.

The model may REQUEST a tool. Kalillac code remains responsible for:
- deciding whether that request satisfies the contract;
- sanitizing bounded search input;
- rejecting unknown tools or malformed arguments;
- executing any approved tool elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Mapping


SEARCH_QUERY_MAX_CHARS = 400
RUNTIME_TOPIC_MAX_CHARS = 400


OPENAI_TOOLS = [
    {
        "type": "function",
        "name": "search_web",
        "description": (
            "Search the public web for a meaningful target or information "
            "requiring current or external public verification. Do not use "
            "this to determine Kalillac's own runtime configuration."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "A concise meaningful public-web search query. "
                        "Preserve relative terms such as today, latest, or "
                        "current instead of inventing a calendar date."
                    ),
                }
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "get_kalillac_runtime_facts",
        "description": (
            "Retrieve authoritative server-side facts about Kalillac's own "
            "configured models, providers, search capability, memory, limits, "
            "routing, or runtime architecture."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": (
                        "The Kalillac runtime or configuration fact needed."
                    ),
                }
            },
            "required": ["topic"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]


_ALLOWED_TOOLS = {
    "search_web",
    "get_kalillac_runtime_facts",
}


_VAGUE_SEARCH_QUERIES = {
    "search",
    "web search",
    "search web",
    "search the web",
    "browse",
    "browse web",
    "browse the web",
    "look up",
    "look it up",
}


_MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)


class ToolValidationError(ValueError):
    """Raised when a requested model tool call is not safe to execute."""


@dataclass(frozen=True)
class ValidatedToolCall:
    """A tool request that passed Kalillac's deterministic boundary."""

    name: str
    arguments: dict[str, str]


def _collapse_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _parse_arguments(raw_arguments: Any) -> dict[str, Any]:
    if isinstance(raw_arguments, str):
        try:
            parsed = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            raise ToolValidationError(
                "Tool arguments are not valid JSON."
            ) from exc

    elif isinstance(raw_arguments, Mapping):
        parsed = dict(raw_arguments)

    else:
        raise ToolValidationError(
            "Tool arguments must be a JSON object."
        )

    if not isinstance(parsed, dict):
        raise ToolValidationError(
            "Tool arguments must decode to an object."
        )

    return parsed


def _require_exact_keys(
    arguments: Mapping[str, Any],
    required: set[str],
) -> None:
    actual = set(arguments)

    missing = required - actual
    extra = actual - required

    if missing:
        raise ToolValidationError(
            "Missing required tool argument: "
            + ", ".join(sorted(missing))
        )

    if extra:
        raise ToolValidationError(
            "Unexpected tool argument: "
            + ", ".join(sorted(extra))
        )


def sanitize_search_query(
    user_message: str,
    model_query: str,
) -> str:
    """Conservatively remove model-invented calendar specificity.

    Relative requests such as "today", "latest", and "current" should stay
    relative. If the model adds a month/year that the user never supplied,
    remove that invented calendar detail before an external search executes.

    This is deliberately narrow. It does not rewrite the semantic subject.
    """

    user_text = str(user_message)
    user_low = user_text.lower()

    query = _collapse_whitespace(str(model_query))

    # Remove invented month + year pairs such as "March 2026".
    month_pattern = "|".join(_MONTHS)

    def replace_month_year(match: re.Match[str]) -> str:
        value = match.group(0)

        if value.lower() in user_low:
            return value

        return " "

    query = re.sub(
        rf"\b(?:{month_pattern})\s+20\d{{2}}\b",
        replace_month_year,
        query,
        flags=re.IGNORECASE,
    )

    # Remove any remaining invented four-digit year.
    for year in set(re.findall(r"\b20\d{2}\b", query)):
        if year not in user_text:
            query = re.sub(
                rf"\b{re.escape(year)}\b",
                " ",
                query,
            )

    query = _collapse_whitespace(query)
    query = query.strip(" ,;:-")

    return query


def _validate_search(
    arguments: Mapping[str, Any],
    user_message: str,
) -> ValidatedToolCall:
    _require_exact_keys(arguments, {"query"})

    query = arguments["query"]

    if not isinstance(query, str):
        raise ToolValidationError(
            "search_web query must be a string."
        )

    query = sanitize_search_query(
        user_message=user_message,
        model_query=query,
    )

    if not query:
        raise ToolValidationError(
            "search_web query is empty."
        )

    if len(query) > SEARCH_QUERY_MAX_CHARS:
        raise ToolValidationError(
            "search_web query exceeds the 400-character limit."
        )

    if not re.search(r"[A-Za-z0-9]", query):
        raise ToolValidationError(
            "search_web query has no meaningful search target."
        )

    if query.casefold() in _VAGUE_SEARCH_QUERIES:
        raise ToolValidationError(
            "search_web request has no meaningful search target."
        )

    return ValidatedToolCall(
        name="search_web",
        arguments={"query": query},
    )


def _validate_runtime(
    arguments: Mapping[str, Any],
) -> ValidatedToolCall:
    _require_exact_keys(arguments, {"topic"})

    topic = arguments["topic"]

    if not isinstance(topic, str):
        raise ToolValidationError(
            "Runtime-facts topic must be a string."
        )

    topic = _collapse_whitespace(topic)

    if not topic:
        raise ToolValidationError(
            "Runtime-facts topic is empty."
        )

    if len(topic) > RUNTIME_TOPIC_MAX_CHARS:
        raise ToolValidationError(
            "Runtime-facts topic exceeds the 400-character limit."
        )

    return ValidatedToolCall(
        name="get_kalillac_runtime_facts",
        arguments={"topic": topic},
    )


def validate_tool_call(
    name: str,
    raw_arguments: Any,
    *,
    user_message: str = "",
) -> ValidatedToolCall:
    """Validate one model-requested tool call before execution."""

    if name not in _ALLOWED_TOOLS:
        raise ToolValidationError(
            f"Unknown tool requested: {name!r}"
        )

    arguments = _parse_arguments(raw_arguments)

    if name == "search_web":
        return _validate_search(
            arguments,
            user_message=user_message,
        )

    return _validate_runtime(arguments)