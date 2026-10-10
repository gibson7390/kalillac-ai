"""Offline quantitative grounding, weather timestamp, and source ownership regressions.

Script the actual Responses API seam; retain the real classifier, prompt
builders, native tool loop, and final reply formatting. These tests verify
instructions/evidence reach the provider and deterministic formatting, not
real model reasoning accuracy.
"""
from __future__ import annotations

import copy
import json
from fractions import Fraction

import pytest

import app_fastapi_candidate as app


WEATHER_QUERY = "Search the web for weather right now in Terre Haute, Indiana."
SEARCH_QUERY = "weather Terre Haute Indiana"
WEATHER_RESULTS = [
    {"title": "WeatherAPI", "url": "https://weatherapi.test/current", "published": "2026-10-10",
     "content": json.dumps({"location": {"localtime": "2026-10-10 18:46", "tz_id": "America/Indiana/Indianapolis"},
                            "current": {"last_updated": "2026-10-10 18:30", "temp_f": 72,
                                        "condition": "overcast", "wind_dir": "SSE", "wind_mph": 9.8}})},
    {"title": "NWS", "url": "https://nws.test/observation", "published": "2026-10-10",
     "content": "Observation: 2026-10-10 08:53 EDT, 55 F, NE 3 mph. Forecast updated 09:45 EDT."},
    {"title": "Ventusky", "url": "https://ventusky.test/terre-haute", "published": None,
     "content": "Morning observation at 08:53 EDT: 55 F, NE 3 mph."},
    {"title": "Almanac", "url": "https://almanac.test/monthly", "published": None,
     "content": "Long-range monthly conditions for October; not a current observation."},
]


def _response(text):
    return {"status": "completed", "output": [{"type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": text}]}]}


@pytest.fixture
def pipeline(monkeypatch):
    state = {"text": "Supported answer.", "search": False, "results": copy.deepcopy(WEATHER_RESULTS),
             "payloads": [], "searches": []}

    def fake_post(payload, timeout=90):
        state["payloads"].append(copy.deepcopy(payload))
        outputs = [item for item in payload["input"] if item.get("type") == "function_call_output"]
        if payload.get("tools") and state["search"] and not outputs:
            return {"output": [{"type": "function_call", "name": "search_web", "call_id": "weather-1",
                                "arguments": json.dumps({"query": SEARCH_QUERY})}]}
        return _response(state["text"])

    def fake_search(query, include_domains=None):
        state["searches"].append((query, include_domains))
        return "ok", copy.deepcopy(state["results"])

    def refuse(*args, **kwargs):
        raise AssertionError("Only scripted providers may run")

    monkeypatch.setattr(app, "OPENAI_API_KEY", "offline-test-key")
    monkeypatch.setattr(app, "_request_limits", None)
    monkeypatch.setattr(app, "_post_openai_responses", fake_post)
    monkeypatch.setattr(app, "run_web_search", fake_search)
    monkeypatch.setattr(app.urllib.request, "urlopen", refuse)
    monkeypatch.setattr(app, "SESSION_STATE", type(app.SESSION_STATE)())
    return state


def _provider_text(state):
    payload = state["payloads"][-1]
    return payload.get("instructions", "") + "\n" + "\n".join(
        item.get("content", "") for item in payload["input"] if isinstance(item.get("content", ""), str)
    )


def _expected_source_list(results):
    return "**Sources**\n\n" + "\n".join(f"- [{item['title']}]({item['url']})" for item in results)


def _weather_chat(state, monkeypatch, native):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    state["search"] = True
    assert app.classify_request(WEATHER_QUERY, []) == "web_search"
    result = app.chat(WEATHER_QUERY, [], session_id="weather-grounding-offline")
    assert len(state["searches"]) == 1
    assert len(state["payloads"]) == (2 if native else 1)
    assert all(payload["store"] is False for payload in state["payloads"])
    assert all(payload["model"] == app.OPENAI_MODEL for payload in state["payloads"])
    assert all(payload["reasoning"]["effort"] == app.OPENAI_REASONING_EFFORT for payload in state["payloads"])
    return result


