r"""Continuing a cut-off answer: code versus explanation.

A reply cut off by the output limit ends with INCOMPLETE_RESPONSE_NOTICE, and
the user says "continue". The application decides between two paths:

- code_continuation, only when the answer stopped inside code (an open code
  fence, an unfinished unfenced HTML document, or unfenced code); it keeps
  its exact-remainder, one-fenced-block contract;
- an explanation continuation otherwise (prose, tables or mathematics, even
  when complete code examples appeared earlier). It takes the normal
  followup / V31 path with instructions to continue in prose, never in a code
  fence, and to restate an interrupted math expression from its opening
  delimiter.

Code evidence is syntax ("def f(", "const x =", an open ```python fence), not
vocabulary: "A function is a rule" or a fenced equation is not code.

The model and Tavily are fakes; every other network destination is refused.
"""

from __future__ import annotations

import json
from pathlib import Path
import socket
import sys
from types import SimpleNamespace

import pytest

CANDIDATE_DIR = Path(__file__).resolve().parents[1]
if str(CANDIDATE_DIR) not in sys.path:
    sys.path.insert(0, str(CANDIDATE_DIR))

import app_fastapi_candidate as app


FIXTURES = Path(__file__).resolve().parent / "fixtures"
CALCULUS_REQUEST = "I need you to teach me calculus in a way that anybody can understand"
CALCULUS_CUT = (FIXTURES / "calculus_cutoff.md").read_text(encoding="utf-8").rstrip()
WEATHER = "tell me the weather for terre haute indiana"
CONTINUATION = (
    r"\[ \text{acceleration} \xrightarrow{\text{integral}} \text{velocity} "
    r"\xrightarrow{\text{integral}} \text{position} \]" "\n\nThis means:\n\n- Integrating velocity gives position."
)


def cut_off(text):
    """A stored reply exactly as chat() returns it after an output-limit cut."""
    return app.mark_incomplete_reply(text)


def history_for(answer, request="Explain this."):
    return [{"role": "user", "content": request}, {"role": "assistant", "content": answer}]


CALCULUS_HISTORY = history_for(cut_off(CALCULUS_CUT), CALCULUS_REQUEST)

# Answers cut off outside code.
EXPLANATIONS = {
    "calculus display math": CALCULUS_CUT,
    "prose with function": "A function is a rule that maps inputs to outputs. Every function has a domain and",
    "inline math": r"The slope of the tangent line is \(f'(x)=2",
    "fenced latex": "The identity is:\n\n```latex\n\\int_0^1 x\\,dx = \\tfrac{1}{2}\n```\n\nNext, consider the",
    "unlabeled fenced math": "We get:\n\n```\n\\frac{d}{dx}(x^2)=2x\n```\n\nThis shows that",
    "mixed example then prose": (
        "Here is an example:\n\n```python\ndef square(x):\n    return x * x\n```\n\n"
        "The function above squares its input. In calculus, the derivative of"
    ),
    "closed HTML example then prose": (
        "An HTML skeleton:\n\n```html\n<html>\n<body>\n```\n\n"
        "Now consider the derivative of"
    ),
    "plain fenced equation": "```\nx^2 + y^2 = z^2\n```\n\nNext consider the",
    "table": "| Function | Derivative |\n|---|---|\n| x^2 | 2x |\n| x^3 |",
}
# Answers cut off inside code.
CODE = {
    "python fence": "Here is the script:\n\n```python\ndef add(a, b):\n    return a +",
    "javascript fence": "```javascript\nconst total = items.reduce((sum, item) => {\n  return sum +",
    "html fence": "```html\n<!doctype html>\n<html>\n<body>\n  <div class=\"card\">",
    "unfenced html": "<!doctype html>\n<html>\n<head><title>Demo</title></head>\n<body>\n<p>Hello",
    "mixed explanation then open code": (
        "The derivative measures change. Here is a numerical version:\n\n"
        "```python\ndef derivative(f, x, h=1e-6):\n    return (f(x + h) -"
    ),
}


