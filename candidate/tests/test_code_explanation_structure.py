"""Code plus explanation: the response structure is chosen at the instruction
level, before anything reaches the frontend.

A request for code that also asks for an explanation of it must not be
collapsed into the code-only contract ("one fenced block and nothing else"),
which left code comments as the only place for the explanation. Explicit
intent decides the structure:

- code plus an explanation: the implementation in a fence, then the
  explanation as prose after it;
- an explicit request for explanatory comments: the explanation stays in the
  code as comments;
- code-only wording: one fenced block and nothing else (unchanged);
- explanation only: ordinary prose, no code contract (unchanged).

Intentionally buggy code keeps its requested defect. Continuation, resend and
revision contracts are unchanged. The model is scripted by the existing
offline pipeline fixture (legacy and native configurations); no provider or
search call can be made.
"""

from __future__ import annotations

import pytest

import app_fastapi_candidate as app
from test_self_knowledge_voice import chat, completed, pipeline, prompt  # noqa: F401  (fixture)


WITH_EXPLANATION = "OUTPUT FOR CODE AND AN EXPLANATION:"
COMMENTED = "OUTPUT FOR EXPLANATORY COMMENTS:"
EXPLAIN_AND_EDIT = "OUTPUT FOR AN EXPLANATION AND A CHANGE:"
SINGLE_FENCE = "Return exactly one fenced code block and nothing else."
RESEND = "RESEND REQUEST"
PRECEDENCE = "EXPLANATORY-ONLY OUTPUT PRECEDENCE"

BUGGY_REQUEST = "Write a Python function with a bug, then explain the bug."
# Equivalent intent, different wording: the structure must not hinge on one sentence.
CODE_AND_EXPLANATION = [
    BUGGY_REQUEST,
    "Write a Python code that encrypts text with a bug, then explain the bug.",
    "Write a Python function that reverses a string and explain how it works.",
    "Build me a Python example and walk me through it.",
    "Write a JavaScript function that debounces calls, then explain why it works.",
    "Create a Python function with an off-by-one error and describe the error afterward.",
    "Write a SQL script that finds duplicate emails and give an explanation of it.",
    "Write a Bash script that backs up a folder and tell me how it works.",
]
EXPLICIT_COMMENTS = [
    "Write the same code with inline comments explaining the bug.",
    "Write a Python function with a bug and comment the code to explain the bug.",
    "Write a Python function with a bug, with comments explaining the bug.",
    "Write an annotated Python example of a race condition and explain it inline.",
]
CODE_ONLY = [
    "Write the Python function. Code only.",
    "Write a Python function that sorts numbers. Return only the code.",
    "Write a Python function with a bug and explain nothing, just the code.",
    "Write a Python function with a bug, no explanation.",
    "Write a Python function that sorts numbers without any explanation.",
]
EXPLANATION_ONLY = [
    "Explain how Python list comprehensions work.",
    "Explain why an off-by-one error happens in loops.",
]

BUGGY_CODE = "```python\ndef average(values):\n    return sum(values) / (len(values) - 1)\n```"
PROSE_AFTER = ("The bug is the divisor: it subtracts one from the length, so every average is too large, "
               "and a single value divides by zero.")
CODE_THEN_PROSE = BUGGY_CODE + "\n\n" + PROSE_AFTER
DELIVERED_CODE = [{"role": "user", "content": "Write a Python function that averages numbers with a bug."},
                  {"role": "assistant", "content": BUGGY_CODE}]


def text(message, history=None, route=None):
    history = history or []
    route = route or app.classify_request(message, history)
    return " ".join("\n".join(m.content for m in app.build_messages(message, history, route, [])).split())


# --- code plus explanation -----------------------------------------------------------------------


@pytest.mark.parametrize("message", CODE_AND_EXPLANATION)
def test_code_plus_explanation_puts_the_prose_after_the_fence(message):
    assert app.classify_request(message, []) == "code"
    sent = text(message)
    assert WITH_EXPLANATION in sent
    assert "Return the implementation in one fenced code block with the correct language." in sent
    assert "After the closing fence, give the requested explanation in ordinary prose." in sent
    # Not collapsed into the code-only contract, and not asked to explain inside the code.
    assert SINGLE_FENCE not in sent and "Do not write any sentence after the code." not in sent
    assert COMMENTED not in sent


