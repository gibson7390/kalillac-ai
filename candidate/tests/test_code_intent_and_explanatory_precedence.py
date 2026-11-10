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


# --- natural modifications of the code just delivered ---------------------------------------------


GET_NAME_REQUEST = ('Write Python code to parse a JSON string and return the value of its "name" field. '
                    "Give me only the code.")
GET_NAME_CODE = '```python\nimport json\n\ndef get_name(json_string):\n    return json.loads(json_string)["name"]\n```'
GET_NAME_IMPLEMENTATION = 'return json.loads(json_string)["name"]'
DELIVERED_GET_NAME = [user(GET_NAME_REQUEST), assistant(GET_NAME_CODE)]
STAGING_TURN_2 = "Now raise a clear error when the name field is missing. Give me only the revised code."
REVISED_GET_NAME = ('```python\nimport json\n\ndef get_name(json_string):\n    data = json.loads(json_string)\n'
                    '    if "name" not in data:\n        raise KeyError("The name field is missing.")\n'
                    '    return data["name"]\n```')

CODE_MODIFICATIONS = [
    STAGING_TURN_2,
    "Raise a ValueError when name is missing.",
    "Handle invalid JSON and return only the updated code.",
    "Add validation for an empty list.",
    "Return None if the key is missing.",
    "Use pathlib instead. Give me only the code.",
    "Add type hints to that function.",
    "Remove the print statement.",
    "Rename the function to parse_name.",
    "Catch the JSON decoding error.",
    "Reject empty values.",
    "Add a timeout parameter.",
    "Give me only the revised code.",
    "Give me only the updated code.",
    "Give me only the modified code.",
]


def test_the_staging_modification_turn_revises_the_delivered_code(pipeline):
    pipeline["text"] = GET_NAME_CODE
    assert chat(pipeline, GET_NAME_REQUEST) == GET_NAME_CODE

    assert app.previous_answer_looks_like_code(app.get_last_assistant_message(DELIVERED_GET_NAME))
    assert app.classify_request(STAGING_TURN_2, DELIVERED_GET_NAME) == "revision"
    pipeline["text"] = REVISED_GET_NAME
    assert chat(pipeline, STAGING_TURN_2, DELIVERED_GET_NAME) == REVISED_GET_NAME   # passes through unchanged
    sent = prompt(pipeline)
    assert GET_NAME_IMPLEMENTATION in sent                             # no need to paste it again
    assert STAGING_TURN_2 in sent
    assert SINGLE_FENCE in sent
    assert RESEND not in sent
    assert len(pipeline["payloads"]) == 2


@pytest.mark.parametrize("message", CODE_MODIFICATIONS)
def test_a_natural_modification_of_delivered_code_enters_the_revision_prompt(message):
    assert app.is_code_modification_followup(message, DELIVERED_GET_NAME)
    assert app.classify_request(message, DELIVERED_GET_NAME) == "revision"
    messages = app.build_messages(message, DELIVERED_GET_NAME, "revision", [])
    raw = "\n".join(m.content for m in messages)
    text = full_text(messages)
    assert GET_NAME_IMPLEMENTATION in raw
    assert message in raw
    assert "The user wants you to improve, continue, or regenerate previous code." in text
    assert "Return only the complete revised code." in text
    assert "Preserve the original code type unless the user explicitly asks to change it." in text
    assert SINGLE_FENCE in text
    assert RESEND not in text


@pytest.mark.parametrize("message", CODE_MODIFICATIONS)
def test_the_same_instructions_without_delivered_code_are_not_revisions(message):
    assert not app.is_code_modification_followup(message, [])
    assert app.classify_request(message, []) != "revision"


def test_code_that_is_no_longer_the_previous_answer_does_not_make_a_revision():
    history = DELIVERED_GET_NAME + [user("Thanks, what else should I learn?"),
                                    assistant("Error handling and testing are good next steps.")]
    assert not app.is_code_modification_followup("Add type hints to that function.", history)