@pytest.fixture
def harness(monkeypatch):
    """Fake native model and legacy model that record what they receive, a
    fake Tavily, and a socket guard."""
    seen = {"instructions": [], "native_inputs": [], "legacy": [], "searches": [], "network": [],
            "native_reply": "[native reply]", "legacy_reply": "[legacy reply]", "search_query": None}

    def fake_native(input_items, instructions):
        seen["instructions"].append(instructions)
        seen["native_inputs"].append(input_items)
        searched = any(isinstance(i, dict) and i.get("type") == "function_call_output" for i in input_items)
        if seen["search_query"] and not searched:
            return {"output": [{"type": "function_call", "name": "search_web", "call_id": "s1",
                                "arguments": json.dumps({"query": seen["search_query"]})}]}
        return {"output": [{"type": "message", "role": "assistant",
                            "content": [{"type": "output_text", "text": seen["native_reply"]}]}]}

    def fake_legacy(messages, max_tokens=None, **kwargs):
        seen["legacy"].append(messages)
        return SimpleNamespace(content=seen["legacy_reply"], incomplete=False, incomplete_reason=None)

    def fake_search(query, include_domains=None):
        seen["searches"].append(query)
        return "ok", [{"title": "Weather in Terre Haute", "url": "https://example.com/weather",
                       "published": "2026-10-10", "content": "Overcast, 62F."}]

    def refuse(name):
        def _refuse(*args, **kwargs):
            raise AssertionError(f"{name} must not be called")
        return _refuse

    real_connect = socket.socket.connect

    def guarded_connect(sock, address):
        host = str(address[0] if isinstance(address, tuple) else address)
        if host in ("127.0.0.1", "::1", "localhost") or host.startswith("127."):
            return real_connect(sock, address)
        seen["network"].append(host)
        raise AssertionError("no outbound network is allowed")

    monkeypatch.setattr(app, "_invoke_openai_native_tools", fake_native)
    monkeypatch.setattr(app, "invoke_llm", fake_legacy)
    monkeypatch.setattr(app, "run_web_search", fake_search)
    for name in ("_post_tavily_for_attempt", "_invoke_openai", "_post_openai_for_attempt"):
        monkeypatch.setattr(app, name, refuse(name))
    monkeypatch.setattr(app.urllib.request, "urlopen", refuse("urlopen"))
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", True)
    monkeypatch.setattr(app, "SESSION_STATE", type(app.SESSION_STATE)())
    return seen


def legacy_prompt(seen):
    return "\n".join(str(getattr(m, "content", m)) for m in seen["legacy"][-1])


# --- code evidence --------------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "A function is a rule that turns an input into an output.",
    "Every function has a domain. Let x be a real number; const values do not change.",
    "```latex\n\\frac{d}{dx}(x^2)=2x\n```",
    "```\n\\int_0^3 x^2\\,dx = 9\n```",
    "```text\nprint this page\n```",
    "```\nx^2 + y^2 = z^2\n```",
    CALCULUS_CUT,
])
def test_prose_and_fenced_math_are_not_code(text):
    assert not app.previous_answer_looks_like_code(text)


@pytest.mark.parametrize("text", [
    "```python\nprint('hi')\n```",
    "```\nfor i in range(3):\n    pass\n```",                 # unlabeled fence without LaTeX
    "```js\nconst x = 1;\n```",
    "def add(a, b):\n    return a + b",
    "function greet(name) {\n  return name;\n}",
    "const total = 0;",
    "<!doctype html>\n<html></html>",
    "body {\n  margin: 0;\n}",
    "```\nf = lambda x: x ** 2  # \\frac is not used here\nprint(f(2))\n```",
])
def test_code_syntax_is_code(text):
    assert app.previous_answer_looks_like_code(text)


# --- which continuation ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", EXPLANATIONS)
def test_explanations_cut_off_outside_code_continue_as_explanations(name):
    history = history_for(cut_off(EXPLANATIONS[name]))

    assert not app.previous_code_answer_looks_incomplete(history[-1]["content"])
    assert app.is_explanation_continuation_request("continue", history)
    assert app.classify_request("continue", history) != "code_continuation"


@pytest.mark.parametrize("name", CODE)
def test_code_cut_off_inside_code_still_continues_as_code(name):
    history = history_for(cut_off(CODE[name]), "Write it.")

    assert app.previous_code_answer_looks_incomplete(history[-1]["content"])
    assert not app.is_explanation_continuation_request("continue", history)
    assert app.classify_request("continue", history) == "code_continuation"


@pytest.mark.parametrize("name", CODE)
def test_code_cut_off_without_the_notice_still_continues_as_code(name):
    history = history_for(CODE[name], "Write it.")
    assert app.classify_request("continue", history) == "code_continuation"