def test_the_explanation_may_not_become_a_commented_walkthrough():
    """The contract itself forbids moving the requested explanation into the
    code in any wording, while ordinary code comments stay allowed."""
    sent = text(BUGGY_REQUEST)
    assert ("Keep that explanation outside the code block. Do not put the explanation, walkthrough, "
            "or description of a bug or flaw inside the code as comments; use comments only where "
            "the code itself would ordinarily need them.") in sent
    # The requested defect is kept, and described in the prose.
    assert ("If the user asked for intentionally buggy or flawed code, keep the requested flaw in the "
            "code and do not silently fix it. Describe it in the prose after the fence.") in sent


def test_the_new_contract_still_defers_to_the_explanatory_only_precedence():
    sent = text(BUGGY_REQUEST)
    precedence = sent.index(PRECEDENCE)
    contract = sent.index(WITH_EXPLANATION)
    assert precedence < contract
    assert sent.index("This contract governs supplied code; a limitation, recovery, or capability "
                      "explanation follows the EXPLANATORY-ONLY OUTPUT PRECEDENCE above.", contract) > contract


def test_code_then_prose_reaches_the_provider_and_returns_unchanged(pipeline):
    pipeline["text"] = CODE_THEN_PROSE
    reply = chat(pipeline, BUGGY_REQUEST)
    assert reply == CODE_THEN_PROSE                       # no backend re-fencing or comment moving
    assert reply.count("```python") == 1 and reply.rstrip().endswith(PROSE_AFTER)
    sent = " ".join(prompt(pipeline).split())
    assert WITH_EXPLANATION in sent and SINGLE_FENCE not in sent


# --- explicit inline comments ----------------------------------------------------------------------


@pytest.mark.parametrize("message", EXPLICIT_COMMENTS)
def test_explicit_explanatory_comments_stay_in_the_code(message):
    assert app.requests_commented_code(message)
    assert not app.requests_code_with_explanation(message)
    sent = text(message)
    assert COMMENTED in sent
    assert "Put the requested explanation in comments inside the code." in sent
    assert WITH_EXPLANATION not in sent


def test_explicit_comments_on_delivered_code_are_not_forced_out_of_the_fence():
    message = "Add inline comments that explain the bug."
    assert app.classify_request(message, DELIVERED_CODE) == "revision"
    sent = text(message, DELIVERED_CODE)
    assert COMMENTED in sent and EXPLAIN_AND_EDIT not in sent
    assert "def average(values)" in sent                  # the delivered code is still revised


def test_commented_code_without_an_explanation_request_keeps_its_contract():
    sent = text("Create commented example code.")
    assert SINGLE_FENCE in sent and COMMENTED not in sent and WITH_EXPLANATION not in sent


# --- code only and explanation only ----------------------------------------------------------------


@pytest.mark.parametrize("message", CODE_ONLY)
def test_code_only_requests_keep_the_single_fence_contract(message):
    assert app.requests_code_only_output(message)
    assert app.explicit_code_explanation_rule(message) is None
    sent = text(message, route="code")
    assert SINGLE_FENCE in sent
    assert WITH_EXPLANATION not in sent and COMMENTED not in sent


def test_plain_code_requests_keep_the_existing_contract():
    sent = text("Write Python code to parse JSON.")
    assert SINGLE_FENCE in sent and WITH_EXPLANATION not in sent and COMMENTED not in sent


@pytest.mark.parametrize("message", EXPLANATION_ONLY)
def test_explanation_only_requests_stay_prose(message):
    route = app.classify_request(message, [])
    assert route != "code"
    sent = text(message, route=route)
    for contract in (WITH_EXPLANATION, COMMENTED, SINGLE_FENCE):
        assert contract not in sent


# --- continuation, resend and revision -------------------------------------------------------------


