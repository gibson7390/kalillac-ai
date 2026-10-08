"""Deterministic calculator routing and bounded evaluation.

- Bare arithmetic and natural-language requests that reduce to one safe
  expression are answered deterministically, with no model call.
- A request is claimed only when recognized wrappers and arithmetic phrases
  alone reduce it to one expression. Any other remaining word or symbol, a
  failed parse of worded input, an unexpected parser exception, numeric-base
  notation, a comma that is not thousands grouping, or a bare date or
  version-range shape sends it through normal routing; it never receives the
  calculator parser error.
- Malformed letter-free arithmetic stays calculator-owned with the parser
  error.
- Expressions whose evaluation could be unbounded are rejected before the
  expensive operation runs.

Every provider entry point is replaced by a refusing recorder; conftest.py
also blocks all non-loopback network access.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


CANDIDATE_DIR = Path(__file__).resolve().parents[1]

if str(CANDIDATE_DIR) not in sys.path:
    sys.path.insert(0, str(CANDIDATE_DIR))

import app_fastapi_candidate as app


PARSE_ERROR = (
    "I couldn't safely parse that as a calculation. "
    "Please type the arithmetic expression clearly."
)
DIVISION_BY_ZERO = "Division by zero is undefined."
SENTINEL = "Normal routing sentinel reply."
PROMPT_SECONDS = 1.0


# --- fixtures and helpers ------------------------------------------------------------------


@pytest.fixture(autouse=True)
def provider_calls(monkeypatch):
    """Record and refuse every provider entry point the chat path can reach."""
    calls = []

    def refuse(name):
        def _refuse(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"{name} must not be called")

        return _refuse

    monkeypatch.setattr(app, "_invoke_openai", refuse("openai"))
    monkeypatch.setattr(app, "invoke_llm", refuse("invoke_llm"))
    monkeypatch.setattr(app, "run_web_search", refuse("web_search"))
    monkeypatch.setattr(app, "_run_v31_native_tool_chat", refuse("native_tool_chat"))
    monkeypatch.setattr(app.urllib.request, "urlopen", refuse("urlopen"))
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", False)
    monkeypatch.setattr(app, "requires_web_verification", lambda message, history: False)
    monkeypatch.setattr(app, "SESSION_STATE", type(app.SESSION_STATE)())
    return calls


@pytest.fixture
def sentinel_model(monkeypatch):
    """Normal routing's model call, answered locally."""
    calls = []

    def invoke(messages, max_tokens=None):
        calls.append(messages)
        return SimpleNamespace(content=SENTINEL, incomplete=False, incomplete_reason=None)

    monkeypatch.setattr(app, "invoke_llm", invoke)
    return calls


@pytest.fixture
def pow_calls(monkeypatch):
    """Replace the evaluator's power operation with a refusing recorder."""
    calls = []

    def refuse(base, exponent):
        calls.append((base, exponent))
        raise AssertionError("power must not be evaluated")

    monkeypatch.setitem(app.SAFE_MATH_OPS, ast.Pow, refuse)
    return calls


@pytest.fixture
def side_effects(monkeypatch, tmp_path):
    """Record process launches, and run in an empty directory to detect writes."""
    calls = []

    def refuse(name):
        def _refuse(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"{name} must not be called")

        return _refuse

    monkeypatch.setattr(os, "system", refuse("os.system"))
    monkeypatch.setattr(subprocess, "Popen", refuse("subprocess.Popen"))
    monkeypatch.chdir(tmp_path)
    return calls, tmp_path


@pytest.fixture
def http_isolated(monkeypatch):
    """The real /api/chat route with no budgets, accounts or admission state."""
    monkeypatch.setattr(app, "_request_limits", None)
    monkeypatch.setattr(app, "_chat_semaphore", None)
    monkeypatch.setattr(app, "_chat_waiting", 0)
    monkeypatch.setattr(app, "_session_locks", {})
    monkeypatch.setattr(app, "_usage_meter", None)
    monkeypatch.setattr(app, "_chats_admitted", 0)


def chat(message):
    return app.chat(message, [], session_id="calculator-routing-test")


def post_chat(message):
    with TestClient(app.api) as client:
        return client.post("/api/chat", json={"message": message, "history": []})