def test_the_code_continuation_contract_is_unchanged():
    messages = app.build_messages("continue", history_for(cut_off(CODE["python fence"]), "Write it."),
                                  "code_continuation", [])
    prompt = "\n".join(str(m.content) for m in messages)

    assert "Return exactly one fenced code block and nothing else." in prompt
    assert "def add(a, b):" in prompt
    assert app.INCOMPLETE_RESPONSE_NOTICE not in prompt


def test_continue_without_a_cut_off_answer_is_not_an_explanation_continuation():
    history = history_for("Calculus studies change and accumulation.")
    assert not app.is_explanation_continuation_request("continue", history)


def test_a_new_question_is_not_a_continuation():
    assert not app.is_explanation_continuation_request(WEATHER, CALCULUS_HISTORY)


# --- the supplied calculus conversation -----------------------------------------------------------


def test_the_calculus_continue_takes_the_native_path_with_prose_continuation_rules(harness):
    reply = app.chat("continue", CALCULUS_HISTORY, session_id="calculus-native")

    assert app.classify_request("continue", CALCULUS_HISTORY) in app.V31_NATIVE_TOOL_ROUTES
    assert reply == "[native reply]" and harness["legacy"] == []
    instructions = harness["instructions"][-1]
    assert app.EXPLANATION_CONTINUATION_RULES in instructions
    assert "not a code block" in instructions and "from its opening delimiter" in instructions
    # The model sees the original unfinished answer, exactly as delivered.
    assert any(item.get("content") == CALCULUS_HISTORY[-1]["content"]
               for item in harness["native_inputs"][-1] if isinstance(item, dict))
    assert harness["searches"] == [] and harness["network"] == []


def test_a_prose_continuation_wrapped_in_a_markdown_fence_is_unwrapped(harness):
    harness["native_reply"] = "```markdown\n" + CONTINUATION + "\n```"
    reply = app.chat("continue", CALCULUS_HISTORY, session_id="calculus-unwrap")

    assert not reply.lstrip().startswith("```")
    assert reply.startswith(r"\[ \text{acceleration}")


def test_the_calculus_continue_on_the_legacy_path_gets_the_same_rules(harness, monkeypatch):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", False)
    reply = app.chat("continue", CALCULUS_HISTORY, session_id="calculus-legacy")

    assert app.classify_request("continue", CALCULUS_HISTORY) == "followup"
    assert reply == "[legacy reply]"
    prompt = legacy_prompt(harness)
    assert app.EXPLANATION_CONTINUATION_RULES in prompt
    assert r"\[ \text{acceleration} \xrightarrow{\text{integral}}" + " \\" in prompt   # unfinished content kept
    assert "Return exactly one fenced code block" not in prompt


def test_rules_are_not_added_to_ordinary_requests(harness, monkeypatch):
    app.chat("What is a derivative?", [], session_id="ordinary-native")
    assert app.EXPLANATION_CONTINUATION_RULES not in harness["instructions"][-1]

    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", False)
    app.chat("why", history_for("Derivatives measure change."), session_id="ordinary-legacy")
    assert app.EXPLANATION_CONTINUATION_RULES not in legacy_prompt(harness)


def test_a_weather_question_after_the_continuation_switches_topic_and_searches(harness):
    history = CALCULUS_HISTORY + [{"role": "user", "content": "continue"},
                                  {"role": "assistant", "content": CONTINUATION}]
    harness["search_query"] = "weather Terre Haute Indiana"
    reply = app.chat(WEATHER, history, session_id="weather-after")

    assert not app.is_explanation_continuation_request(WEATHER, history)
    assert app.classify_request(WEATHER, history) != "code_continuation"
    assert app.EXPLANATION_CONTINUATION_RULES not in harness["instructions"][-1]
    assert harness["searches"] == ["weather Terre Haute Indiana"]
    assert "https://example.com/weather" in reply


@pytest.mark.parametrize("native", [True, False], ids=["native", "legacy"])
@pytest.mark.parametrize("signal", ["continue", "keep going", "send the rest", "you cut off"])
def test_explanation_signals_retain_context_and_rules(harness, monkeypatch, native, signal):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    assert app.classify_request(signal, CALCULUS_HISTORY) == "followup"
    app.chat(signal, CALCULUS_HISTORY, session_id="explanation-signals")
    prompt = harness["instructions"][-1] if native else legacy_prompt(harness)
    assert app.EXPLANATION_CONTINUATION_RULES in prompt
    assert "Do not invent missing mathematics" in prompt
    if not native:
        assert CALCULUS_REQUEST in prompt and r"\text{acceleration}" in prompt
    assert harness["network"] == []


