"""Shared response-intent guidance and route-contract preservation.

The shared RESPONSE QUALITY block (in CURRENT_INTERFACE_CAPABILITIES) reaches
every model-backed path: legacy calls receive it as a system message and the
native path receives it at the start of its instructions. It now asks the
model to serve what the user is trying to accomplish, to guide one step at a
time when the user wants to do a task now, to start troubleshooting with the
most informative check, and to start teaching instead of listing a curriculum.

These tests prove prompt construction, routing and output contracts only. The
model output is scripted; they do not prove how GPT-6 Luna will word a reply.
"""

from __future__ import annotations

import pytest

import app_fastapi_candidate as app
from test_self_knowledge_voice import chat, completed, pipeline, prompt  # noqa: F401  (fixture)


OBJECTIVE = "Work out what the user is trying to accomplish, not only the subject they named"
GUIDED = "When the user wants to do a task now with your help"
GUIDED_WAIT = "ask them to report what they see or what happened, and continue from that result"
NO_CHECKLIST = "do not replace the first step with an overview or a full checklist"
TROUBLESHOOT = "For troubleshooting, start with the most likely cause or the most informative check"
LEARN = "When the user wants to start learning a broad subject, start teaching it"
REFERENCE_STEPS = "Use numbered steps for a procedure the user wants to read or keep as a reference."
FIRST_STEP = "When a safe, useful first step is available, take it"
NEW_RULES = [OBJECTIVE, GUIDED, GUIDED_WAIT, NO_CHECKLIST, TROUBLESHOOT, LEARN, REFERENCE_STEPS, FIRST_STEP]
REPLACED = [
    "Use numbered steps for procedures.",
    "Response length should be proportional to the task.",
]
KEPT = [
    "Match depth to the task: keep simple answers concise",
    "Follow exact length, format, audience, and output-only instructions.",
    "Do not turn a simple answer into a report",
    "Do not impose a fixed length",
    "If the user signals confusion about the previous reply, explain your interpretation once in simpler, "
    "genuinely different words",
]
SINGLE_FENCE = "Return exactly one fenced code block and nothing else."

GENERAL_REQUESTS = [
    "I really want to learn cybersecurity.",
    "I want to get started now. I don't know where to start. What should I do?",
    "I have a Lenovo computer. Help me start securing it.",
    "Help me understand Python functions.",
    "My computer isn't working.",
    "Should I learn Flask or FastAPI?",
    "What is an IP address?",
    "Explain in depth how TLS works.",
    "Give me a one-sentence answer: what is DNS?",
    "My computer is installing malware and my mouse is moving by itself.",
    "What is the latest stable Python release?",
]
FIRST_STEP_GIVEN = [
    {"role": "user", "content": "I want to start securing my computer now. Walk me through it."},
    {"role": "assistant", "content": "First, open Settings and check whether updates are waiting. Tell me what you see."},
]
EXPLAINED = [
    {"role": "user", "content": "What is a Python function?"},
    {"role": "assistant", "content": "A function is a named block of code you can call with inputs."},
]
DELIVERED_CODE = [
    {"role": "user", "content": "Write a Python function that adds two numbers."},
    {"role": "assistant", "content": "```python\ndef add(a, b):\n    return a + b\n```"},
]


def text(state):
    return " ".join(prompt(state).split())


# --- the shared guidance reaches both paths once ---------------------------------------------------


@pytest.mark.parametrize("message", GENERAL_REQUESTS)
def test_general_requests_receive_the_intent_guidance_once(pipeline, message):
    assert app.classify_request(message, []) == "general"
    pipeline["text"] = "Scripted reply."
    assert chat(pipeline, message) == "Scripted reply."
    sent = text(pipeline)
    assert sent.count("RESPONSE QUALITY:") == 1
    for rule in NEW_RULES:
        assert sent.count(rule) == 1, rule
    for rule in REPLACED:
        assert rule not in sent, rule
    for rule in KEPT:
        assert rule in sent, rule
    assert "ABSOLUTE OUTPUT RULE" not in sent
    assert pipeline["payloads"][-1]["max_output_tokens"] == app.MAX_RESPONSE_TOKENS == 1600
    assert len(pipeline["payloads"]) == 1


def test_legacy_and_native_receive_the_same_shared_block(pipeline):
    chat(pipeline, "I want to get started now. I don't know where to start. What should I do?")
    payload = pipeline["payloads"][-1]
    if pipeline["native"]:
        assert payload["instructions"].startswith(app.CURRENT_INTERFACE_CAPABILITIES)
        assert "tools" in payload
    else:
        assert payload["input"][0]["role"] == "system"
        assert payload["input"][0]["content"] == app.CURRENT_INTERFACE_CAPABILITIES


