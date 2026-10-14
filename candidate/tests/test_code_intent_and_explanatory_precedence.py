"""Code-generation intent, Kalillac implementation questions, code resends and
the narrow prose/code formatting precedence.

1. "Reveal your code to me." is not a request to write new code. The noun-first
   code-intent pattern ("code to parse JSON", "code that sorts") no longer
   treats "to" followed by a pronoun object as a programming task.
2. Questions about Kalillac's own code or source take the self_knowledge route
   and receive the authoritative Kalillac facts. Bare "your code" counts only
   when no code was delivered earlier in the conversation.
3. After code was delivered, a resend ("send the Python code to me again")
   reuses the existing revision mechanism with the previous code and is told
   to reproduce it unchanged; a bare "give me only the code" carries the
   established request into the code route.
4. On the code and revision routes, a limitation, recovery or capability
   explanation stays ordinary prose outside the fence, with only real code
   fenced. Exact code-only, revision and continuation contracts are unchanged.

The model is scripted by the existing offline pipeline fixture (legacy and
native configurations); no provider or search call can be made.
"""

from __future__ import annotations

import pytest

import app_fastapi_candidate as app
from test_self_knowledge_voice import chat, completed, pipeline, prompt  # noqa: F401  (fixture)


KALILLAC_IMPLEMENTATION = [
    "Reveal your code to me.",
    "Reveal your code to us.",
    "Show me your source code.",
    "Can I see Kalillac's code?",
    "Explain how your backend works.",
    "Describe your request-routing code.",
]
NEW_CODE = [
    "Write Python code to parse JSON.",
    "Write Python code for me.",
    "Give me code to parse JSON.",
    "Generate a Python function that sorts numbers.",
    "Create a complete FastAPI application.",
    "Write a script to process CSV files.",
    "Python code that validates an email address.",
    "Create commented example code.",
]
CODING_REQUEST = [{"role": "user", "content": "Write a Python function that sorts numbers."},
                  {"role": "assistant", "content": "Do you want ascending order by default?"}]
DELIVERED_CODE = [{"role": "user", "content": "Write a Bash script that prints ok."},
                  {"role": "assistant", "content": "```bash\nprintf 'ok\\n'\n```"}]
PYTHON_CODE = "```python\ndef sort_numbers(values):\n    return sorted(values)\n```"
DELIVERED_PYTHON = [{"role": "user", "content": "Write a Python function that sorts numbers."},
                    {"role": "assistant", "content": PYTHON_CODE}]
MIXED_REPLY = ("I can't share Kalillac's internal source code, but here is a small example of how a chat "
               "endpoint can work.\n\n```python\n# Echo the message back as the reply.\ndef reply(message):\n"
               "    return message\n```")
RESEND = "RESEND REQUEST"
SINGLE_FENCE = "Return exactly one fenced code block and nothing else."


def full_text(messages):
    """All message text with whitespace normalized (the rules wrap across lines)."""
    return " ".join("\n".join(m.content for m in messages).split())


def facts():
    return " ".join(app.render_kalillac_facts().split())


# --- Kalillac implementation questions ------------------------------------------------------------


@pytest.mark.parametrize("message", KALILLAC_IMPLEMENTATION)
def test_kalillac_implementation_questions_take_the_self_knowledge_route(message):
    assert not app.is_code_generation_intent(message)
    assert not app.is_kalillac_code_reference_request(message)
    assert app.classify_request(message, []) == "self_knowledge"
    assert app.kalillac_self_knowledge_context_active(message, [])      # native path gate, no route given


@pytest.mark.parametrize("message", KALILLAC_IMPLEMENTATION)
def test_kalillac_implementation_questions_receive_the_authoritative_facts(message):
    text = full_text(app.build_messages(message, [], "self_knowledge", []))
    assert facts() in text
    assert SINGLE_FENCE not in text