@pytest.mark.parametrize("native", [True, False], ids=["native", "legacy"])
def test_explanation_unwrapping_preserves_actual_code(harness, monkeypatch, native):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    reply = "A numerical example:\n\n```python\ndef square(x):\n    return x * x\n```"
    harness["native_reply"] = harness["legacy_reply"] = reply
    assert app.chat("continue", CALCULUS_HISTORY, session_id="mixed-continuation") == reply


@pytest.mark.parametrize("native", [True, False], ids=["native", "legacy"])
def test_whole_prose_fence_is_unwrapped_on_both_paths(harness, monkeypatch, native):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    harness["native_reply"] = harness["legacy_reply"] = "```markdown\n" + CONTINUATION + "\n```"
    assert app.chat("continue", CALCULUS_HISTORY, session_id="unwrap-both") == CONTINUATION


@pytest.mark.parametrize("label", ["python", "javascript", "html"])
@pytest.mark.parametrize("notice", [False, True])
def test_code_cutoff_in_the_fence_header_retains_code_intent(label, notice):
    answer = "```" + label
    if notice:
        answer = cut_off(answer)
    history = history_for(answer, "Write it.")
    assert app.previous_answer_looks_like_code(answer)
    assert app.classify_request("continue", history) == "code_continuation"


@pytest.mark.parametrize("label", ["latex", "math"])
def test_math_fence_header_does_not_establish_code_intent(label):
    history = history_for(cut_off("```" + label))
    assert app.classify_request("continue", history) == "followup"


# Real preceding requests distinguish programming assignments from equations.
SYNTAX_CONTINUATIONS = [
    ("html mention in prose", "Explain HTML document structure in plain English.",
     "The <html> element is the root element of a web page.", "followup"),
    ("python xor", "Write a Python example that combines two bit masks with XOR.",
     "```\nmask = 1 ^ 2", "code_continuation"),
    ("python windows path", "Write Python code that sets a raw Windows filename string.",
     '```\npath = r"C:\\Users\\kalil\\notes.txt"', "code_continuation"),
    ("inline html mention", "Explain the root HTML element.",
     "The `<html>` element wraps the document. Its children are", "followup"),
    ("html tag sentence at line start", "Explain the root HTML tag in prose.",
     "<html> is the root element of a web page. Its purpose is", "followup"),
    ("doctype mentioned in prose", "Explain the HTML doctype declaration.",
     "<!doctype html> tells the browser which document mode to use. Next,", "followup"),
    ("different xor assignment", "Write a Python snippet that updates a permissions bitmask.",
     "```\npermissions ^= new_flags", "code_continuation"),
    ("different raw string", "Write Python code that stores a Windows directory.",
     '```\nfolder = r"D:\\Projects\\drafts"', "code_continuation"),
    ("unfinished string", "Write Python code that stores a raw Windows directory string.",
     '```\nfolder = r"D:\\Projects\\', "code_continuation"),
    ("unfinished xor assignment", "Write Python code that combines flag bits.",
     "```\nflags = first ^", "code_continuation"),
    ("mathematical function prose", "Teach me calculus in plain English.",
     "Function f(x) maps inputs to outputs. Its derivative gives", "followup"),
    ("unlabeled power equation", "Explain the Pythagorean equation in plain English.",
     "```\nx^2 + y^2 = z^2", "followup"),
    ("unlabeled numeric equation", "Explain why this arithmetic identity is true.",
     "```\n2 + 2 = 4", "followup"),
    ("ambiguous equation with math context", "Explain this algebra equation and its graph.",
     "```\ny = x^2", "followup"),
    ("same syntax with python context", "Write Python code for a bitwise operation.",
     "```\ny = x^2", "code_continuation"),
    ("unlabeled latex", "Explain the integral notation used in calculus.",
     "```\n\\int_0^1 x\\,dx = \\frac{1}{2}", "followup"),
    ("completed xor then prose", "Explain this Python bitwise example.",
     "```\nmask = 1 ^ 2\n```\n\nThis combines the flags. The <html> element is unrelated to", "followup"),
    ("completed html then prose", "Explain an HTML skeleton in plain English.",
     "```html\n<html>\n<body>\n```\n\nThe document structure shows that", "followup"),
    ("completed unfenced html then prose", "Explain this complete HTML document.",
     "<!doctype html>\n<html><body>Done.</body></html>\n\nThe document illustrates", "followup"),
    ("genuine unfinished html", "Write a complete HTML page with a greeting.",
     '<!doctype html>\n<html lang="en">\n<body>\n<p>Hello', "code_continuation"),
    ("unfinished root attributes", "Write an HTML document with language metadata.",
     '<html lang="en', "code_continuation"),
    ("root followed by html structure", "Write an HTML page.",
     "<html>\n<head><title>Example</title></head>\n<body>", "code_continuation"),
    ("doctype header cutoff", "Write an HTML document.",
     "<!doctype html>", "code_continuation"),
    ("python labeled fence", "Write Python code that updates a bit mask.",
     "```python\nmask = 1 ^", "code_continuation"),
    ("code fence header cutoff", "Write a JavaScript program.",
     "```javascript", "code_continuation"),
    ("tag relationships in prose", "Explain the HTML root tag in prose.",
     "<html> contains <head> and <body>. This means", "followup"),
    ("html text-only document", "Write an HTML document containing a greeting.",
     "<html>Hello", "code_continuation"),
    ("cutoff html comment", "Write an HTML document with a comment before its content.",
     "<html><!-- introductory", "code_continuation"),
    ("html structure after a comment", "Write a complete HTML document.",
     "<html>\n<!-- Intro -->\n<head><title>Example", "code_continuation"),
    ("ordinary controlword", "Explain the everyday English word pass.",
     "pass", "followup"),
    ("quoted explanation", "Give me a short greeting in everyday English.",
     '\"Hello there\"', "followup"),
    ("program import in a math lesson", "Teach calculus with a numerical example.",
     "```\nimport math\nslope = 1 ^ 2", "code_continuation"),
    ("program mutation in a math lesson", "Teach algebra with a numerical example.",
     "```\nflags ^= new_flags", "code_continuation"),
    ("unfinished program mutation in a math lesson", "Teach algebra with a numerical example.",
     "```\nflags ^=", "code_continuation"),
    ('unicode equation with math context', 'Explain the algebra equation using Greek variables.', '```\nα = β ^ 2', 'followup'),
    ('unicode assignment with python context', 'Write Python code using Greek variable names.', '```\nα = β ^ 2', 'code_continuation'),
    ('unicode unfinished string assignment', 'Write Python code that stores a Windows directory string.', '```\nν = r"D:\\Projects\\', 'code_continuation'),
]