def test_a_modification_that_also_asks_for_the_code_is_not_a_resend():
    message = "Add type hints and send me the code."
    assert app.is_send_code_followup(message)
    assert app.classify_request(message, DELIVERED_GET_NAME) == "revision"
    text = full_text(app.build_messages(message, DELIVERED_GET_NAME, "revision", []))
    assert RESEND not in text
    assert SINGLE_FENCE in text


@pytest.mark.parametrize("message,resend", [
    ("Fix that function.", False),
    ("Rewrite it.", False),
    ("Improve the code.", False),
    ("Add error handling.", False),
    ("Send the code to me again.", True),
])
def test_existing_revision_and_resend_forms_keep_working(message, resend):
    assert app.classify_request(message, DELIVERED_GET_NAME) == "revision"
    text = full_text(app.build_messages(message, DELIVERED_GET_NAME, "revision", []))
    assert (RESEND in text) is resend
    assert SINGLE_FENCE in text


def test_give_me_only_the_code_keeps_the_established_code_route():
    assert app.classify_request("Give me only the code.", DELIVERED_GET_NAME) == "code"
    messages = app.build_messages("Give me only the code.", DELIVERED_GET_NAME, "code", [])
    assert GET_NAME_IMPLEMENTATION in "\n".join(m.content for m in messages)
    assert SINGLE_FENCE in full_text(messages)


@pytest.mark.parametrize("message,route", [
    ("Raise awareness about password security.", "general"),
    ("I need to raise money for a project.", "general"),
    ("Return to the previous topic.", "general"),
    ("Add this item to my notes.", "file_unavailable"),
    ("Add milk to my grocery list.", "general"),
    ("Use Python for this project.", "general"),
    ("Use plain English.", "general"),
    ("Handle this situation professionally.", "general"),
])
def test_ordinary_imperatives_without_code_are_not_revisions(message, route):
    assert app.classify_request(message, []) == route


@pytest.mark.parametrize("message,route", [
    ("Raise awareness about password security.", "general"),
    ("I need to raise money for a project.", "general"),
    ("Return to the previous topic.", "general"),
    ("Add this item to my notes.", "file_unavailable"),
    ("Add milk to my grocery list.", "general"),
    ("Use Python for this project.", "followup"),
    ("Use plain English.", "general"),
    ("Handle this situation professionally.", "followup"),
])
def test_ordinary_imperatives_after_delivered_code_are_not_revisions(message, route):
    assert not app.is_code_modification_followup(message, DELIVERED_GET_NAME)
    assert app.classify_request(message, DELIVERED_GET_NAME) == route


@pytest.mark.parametrize("message,route", [
    ("What does this code do?", "followup"),
    ("Why does this error happen?", "followup"),
    ("Explain that function.", "followup"),
    ("Is this implementation efficient?", "followup"),
    ("What are the security risks?", "general"),
    ("What would this return?", "followup"),
])
def test_questions_about_delivered_code_stay_explanations(message, route):
    assert not app.is_code_modification_followup(message, DELIVERED_GET_NAME)
    assert app.classify_request(message, DELIVERED_GET_NAME) == route
    text = "\n".join(m.content for m in app.build_messages(message, DELIVERED_GET_NAME, route, []))
    assert GET_NAME_IMPLEMENTATION in text
    assert "ABSOLUTE OUTPUT RULE" not in text


@pytest.mark.parametrize("message", ["Raise a clear error when it is missing.", "Add validation."])
def test_underspecified_modifications_without_code_keep_their_existing_routes(message):
    assert not app.is_code_modification_followup(message, [])
    assert not app.requests_unavailable_code_revision(message, [])
    assert app.classify_request(message, []) == "general"


# --- revised code that does not exist -------------------------------------------------------------


UNAVAILABLE_REVISIONS = [
    "Give me only the revised code.",
    "Give me only the updated code.",
    "Give me only the modified code.",
    STAGING_TURN_2,
]
CLARIFICATION = "Which code should I revise? Please paste it or describe what it does."


