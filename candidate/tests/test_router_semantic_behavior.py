"""Offline behavioral regressions for semantic routing and self-knowledge.

The shared conftest blocks external networking. Both model paths are mocked;
these tests check what reaches the model and what the application returns.
"""
import json
from types import SimpleNamespace

import pytest
import app_fastapi_candidate as app


@pytest.fixture
def models(monkeypatch):
    seen = {"native": [], "legacy": [], "reply": "A normal explanation."}
    monkeypatch.setattr(app, "SESSION_STATE", type(app.SESSION_STATE)())
    def native(items, instructions):
        seen["native"].append((items, instructions))
        return {"output": [{"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": seen["reply"]}]}]}
    def legacy(messages, max_tokens=None, **kwargs):
        seen["legacy"].append((messages, max_tokens))
        return SimpleNamespace(content=seen["reply"], incomplete=False, incomplete_reason=None)
    monkeypatch.setattr(app, "_invoke_openai_native_tools", native)
    monkeypatch.setattr(app, "invoke_llm", legacy)
    def refuse(*args, **kwargs):
        raise AssertionError("Unmocked provider/search request")
    for name in ("run_web_search", "_post_openai_for_attempt", "_post_tavily_for_attempt"):
        monkeypatch.setattr(app, name, refuse)
    return seen


PROSE = [
    "Show me how to prepare for a job interview.",
    "Give me an example of compound interest.",
    "Make a table of monthly expenses.",
    "Write an example of a professional email.",
    "Create a function for y = x^2 and explain its derivative.",
]


@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
@pytest.mark.parametrize("message", PROSE)
def test_ordinary_requests_reach_the_model_without_code_only_instructions(models, monkeypatch, native, message):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    models["reply"] = "The explanation stays in prose and includes **useful context**."
    assert app.classify_request(message, []) not in {"code", "revision", "code_continuation"}
    assert app.chat(message, [], session_id="ordinary") == models["reply"]
    if native:
        assert models["native"] and not models["legacy"]
        prompt = models["native"][-1][1]
    else:
        assert models["legacy"] and not models["native"]
        prompt = "\n".join(m.content for m in models["legacy"][-1][0])
    assert "Return exactly one fenced code block" not in prompt
    assert "ABSOLUTE OUTPUT RULE" not in prompt


@pytest.mark.parametrize("text", [
    "Function f(x) maps inputs to outputs.",
    "Function f(x) = x^2 has a derivative.",
    "```\n2 + 2 = 4\n```",
])
def test_math_syntax_and_function_notation_do_not_establish_code(text):
    assert not app.previous_answer_looks_like_code(text)


@pytest.mark.parametrize("text", [
    "function greet(name) {\n  return name;\n}",
    "function greet(name,",
    "function greet(name)",
    "```python\nx = 2\n```",
    "```javascript\nconst result = x ^ 2;\n```",
])
def test_real_code_remains_code(text):
    assert app.previous_answer_looks_like_code(text)


@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
def test_pasted_notes_are_used_instead_of_a_file_access_dead_end(models, monkeypatch, native):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    message = "Summarize my notes:\nDerivatives measure change. Integrals measure accumulated totals."
    assert app.classify_request(message, []) != "file_unavailable"
    assert app.chat(message, [], session_id="pasted-notes") == models["reply"]
    items = models["native"][-1][0] if native else models["legacy"][-1][0]
    assert "Derivatives measure change" in str(items)


def test_a_file_that_was_not_pasted_still_gets_the_capability_boundary(models, monkeypatch):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", True)
    reply = app.chat("Summarize my notes", [], session_id="missing-notes")
    assert "doesn't have access" in reply
    assert not models["native"] and not models["legacy"]


@pytest.mark.parametrize("enabled", [False, True])
def test_runtime_facts_report_active_routing_and_actual_limits(monkeypatch, enabled):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", enabled)
    monkeypatch.setattr(app, "OPENAI_MODEL", "test-current-model")
    monkeypatch.setattr(app, "OPENAI_REASONING_EFFORT", "test-effort")
    facts = app._v31_runtime_facts()
    assert facts["configured_primary"]["model"] == "test-current-model"
    assert facts["configured_primary"]["reasoning_effort"] == "test-effort"
    handling = facts["request_handling"]
    assert handling["native_tool_routing_enabled"] is enabled
    assert handling["active_mode"] == ("native_tools" if enabled else "legacy")
    assert handling["native_tool_path"]["enabled"] is enabled
    assert facts["limits"]["message_characters"] == app.MAX_INPUT_CHARS
    assert facts["limits"]["default_output_tokens"] == app.MAX_RESPONSE_TOKENS
    assert facts["limits"]["code_output_tokens"] == app.CODE_RESPONSE_TOKENS
    assert facts["limits"]["history_turns"] == app.MAX_HISTORY_TURNS
    assert facts["configuration_provenance"]["scope"] == "current application process"
    serialized = json.dumps(facts).lower()
    assert "api_key" not in serialized and "authorization" not in serialized