@pytest.mark.parametrize("native", [False, True], ids=["native-off", "native-on"])
@pytest.mark.parametrize("name, initial_request, fragment, expected", SYNTAX_CONTINUATIONS,
                         ids=[case[0] for case in SYNTAX_CONTINUATIONS])
def test_continue_uses_syntax_and_request_context_through_real_pipeline(
    harness, monkeypatch, native, name, initial_request, fragment, expected,
):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    history = history_for(cut_off(fragment), initial_request)
    route = app.classify_request("continue", history)
    assert route == expected
    messages = app.build_messages("continue", history, route, [])
    prompt = "\n".join(m.content for m in messages)
    code_rule = "Return exactly one fenced code block and nothing else."
    if expected == "code_continuation":
        assert code_rule in prompt
        assert app.EXPLANATION_CONTINUATION_RULES not in prompt
        assert fragment in prompt and app.INCOMPLETE_RESPONSE_NOTICE not in prompt
        harness["legacy_reply"] = "```python\n# remaining code\n```"
    else:
        assert app.EXPLANATION_CONTINUATION_RULES in prompt
        assert code_rule not in prompt
        assert initial_request in prompt and fragment in prompt
        harness["legacy_reply"] = harness["native_reply"] = "The remaining explanation stays on topic."
    app.chat("continue", history, session_id="syntax-continuation")
    if native and expected == "followup":
        assert harness["native_inputs"] and not harness["legacy"]
        assert app.EXPLANATION_CONTINUATION_RULES in harness["instructions"][-1]
        assert any(i.get("content") == history[-1]["content"] for i in harness["native_inputs"][-1])
    else:
        assert harness["legacy"] and not harness["native_inputs"]
        sent = legacy_prompt(harness)
        assert (code_rule in sent) == (expected == "code_continuation")
        assert (app.EXPLANATION_CONTINUATION_RULES in sent) == (expected == "followup")
    assert harness["searches"] == [] and harness["network"] == []