def test_a_code_continuation_keeps_its_exact_contract_after_an_explained_request():
    history = [{"role": "user", "content": BUGGY_REQUEST},
               {"role": "assistant", "content": app.mark_incomplete_reply("```python\ndef average(values):\n")}]
    assert app.classify_request("continue", history) == "code_continuation"
    sent = text("continue", history, "code_continuation")
    assert SINGLE_FENCE in sent and "The fenced block contains only the missing remainder." in sent
    assert WITH_EXPLANATION not in sent and COMMENTED not in sent


def test_a_plain_resend_reproduces_the_code_only():
    message = "Send the code again."
    assert app.classify_request(message, DELIVERED_CODE) == "revision"
    sent = text(message, DELIVERED_CODE)
    assert RESEND in sent and SINGLE_FENCE in sent and WITH_EXPLANATION not in sent


def test_a_resend_with_an_explanation_puts_the_prose_after_the_fence():
    message = "Show the code and explain why it fails."
    assert app.classify_request(message, DELIVERED_CODE) == "revision"
    sent = text(message, DELIVERED_CODE)
    assert RESEND in sent and WITH_EXPLANATION in sent and SINGLE_FENCE not in sent
    assert "def average(values)" in sent


def test_an_explained_modification_keeps_its_existing_contract():
    message = "Explain the function and then add error handling to it."
    assert app.is_code_modification_followup(message, DELIVERED_CODE)
    sent = text(message, DELIVERED_CODE, "revision")
    assert EXPLAIN_AND_EDIT in sent and WITH_EXPLANATION not in sent


def test_a_resent_code_and_prose_reply_passes_through_unchanged(pipeline):
    pipeline["text"] = CODE_THEN_PROSE
    reply = chat(pipeline, "Show the code and explain why it fails.", DELIVERED_CODE)
    assert reply == CODE_THEN_PROSE
    sent = " ".join(prompt(pipeline).split())
    assert RESEND in sent and WITH_EXPLANATION in sent


# --- false positives and negations ---------------------------------------------------------------


@pytest.mark.parametrize("message", [
    "Write a function that tells me what time it is.",      # describes the program, not the answer
    "Write code that explains a JSON file.",
    "Write a function to describe the data in a CSV file.",
    "Write a function that tells me how many days are left.",
])
def test_explanation_words_describing_the_program_are_not_explanation_requests(message):
    assert app.explicit_code_explanation_rule(message) is None
    assert not app.requests_code_with_explanation(message)
    sent = text(message, route="code")                        # even forced onto the code route
    assert SINGLE_FENCE in sent and WITH_EXPLANATION not in sent and COMMENTED not in sent


@pytest.mark.parametrize("message", [
    "Write code without comments, then explain it afterward.",
    "Write code and explain it, but do not add comments.",
    "Write a Python function that sorts a list. I want you to explain it.",
    "Write a Python script and add an explanation.",
])
def test_code_then_prose_even_when_comments_are_declined(message):
    assert not app.requests_commented_code(message)
    assert app.requests_code_with_explanation(message)
    sent = text(message, route="code")
    assert WITH_EXPLANATION in sent and COMMENTED not in sent and SINGLE_FENCE not in sent


@pytest.mark.parametrize("message", [
    "Write code only. Do not explain it.",
    "Write a Python function. Don't explain it.",
    "Write a Python function, but please do not add an explanation.",
])
def test_a_declined_explanation_keeps_the_code_only_contract(message):
    assert app.explicit_code_explanation_rule(message) is None
    sent = text(message, route="code")
    assert SINGLE_FENCE in sent and WITH_EXPLANATION not in sent and COMMENTED not in sent


def test_commented_code_with_a_declined_explanation_keeps_comments_and_no_prose():
    message = "Write commented code, but do not add an explanation afterward."
    assert app.requests_commented_code(message)
    assert app.explicit_code_explanation_rule(message) is None
    sent = text(message, route="code")
    # The single-fence contract: the requested comments stay in the code and
    # no prose explanation follows it.
    assert SINGLE_FENCE in sent and "Do not write any sentence after the code." in sent
    assert WITH_EXPLANATION not in sent