def timed(function, *args):
    started = time.perf_counter()
    value = function(*args)
    elapsed = time.perf_counter() - started
    assert elapsed < PROMPT_SECONDS, f"{args!r} took {elapsed:.3f}s"
    return value


def balanced_sum(depth):
    if depth == 0:
        return "1"

    return f"({balanced_sum(depth - 1)}+{balanced_sum(depth - 1)})"


# --- deterministic success ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("2+2", "4"),
        ("what is 2 plus 2", "4"),
        ("What is 2 + 2? Reply with only the number.", "4"),
        ("What is 3 * 7? Just the number.", "21"),
        ("what is 60 + 70", "130"),
        ("what is 5x8", "40"),
        ("what is 25 * 8", "200"),
        ("what is 10 / 0", DIVISION_BY_ZERO),
        ("Solve 144 divided by 12", "12"),
        ("What is 2 to the power of 10?", "1,024"),
        ("How much is 15% of 240?", "36"),
        ("2 + 2 = ?", "4"),
        ("2**10", "1,024"),
        ("what is 9 - 4, answer with just the number please", "5"),
        ("what is one plus one", "2"),
        ("what is 3 squared", "9"),
        ("what is 2 raised to the power of 3", "8"),
        ("what is 2 + 2 equal to?", "4"),
        ("0**-1", DIVISION_BY_ZERO),
    ],
)
def test_calculator_answers_deterministically(provider_calls, message, expected):
    assert app.is_calculator_request(message)
    assert app.classify_request(message, []) == "calculator"
    assert chat(message) == expected
    assert provider_calls == []


# --- false calculator claims ----------------------------------------------------------------


FALSE_CLAIMS = [
    "spell whatever word 2 plus two +",
    "spell the word that 2+2+",
    "what is python 3.12 - new features",
    "what's new in 3.12 - 3.13",
    "sometimes 3 times a day is enough",
    "what is the 2024-2025 season",
    "how much is 2 + 2 dollars in euros",
    "what is 2 + 2 and who is the president",
    "what is 2 plus",
    "what is 1e9 + 1",
    "write python code to calculate 60 + 70",
    "write 2-3 sentences about Python",
    # Words the classifier must not drop to reach an expression.
    "what is half of 10 + 2",
    "what is 50 percent + 10",
    "what is 2 + 2 and -3",
    "what is 2 + 2 and 3 + 3",
    "what is 2 + 2 of -3",
    "convert 2 + 2 dollars to euros",
    "$2 + $2",
    "What is 2 + 2? Reply with only the number and explain why.",
    "Tell me why 2 + 2 equals 4; reply with only the number.",
]

# Whole expressions shaped like a date or version range, spaced or not:
# normal routing. Shape checks, not calendar validation (2026-13-40).
GUARDED_SHAPES = [
    "2024-2025",
    "2024 - 2025",
    "2024\t-\t2025",
    "1/2/2026",
    "1 / 2 / 2026",
    "12/31/26",
    "12 / 31 / 26",
    "2026-1-2",
    "2026-01-02",
    "2026 - 1 - 2",
    "1-2-2026",
    "01-02-2026",
    "1 - 2 - 2026",
    "3.12-3.13",
    "3.12 - 3.13",
    "2026-13-40",
    "what is 2024-2025",
    # Four digits minus four digits, whatever the values.
    "1850-1900",
    "1850 - 1900",
    "2099-2100",
    "3000-3001",
    "1234-5678",
    "5000-1000",
    "5000 - 1000",
]

# Every other shape stays ordinary arithmetic.
UNGUARDED_ARITHMETIC = [
    ("10-5", "5"),
    ("10 - 5", "5"),
    ("2025-1", "2,024"),
    ("2025 - 1", "2,024"),
    ("5000-100", "4,900"),
    ("5000 - 100", "4,900"),
    ("999-1000", "-1"),
    ("10000-2000", "8,000"),
    ("100-5000", "-4,900"),
    ("3.12-1", "2.12"),
    ("3.12 - 1", "2.12"),
    ("1-2-3", "-4"),
    ("100-20-5", "75"),
    ("10/2/5", "1"),
    ("1.5-2.5", "-1"),
]

# Numeric-base notation is declined, never read as "0 times ...".
BASE_PREFIXED = ["0x10", "0XFF", "what is 0x10", "what is 0XFF", "0x10 + 1", "0b1010", "0o10"]