@pytest.mark.parametrize("message", UNAVAILABLE_REVISIONS)
def test_revised_code_without_delivered_code_asks_instead_of_inventing(message):
    assert app.requests_unavailable_code_revision(message, [])
    assert app.classify_request(message, []) == "unclear"
    text = full_text(app.build_messages(message, [], "unclear", []))
    assert "Do not write, guess, or invent the code to be revised." in text
    assert "Ask one short question requesting that code or the missing context." in text
    assert "Do not guess an action or unavailable contents." in text
    assert "ABSOLUTE OUTPUT RULE" not in text
    assert SINGLE_FENCE not in text


@pytest.mark.parametrize("message", ["Give me only the revised code.", STAGING_TURN_2])
def test_the_model_is_asked_for_the_missing_code_not_for_a_program(pipeline, message):
    pipeline["text"] = CLARIFICATION
    reply = chat(pipeline, message)
    assert reply == CLARIFICATION
    assert "```" not in reply
    sent = prompt(pipeline)
    assert app.UNAVAILABLE_CODE_REVISION_RULE in sent
    assert "ABSOLUTE OUTPUT RULE" not in sent
    assert SINGLE_FENCE not in sent
    assert len(pipeline["payloads"]) == 1


def test_revised_code_wording_after_delivered_code_is_not_a_clarification():
    assert not app.requests_unavailable_code_revision("Give me only the revised code.", DELIVERED_GET_NAME)
    text = full_text(app.build_messages(STAGING_TURN_2, DELIVERED_GET_NAME, "revision", []))
    assert "NO CODE TO REVISE" not in text


# --- polite and conversational modifications ------------------------------------------------------


POLITE_MODIFICATIONS = [
    "Can you add type hints?",
    "Could you handle invalid JSON?",
    "Would you use pathlib instead?",
    "Please make it async.",
    "Can you make this async?",
    "Add unit tests.",
    "Now sort it in descending order.",
    "Change the return type to str.",
    "Could you rename it to parse_name?",
    "Can you catch the decoding exception?",
    "Make it async.",
]


@pytest.mark.parametrize("message", POLITE_MODIFICATIONS)
def test_polite_and_conversational_modifications_revise_the_delivered_code(message):
    assert app.is_code_modification_followup(message, DELIVERED_GET_NAME)
    assert app.classify_request(message, DELIVERED_GET_NAME) == "revision"
    messages = app.build_messages(message, DELIVERED_GET_NAME, "revision", [])
    assert GET_NAME_IMPLEMENTATION in "\n".join(m.content for m in messages)
    assert message in "\n".join(m.content for m in messages)
    text = full_text(messages)
    assert SINGLE_FENCE in text
    assert RESEND not in text


@pytest.mark.parametrize("message", POLITE_MODIFICATIONS)
def test_polite_modifications_without_delivered_code_are_not_revisions(message):
    assert not app.is_code_modification_followup(message, [])
    assert app.classify_request(message, []) != "revision"


# --- topic changes after delivered code -----------------------------------------------------------


@pytest.mark.parametrize("message,route", [
    ("Use JSON for my notes app.", "file_unavailable"),
    ("Add a timeout to my calendar reminder.", "general"),
    ("Raise awareness about JSON security.", "general"),
    ("Add tests to my study plan.", "general"),
    ("Use pathlib in a new project.", "general"),
    ("Handle errors professionally when talking to customers.", "general"),
    ("Return to my earlier question about school.", "general"),
])
def test_a_topic_change_after_delivered_code_is_not_a_revision(message, route):
    assert not app.is_code_modification_followup(message, DELIVERED_GET_NAME)
    assert app.classify_request(message, DELIVERED_GET_NAME) == route


# --- "version" is not code ------------------------------------------------------------------------


@pytest.mark.parametrize("message", [
    "What is the updated version of Python?",
    "What changed in the updated version of FastAPI?",
    "Give me the updated version of the schedule.",
    "Compare the revised and original versions of this paragraph.",
])
def test_generic_version_requests_are_not_missing_code_revisions(message):
    assert not app.requests_unavailable_code_revision(message, [])
    route = app.classify_request(message, [])
    assert route == "general"
    assert "NO CODE TO REVISE" not in full_text(app.build_messages(message, [], route, []))