@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
@pytest.mark.parametrize("case", ["unspecified-scale", "signed-velocity", "explicit-hypothetical-scale"])
def test_quantitative_grounding_reaches_actual_provider_prompt(pipeline, monkeypatch, native, case):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    if case == "unspecified-scale":
        task = "Teach calculus using the profit function P(x) = -2x^2 + 40x - 100."
        cutoff = "The profit function is \\[P(x)=-2x^2+40x-100\\]. The maximum occurs at"
        vertex = -Fraction(40, 2 * -2)
        assert vertex == 10 and -2 * vertex ** 2 + 40 * vertex - 100 == 100
        answer = r"The maximum is at \(x=10\), with \(P(10)=100\). Physical units and any scale factor are unspecified."
    elif case == "signed-velocity":
        task = "Explain displacement and total distance for velocity v(t)=t-1 over [0,2]."
        cutoff = "The velocity changes sign at t=1. Integrating over the interval gives"
        primitive = lambda t: Fraction(t * t, 2) - t
        assert primitive(2) - primitive(0) == 0
        assert -(primitive(1) - primitive(0)) + primitive(2) - primitive(1) == 1
        answer = r"Displacement is \(\int_0^2(t-1)\,dt=0\); total distance is \(\int_0^2|t-1|\,dt=1\)."
    else:
        task = "For a separate hypothetical example, assume x counts hundreds of products and maximize P(x)=-2x^2+40x-100."
        cutoff = "In this hypothetical example, the maximum is at"
        answer = "Under the explicitly stated hypothetical scale, x=10 represents 1,000 products; P(10)=100."
    history = [{"role": "user", "content": task},
               {"role": "assistant", "content": app.mark_incomplete_reply(cutoff)}]
    pipeline["text"] = answer

    result = app.chat("continue", history, session_id="quantitative-continuation-offline")

    assert result == answer
    prompt = _provider_text(pipeline)
    assert task in prompt and cutoff in prompt
    assert "Do not invent units, scale factors, or conversions" in prompt
    assert "physical units remain unspecified" in prompt
    assert "Clearly introduced hypothetical examples are allowed" in prompt
    assert "signed displacement" in prompt
    assert "total distance" in prompt and r"\int_a^b |v(t)|\,dt" in prompt
    assert "changes sign" in prompt
    assert len(pipeline["payloads"]) == 1 and pipeline["searches"] == []


@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
@pytest.mark.parametrize("freshness", ["mixed-times", "missing-time", "older-observation"])
def test_weather_time_evidence_and_guidance_reach_actual_provider(pipeline, monkeypatch, native, freshness):
    if freshness == "mixed-times":
        answer = "WeatherAPI reports 72 F, overcast, SSE 9.8 mph, updated October 10 at 18:30 America/Indiana/Indianapolis. Its local clock reading 18:46 is not the update time. NWS's 55 F, NE 3 mph observation was at 08:53 EDT; its forecast update was 09:45. Those morning readings describe a different time. The Almanac monthly outlook is not a current measurement."
    elif freshness == "missing-time":
        pipeline["results"] = [{"title": "Weather report", "url": "https://weather.test/report",
                                "published": "2026-10-10", "content": "72 F and overcast; observation/update time not supplied."}]
        answer = "The report lists 72 F and overcast. Its observation/update time was not supplied, so I cannot confirm how fresh that reading is."
    else:
        pipeline["results"] = [copy.deepcopy(WEATHER_RESULTS[1])]
        answer = "The available observation was 55 F with NE wind at 3 mph at 08:53 EDT. That is a morning reading; the forecast's 09:45 update does not make it a newer observation."
    pipeline["text"] = answer

    result = _weather_chat(pipeline, monkeypatch, native)

    prompt = _provider_text(pipeline)
    assert "observation/update time" in prompt
    assert "forecast update" in prompt and "publication" in prompt and "retrieval" in prompt
    assert "last_updated" in prompt and "location.localtime" in prompt
    assert "Do not combine differently timed observations" in prompt
    assert "long-range" in prompt and "timezone" in prompt
    assert "freshness is unavailable" in prompt and "useful supported information" in prompt
    assert result == answer + "\n\n" + _expected_source_list(pipeline["results"])
    if native:
        outputs = [json.loads(item["output"]) for item in pipeline["payloads"][-1]["input"]
                   if item.get("type") == "function_call_output"]
        assert outputs[0]["results"] == pipeline["results"]
    else:
        assert all(item["content"] in prompt for item in pipeline["results"])
    if freshness == "mixed-times":
        assert "18:30" in result and "18:46" in result and "08:53" in result and "09:45" in result


