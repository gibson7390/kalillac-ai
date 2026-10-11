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
                                "arguments": json.dumps({"query": state.get("query", SEARCH_QUERY)})}]}
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
    state["model_response"] = fake_post
    return state


def _provider_text(state):
    payload = state["payloads"][-1]
    return payload.get("instructions", "") + "\n" + "\n".join(
        item.get("content", item.get("output", "")) for item in payload["input"]
        if isinstance(item.get("content", item.get("output", "")), str)
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


# --- deterministic local dates, native query dates and weather value ownership ---

LIVE_BOUNDARY_CLOCK = RealDatetime(2026, 10, 11, 2, 28, 1, 83347, tzinfo=ClockTimezone.utc)
_REAL_WEATHER_SEARCH = app.run_web_search


@pytest.fixture
def live_boundary_clock(monkeypatch):
    class FixedDatetime(RealDatetime):
        @classmethod
        def now(cls, tz=None):
            return LIVE_BOUNDARY_CLOCK.astimezone(tz) if tz else LIVE_BOUNDARY_CLOCK.replace(tzinfo=None)
    monkeypatch.setattr(app, "datetime", FixedDatetime)
    return LIVE_BOUNDARY_CLOCK


@pytest.mark.parametrize("clock,zone", [
    (LIVE_BOUNDARY_CLOCK, "America/Indiana/Indianapolis"),
    (LIVE_BOUNDARY_CLOCK, "America/Los_Angeles"),
    (LIVE_BOUNDARY_CLOCK, "Asia/Tokyo"),
    (RealDatetime(2027, 1, 1, 0, 10, tzinfo=ClockTimezone.utc), "America/New_York"),
])
def test_application_computes_explicit_iana_location_date(clock, zone):
    context = app.render_current_time_context(clock, message="Use timezone " + zone)
    local = clock.astimezone(ZoneInfo(zone))
    assert clock.isoformat() in context and "Timezone: UTC" in context
    assert '"timezone": "' + zone + '"' in context
    assert '"local_date": "' + local.date().isoformat() + '"' in context
    assert local.isoformat() in context
    assert "APPLICATION-CALCULATED LOCATION DATES" in context


@pytest.mark.parametrize("message,content", [
    ("Weather today", "Timezone America/Indiana/Indianapolis; Current: 61 degrees"),
    ("Weather today, timezone Not/AZone", '{}'),
    ("Weather today, timezone EDT", '{"location":{"localtime":"2026-10-10 22:28"}}'),
    ("Weather today", '{"location":{"tz_id":"Not/AZone"}}'),
])
def test_unavailable_timezone_does_not_create_a_local_date(message, content):
    context = app.render_current_time_context(LIVE_BOUNDARY_CLOCK, message=message,
        results=[{"url":"https://weather.test/report", "content":content}])
    assert "TARGET LOCATION DATE: unknown" in context
    assert '"local_date"' not in context
    assert "Do not use the UTC or server calendar date" in context


@pytest.mark.parametrize("original,model_query,expected", [
    ("weather today", "weather October 11, 2026", "weather today"),
    ("weather right now", "weather 2026-10-11", "weather right now"),
    ("current weather", "weather 11 October 2026", "current weather"),
    ("weather tonight", "weather Oct. 11 2026", "weather tonight"),
    ("weather tomorrow", "weather 10/11/2026", "weather tomorrow"),
    ("weather today and October 10, 2026", "weather October 11, 2026", "weather today and October 10, 2026"),
    ("weather today and October 10, 2026", "weather 2026-10-10", "weather 2026-10-10"),
    ("weather on October 11, 2026", "weather October 11, 2026", "weather October 11, 2026"),
    ("weather today", "weather today", "weather today"),
    ("current Python 3.11 release", "Python 3.11 current release site:docs.python.org", "Python 3.11 current release site:docs.python.org"),
])
def test_native_relative_query_guard_preserves_user_dates_and_other_query_text(original, model_query, expected):
    assert app.ground_relative_search_query(model_query, original) == expected


def _use_scripted_weather_transport(state, monkeypatch, provider):
    from types import SimpleNamespace
    from kalillac_routing import search_providers, tavily_transport
    from kalillac_routing.provider_transport import TransportHolder
    from kalillac_routing.request_limits import RequestLimits, TransportLimits
    settings = TransportLimits(3, 1, 3, 0.02, 0.5, 1.0, 2.0)
    limits = RequestLimits(45.0, 2.0, 6, 6, settings, 2097152, settings, 262144, 524288)
    state["wire_searches"] = []

    class ScriptTransport:
        def __init__(self, **kwargs):
            pass
        def post_json(self, url, payload, *, headers, timeout, max_bytes, cancelled=None):
            assert 0 < timeout <= 45 and max_bytes > 0
            if url == app.OPENAI_RESPONSES_URL:
                return state["model_response"](payload)
            assert url == (search_providers.BRAVE_CONTEXT_URL if provider == "brave" else tavily_transport.SEARCH_URL)
            state["wire_searches"].append(copy.deepcopy(payload))
            if provider == "tavily":
                return {"results": copy.deepcopy(state["results"])}
            return {"grounding": {"generic": [
                {"url":r["url"], "title":r["title"], "snippets":[r["content"]]} for r in state["results"]]}, "sources":{}}
        def close(self, timeout):
            return SimpleNamespace(clean=True)

    monkeypatch.setattr(app, "run_web_search", _REAL_WEATHER_SEARCH)
    monkeypatch.setattr(app, "_request_limits", limits)
    monkeypatch.setattr(app, "_OPENAI_TRANSPORT", TransportHolder(factory=ScriptTransport))
    monkeypatch.setattr(tavily_transport, "_SLOT", tavily_transport.TavilyTransportSlot(factory=ScriptTransport))
    monkeypatch.setattr(app, "SEARCH_PROVIDER_POLICY", search_providers.SearchProviderPolicy(provider, "none"))
    monkeypatch.setattr(app, "BRAVE_API_KEY", "offline-brave-placeholder")
    monkeypatch.setattr(app, "TAVILY_API_KEY", "offline-tavily-placeholder")


def _wire_weather_chat(state, monkeypatch, native, provider, message):
    from kalillac_routing.request_budget import RequestBudget, budget_scope
    _use_scripted_weather_transport(state, monkeypatch, provider)
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    state["search"] = True
    assert app.classify_request(message, []) == "web_search"
    budget = RequestBudget(duration_seconds=45, max_model_attempts=6, max_search_attempts=6)
    with budget_scope(budget):
        answer = app.chat(message, [], session_id="weather-date-wire")
    assert budget.search_attempts == 1
    assert budget.model_attempts == (2 if native else 1)
    assert len(state["wire_searches"]) == 1
    assert len(state["payloads"]) == (2 if native else 1)
    assert all(p["store"] is False and p["model"] == app.OPENAI_MODEL
               and p["reasoning"]["effort"] == app.OPENAI_REASONING_EFFORT for p in state["payloads"])
    return answer


@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
@pytest.mark.parametrize("provider", ["brave", "tavily"])
@pytest.mark.parametrize("zone", ["America/Indiana/Indianapolis", "Asia/Tokyo", None, "Not/AZone"])
def test_local_date_metadata_through_actual_chat_and_scripted_transport(
    pipeline, monkeypatch, live_boundary_clock, native, provider, zone,
):
    message = "Search the web for weather today at the reported location."
    pipeline["query"] = "weather reported location October 11, 2026"
    report = {"location":{"tz_id":zone,"localtime":"2026-10-01 02:00"},
              "current":{"last_updated":"2026-10-01 01:45","temp_f":61},
              "forecast":{"date":"2026-10-11"}}
    pipeline["results"] = [{"title":"Weather report", "url":"https://weather.test/current", "content":json.dumps(report)}]
    pipeline["text"] = "The report is older; its dated forecast does not establish current conditions."
    answer = _wire_weather_chat(pipeline, monkeypatch, native, provider, message)
    prompt = _provider_text(pipeline)
    assert LIVE_BOUNDARY_CLOCK.isoformat() in prompt
    if not native:
        assert pipeline["results"][0]["content"] in prompt
    if zone in {"America/Indiana/Indianapolis", "Asia/Tokyo"}:
        local = LIVE_BOUNDARY_CLOCK.astimezone(ZoneInfo(zone))
        assert local.isoformat() in prompt
        assert local.date().isoformat() in prompt
    else:
        assert "TARGET LOCATION DATE: unknown" in prompt
    if native:
        output = next(json.loads(i["output"]) for i in pipeline["payloads"][-1]["input"] if i.get("type")=="function_call_output")
        assert output["request_time_utc"] == LIVE_BOUNDARY_CLOCK.isoformat()
        assert output["search_date_timezone"] == "UTC"
        assert output["search_date"] == "2026-10-11"
        assert output["query"] == message
        assert json.loads(output["results"][0]["content"]) == report
        assert "October 11" not in pipeline["wire_searches"][0].get("q", pipeline["wire_searches"][0].get("query"))
    assert answer == pipeline["text"] + "\n\n" + _expected_source_list(pipeline["results"])


@pytest.mark.parametrize("native", [False, True])
def test_explicit_user_date_reaches_provider_without_reinterpretation(pipeline, monkeypatch, live_boundary_clock, native):
    message = "Search the web for weather today and the forecast for October 12, 2026, timezone Asia/Tokyo."
    pipeline["query"] = "weather today forecast October 12, 2026 Asia/Tokyo"
    pipeline["results"] = [{"title":"Forecast", "url":"https://weather.test/forecast", "content":"Forecast dated October 12, 2026; observation time unavailable."}]
    answer = _wire_weather_chat(pipeline, monkeypatch, native, "brave", message)
    wire = pipeline["wire_searches"][0]["q"]
    assert "October 12" in wire and "2026" in wire
    assert LIVE_BOUNDARY_CLOCK.astimezone(ZoneInfo("Asia/Tokyo")).isoformat() in _provider_text(pipeline)
    assert answer.startswith("Supported answer.")


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("provider", ["brave", "tavily"])
def test_weather_labels_values_and_freshness_guidance_reach_actual_prompt(pipeline, monkeypatch, live_boundary_clock, native, provider):
    content = "Current: 61\u00b0\nTonight: 57\u00b0\nTomorrow: 78\u00b0\nObservation and forecast update dates not supplied."
    pipeline["results"] = [{"title":"Local forecast", "url":"https://weather.test/periods", "content":content}]
    pipeline["text"] = "The excerpt labels current as 61\u00b0, tonight as 57\u00b0, and tomorrow as 78\u00b0. Applicable update dates were not supplied, so their freshness is unverified."
    answer = _wire_weather_chat(pipeline, monkeypatch, native, provider, "Search the web for weather today.")
    prompt = _provider_text(pipeline)
    assert "Keep each value attached to its source label and time period" in prompt
    assert "current, tonight, tomorrow, observation, and forecast" in prompt
    assert "do not transfer" in prompt and "useful supported information" in prompt
    if native:
        output = next(json.loads(i["output"]) for i in pipeline["payloads"][-1]["input"] if i.get("type")=="function_call_output")
        assert output["results"][0]["content"] == content
    else:
        assert content in prompt
    # This scripted output is a formatting control, not proof of model adherence.
    assert answer == pipeline["text"] + "\n\n" + _expected_source_list(pipeline["results"])



def test_source_location_dates_are_attributed_and_do_not_override_user_timezone():
    results = [
        {"url":"https://weather.test/west", "content":json.dumps({"location":{"name":"West", "timezone":"America/Los_Angeles"}})},
        {"url":"https://weather.test/east", "content":json.dumps({"location":{"name":"East", "tz_id":"Asia/Tokyo"}})},
    ]
    dates = app._location_dates_for_request(LIVE_BOUNDARY_CLOCK, "Weather today, timezone Europe/London", results)
    assert dates[0]["basis"] == "explicit user timezone" and dates[0]["timezone"] == "Europe/London"
    assert dates[0]["local_date"] == "2026-10-11"
    assert dates[1]["source_url"] == results[0]["url"] and dates[1]["local_date"] == "2026-10-10"
    assert dates[2]["source_url"] == results[1]["url"] and dates[2]["local_date"] == "2026-10-11"
    context = app.render_current_time_context(LIVE_BOUNDARY_CLOCK, message="timezone Europe/London", results=results)
    assert "another source location does not override it" in context


def test_assistant_invented_date_cannot_authorize_a_native_query_date():
    history = [{"role":"user", "content":"Search the web for weather today."},
               {"role":"assistant", "content":"Sunday, October 11, 2026."}]
    task = app.build_web_search_query("weather today", history)
    assert "October 11" not in task
    assert app.ground_relative_search_query("weather October 11, 2026", task) == task


# --- timezone-data loader errors are unavailable information, not chat failures ---

@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
@pytest.mark.parametrize("provider", ["brave", "tavily"])
@pytest.mark.parametrize("loader_error", [None, IsADirectoryError, PermissionError, OSError],
                         ids=["actual-directory-key", "simulated-directory", "simulated-permission", "simulated-os-error"])
def test_malformed_structured_timezone_loader_remains_unknown_in_chat(
    pipeline, monkeypatch, live_boundary_clock, native, provider, loader_error,
):
    real_zoneinfo = app.ZoneInfo
    if loader_error:
        def load_zone(key):
            if key == "Europe":
                raise loader_error("Simulated timezone-data load failure")
            return real_zoneinfo(key)
        monkeypatch.setattr(app, "ZoneInfo", load_zone)
    report = {"location":{"name":"Reported location", "tz_id":"Europe"},
              "current":{"last_updated":"2026-10-10 22:15", "temp_f":61}}
    pipeline["results"] = [{"title":"Weather report", "url":"https://weather.test/malformed-zone",
                            "content":json.dumps(report)}]
    pipeline["text"] = "The report provides a dated reading; the location timezone is unavailable."
    answer = _wire_weather_chat(pipeline, monkeypatch, native, provider, "Search the web for weather today.")
    prompt = _provider_text(pipeline)
    assert "TARGET LOCATION DATE: unknown" in prompt
    assert '"local_date"' not in prompt
    assert LIVE_BOUNDARY_CLOCK.isoformat() in prompt
    if native:
        output = next(json.loads(i["output"]) for i in pipeline["payloads"][-1]["input"]
                      if i.get("type")=="function_call_output")
        assert "TARGET LOCATION DATE: unknown" in output["location_time_context"]
        assert output["request_time_utc"] == LIVE_BOUNDARY_CLOCK.isoformat()
        assert output["search_date_timezone"] == "UTC"
        assert json.loads(output["results"][0]["content"]) == report
    else:
        assert pipeline["results"][0]["content"] in prompt
    assert answer == pipeline["text"] + "\n\n" + _expected_source_list(pipeline["results"])


def test_timezone_loader_error_keeps_valid_conversion_and_source_attribution(monkeypatch):
    real_zoneinfo = app.ZoneInfo
    def load_zone(key):
        if key == "Europe":
            raise IsADirectoryError("Simulated directory key")
        return real_zoneinfo(key)
    monkeypatch.setattr(app, "ZoneInfo", load_zone)
    results = [
        {"url":"https://weather.test/invalid", "content":json.dumps({"location":{"tz_id":"Europe"}})},
        {"url":"https://weather.test/valid", "content":json.dumps({"location":{"name":"Valid location", "tz_id":"Asia/Tokyo"}})},
    ]
    dates = app._location_dates_for_request(LIVE_BOUNDARY_CLOCK, "timezone America/New_York", results)
    assert len(dates) == 2
    assert dates[0]["timezone"] == "America/New_York" and dates[0]["basis"] == "explicit user timezone"
    assert dates[0]["local_time"] == LIVE_BOUNDARY_CLOCK.astimezone(ZoneInfo("America/New_York")).isoformat()
    assert dates[1]["timezone"] == "Asia/Tokyo" and dates[1]["source_url"] == results[1]["url"]
    assert dates[1]["location"] == "Valid location" and dates[1]["basis"] == "structured source location"
    assert dates[1]["local_time"] == LIVE_BOUNDARY_CLOCK.astimezone(ZoneInfo("Asia/Tokyo")).isoformat()


@pytest.mark.parametrize("error", [RuntimeError("Unexpected loader defect"), TypeError("Unexpected type")])
def test_timezone_loader_unexpected_application_errors_are_not_swallowed(monkeypatch, error):
    def load_zone(key):
        raise error
    monkeypatch.setattr(app, "ZoneInfo", load_zone)
    with pytest.raises(type(error)):
        app._location_dates_for_request(LIVE_BOUNDARY_CLOCK, "timezone Asia/Tokyo")


def test_timezone_os_error_outside_conversion_is_not_swallowed():
    class BadLocalTime:
        def date(self):
            raise OSError("Unrelated result-formatting failure")
    class Clock:
        def astimezone(self, zone):
            return BadLocalTime()
    with pytest.raises(OSError, match="result-formatting"):
        app._location_dates_for_request(Clock(), "timezone Asia/Tokyo")



def test_timezone_os_error_from_clock_conversion_is_not_swallowed():
    class Clock:
        def astimezone(self, zone):
            raise OSError("Unrelated clock-conversion failure")
    with pytest.raises(OSError, match="clock-conversion"):
        app._location_dates_for_request(Clock(), "timezone Asia/Tokyo")


# --- exact live footer examples and evidence-backed plain citation names ---

LIVE_WEATHER_FOOTER = "Sources: WeatherAPI Terre Haute observation; National Weather Service Terre Haute point forecast."
LIVE_LIBRARY_FOOTER = "Source: [Vigo County Public Library \u2014 Main Library](https://vigocounty.librarycalendar.com/branch/main-library)"
LIVE_FOOTER_CASES = [
    ("weather", LIVE_WEATHER_FOOTER, [
        {"title":"WeatherAPI Terre Haute observation", "url":"https://weather.test/observation", "published":"", "score":None, "content":"A dated observation."},
        {"title":"National Weather Service Terre Haute point forecast", "url":"https://weather.test/forecast", "published":"", "score":None, "content":"A separately dated forecast."},
    ]),
    ("library", LIVE_LIBRARY_FOOTER, [
        {"title":"Vigo County Public Library \u2014 Main Library", "url":"https://vigocounty.librarycalendar.com/branch/main-library", "published":"", "score":None, "content":"Public branch information."},
    ]),
]


@pytest.mark.parametrize("case,footer,sources", LIVE_FOOTER_CASES)
def test_live_source_footer_examples_direct(case, footer, sources):
    answer = "Keep the complete answer.\n\n" + footer
    assert app.strip_model_source_footer(answer, sources=sources) == "Keep the complete answer."


@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
@pytest.mark.parametrize("case,footer,sources", LIVE_FOOTER_CASES)
def test_live_source_footer_examples_through_actual_chat(pipeline, monkeypatch, native, case, footer, sources):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    message = "Search the web for weather at the reported location." if case=="weather" else "Search the web for the public library branch page."
    pipeline["search"] = True
    pipeline["query"] = "weather at the reported location" if case=="weather" else "public library main branch"
    pipeline["results"] = copy.deepcopy(sources)
    body = "Keep the complete answer, including 100 and \\(f(x)=x^2\\). See [the page](" + sources[0]["url"] + ") for details."
    pipeline["text"] = body + "\n\n" + footer
    answer = app.chat(message, [], session_id="live-source-footer")
    assert answer == body + "\n\n" + _expected_source_list(sources)
    assert len(pipeline["searches"]) == 1 and len(pipeline["payloads"]) == (2 if native else 1)
    assert pipeline["results"] == sources
    assert answer.count("**Sources**") == 1 and "\nSource:" not in answer and "\nSources:" not in answer
    if native:
        result = next(json.loads(i["output"]) for i in pipeline["payloads"][-1]["input"] if i.get("type")=="function_call_output")
        assert result["results"] == [{"title":s["title"],"url":s["url"],"published":s["published"],"content":s["content"]} for s in sources]


PRESENTATION_PRESERVED_ANSWERS = [
    "Sources: the observation and forecast represent different time periods.",
    "Source: [Main Library](https://vigocounty.librarycalendar.com/branch/main-library) explains its services.",
    "The explanation cites [Main Library](https://vigocounty.librarycalendar.com/branch/main-library) inline.",
    "Sources: WeatherAPI Terre Haute observation; an unidentified reference.",
    "Sources: WeatherAPI Terre Haute observation; National Weather Service Terre Haute point forecast. These may be outdated.",
    "Source: 100",
    "Source: f(x)=x^2",
    "Sources: [Reference](https://example.test)\n\n100",
    "Use `Source: [Main Library](https://vigocounty.librarycalendar.com/branch/main-library)` as a literal example.",
    "Source: `WeatherAPI Terre Haute observation`",
    "Use ``Sources: WeatherAPI Terre Haute observation; `literal` `` as an example.",
    "Literal indented code:\n\n    " + LIVE_WEATHER_FOOTER,
    "Literal indented code:\n\n\t" + LIVE_LIBRARY_FOOTER,
    "Literal code:\n\n```markdown\n" + LIVE_WEATHER_FOOTER + "\n```",
    "Literal code:\n\n~~~markdown\n" + LIVE_LIBRARY_FOOTER + "\n~~~",
    "Literal unfinished code:\n\n```markdown\n" + LIVE_LIBRARY_FOOTER,
    "\\[\n" + LIVE_WEATHER_FOOTER,
    "\\(\n" + LIVE_LIBRARY_FOOTER,
    "$$\n" + LIVE_WEATHER_FOOTER + "\n$$",
    "\\[\n" + LIVE_LIBRARY_FOOTER + "\n\\]",
]


@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
@pytest.mark.parametrize("body", PRESENTATION_PRESERVED_ANSWERS)
def test_evidence_aware_footer_preserves_answer_code_and_math(pipeline, monkeypatch, native, body):
    pipeline["results"] = copy.deepcopy(LIVE_FOOTER_CASES[0][2] + LIVE_FOOTER_CASES[1][2])
    pipeline["text"] = body
    assert _weather_chat(pipeline, monkeypatch, native) == body + "\n\n" + _expected_source_list(pipeline["results"])


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("footer", [LIVE_WEATHER_FOOTER, LIVE_LIBRARY_FOOTER, "Sources: [Reference](https://example.test)"])
def test_answers_without_application_sources_keep_their_citations(pipeline, monkeypatch, native, footer):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    body = "The bibliographic reference is below.\n\n" + footer
    pipeline["text"] = body
    assert app.chat("Explain a bibliographic reference.", [], session_id="no-source-list") == body
    assert pipeline["searches"] == [] and len(pipeline["payloads"]) == 1


@pytest.mark.parametrize("native", [False, True])
def test_whole_fenced_plain_source_example_stays_code(pipeline, monkeypatch, native):
    pipeline["results"] = copy.deepcopy(LIVE_FOOTER_CASES[0][2])
    body = "```markdown\n" + LIVE_WEATHER_FOOTER + "\n```"
    pipeline["text"] = body
    assert _weather_chat(pipeline, monkeypatch, native) == body + "\n\n" + _expected_source_list(pipeline["results"])


def test_plain_footer_requires_known_complete_source_titles():
    answer = "Answer.\n\n" + LIVE_WEATHER_FOOTER
    assert app.strip_model_source_footer(answer, sources=[{"title":"Other page", "url":"https://other.test"}]) == answer
    assert app.strip_model_source_footer(answer) == answer
    for title in ["100", "f(x)=x^2", "[0,2]"]:
        text = "Source: " + title
        assert app.strip_model_source_footer(text, sources=[{"title":title, "url":"https://math.test"}]) == text


def test_plain_citation_matching_is_general_and_handles_mixed_reference_syntax():
    sources = [{"title":"Example Observatory measurement ledger", "url":"https://example.test/ledger"}]
    answer = "Answer.\n\nSources: Example Observatory measurement ledger; [Another](https://example.test/other)."
    assert app.strip_model_source_footer(answer, sources=sources) == "Answer."



def test_native_continuation_keeps_fenced_plain_citation_example(pipeline, monkeypatch):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", True)
    pipeline["search"] = True
    pipeline["results"] = copy.deepcopy(LIVE_FOOTER_CASES[0][2])
    body = "```markdown\n" + LIVE_WEATHER_FOOTER + "\n```"
    pipeline["text"] = body
    history = [{"role":"user", "content":"Explain weather source references."},
               {"role":"assistant", "content":app.mark_incomplete_reply("A weather citation list can be written as")}]
    assert app.is_explanation_continuation_request("continue", history)
    answer = app.chat("continue", history, session_id="citation-code-continuation")
    assert answer == body + "\n\n" + _expected_source_list(pipeline["results"])
    assert len(pipeline["searches"]) == 1 and len(pipeline["payloads"]) == 2


def test_source_cleanup_without_replacement_preserves_direct_answer():
    for footer in [LIVE_WEATHER_FOOTER, LIVE_LIBRARY_FOOTER, "Sources: [Reference](https://example.test)"]:
        answer = "Answer.\n\n" + footer
        assert app.strip_model_source_footer(answer, sources=[]) == answer