def test_a_version_question_reaches_the_model_without_the_missing_code_rule(pipeline):
    pipeline["text"] = "The current Python release line is listed on python.org."
    assert chat(pipeline, "What is the updated version of Python?") == pipeline["text"]
    assert "NO CODE TO REVISE" not in prompt(pipeline)


def test_update_the_code_without_any_code_asks_for_it():
    message = "Please update the code and return only the code."
    assert app.requests_unavailable_code_revision(message, [])
    assert app.classify_request(message, []) == "unclear"
    assert "Do not write, guess, or invent the code to be revised." in full_text(
        app.build_messages(message, [], "unclear", []))


# --- code supplied in the current message ---------------------------------------------------------


UNFENCED_SUPPLIED = ('Revise this code and return only the updated code:\n\ndef greet(name):\n'
                     '    return "Hello " + name\n\nAdd a return type annotation.')
FENCED_SUPPLIED = ("Please fix this and give me only the revised code:\n\n```python\ndef average(values):\n"
                   "    return sum(values) / len(values)\n```\n\nHandle an empty list.")


@pytest.mark.parametrize("message,code_line,history", [
    (UNFENCED_SUPPLIED, 'return "Hello " + name', []),
    (FENCED_SUPPLIED, "return sum(values) / len(values)", []),
    (FENCED_SUPPLIED, "return sum(values) / len(values)", DELIVERED_GET_NAME),
])
def test_code_supplied_in_the_message_is_revised_without_asking_for_it(message, code_line, history):
    assert app.supplies_code_to_revise(message)
    assert not app.requests_unavailable_code_revision(message, history)
    assert not app.is_code_modification_followup(message, history)       # the supplied code is the target
    assert app.classify_request(message, history) == "code"
    messages = app.build_messages(message, history, "code", [])
    raw = "\n".join(m.content for m in messages)
    assert code_line in raw
    text = full_text(messages)
    assert "NO CODE TO REVISE" not in text
    assert SINGLE_FENCE in text


@pytest.mark.parametrize("message,code_line", [
    (UNFENCED_SUPPLIED, 'return "Hello " + name'),
    (FENCED_SUPPLIED, "return sum(values) / len(values)"),
])
def test_supplied_code_reaches_the_model_payload(pipeline, message, code_line):
    revised = "```python\ndef revised():\n    return None\n```"
    pipeline["text"] = revised
    assert chat(pipeline, message) == revised
    sent = prompt(pipeline)
    assert code_line in sent
    assert "NO CODE TO REVISE" not in sent
    assert SINGLE_FENCE in sent
    assert len(pipeline["payloads"]) == 1


def test_prose_about_code_is_not_supplied_code():
    for message in ["Give me only the revised code.", "The def keyword starts a function in Python.",
                    "A function is a rule that maps inputs to outputs."]:
        assert not app.supplies_code_to_revise(message)


# --- further natural revisions and topic changes --------------------------------------------------


@pytest.mark.parametrize("message", [
    "Add comments.",
    "Document the function.",
    "Use a dataclass.",
    "Make the output prettier.",
    "Optimize it for speed.",
])
def test_further_natural_revisions_revise_the_delivered_code(message):
    assert app.is_code_modification_followup(message, DELIVERED_GET_NAME)
    assert app.classify_request(message, DELIVERED_GET_NAME) == "revision"
    messages = app.build_messages(message, DELIVERED_GET_NAME, "revision", [])
    raw = "\n".join(m.content for m in messages)
    assert GET_NAME_IMPLEMENTATION in raw and message in raw
    assert SINGLE_FENCE in full_text(messages)
    assert not app.is_code_modification_followup(message, [])


