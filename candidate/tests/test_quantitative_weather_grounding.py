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


# --- timezone-aware request clock and complete committed calculus context ---

from datetime import datetime as RealDatetime, timezone as ClockTimezone
from pathlib import Path
from zoneinfo import ZoneInfo

FIXED_UTC_CLOCK = RealDatetime(2026, 10, 11, 0, 22, tzinfo=ClockTimezone.utc)


@pytest.fixture
def utc_server_clock(monkeypatch):
    class FixedDatetime(RealDatetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return FIXED_UTC_CLOCK.replace(tzinfo=None)
            return FIXED_UTC_CLOCK.astimezone(tz)

    monkeypatch.setattr(app, "datetime", FixedDatetime)
    return FIXED_UTC_CLOCK


@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
@pytest.mark.parametrize("zone_name,place", [
    ("America/Indiana/Indianapolis", "Terre Haute, Indiana"),
    ("Asia/Tokyo", "Tokyo, Japan"),
    (None, "an unspecified location"),
    ("Not/AZone", "a location with an unverified timezone"),
])
@pytest.mark.parametrize("old_report_clock", [False, True], ids=["recent-report", "old-report"])
def test_current_clock_location_date_boundary_in_actual_payload(
    pipeline, monkeypatch, utc_server_clock, native, zone_name, place, old_report_clock,
):
    monkeypatch.setitem(globals(), "WEATHER_QUERY", f"Search the web for weather right now in {place}.")
    monkeypatch.setitem(globals(), "SEARCH_QUERY", "weather " + place)
    known_zone = zone_name in {"America/Indiana/Indianapolis", "Asia/Tokyo"}
    local_now = utc_server_clock.astimezone(ZoneInfo(zone_name)) if known_zone else None
    if local_now:
        assert local_now.date().isoformat() == ("2026-10-10" if place.startswith("Terre Haute") else "2026-10-11")
    # An old report's clock cannot override the independent request clock.
    localtime = "2026-10-01 02:00" if old_report_clock else (
        local_now.strftime("%Y-%m-%d %H:%M") if local_now else "2026-10-10 20:22")
    update = "2026-10-01 01:45" if old_report_clock else (
        local_now.replace(minute=15).strftime("%Y-%m-%d %H:%M") if local_now else "2026-10-10 20:15")
    evidence = {"location": {"name": place, "tz_id": zone_name, "localtime": localtime},
                "current": {"last_updated": update, "temp_f": 66.7, "condition": "overcast",
                            "wind_dir": "SE", "wind_mph": 7.2},
                "forecast": {"forecastday": [{"date": "2026-10-11", "day": "Cloudy"}]}}
    pipeline["results"] = [{"title": "Weather report", "url": "https://weather.test/report",
                            "published": "2026-10-11", "content": json.dumps(evidence)}]
    relation = ("tomorrow" if local_now.date().isoformat() == "2026-10-10" else "today") if local_now else "a dated forecast; the location timezone is unverified"
    pipeline["text"] = f"The report lists 66.7 F, overcast, SE wind 7.2 mph, updated {update}. The October 11 forecast is {relation}."

    result = _weather_chat(pipeline, monkeypatch, native)

    prompt = _provider_text(pipeline)
    assert "CURRENT REQUEST TIME (UTC):" in prompt
    assert utc_server_clock.isoformat() in prompt and "Timezone: UTC" in prompt
    assert "CURRENT SERVER DATE:" not in prompt and "\nSEARCH DATE:\n" not in prompt
    assert 'location-specific "today"' in prompt and "reliable location timezone" in prompt
    assert "timezone is unknown" in prompt and "do not assume" in prompt
    assert "Do not substitute a report's localtime" in prompt
    assert "observation/update" in prompt and "forecast" in prompt
    assert result == pipeline["text"] + "\n\n" + _expected_source_list(pipeline["results"])
    if native:
        outputs = [json.loads(item["output"]) for item in pipeline["payloads"][-1]["input"]
                   if item.get("type") == "function_call_output"]
        assert outputs[0]["search_date"] == utc_server_clock.date().isoformat()
        assert outputs[0]["results"] == pipeline["results"]
    else:
        assert pipeline["results"][0]["content"] in prompt


@pytest.mark.parametrize("clock", [
    FIXED_UTC_CLOCK,
    FIXED_UTC_CLOCK.astimezone(ClockTimezone(RealDatetime(2026, 1, 1, 5, 30) - RealDatetime(2026, 1, 1))),
    FIXED_UTC_CLOCK.astimezone(ZoneInfo("Asia/Tokyo")),
])
def test_shared_current_clock_is_explicit_and_normalized_to_utc(clock):
    context = app.render_current_time_context(clock)
    assert FIXED_UTC_CLOCK.isoformat() in context
    assert "Timezone: UTC" in context
    assert 'location-specific "today"' in context


def test_current_clock_rejects_naive_injected_time():
    with pytest.raises(ValueError, match="timezone-aware"):
        app.render_current_time_context(FIXED_UTC_CLOCK.replace(tzinfo=None))


@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
def test_supplied_report_uses_independent_current_clock(pipeline, monkeypatch, utc_server_clock, native):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    message = "Summarize this supplied report: timezone Asia/Tokyo, localtime 2026-10-01 02:00, observation updated 01:45, forecast dated October 11."
    pipeline["text"] = "The report clock and observation are older. The forecast is dated October 11."

    answer = app.chat(message, [], session_id="supplied-report-clock")

    prompt = _provider_text(pipeline)
    assert FIXED_UTC_CLOCK.isoformat() in prompt and "Timezone: UTC" in prompt
    assert "Do not substitute a report's localtime" in prompt
    assert message in prompt and answer == pipeline["text"]
    assert len(pipeline["payloads"]) == 1 and pipeline["searches"] == []


@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
@pytest.mark.parametrize("defined_scale", [False, True], ids=["unspecified-x", "explicit-hypothetical-scale"])
def test_complete_calculus_fixture_and_existing_guidance_reach_provider(
    pipeline, monkeypatch, tmp_path, native, defined_scale,
):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    fixture = (Path(__file__).parent / "fixtures" / "calculus_cutoff.md").read_text(encoding="utf-8").rstrip()
    profit_section = fixture.split("## 15. Finding when profit is greatest", 1)[1].split("---", 1)[0]
    assert "profit is:" in profit_section and "P(x)=-2x^2+40x-100" in profit_section
    assert "products" not in profit_section and "hundreds" not in profit_section
    request = "I need you to teach me calculus in a way that anybody can understand"
    if defined_scale:
        request += ". In the hypothetical profit example, define x in hundreds of products."
        pipeline["text"] = "Under the explicitly supplied hypothetical scale, x=10 represents 1,000 products, and P(10)=100."
    else:
        pipeline["text"] = r"Mathematically, \(x=10\) maximizes profit with \(P(10)=100\). The physical meaning of x and its scale are unspecified."
    history = [{"role": "user", "content": request},
               {"role": "assistant", "content": app.mark_incomplete_reply(fixture)}]
    assert app.is_explanation_continuation_request("continue", history)
    assert app.classify_request("continue", history) == "followup"

    reply = app.chat("continue", history, session_id="complete-calculus-context")

    prompt = _provider_text(pipeline)
    assert request in prompt and fixture in prompt
    assert app.QUANTITATIVE_GROUNDING_RULES in prompt
    assert "variable definitions" in prompt
    assert "physical interpretation that needs additional assumptions" in prompt
    assert "Clearly introduced hypothetical examples are allowed" in prompt
    assert app.EXPLANATION_CONTINUATION_RULES in prompt
    assert reply == pipeline["text"]
    assert len(pipeline["payloads"]) == 1 and pipeline["searches"] == []
    evidence_path = tmp_path / "actual-provider-payloads.json"
    evidence_path.write_text(json.dumps(pipeline["payloads"], indent=2), encoding="utf-8")
    print("CALCULUS_PAYLOAD_PATH=" + str(evidence_path))
