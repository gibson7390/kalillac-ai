"""Regression tests for Kalillac V31 native-tool validation."""

from pathlib import Path
import sys

import pytest


CANDIDATE_DIR = Path(__file__).resolve().parents[1]

if str(CANDIDATE_DIR) not in sys.path:
    sys.path.insert(0, str(CANDIDATE_DIR))


from kalillac_routing.tool_contract import (
    ToolValidationError,
    sanitize_search_query,
    validate_tool_call,
)


def test_search_accepts_meaningful_query():
    call = validate_tool_call(
        "search_web",
        {"query": "effective study habits"},
        user_message="search the web for effective study habits",
    )

    assert call.name == "search_web"
    assert call.arguments == {
        "query": "effective study habits",
    }


def test_search_removes_invented_month_and_year():
    call = validate_tool_call(
        "search_web",
        {"query": "AI news today March 2026"},
        user_message="AI news today",
    )

    assert call.arguments == {
        "query": "AI news today",
    }


def test_search_preserves_user_supplied_calendar_date():
    query = sanitize_search_query(
        user_message="AI news from March 2026",
        model_query="AI news March 2026",
    )

    assert query == "AI news March 2026"


def test_search_rejects_targetless_search():
    with pytest.raises(
        ToolValidationError,
        match="meaningful search target",
    ):
        validate_tool_call(
            "search_web",
            {"query": "search the web"},
            user_message="search",
        )


def test_search_rejects_empty_query():
    with pytest.raises(
        ToolValidationError,
        match="query is empty",
    ):
        validate_tool_call(
            "search_web",
            {"query": "   "},
            user_message="search",
        )


def test_search_rejects_non_meaningful_punctuation():
    with pytest.raises(
        ToolValidationError,
        match="no meaningful search target",
    ):
        validate_tool_call(
            "search_web",
            {"query": "???"},
            user_message="???",
        )


def test_search_rejects_query_over_limit():
    with pytest.raises(
        ToolValidationError,
        match="400-character limit",
    ):
        validate_tool_call(
            "search_web",
            {"query": "a" * 401},
            user_message="search for something",
        )


def test_search_rejects_extra_argument():
    with pytest.raises(
        ToolValidationError,
        match="Unexpected tool argument",
    ):
        validate_tool_call(
            "search_web",
            {
                "query": "AI news today",
                "dangerous_extra": "value",
            },
            user_message="AI news today",
        )


def test_runtime_accepts_topic():
    call = validate_tool_call(
        "get_kalillac_runtime_facts",
        {"topic": "current primary model"},
    )

    assert call.name == "get_kalillac_runtime_facts"
    assert call.arguments == {
        "topic": "current primary model",
    }


def test_runtime_rejects_empty_topic():
    with pytest.raises(
        ToolValidationError,
        match="topic is empty",
    ):
        validate_tool_call(
            "get_kalillac_runtime_facts",
            {"topic": "   "},
        )


def test_invalid_json_is_rejected():
    with pytest.raises(
        ToolValidationError,
        match="not valid JSON",
    ):
        validate_tool_call(
            "search_web",
            '{"query":',
            user_message="AI news today",
        )


def test_unknown_tool_is_rejected():
    with pytest.raises(
        ToolValidationError,
        match="Unknown tool requested",
    ):
        validate_tool_call(
            "delete_everything",
            {},
        )