# Commas that are not thousands grouping are declined, never removed.
INVALID_COMMA_GROUPING = [
    "1,5 + 1",
    "1,2,3 + 1",
    "12,34 + 1",
    "1000,000 + 1",
    "1,0000 + 1",
    "what is 1,5 + 1",
    "1,000 + 1,5",
    "1.000,5 + 1",
]

# Multiplication and thousands grouping that stay calculator-owned.
SUPPORTED_NOTATION = [
    ("5x8", "40"),
    ("5X8", "40"),
    ("what is 5x8", "40"),
    ("1920x1080", "2,073,600"),
    ("10x5", "50"),
    ("1,000 + 1", "1,001"),
    ("12,345 + 5", "12,350"),
    ("1,000,000 - 1", "999,999"),
    ("1,000.5 + 1", "1,001.5"),
    ("what is 1,000 + 1", "1,001"),
]

DECLINED = FALSE_CLAIMS + GUARDED_SHAPES + BASE_PREFIXED + INVALID_COMMA_GROUPING


@pytest.mark.parametrize(("message", "expected"), SUPPORTED_NOTATION)
def test_multiplication_and_thousands_grouping_stay_supported(provider_calls, message, expected):
    assert app.classify_request(message, []) == "calculator"
    assert chat(message) == expected
    assert provider_calls == []


@pytest.mark.parametrize("message", DECLINED)
def test_message_is_not_claimed_by_calculator(message):
    assert not app.is_calculator_request(message)
    assert app.classify_request(message, []) != "calculator"


@pytest.mark.parametrize(("message", "expected"), UNGUARDED_ARITHMETIC)
def test_guards_leave_ordinary_subtraction_and_division_alone(provider_calls, message, expected):
    assert app.classify_request(message, []) == "calculator"
    assert chat(message) == expected
    assert provider_calls == []


UNCLAIMED_REACHING_MODEL = [
    "spell whatever word 2 plus two +",
    "spell the word that 2+2+",
    "what is python 3.12 - new features",
    "sometimes 3 times a day is enough",
    "what is 2 plus",
    "what is half of 10 + 2",
    "what is 50 percent + 10",
    "what is 2 + 2 and -3",
    "what is 2 + 2 and 3 + 3",
    "how much is 2 + 2 dollars in euros",
    "1e309",
    *GUARDED_SHAPES,
    *BASE_PREFIXED,
    *INVALID_COMMA_GROUPING,
]


@pytest.mark.parametrize("message", UNCLAIMED_REACHING_MODEL)
def test_unclaimed_messages_reach_normal_routing(provider_calls, sentinel_model, message):
    assert app.classify_request(message, []) != "calculator"

    reply = chat(message)

    assert SENTINEL in reply
    assert PARSE_ERROR not in reply
    assert reply not in {"0", "1", "16", "124"}
    assert len(sentinel_model) == 1
    assert provider_calls == []


def test_mixed_arithmetic_and_search_question_is_not_calculator(provider_calls):
    # Routed to web search (not exercised here); the calculator must not
    # answer only its arithmetic half.
    message = "what is 2 + 2 and who is the president"

    assert not app.is_calculator_request(message)
    assert app.classify_request(message, []) != "calculator"
    assert app.calculate_expression(message) is None
    assert provider_calls == []


# --- answer-format suffix ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("What is 2 + 2? Reply with only the number.", "What is 2 + 2?"),
        ("what is 2 + 2 respond with just the result", "what is 2 + 2"),
        ("what is 2 + 2, answer with only the answer please!", "what is 2 + 2,"),
        ("2 + 2 ONLY THE NUMBER", "2 + 2"),
        ("Reply with only the number. What is 2 + 2?", "Reply with only the number. What is 2 + 2?"),
        ("what is 2 + 2 commonly the number", "what is 2 + 2 commonly the number"),
        ("2 + 2 just the number just the number", "2 + 2 just the number"),
        ("What is 2 + 2? Please reply with only the number.", "What is 2 + 2?"),
        ("What is 2 + 2? Reply with only the number, please.", "What is 2 + 2?"),
        ("What is 2 + 2? Reply with only the number?", "What is 2 + 2?"),
        ("What is 2 + 2? PLEASE JUST THE RESULT?", "What is 2 + 2?"),
        (
            "What is 2 + 2? Reply with only the number and explain why.",
            "What is 2 + 2? Reply with only the number and explain why.",
        ),
        ("2 + 2 the onlyjust the number", "2 + 2 the onlyjust the number"),
    ],
)
def test_answer_format_suffix_is_removed_once_and_only_at_the_end(message, expected):
    assert app.strip_calculator_answer_format(message) == expected