@pytest.mark.parametrize("message", [
    "Add comments to my blog post.",
    "Document the meeting.",
    "Use a dataclass in a new project.",
    "Make the output prettier for my report.",
    "Optimize my website.",
])
def test_further_topic_changes_do_not_revise_the_delivered_code(message):
    assert not app.is_code_modification_followup(message, DELIVERED_GET_NAME)
    assert app.classify_request(message, DELIVERED_GET_NAME) == "general"


def test_a_further_revision_reaches_the_model_with_the_delivered_code(pipeline):
    pipeline["text"] = REVISED_GET_NAME
    assert chat(pipeline, "Add comments.", DELIVERED_GET_NAME) == REVISED_GET_NAME
    sent = prompt(pipeline)
    assert GET_NAME_IMPLEMENTATION in sent
    assert SINGLE_FENCE in sent
    assert RESEND not in sent


def test_a_topic_change_reaches_the_model_without_the_revision_contract(pipeline):
    pipeline["text"] = "Start with a short summary of the meeting's decisions."
    assert chat(pipeline, "Document the meeting.", DELIVERED_GET_NAME) == pipeline["text"]
    sent = prompt(pipeline)
    assert "ABSOLUTE OUTPUT RULE" not in sent
    assert SINGLE_FENCE not in sent


# --- supplied code is edited, not regenerated -----------------------------------------------------


SUPPLIED_NOT_CODE_ONLY = ("Please update the code to handle an empty list:\n\n```python\ndef average(values):\n"
                          "    return sum(values) / len(values)\n```")
EDIT_RULE_LINES = [
    "The code in the user's message is the source to modify.",
    "do not ask the user to paste it.",
    "Do not replace it with an unrelated implementation.",
    "Preserve the behavior, names, and structure the request does not affect.",
]


@pytest.mark.parametrize("message,code_line,code_only", [
    (FENCED_SUPPLIED, "return sum(values) / len(values)", True),
    (UNFENCED_SUPPLIED, 'return "Hello " + name', True),
    (SUPPLIED_NOT_CODE_ONLY, "return sum(values) / len(values)", False),
])
def test_supplied_code_receives_edit_instructions_in_the_model_payload(pipeline, message, code_line, code_only):
    assert app.classify_request(message, []) == "code"
    assert app.requests_code_only_output(message) is code_only
    revised = "```python\ndef average(values):\n    return sum(values) / len(values) if values else 0\n```"
    pipeline["text"] = revised
    assert chat(pipeline, message) == revised
    sent = prompt(pipeline)
    assert message in sent                                             # the supplied code, verbatim
    assert code_line in sent
    normalized = " ".join(sent.split())
    for line in EDIT_RULE_LINES:
        assert line in normalized
    assert "NO CODE TO REVISE" not in sent
    assert (SINGLE_FENCE in sent) is code_only                         # code-only only when asked for
    if not code_only:
        assert "Return the complete revised code in one fenced code block" in normalized
        assert "Do not write any sentence before the code." not in sent
    assert len(pipeline["payloads"]) == 1


def test_new_code_requests_receive_no_supplied_code_instructions():
    for message in ["Write Python code to parse JSON.", "Write a Python function that sorts numbers. Return only the code."]:
        text = full_text(app.build_messages(message, [], "code", []))
        assert "SUPPLIED CODE TO EDIT" not in text
        assert SINGLE_FENCE in text


# --- an explanation plus a change -----------------------------------------------------------------


@pytest.mark.parametrize("message", [
    "Can you explain it and then add tests?",
    "Add tests and explain what you changed.",
])
def test_an_explanation_plus_a_change_gets_both(message):
    assert app.is_code_modification_followup(message, DELIVERED_GET_NAME)
    assert app.requests_explanation_with_code_change(message)
    assert app.classify_request(message, DELIVERED_GET_NAME) == "revision"
    messages = app.build_messages(message, DELIVERED_GET_NAME, "revision", [])
    raw = "\n".join(m.content for m in messages)
    assert GET_NAME_IMPLEMENTATION in raw and message in raw
    text = full_text(messages)
    assert "First explain concisely, in ordinary prose" in text
    assert "Then return the complete revised code in exactly one fenced code block" in text
    assert SINGLE_FENCE not in text
    assert "Do not add explanations." not in text
    assert "NO CODE TO REVISE" not in text