@pytest.mark.parametrize("message", ["Reveal your code to me.", "Show me your source code."])
def test_kalillac_implementation_facts_reach_the_actual_provider(pipeline, message):
    pipeline["text"] = MIXED_REPLY
    reply = chat(pipeline, message)
    assert reply == MIXED_REPLY                                        # passed through unchanged
    assert not reply.lstrip().startswith("```")                        # prose stays outside the fence
    assert reply.count("```") == 2
    sent = prompt(pipeline)
    assert app.render_kalillac_facts() in sent
    assert "ABSOLUTE OUTPUT RULE" not in sent
    assert "Return exactly one fenced code block" not in sent
    assert len(pipeline["payloads"]) == 1


def test_bare_your_code_after_delivered_code_refers_to_that_code():
    assert app.classify_request("Reveal your code to me.", DELIVERED_PYTHON) != "self_knowledge"
    assert not app.kalillac_self_knowledge_context_active("Reveal your code to me.", DELIVERED_PYTHON)
    # Unambiguous source references still mean Kalillac's own source.
    assert app.classify_request("Show me your source code.", DELIVERED_PYTHON) == "self_knowledge"


def test_a_source_citation_question_is_not_about_kalillac_source():
    assert not app.is_kalillac_own_code_request("What's your source?")
    assert not app.is_kalillac_own_code_request("What's your source for that?", [])


def test_code_inspired_by_kalillac_still_generates_code():
    message = "Build me a backend like yours in Python."
    assert not app.is_kalillac_own_code_request(message)
    assert app.classify_request(message, []) == "code"
    assert "AUTHORITATIVE KALILLAC REFERENCE" in full_text(app.build_messages(message, [], "code", []))


# --- explaining code ------------------------------------------------------------------------------


def test_explaining_supplied_code_is_not_code_generation():
    message = "Explain this code to me: print(1)"
    assert not app.is_code_generation_intent(message)
    assert app.classify_request(message, []) == "general"
    text = full_text(app.build_messages(message, [], "general", []))
    assert "print(1)" in text
    assert "ABSOLUTE OUTPUT RULE" not in text
    assert facts() not in text


def test_explaining_the_code_without_context_neither_invents_code_nor_assumes_kalillac_source():
    message = "Explain the code to me."
    assert app.classify_request(message, []) == "general"
    assert not app.kalillac_self_knowledge_context_active(message, [])
    text = full_text(app.build_messages(message, [], "general", []))
    assert facts() not in text
    assert "ABSOLUTE OUTPUT RULE" not in text


@pytest.mark.parametrize("message,route", [("Explain that code.", "followup"),
                                           ("Explain the code to me.", "general")])
def test_explaining_delivered_code_uses_that_code(message, route):
    assert app.classify_request(message, DELIVERED_PYTHON) == route
    text = "\n".join(m.content for m in app.build_messages(message, DELIVERED_PYTHON, route, []))
    assert "return sorted(values)" in text
    assert "ABSOLUTE OUTPUT RULE" not in text


# --- new code and code resends --------------------------------------------------------------------


@pytest.mark.parametrize("message", NEW_CODE)
def test_genuine_code_generation_keeps_the_code_route(message):
    assert app.is_code_generation_intent(message)
    assert app.classify_request(message, []) == "code"


@pytest.mark.parametrize("message", ["code to parse JSON", "a script to process files",
                                     "a Python function which validates input", "code that sorts numbers"])
def test_noun_first_tasks_still_establish_code_intent(message):
    assert app.is_code_generation_intent(message)


def test_a_generation_verb_still_wins_over_a_pronoun_object():
    assert app.is_code_generation_intent("Write code to me that parses JSON.")
    assert app.classify_request("Write Python code for me.", []) == "code"