@pytest.mark.parametrize(
    "message",
    [
        "What is 2 + 2? Please reply with only the number.",
        "What is 2 + 2? Reply with only the number, please.",
        "What is 2 + 2? Reply with only the answer, please.",
        "What is 2 + 2? Reply with only the result.",
        "What is 2 + 2? Reply with only the number?",
        "What is 2 + 2? Please reply with only the number?",
        "What is 2 + 2? Reply with only the answer, please?",
        "What is 2 + 2? Just the result?",
    ],
)
def test_approved_answer_format_suffixes_keep_the_calculator_answer(provider_calls, message):
    assert app.classify_request(message, []) == "calculator"
    assert chat(message) == "4"
    assert provider_calls == []


@pytest.mark.parametrize(
    "message",
    [
        "just the number",
        "Reply with only the number. What is 2 + 2?",
        "what is 2 + 2 just the number just the number",
        "Write a poem about 2 + 2. Reply with only the number.",
    ],
)
def test_answer_format_suffix_does_not_create_a_calculator_claim(message):
    assert not app.is_calculator_request(message)


# --- malformed standalone arithmetic ----------------------------------------------------------


@pytest.mark.parametrize("message", ["2+2+", "2 +", "(2+2", "2 // 3"])
def test_malformed_standalone_arithmetic_keeps_the_parser_error(provider_calls, message):
    assert app.is_calculator_request(message)
    assert app.classify_request(message, []) == "calculator"
    assert chat(message) == PARSE_ERROR
    assert provider_calls == []


# --- structural safety ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "__import__('os').system('ls')",
        "x",
        "abs(2)",
        "(2).real",
        "x[0]",
        "True + 1",
        "2 // 3",
        "2 % 3",
        "[1, 2]",
        "(1, 2)",
        "{1: 2}",
        "{1, 2}",
        "[n for n in (1,)]",
        "2 if 1 else 3",
        "lambda: 1",
        "1 < 2",
        "'a' + 'b'",
        "2j",
        "not 1",
        "~1",
        "1 << 2",
        "1 & 1",
        "",
        "1" * (app.CALCULATOR_MAX_EXPRESSION_CHARS + 1),
    ],
)
def test_parse_only_check_rejects_unsupported_syntax(text):
    assert app.parse_safe_math_expression(text) is None
    assert not app.is_safe_math_expression(text)


def test_parse_only_check_evaluates_nothing(monkeypatch):
    calls = []

    for node_type in list(app.SAFE_MATH_OPS):
        monkeypatch.setitem(
            app.SAFE_MATH_OPS,
            node_type,
            lambda *operands, _type=node_type: calls.append(_type),
        )

    assert app.is_safe_math_expression("2 ** 10 + 3 * (4 - 1) / -2")
    assert app.is_calculator_request("what is 2 to the power of 10 plus 3")
    assert calls == []


def test_code_execution_text_is_rejected_without_side_effects(provider_calls, side_effects):
    calls, directory = side_effects
    message = "__import__('os').system('ls')"

    assert not app.is_calculator_request(message)
    assert app.classify_request(message, []) != "calculator"
    assert app.calculate_expression(message) is None
    assert calls == []
    assert list(directory.iterdir()) == []
    assert provider_calls == []


# --- bounded evaluation -----------------------------------------------------------------------


@pytest.mark.parametrize("message", ["9**9**9", "(2**3)**2", "2**(3**2)", "what is 9**9**9"])
def test_nested_exponentiation_is_rejected_before_any_power(pow_calls, message):
    assert timed(app.calculate_expression, message) is None
    assert pow_calls == []


@pytest.mark.parametrize("message", ["2**999999", "10**101", "0.5**-400", "what is 2 to the power of 999999"])
def test_oversized_power_is_rejected_before_it_is_computed(pow_calls, message):
    assert timed(app.calculate_expression, message) is None
    assert pow_calls == []