def test_an_explanation_plus_a_change_reaches_the_model_and_passes_through(pipeline):
    reply = ("The function parses the JSON string and returns its name field.\n\n```python\nimport json\n\n"
             "def get_name(json_string):\n    return json.loads(json_string)[\"name\"]\n\n"
             "def test_get_name():\n    assert get_name('{\"name\": \"Ada\"}') == \"Ada\"\n```")
    pipeline["text"] = reply
    assert chat(pipeline, "Can you explain it and then add tests?", DELIVERED_GET_NAME) == reply
    sent = prompt(pipeline)
    assert GET_NAME_IMPLEMENTATION in sent
    assert "OUTPUT FOR AN EXPLANATION AND A CHANGE" in sent
    assert SINGLE_FENCE not in sent
    assert len(pipeline["payloads"]) == 1


@pytest.mark.parametrize("message", [
    "Give me only the revised code.",
    "Explain nothing, just add tests and return only the code.",
    "Add tests and explain nothing; give me only the code.",
])
def test_explicit_code_only_revisions_stay_code_only(message):
    assert app.classify_request(message, DELIVERED_GET_NAME) == "revision"
    text = full_text(app.build_messages(message, DELIVERED_GET_NAME, "revision", []))
    assert SINGLE_FENCE in text
    assert "OUTPUT FOR AN EXPLANATION AND A CHANGE" not in text


# --- explicit non-code targets --------------------------------------------------------------------


@pytest.mark.parametrize("message", [
    "Add comments to the blog post.",
    "Add comments to the explanation.",
    "Make the output prettier for the report.",
    "Document the meeting notes.",
    "Use a dataclass in the new project.",
    "Optimize the website.",
    "Add tests to the lesson plan.",
])
def test_a_named_non_code_target_does_not_revise_the_delivered_code(message):
    assert not app.is_code_modification_followup(message, DELIVERED_GET_NAME)
    assert app.classify_request(message, DELIVERED_GET_NAME) != "revision"


@pytest.mark.parametrize("message", [
    "Add comments.",
    "Add comments to the function.",
    "Document the function.",
    "Make the output prettier.",
    "Make the function output prettier.",
    "Use a dataclass.",
    "Use a dataclass in the implementation.",
    "Optimize it for speed.",
    "Add tests.",
    "Add tests for the function.",
    "Handle the edge case of an empty string.",
    "Add a comment at the top of the file.",
    "Raise an error in the case of invalid JSON.",
])
def test_a_code_target_revises_the_delivered_code(message):
    assert app.is_code_modification_followup(message, DELIVERED_GET_NAME)
    assert app.classify_request(message, DELIVERED_GET_NAME) == "revision"
    messages = app.build_messages(message, DELIVERED_GET_NAME, "revision", [])
    assert GET_NAME_IMPLEMENTATION in "\n".join(m.content for m in messages)
    assert SINGLE_FENCE in full_text(messages)


def test_a_named_non_code_target_reaches_the_model_without_the_revision_contract(pipeline):
    pipeline["text"] = "Here are a few comments you could add to the blog post."
    assert chat(pipeline, "Add comments to the blog post.", DELIVERED_GET_NAME) == pipeline["text"]
    sent = prompt(pipeline)
    assert "ABSOLUTE OUTPUT RULE" not in sent
    assert SINGLE_FENCE not in sent


# --- code constructs, defects and missing values --------------------------------------------------


CONSTRUCT_REVISIONS = [
    "Handle the case where the name is missing.",
    "Add logging to the main loop.",
    "Remove the bug in the parser.",
    "Wrap the payload in a try block.",
]
CONSTRUCT_TOPIC_CHANGES = [
    "Handle the case where the meeting is canceled.",
    "Add logging to the meeting notes.",
    "Remove the error from the report.",
    "Wrap the package for shipping.",
    "Fix the wording in the article.",
    "Add examples to the lesson plan.",
    "Explain the test results for the class.",
]