SOURCE_FOOTERS = [
    "Sources: [WeatherAPI] \u00b7 [NWS]",
    "Sources: [WeatherAPI](https://wrong.test/a) | [NWS](https://wrong.test/b)",
    "**Sources**\n\n- [Wrong source](https://wrong.test/a)",
    "**Sources:**\n- [Wrong source](https://wrong.test/a)",
    "### Sources\n\n1. [Wrong source](https://wrong.test/a)",
    "sources: https://wrong.test/a",
    "Sources: [WeatherAPI] \u00b7 [NWS]\n\n**Sources**\n\n- [Wrong source](https://wrong.test/a)",
]


@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
@pytest.mark.parametrize("footer", SOURCE_FOOTERS)
def test_only_application_source_footer_remains(pipeline, monkeypatch, native, footer):
    body = "The report lists 72 F and overcast, updated at 18:30 local time."
    pipeline["text"] = body + "\n\n" + footer

    result = _weather_chat(pipeline, monkeypatch, native)

    assert result == body + "\n\n" + _expected_source_list(pipeline["results"])
    assert "wrong.test" not in result
    assert _expected_source_list(pipeline["results"]) in result


PRESERVED_BODIES = [
    "Sources: the weather report and observation time are separate concepts.",
    "**Sources**\n\n100",
    "Sources: [0,2]",
    "Literal indented code:\n\n    Sources: [WeatherAPI] | [NWS]",
    "Literal indented code:\n\n\tSources: [WeatherAPI] | [NWS]",
    "\\[\nSources: [NWS]",
    "$$\nSources: [NWS]\n$$",
    "\\(\nSources: [NWS]",
    "**Sources**\n\n- [Link](https://literal.test)\n\n\\[P(10)=100\\]",
    "The phrase Sources: [WeatherAPI] is an example of a footer, not a timestamp.",
    "**Sources**\n\nThis paragraph explains how observations differ from forecasts.",
    r"The interval is \([0,2]\), and \(\int_0^2 |t-1|\,dt=1\).",
    "\\[\nSources: [0,2]\n\\]",
    "```python\nlabel = 'Sources: [WeatherAPI] | [NWS]'\n```",
    "```markdown\n**Sources**\n\n- [Literal](https://literal.test)\n```",
    "````markdown\n**Sources**\n\n- [Literal](https://literal.test)\n````",
    "~~~markdown\nSources: [WeatherAPI] | [NWS]\n~~~",
    "```markdown\n**Sources**\n\n- [Literal](https://literal.test)",
    "**Sources**\n\n- [Link](https://literal.test)\n\nThis is substantive prose after the example, not a source footer.",
]


@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
@pytest.mark.parametrize("body", PRESERVED_BODIES)
def test_footer_cleanup_preserves_prose_math_and_code(pipeline, monkeypatch, native, body):
    pipeline["text"] = body

    result = _weather_chat(pipeline, monkeypatch, native)

    assert result == body + "\n\n" + _expected_source_list(pipeline["results"])


@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
@pytest.mark.parametrize("body", [
    "Literal example:\n\n```markdown\n**Sources**\n\n- [Literal](https://literal.test)\n```",
    "The result is:\n\\[\nP(10)=100\n\\]",
])
def test_real_footer_after_fenced_source_example_is_removed(pipeline, monkeypatch, native, body):
    pipeline["text"] = body + "\n\nSources: [WeatherAPI] | [NWS]"

    result = _weather_chat(pipeline, monkeypatch, native)

    assert result == body + "\n\n" + _expected_source_list(pipeline["results"])


@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
def test_supplied_weather_evidence_gets_time_guidance_without_new_search(pipeline, monkeypatch, native):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    message = "Summarize this report: WeatherAPI last_updated 2026-10-10 18:30, location.localtime 18:46, timezone America/Indiana/Indianapolis, 72 F and overcast."
    pipeline["text"] = "The supplied report lists 72 F and overcast, updated at 18:30 America/Indiana/Indianapolis; 18:46 is the local clock time."
    assert app.classify_request(message, []) != "web_search"

    result = app.chat(message, [], session_id="supplied-weather-offline")

    prompt = _provider_text(pipeline)
    assert "observation/update time" in prompt and "location.localtime" in prompt
    assert "Do not combine differently timed observations" in prompt
    assert message in prompt
    assert result == pipeline["text"]
    assert len(pipeline["payloads"]) == 1 and pipeline["searches"] == []