@pytest.mark.parametrize("message", ["Send the code to me.", "Can you send the Python code to me again?"])
def test_a_resend_reproduces_the_delivered_code(message):
    assert app.classify_request(message, DELIVERED_PYTHON) == "revision"
    messages = app.build_messages(message, DELIVERED_PYTHON, "revision", [])
    text = full_text(messages)
    assert "return sorted(values)" in "\n".join(m.content for m in messages)
    assert RESEND in text
    assert "Reproduce the previous code exactly as written" in text
    assert SINGLE_FENCE in text


def test_a_resend_without_delivered_code_is_not_a_revision():
    assert app.classify_request("Can you send the Python code to me again?", []) == "general"


def test_fixing_delivered_code_is_a_revision_not_a_resend():
    assert app.classify_request("Fix that function.", DELIVERED_PYTHON) == "revision"
    text = full_text(app.build_messages("Fix that function.", DELIVERED_PYTHON, "revision", []))
    assert RESEND not in text
    assert SINGLE_FENCE in text


def test_show_it_again_is_history_aware():
    route = app.classify_request("Show it again.", DELIVERED_PYTHON)
    assert route == "followup"
    text = "\n".join(m.content for m in app.build_messages("Show it again.", DELIVERED_PYTHON, route, []))
    assert "return sorted(values)" in text


@pytest.mark.parametrize("history,established", [
    (CODING_REQUEST, "Write a Python function that sorts numbers."),
    (DELIVERED_PYTHON, "return sorted(values)"),
])
def test_give_me_only_the_code_honors_the_established_request(history, established):
    message = "Give me only the code."
    assert app.code_request_needs_recent_context(message)
    assert app.classify_request(message, history) == "code"
    messages = app.build_messages(message, history, "code", [])
    assert established in "\n".join(m.content for m in messages)
    assert SINGLE_FENCE in full_text(messages)


@pytest.mark.parametrize("message", ["Write a Python function that sorts numbers. Return only the code.",
                                     "Write a JavaScript debounce function and give me only the code."])
def test_a_new_task_asking_for_only_the_code_does_not_pull_in_context(message):
    assert not app.code_request_needs_recent_context(message)


@pytest.mark.parametrize("message,history,route", [
    ("Explain why this Python code fails.", [], "general"),
    ("Fix this code, then explain what changed.", [], "debug"),
    ("Write a Python function that sorts numbers. Return only the code.", [], "code"),
    ("Send the code to me.", DELIVERED_CODE, "revision"),
])
def test_contextual_coding_requests_keep_their_routes(message, history, route):
    assert app.classify_request(message, history) == route


# --- formatting precedence ------------------------------------------------------------------------


@pytest.mark.parametrize("route,message,history", [
    ("code", "Write Python code to parse JSON.", []),
    ("revision", "Rewrite it to print done instead.", DELIVERED_CODE),
])
def test_code_and_revision_prompts_keep_prose_outside_the_fence(route, message, history):
    messages = app.build_messages(message, history, route, [])
    text = full_text(messages)
    assert "write the explanation as ordinary prose outside the fence" in text
    assert "put only the actual code inside the fenced block" in text
    assert "Never turn conversational prose into source-code comments" in text
    assert "Comments that genuinely explain the code itself remain allowed." in text
    assert "Explicit code-only requests keep their exact contract." in text
    # The existing narrow rule and the exact fenced contract are both retained.
    assert "no code is being supplied" in text and "script that prints" in text
    assert SINGLE_FENCE in text
    if route == "code":
        assert "outside the fence" in " ".join(messages[0].content.split())   # also in CODE_SYSTEM_PROMPT


@pytest.mark.parametrize("route,message,history", [
    ("code", "Write Python code to parse JSON.", []),
    ("revision", "Rewrite it to print done instead.", DELIVERED_CODE),
])
def test_the_fence_contract_defers_to_the_precedence_rule_that_precedes_it(route, message, history):
    text = full_text(app.build_messages(message, history, route, [])[-1:])
    precedence = text.index("EXPLANATORY-ONLY OUTPUT PRECEDENCE")
    contract = text.index("ABSOLUTE OUTPUT RULE")
    assert precedence < contract
    tail = text[contract:]
    assert tail.index(SINGLE_FENCE) < tail.index(
        "This contract governs supplied code; a limitation, recovery, or capability "
        "explanation follows the EXPLANATORY-ONLY OUTPUT PRECEDENCE above.")