@pytest.mark.parametrize(
    "message",
    [
        "1" + "0" * (app.CALCULATOR_MAX_RESULT_DIGITS + 1),
        "9" * 200 + ".5 + 1",
        "9" * (app.CALCULATOR_MAX_EXPRESSION_CHARS + 1),
    ],
)
def test_oversized_literals_are_rejected(message):
    assert timed(app.calculate_expression, message) is None


def test_bare_number_without_an_operator_is_not_calculator_owned():
    message = "1" + "0" * 200

    assert not app.is_calculator_request(message)
    assert app.classify_request(message, []) != "calculator"


def test_intermediate_results_are_bounded():
    # Each operand is within bounds; the product is not.
    assert timed(app.calculate_expression, "10**60 * 10**60 - 10**120") is None
    assert timed(app.calculate_expression, "10**60 * 10**40") == 10 ** 100


def test_non_real_power_result_is_rejected():
    assert timed(app.calculate_expression, "(-8) ** 0.5") is None


def test_expression_depth_is_bounded():
    # A 31-term chain is exactly CALCULATOR_MAX_AST_DEPTH levels deep.
    within = "+".join(["1"] * 31)
    beyond = "+".join(["1"] * 32)

    assert timed(app.calculate_expression, within) == 31
    assert timed(app.calculate_expression, beyond) is None
    assert timed(app.calculate_expression, "-" * 40 + "1") is None


def test_expression_node_count_is_bounded():
    # Six balanced levels: 64 leaves, 191 nodes, depth 8, 253 characters.
    expression = balanced_sum(6)

    assert len(expression) <= app.CALCULATOR_MAX_EXPRESSION_CHARS
    assert timed(app.calculate_expression, expression) is None
    assert timed(app.calculate_expression, balanced_sum(5)) == 32


def test_largest_supported_power_is_still_calculated():
    assert app.calculate_expression("10**100") == 10 ** 100
    assert app.calculate_expression("2**332") == 2 ** 332
    assert app.calculate_expression("2**10") == 1024


# --- HTTP route ------------------------------------------------------------------------------


def assert_controlled(response):
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "Traceback" not in response.text


@pytest.mark.parametrize(
    "message",
    ["9**9**9", "2**999999", "what is 9**9**9", "1" + "0" * 200 + " + 1", "+".join(["1"] * 40)],
)
def test_rejected_expressions_return_the_parser_error_over_http(
    provider_calls, pow_calls, http_isolated, message
):
    started = time.perf_counter()
    response = post_chat(message)

    assert time.perf_counter() - started < 5.0
    assert_controlled(response)
    assert response.json()["reply"] == PARSE_ERROR
    assert pow_calls == []
    assert provider_calls == []


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("0**-1", DIVISION_BY_ZERO),
        ("(-2)**0.5", PARSE_ERROR),
        ("What is 2 + 2? Please reply with only the number.", "4"),
        ("What is 2 + 2? Reply with only the number?", "4"),
    ],
)
def test_calculator_edges_over_http(provider_calls, http_isolated, message, expected):
    response = post_chat(message)

    assert_controlled(response)
    assert response.json()["reply"] == expected
    assert provider_calls == []


@pytest.mark.parametrize(
    "message",
    [
        "1e309",
        "what is half of 10 + 2",
        "what is 50 percent + 10",
        "what is 2 + 2 and -3",
        "what is 2 + 2 and 3 + 3",
        "how much is 2 + 2 dollars in euros",
        *GUARDED_SHAPES,
        *BASE_PREFIXED,
        *INVALID_COMMA_GROUPING,
    ],
)
def test_unclaimed_messages_reach_normal_routing_over_http(
    provider_calls, sentinel_model, http_isolated, message
):
    response = post_chat(message)

    assert_controlled(response)
    assert SENTINEL in response.json()["reply"]
    assert PARSE_ERROR not in response.text
    assert len(sentinel_model) == 1
    assert provider_calls == []


@pytest.mark.parametrize(
    ("message", "previous_wrong_result"),
    [
        ("2024 - 2025", "-1"),
        ("2026-1-2", "2,023"),
        ("1850-1900", "-50"),
        ("2099-2100", "-1"),
        ("3000-3001", "-1"),
    ],
)
def test_date_and_year_range_shapes_are_no_longer_calculated(
    provider_calls, sentinel_model, message, previous_wrong_result
):
    # The text still evaluates as arithmetic; only the shape guard declines it.
    assert app.calculate_expression(message) is not None
    assert not app.is_calculator_request(message)

    reply = chat(message)

    assert reply != previous_wrong_result
    assert SENTINEL in reply
    assert provider_calls == []