# --- numeric results and literal delimiters must not change footer boundaries ---

NUMERIC_AFTER_CITATION = ["100", "0", "-3", "42.5", "1,000", "1."]
LITERAL_DELIMITER_BODIES = [
    r"Use `\[` to open display math.",
    r"Use ``\[ and `literal` `` to open display math.",
    r"Use ```\[ and ``literal`` ``` to open display math.",
    "Use ``\n\\[ and `literal`\n`` to open display math.",
    r"Use `\(` for inline math and `$$` for display math.",
    r"Use ``\[` unmatched shorter tick`` to open display math.",
]
SOURCE_CLEANUP_BOUNDARIES = [
    (
        "numeric-after-citation-" + number,
        "The maximum is x=10.\n\n**Sources**\n- [Reference](https://example.test)\n\n" + number,
        "The maximum is x=10.\n\n**Sources**\n- [Reference](https://example.test)\n\n" + number,
    ) for number in NUMERIC_AFTER_CITATION
] + [
    ("inline-code-" + str(index), body + "\n\nSources: [NWS](https://weather.test)", body)
    for index, body in enumerate(LITERAL_DELIMITER_BODIES)
]


@pytest.mark.parametrize("case,answer,expected", SOURCE_CLEANUP_BOUNDARIES,
                         ids=[case[0] for case in SOURCE_CLEANUP_BOUNDARIES])
def test_source_cleanup_numeric_and_inline_code_boundaries_direct(case, answer, expected):
    assert app.strip_model_source_footer(answer) == expected


@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
@pytest.mark.parametrize("case,answer,expected", SOURCE_CLEANUP_BOUNDARIES,
                         ids=[case[0] for case in SOURCE_CLEANUP_BOUNDARIES])
def test_source_cleanup_numeric_and_inline_code_boundaries_pipeline(
    pipeline, monkeypatch, native, case, answer, expected,
):
    pipeline["text"] = answer

    result = _weather_chat(pipeline, monkeypatch, native)

    assert result == expected + "\n\n" + _expected_source_list(pipeline["results"])


@pytest.mark.parametrize("prefix", ["1.", "2)", "100."])
def test_numbered_citation_prefix_remains_removable(prefix):
    answer = "Supported answer.\n\n**Sources**\n" + prefix + " [Reference](https://example.test)"
    assert app.strip_model_source_footer(answer) == "Supported answer."


@pytest.mark.parametrize("answer", [
    "\\[\nSources: [NWS]",
    "\\[\nSources: [NWS]\n\\]",
    "$$\nSources: [NWS]\n$$",
    "```markdown\nSources: [NWS](https://weather.test)\n```",
    "````markdown\nSources: [NWS](https://weather.test)\n````",
    "~~~markdown\nSources: [NWS](https://weather.test)",
])
def test_source_cleanup_keeps_actual_math_and_code_direct(answer):
    assert app.strip_model_source_footer(answer) == answer


PROTECTED_TICK_CONTEXTS = [
    'Use an unmatched ` character.\n\n\\[\n\\text{a ` character}\nSources: [NWS](https://weather.test)',
    '```python\nliteral = "`"\n````\n\n\\[\n\\text{a ` character}\nSources: [NWS](https://weather.test)',
]


@pytest.mark.parametrize("answer", PROTECTED_TICK_CONTEXTS)
def test_inline_ticks_cannot_cross_math_paragraph_or_fenced_block_direct(answer):
    assert app.strip_model_source_footer(answer) == answer


@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
@pytest.mark.parametrize("answer", PROTECTED_TICK_CONTEXTS)
def test_inline_ticks_cannot_cross_math_paragraph_or_fenced_block_pipeline(pipeline, monkeypatch, native, answer):
    pipeline["text"] = answer

    result = _weather_chat(pipeline, monkeypatch, native)

    assert result == answer + "\n\n" + _expected_source_list(pipeline["results"])