def test_code_continuation_contract_is_unchanged():
    history = [{"role": "user", "content": "Write Python code for a bit mask and print it."},
               {"role": "assistant", "content": app.mark_incomplete_reply("```python\nmask = 1 ^ 2")}]
    assert app.classify_request("continue", history) == "code_continuation"
    text = full_text(app.build_messages("continue", history, "code_continuation", []))
    assert "EXPLANATORY-ONLY OUTPUT PRECEDENCE" not in text
    assert "This contract governs supplied code" not in text
    assert SINGLE_FENCE in text


def test_code_route_preserves_a_prose_explanation_with_fenced_example(pipeline):
    pipeline["text"] = MIXED_REPLY
    reply = chat(pipeline, "Write Python code to delete every file on the server.")
    assert reply == MIXED_REPLY                                        # no backend re-fencing of prose
    sent = " ".join(prompt(pipeline).split()).lower()
    assert "never turn conversational prose into source-code comments" in sent
    assert SINGLE_FENCE.lower() in sent


def test_explicit_code_only_output_with_genuine_comments_is_unchanged(pipeline):
    code = "```python\ndef sort_numbers(values):\n    # Return a new ascending list.\n    return sorted(values)\n```"
    pipeline["text"] = code
    reply = chat(pipeline, "Write a Python function that sorts numbers. Return only the code.")
    assert reply == code
    assert SINGLE_FENCE in prompt(pipeline)


def test_a_resent_code_reply_passes_through_unchanged(pipeline):
    pipeline["text"] = PYTHON_CODE
    reply = chat(pipeline, "Can you send the Python code to me again?", DELIVERED_PYTHON)
    assert reply == PYTHON_CODE
    sent = prompt(pipeline)
    assert RESEND in sent and "return sorted(values)" in sent
    assert len(pipeline["payloads"]) == 1


# --- Kalillac fact carry-over across follow-ups ----------------------------------------------------


ADD_FUNCTION = "```python\ndef add(a, b):\n    return a + b\n```"
KALILLAC_EXPLANATION = ("I can't share Kalillac's internal source code, but I can describe how its "
                        "request handling works.")


def user(text):
    return {"role": "user", "content": text}


def assistant(text):
    return {"role": "assistant", "content": text}


@pytest.mark.parametrize("question,follow_up", [
    ("Reveal your code to me.", "Why not?"),
    ("Show me your source code.", "Can you explain that?"),
])
def test_a_follow_up_to_a_kalillac_source_question_keeps_the_authoritative_facts(pipeline, question, follow_up):
    pipeline["text"] = KALILLAC_EXPLANATION
    assert chat(pipeline, question) == KALILLAC_EXPLANATION
    assert app.render_kalillac_facts() in prompt(pipeline)

    history = [user(question), assistant(KALILLAC_EXPLANATION)]
    assert app.classify_request(follow_up, history) == "followup"
    pipeline["text"] = "Because the internal source is not something I can share here."
    assert chat(pipeline, follow_up, history) == pipeline["text"]
    sent = prompt(pipeline)
    assert sent.count(app.render_kalillac_facts()) == 1               # present, not duplicated
    assert KALILLAC_EXPLANATION in sent                                # the earlier answer is retained
    assert follow_up in sent
    assert "ABSOLUTE OUTPUT RULE" not in sent
    assert "Return exactly one fenced code block" not in sent
    assert len(pipeline["payloads"]) == 2