def test_the_legacy_general_route_asks_for_the_users_objective():
    messages = app.build_messages("My computer isn't working.", [], "general", [])
    joined = " ".join("\n".join(m.content for m in messages).split())
    assert "serving what they are trying to accomplish rather than only the subject they named" in joined
    assert "Answer the user's current question directly." not in joined


# --- conversation state -----------------------------------------------------------------------------


def test_an_active_step_by_step_task_continues_from_the_reported_result(pipeline):
    message = "OK, I see two updates waiting. What next?"
    assert app.classify_request(message, FIRST_STEP_GIVEN) == "general"
    chat(pipeline, message, FIRST_STEP_GIVEN)
    sent = text(pipeline)
    assert "check whether updates are waiting" in sent
    assert GUIDED_WAIT in sent


@pytest.mark.parametrize("message", ["I don't understand. Explain it another way.", "Show me an example."])
def test_follow_ups_after_an_explanation_keep_that_explanation(pipeline, message):
    assert app.classify_request(message, EXPLAINED) == "general"
    chat(pipeline, message, EXPLAINED)
    sent = text(pipeline)
    assert "A function is a named block of code you can call with inputs." in sent
    assert "genuinely different words" in sent


# --- route contracts are unchanged ------------------------------------------------------------------


def test_explicit_code_only_generation_keeps_its_contract(pipeline):
    code = "```python\ndef add(a, b):\n    return a + b\n```"
    pipeline["text"] = code
    assert chat(pipeline, "Write a Python function that adds two numbers. Return only the code.") == code
    sent = text(pipeline)
    assert SINGLE_FENCE in sent
    assert sent.count(OBJECTIVE) == 1
    assert pipeline["payloads"][-1]["max_output_tokens"] == app.CODE_RESPONSE_TOKENS


@pytest.mark.parametrize("message,contract", [
    ("Add type hints to that function.", SINGLE_FENCE),
    ("Can you explain it and then add tests?", "OUTPUT FOR AN EXPLANATION AND A CHANGE"),
])
def test_code_revisions_keep_their_contracts(pipeline, message, contract):
    assert app.classify_request(message, DELIVERED_CODE) == "revision"
    pipeline["text"] = "```python\ndef add(a: int, b: int) -> int:\n    return a + b\n```"
    chat(pipeline, message, DELIVERED_CODE)
    sent = text(pipeline)
    assert "return a + b" in sent
    assert contract in sent
    assert pipeline["payloads"][-1]["max_output_tokens"] == app.CODE_RESPONSE_TOKENS


def test_a_non_code_topic_change_after_code_gets_no_code_contract(pipeline):
    message = "Add comments to the blog post."
    assert app.classify_request(message, DELIVERED_CODE) == "general"
    chat(pipeline, message, DELIVERED_CODE)
    assert "ABSOLUTE OUTPUT RULE" not in text(pipeline)


def test_code_continuation_keeps_its_exact_contract():
    history = [{"role": "user", "content": "Write Python code for a bit mask and print it."},
               {"role": "assistant", "content": app.mark_incomplete_reply("```python\nmask = 1 ^ 2")}]
    assert app.classify_request("continue", history) == "code_continuation"
    joined = " ".join("\n".join(m.content for m in app.build_messages("continue", history, "code_continuation", [])).split())
    assert SINGLE_FENCE in joined
    assert OBJECTIVE not in joined                     # the shared block is added at invocation, not here


def test_the_product_question_keeps_the_authoritative_facts(pipeline):
    message = "Why should I choose Kalillac AI?"
    assert app.classify_request(message, []) == "self_knowledge"
    chat(pipeline, message)
    sent = prompt(pipeline)
    assert app.render_kalillac_facts() in sent
    assert " ".join(sent.split()).count(OBJECTIVE) == 1
    expected = app.MAX_RESPONSE_TOKENS if pipeline["native"] else app.SELF_KNOWLEDGE_RESPONSE_TOKENS
    assert pipeline["payloads"][-1]["max_output_tokens"] == expected


def test_output_budgets_are_unchanged():
    assert (app.MAX_RESPONSE_TOKENS, app.SELF_KNOWLEDGE_RESPONSE_TOKENS,
            app.CODE_RESPONSE_TOKENS, app.CODE_CONTINUATION_RESPONSE_TOKENS) == (1600, 2000, 6000, 2400)