@pytest.mark.parametrize("enabled", [False, True])
def test_all_self_knowledge_surfaces_distinguish_enabled_and_optional_paths(monkeypatch, enabled):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", enabled)
    expected = "Native model tool routing is " + ("enabled" if enabled else "disabled")
    assert expected in app.render_kalillac_facts()
    assert expected in app.kalillac_ascii_diagram()
    family, reply = app.get_canonical_self_knowledge_response("How does Kalillac AI work?")
    assert family == "how_it_works" and expected in reply
    assert "origin HTTPS :443" in app.kalillac_ascii_diagram()


@pytest.mark.parametrize("message, history", [
    ("I got a ValueError in Python. Explain what it means.", []),
    ("Create a truth table for XNOR and explain it.", []),
    ("What changed between those code examples?", [
        {"role": "user", "content": "Write a Python example"},
        {"role": "assistant", "content": "```python\nprint(1)\n```"}]),
])
def test_more_semantic_requests_reach_native_model_with_context(models, monkeypatch, message, history):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", True)
    app.chat(message, history, session_id="semantic")
    assert models["native"] and not models["legacy"]
    items, instructions = models["native"][-1]
    assert items[-1]["content"] == message
    assert "Return exactly one fenced code block" not in instructions
    assert "Native model tool routing is enabled" in instructions
    if history:
        assert any(i.get("content") == history[-1]["content"] for i in items)


def test_existing_code_budget_and_exact_continuation_path_are_preserved(models, monkeypatch):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", True)
    models["reply"] = "```python\nprint('hello')\n```"
    app.chat("Write Python code to print hello", [], session_id="code-budget")
    assert not models["native"]
    assert models["legacy"][-1][1] == app.CODE_RESPONSE_TOKENS
    history = [{"role": "user", "content": "Write Python code"},
               {"role": "assistant", "content": app.mark_incomplete_reply("```python\ndef add(a, b):\n    return a +")}]
    models["reply"] = "```python\nb\n```"
    app.chat("continue", history, session_id="code-remainder")
    assert not models["native"]
    assert models["legacy"][-1][1] == app.CODE_CONTINUATION_RESPONSE_TOKENS


@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
def test_precise_model_self_knowledge_uses_current_facts_without_model_generation(models, monkeypatch, native):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    monkeypatch.setattr(app, "OPENAI_MODEL", "test-current-model")
    monkeypatch.setattr(app, "OPENAI_REASONING_EFFORT", "test-effort")
    reply = app.chat("What model do you use?", [], session_id="factual-model")
    assert "test-current-model" in reply and "test-effort" in reply
    assert "no automatic fallback" in reply
    assert "gpt-5.6-luna" not in reply
    assert not models["native"] and not models["legacy"]


@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
def test_factual_shortcuts_do_not_discard_a_separate_requested_task(models, monkeypatch, native):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    message = "What model do you use and write a poem about calculus?"
    assert app.get_canonical_self_knowledge_response(message) == (None, None)
    assert app.chat(message, [], session_id="mixed-self-task") == models["reply"]
    if native:
        assert models["native"][-1][0][-1]["content"] == message
    else:
        assert message in "\n".join(m.content for m in models["legacy"][-1][0])


def test_runtime_facts_include_recorded_network_and_session_facts_with_provenance():
    facts = app._v31_runtime_facts()
    deployment = facts["recorded_deployment"]
    assert deployment["verified_live_this_request"] is False
    assert deployment["network"] == app.KALILLAC_SELF_KNOWLEDGE["network"]
    assert facts["session_state"] == app.KALILLAC_SELF_KNOWLEDGE["session_state"]
    assert "legacy_self_knowledge_output_tokens" in facts["limits"]
    assert "self_knowledge_output_tokens" not in facts["limits"]



def test_code_history_outside_the_native_window_keeps_its_existing_context(models, monkeypatch):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", True)
    original = "```python\nprint('earlier version')\n```"
    history = [{"role": "user", "content": "Write Python code"},
               {"role": "assistant", "content": original}]
    for n in range(5):
        history.extend([{"role": "user", "content": f"Explain topic {n}"},
                        {"role": "assistant", "content": "A prose explanation."}])
    app.chat("What changed between those code examples?", history, session_id="earlier-code")
    assert not models["native"] and models["legacy"]
    assert original in "\n".join(m.content for m in models["legacy"][-1][0])


def test_configuration_grounding_does_not_replace_the_code_system_policy():
    messages = app.build_messages("Write Python code", [], "code", [])
    assert messages[0].content == app.CODE_SYSTEM_PROMPT
    messages = app.build_messages("Explain a derivative", [], "general", [])
    assert messages[0].content == app.SYSTEM_PROMPT
    messages = app.build_messages("Explain your architecture", [], "self_knowledge", [])
    assert messages[0].content == app.SYSTEM_PROMPT
    assert "CURRENT APPLICATION CONFIGURATION:" in messages[1].content
    assert "Native model tool routing is" in messages[1].content