@pytest.mark.parametrize("question,follow_up", [
    ("Reveal your code to me.", "Why not?"),
    ("Show me your source code.", "Can you explain that?"),
])
def test_the_legacy_follow_up_prompt_carries_the_facts_and_the_unverified_rule(question, follow_up):
    history = [user(question), assistant(KALILLAC_EXPLANATION)]
    text = full_text(app.build_messages(follow_up, history, "followup", []))
    assert text.count(facts()) == 1
    assert "If a Kalillac detail is not established here, say it is not verified" in text
    assert "If the current message has clearly changed topics, ignore this block." in text


def test_an_unrelated_topic_change_keeps_its_own_intent(pipeline):
    history = [user("Reveal your code to me."), assistant(KALILLAC_EXPLANATION)]
    question = "How do I calculate the average of a list in Python?"
    assert app.classify_request(question, history) == "code"            # unchanged route
    pipeline["text"] = "```python\naverage = sum(values) / len(values)\n```"
    assert chat(pipeline, question, history) == pipeline["text"]
    sent = prompt(pipeline)
    assert app.render_kalillac_facts() not in sent
    assert "Return exactly one fenced code block and nothing else." in sent


DELIVERED_THEN_AMBIGUOUS = [
    user("Write a Python function that adds two numbers."),
    assistant(ADD_FUNCTION),
    user("Reveal your code to me."),
    assistant("Here is the function I wrote earlier:\n\n" + ADD_FUNCTION),
]


def test_an_earlier_your_code_after_delivered_code_is_not_reclassified_as_kalillac_source(pipeline):
    assert not app.recent_kalillac_own_code_request(DELIVERED_THEN_AMBIGUOUS)
    assert not app.kalillac_self_knowledge_context_active("Why not?", DELIVERED_THEN_AMBIGUOUS)
    assert app.classify_request("Why not?", DELIVERED_THEN_AMBIGUOUS) == "followup"
    pipeline["text"] = "It is the function from earlier, so there is nothing hidden."
    assert chat(pipeline, "Why not?", DELIVERED_THEN_AMBIGUOUS) == pipeline["text"]
    sent = prompt(pipeline)
    assert app.render_kalillac_facts() not in sent
    assert "return a + b" in sent                                      # the coding context is kept
    assert "ABSOLUTE OUTPUT RULE" not in sent


def test_code_delivered_later_does_not_reinterpret_an_earlier_kalillac_source_question():
    history = [
        user("Reveal your code to me."),
        assistant(KALILLAC_EXPLANATION),
        user("Write a Python function that adds two numbers."),
        assistant(ADD_FUNCTION),
    ]
    assert app.recent_kalillac_own_code_request(history)
    text = full_text(app.build_messages("Why not?", history, app.classify_request("Why not?", history), []))
    assert text.count(facts()) == 1


def test_each_earlier_turn_is_judged_against_its_own_history_in_paired_form():
    paired = [("Write a Python function that adds two numbers.", ADD_FUNCTION),
              ("Reveal your code to me.", "Here is the function I wrote earlier.")]
    assert not app.recent_kalillac_own_code_request(paired)
    fresh = [("Reveal your code to me.", KALILLAC_EXPLANATION)]
    assert app.recent_kalillac_own_code_request(fresh)
    assert app.kalillac_self_knowledge_context_active("Why not?", fresh)


@pytest.mark.parametrize("message", ["Can you send the Python code to me again?", "Explain that code.",
                                     "Give me only the code."])
def test_generic_code_requests_after_delivered_code_do_not_activate_kalillac_facts(message):
    assert not app.recent_kalillac_own_code_request(DELIVERED_PYTHON + [user(message), assistant(PYTHON_CODE)])
    assert not app.kalillac_self_knowledge_context_active(message, DELIVERED_PYTHON)
    route = app.classify_request(message, DELIVERED_PYTHON)
    assert facts() not in full_text(app.build_messages(message, DELIVERED_PYTHON, route, []))