@pytest.mark.parametrize("message", CONSTRUCT_REVISIONS)
def test_a_code_construct_or_missing_value_revises_the_delivered_code(message):
    assert app.is_code_modification_followup(message, DELIVERED_GET_NAME)
    assert app.classify_request(message, DELIVERED_GET_NAME) == "revision"
    messages = app.build_messages(message, DELIVERED_GET_NAME, "revision", [])
    raw = "\n".join(m.content for m in messages)
    assert GET_NAME_IMPLEMENTATION in raw and message in raw
    text = full_text(messages)
    assert "The user wants you to improve, continue, or regenerate previous code." in text
    assert SINGLE_FENCE in text
    assert RESEND not in text
    assert "NO CODE TO REVISE" not in text


@pytest.mark.parametrize("message", CONSTRUCT_REVISIONS)
def test_a_code_construct_revision_reaches_the_model_with_the_delivered_code(pipeline, message):
    pipeline["text"] = REVISED_GET_NAME
    assert chat(pipeline, message, DELIVERED_GET_NAME) == REVISED_GET_NAME
    sent = prompt(pipeline)
    assert GET_NAME_IMPLEMENTATION in sent
    assert message in sent
    assert SINGLE_FENCE in sent
    assert len(pipeline["payloads"]) == 1


@pytest.mark.parametrize("message", CONSTRUCT_REVISIONS)
def test_a_code_construct_without_delivered_code_is_not_a_revision(message):
    assert not app.is_code_modification_followup(message, [])
    assert app.classify_request(message, []) != "revision"
    stale = DELIVERED_GET_NAME + [user("Thanks."), assistant("You're welcome.")]
    assert not app.is_code_modification_followup(message, stale)


def test_construct_revisions_keep_code_only_and_explanation_forms():
    code_only = "Wrap the payload in a try block and return only the code."
    assert app.classify_request(code_only, DELIVERED_GET_NAME) == "revision"
    assert SINGLE_FENCE in full_text(app.build_messages(code_only, DELIVERED_GET_NAME, "revision", []))
    explained = "Remove the bug in the parser and explain what was wrong."
    assert app.classify_request(explained, DELIVERED_GET_NAME) == "revision"
    text = full_text(app.build_messages(explained, DELIVERED_GET_NAME, "revision", []))
    assert "OUTPUT FOR AN EXPLANATION AND A CHANGE" in text
    assert SINGLE_FENCE not in text


@pytest.mark.parametrize("message", CONSTRUCT_TOPIC_CHANGES)
def test_a_non_code_object_keeps_its_ordinary_route_after_delivered_code(message):
    assert not app.is_code_modification_followup(message, DELIVERED_GET_NAME)
    route = app.classify_request(message, DELIVERED_GET_NAME)
    assert route == "general"
    text = full_text(app.build_messages(message, DELIVERED_GET_NAME, route, []))
    assert "ABSOLUTE OUTPUT RULE" not in text
    assert SINGLE_FENCE not in text
    assert "OUTPUT FOR AN EXPLANATION AND A CHANGE" not in text


@pytest.mark.parametrize("message", ["Wrap the package for shipping.", "Remove the error from the report."])
def test_a_non_code_object_reaches_the_model_without_a_revision_contract(pipeline, message):
    pipeline["text"] = "Here is how to handle that."
    assert chat(pipeline, message, DELIVERED_GET_NAME) == pipeline["text"]
    sent = prompt(pipeline)
    assert "ABSOLUTE OUTPUT RULE" not in sent
    assert SINGLE_FENCE not in sent


@pytest.mark.parametrize("message", [
    "Add a summary at the end.",
    "Return the result to the customer.",
    "Remove the bug from the garden.",
    "Add a note when the fridge is empty.",
    "Change the name of the event.",
])
def test_generic_words_alone_do_not_make_a_revision(message):
    assert not app.is_code_modification_followup(message, DELIVERED_GET_NAME)
    assert app.classify_request(message, DELIVERED_GET_NAME) != "revision"