@pytest.mark.parametrize(
    ("message", "previous_wrong_result"),
    [("0x10", "0"), ("what is 0x10", "0"), ("0x10 + 1", "1"), ("1,5 + 1", "16"), ("1,2,3 + 1", "124")],
)
def test_base_notation_and_comma_misreadings_are_no_longer_calculated(
    provider_calls, sentinel_model, message, previous_wrong_result
):
    assert app.calculate_expression(message) is None
    assert not app.is_calculator_request(message)

    reply = chat(message)

    assert reply != previous_wrong_result
    assert SENTINEL in reply
    assert provider_calls == []


# --- classifier exception boundary -----------------------------------------------------------


class InjectedParserFailure(Exception):
    """An unexpected failure raised from the parse/validation boundary."""


@pytest.fixture
def failing_parser(monkeypatch):
    calls = []

    def fail(text):
        calls.append(text)
        raise InjectedParserFailure("unexpected parser failure")

    monkeypatch.setattr(app, "parse_safe_math_expression", fail)
    return calls


def test_classifier_fails_closed_on_unexpected_parser_exception(provider_calls, failing_parser):
    assert app.is_calculator_request("what is 2 + 2") is False
    assert app.classify_request("what is 2 + 2", []) != "calculator"
    assert failing_parser and set(failing_parser) == {"2 + 2"}
    assert provider_calls == []


def test_unexpected_parser_exception_never_becomes_http_500(
    provider_calls, sentinel_model, failing_parser, http_isolated
):
    worded = post_chat("what is 2 + 2")
    bare = post_chat("2 + 2")

    # A worded request is not claimed and continues to normal routing.
    assert_controlled(worded)
    assert SENTINEL in worded.json()["reply"]
    # Letter-free input stays calculator-owned; calculation catches the
    # failure and returns the controlled parser error.
    assert_controlled(bare)
    assert bare.json()["reply"] == PARSE_ERROR
    assert len(sentinel_model) == 1
    assert provider_calls == []


# --- production native-tool flag -------------------------------------------------------------


@pytest.fixture
def native_routing(monkeypatch):
    # Restored by monkeypatch after the test.
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", True)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("What is 2 + 2? Reply with only the number.", "4"),
        ("2**10", "1,024"),
    ],
)
def test_native_flag_keeps_calculator_answers_deterministic(
    provider_calls, native_routing, message, expected
):
    assert app.V31_NATIVE_TOOL_ROUTING is True
    assert "calculator" not in app.V31_NATIVE_TOOL_ROUTES
    assert app.classify_request(message, []) == "calculator"
    assert chat(message) == expected
    assert provider_calls == []


def test_native_flag_rejects_nested_power_before_the_power(provider_calls, pow_calls, native_routing):
    assert chat("9**9**9") == PARSE_ERROR
    assert pow_calls == []
    assert provider_calls == []


@pytest.mark.parametrize("message", DECLINED)
def test_native_flag_does_not_change_false_claim_classification(native_routing, message):
    assert not app.is_calculator_request(message)
    assert app.classify_request(message, []) != "calculator"


@pytest.mark.parametrize(
    "message",
    ["1850-1900", "0x10", "1,5 + 1", "what is half of 10 + 2"],
)
def test_native_flag_sends_declined_messages_through_normal_routing(
    provider_calls, sentinel_model, monkeypatch, message
):
    native_calls = []

    def native_sentinel(message, history, state):
        native_calls.append(message)
        return SENTINEL

    with monkeypatch.context() as patch:
        patch.setattr(app, "V31_NATIVE_TOOL_ROUTING", True)
        patch.setattr(app, "_run_v31_native_tool_chat", native_sentinel)

        assert not app.is_calculator_request(message)
        assert app.classify_request(message, []) != "calculator"

        reply = chat(message)

    # Whichever normal path the route uses (native tools or the legacy model
    # call), exactly one sentinel answered and no real provider was reached.
    assert SENTINEL in reply
    assert PARSE_ERROR not in reply
    assert len(native_calls) + len(sentinel_model) == 1
    assert provider_calls == []
    assert app.V31_NATIVE_TOOL_ROUTING is False
