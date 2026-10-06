"""Bounded Tavily transport: request-budgeted search and extraction.

Every Tavily response comes from a scripted transport, a capturing
tavily-python session, or a loopback HTTP server. conftest.py blocks every
non-loopback network attempt and fails the test that makes one.
"""

from __future__ import annotations

import asyncio
import copy
import http.server
import json
from pathlib import Path
import socket
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient


CANDIDATE_DIR = Path(__file__).resolve().parents[1]

if str(CANDIDATE_DIR) not in sys.path:
    sys.path.insert(0, str(CANDIDATE_DIR))

import app_fastapi_candidate as app
from kalillac_routing import provider_transport, tavily_transport
from kalillac_routing.bounded_transport import (
    InvalidJSONResponse,
    ResponseTooLarge,
    TransportCleanupUnconfirmed,
    TransportClosed,
    TransportConnectionError,
    TransportDeadlineExceeded,
    TransportError,
    TransportHTTPError,
    TransportOverloaded,
    TransportQuarantined,
    UnsupportedContentEncoding,
)
from kalillac_routing.provider_transport import TransportUnavailable
from kalillac_routing.request_budget import (
    CallBudgetExhausted,
    RequestBudget,
    RequestCancelled,
    RequestDeadlineExceeded,
    budget_scope,
    current_budget,
)
from kalillac_routing.request_limits import RequestLimits, TransportLimits
from kalillac_routing.tavily_transport import TavilyResponseInvalid, TavilyTransportSlot


TAVILY_KEY = "test-tavily-key-must-not-leak"
SECRET = "SECRET-DEFECT-TEXT-MUST-NOT-LEAK"
SEARCH_MAX_BYTES = 262144
EXTRACT_MAX_BYTES = 524288
OPENAI_TRANSPORT_LIMITS = TransportLimits(
    max_outstanding=4,
    dns_threads=1,
    max_pending_dns=4,
    cancel_poll_interval_seconds=0.02,
    backstop_grace_seconds=0.5,
    cleanup_grace_seconds=1.0,
    close_timeout_seconds=3.0,
)
TAVILY_TRANSPORT_LIMITS = TransportLimits(
    max_outstanding=3,
    dns_threads=1,
    max_pending_dns=3,
    cancel_poll_interval_seconds=0.02,
    backstop_grace_seconds=0.5,
    cleanup_grace_seconds=1.0,
    close_timeout_seconds=2.0,
)
BASE_LIMITS = {
    "deadline_seconds": 5.0,
    "queue_wait_seconds": 2.0,
    "max_model_attempts": 6,
    "max_search_attempts": 6,
    "transport": OPENAI_TRANSPORT_LIMITS,
    "openai_max_bytes": 2097152,
    "tavily_transport": TAVILY_TRANSPORT_LIMITS,
    "tavily_search_max_bytes": SEARCH_MAX_BYTES,
    "tavily_extract_max_bytes": EXTRACT_MAX_BYTES,
}
SERVICE_UNAVAILABLE = (503, {"error": "service_unavailable"})
PROVIDER_UNAVAILABLE = (503, {"error": "model_provider_unavailable"})
INTERNAL_ERROR = (500, {"error": "internal_error"})
LEGACY_UNAVAILABLE = (
    "Live web search is temporarily unavailable. I can still "
    "help with questions that don't require current "
    "verification."
)

# The four documented search variants: (query, include_domains, body).
SEARCH_VARIANTS = {
    "basic": (
        "history of roman aqueducts", None,
        {"query": "history of roman aqueducts", "search_depth": "basic", "max_results": 4},
    ),
    "named_domain_basic": (
        "python release notes", ["python.org"],
        {"query": "python release notes", "search_depth": "basic", "max_results": 4,
         "include_domains": ["python.org"]},
    ),
    "advanced": (
        "latest python release", ["python.org"],
        {"query": "latest python release", "search_depth": "advanced", "max_results": 5,
         "include_domains": ["www.python.org"], "chunks_per_source": 3},
    ),
    "advanced_news": (
        "AI news today", None,
        {"query": "AI news today", "search_depth": "advanced", "topic": "news",
         "time_range": "day", "max_results": 5, "chunks_per_source": 1},
    ),
}


class ScriptExhausted(BaseException):
    """A scripted transport received more requests than the test planned."""


class UnexpectedOpenAI(BaseException):
    """A test reached OpenAI without scripting it."""


def _refuse(*args, **kwargs):
    raise UnexpectedOpenAI()


# --- fixtures and helpers --------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(app, "_request_limits", None)
    monkeypatch.setattr(app, "_chat_semaphore", None)
    monkeypatch.setattr(app, "_chat_waiting", 0)
    monkeypatch.setattr(app, "_session_locks", {})
    monkeypatch.setattr(app, "_usage_meter", None)
    monkeypatch.setattr(app, "_chats_admitted", 0, raising=False)
    monkeypatch.setattr(app.urllib.request, "urlopen", _refuse)
    monkeypatch.setattr(app, "_OPENAI_TRANSPORT", provider_transport.TransportHolder(factory=_refuse))
    monkeypatch.setattr(app, "OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setattr(app, "TAVILY_API_KEY", TAVILY_KEY)
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", False)


def limits(**overrides):
    return RequestLimits(**{**BASE_LIMITS, **overrides})


def enable(monkeypatch, **overrides):
    value = limits(**overrides)
    monkeypatch.setattr(app, "_request_limits", value)
    return value


def budget(searches=6, models=6, seconds=30.0, clock=time.monotonic):
    return RequestBudget(
        duration_seconds=seconds,
        max_model_attempts=models,
        max_search_attempts=searches,
        clock=clock,
    )


class Scripted:
    """State for a scripted Tavily transport installed in the Tavily slot."""

    def __init__(self):
        self.script = []
        self.posts = []
        self.transports = []


@pytest.fixture
def tavily(monkeypatch):
    state = Scripted()

    class ScriptedTavilyTransport:
        def __init__(self, **settings):
            self.settings = settings
            self.closes = []
            state.transports.append(self)

        def post_json(self, url, payload, *, headers, timeout, max_bytes, cancelled=None):
            state.posts.append({
                "url": url,
                "payload": copy.deepcopy(payload),
                "headers": dict(headers),
                "timeout": timeout,
                "max_bytes": max_bytes,
                "cancelled": cancelled,
            })

            if not state.script:
                raise ScriptExhausted()

            result = state.script.pop(0)
            result = result() if callable(result) else result

            if isinstance(result, BaseException):
                raise result

            return copy.deepcopy(result)

        def close(self, timeout):
            self.closes.append(timeout)

    state.slot = TavilyTransportSlot(factory=ScriptedTavilyTransport)
    monkeypatch.setattr(tavily_transport, "_SLOT", state.slot)
    monkeypatch.setattr(app, "_request_limits", limits())
    return state


@pytest.fixture
def openai(monkeypatch):
    """Scripted budgeted OpenAI transport (its own holder)."""

    state = {"script": [], "posts": []}

    class ScriptedOpenAITransport:
        def __init__(self, **settings):
            pass

        def post_json(self, url, payload, *, headers, timeout, max_bytes, cancelled=None):
            state["posts"].append({"url": url, "payload": copy.deepcopy(payload)})
            return state["script"].pop(0)

        def close(self, timeout):
            return None

    state["holder"] = provider_transport.TransportHolder(factory=ScriptedOpenAITransport)
    monkeypatch.setattr(app, "_OPENAI_TRANSPORT", state["holder"])
    return state


def article(n, published="2026-10-05"):
    return {
        "title": f"Story {n} about AI models",
        "url": f"https://news.example.org/2026/10/05/story-{n}",
        "content": "summary",
        "published_date": published,
        "score": 0.9,
    }


def page(n):
    return {
        "title": f"Page {n}",
        "url": f"https://www.python.org/page-{n}",
        "content": "Content " * 20,
        "published_date": "",
        "score": 0.5,
    }


def results(*items):
    return {"results": list(items)}


LONG = {"results": [{"raw_content": "Article text. " * 30}]}
SHORT = {"results": [{"raw_content": "short"}]}


def search_posts(state):
    return [post for post in state.posts if post["url"] == tavily_transport.SEARCH_URL]


def extract_posts(state):
    return [post for post in state.posts if post["url"] == tavily_transport.EXTRACT_URL]


def web_search_chat(monkeypatch):
    """Route /api/chat through the real legacy web_search path."""
    monkeypatch.setattr(app, "classify_request", lambda message, history: "web_search")
    monkeypatch.setattr(app, "requires_web_verification", lambda message, history: False)


def post_chat(message="latest AI news"):
    with TestClient(app.api) as client:
        response = client.post("/api/chat", json={"message": message, "history": []})
    return response.status_code, response.json(), response


def assert_nothing_leaked(capsys, body):
    out = capsys.readouterr()
    text = out.out + out.err + json.dumps(body)

    for secret in (SECRET, TAVILY_KEY, "Bearer", "latest AI news", "Article text"):
        assert secret not in text


# --- pure payloads and response validation ----------------------------------------------------


@pytest.mark.parametrize("variant", list(SEARCH_VARIANTS))
def test_search_payload_variants(variant):
    query, domains, body = SEARCH_VARIANTS[variant]
    args = dict(body)
    args["timeout"] = 10

    payload = tavily_transport.search_payload(args)

    assert payload == body
    assert "timeout" not in payload


def test_search_payload_never_adds_unused_fields():
    payload = tavily_transport.search_payload(
        {"query": "q", "search_depth": "basic", "max_results": 4, "topic": None}
    )

    assert payload == {"query": "q", "search_depth": "basic", "max_results": 4}

    with pytest.raises(ValueError):
        tavily_transport.search_payload({"query": "q", "include_answer": True})


def test_extract_payload_shape():
    args = {
        "urls": "https://news.example.org/a",
        "query": "Title",
        "chunks_per_source": 1,
        "extract_depth": "basic",
        "format": "markdown",
    }

    payload = tavily_transport.extract_payload(args, 7.25)

    assert payload == {
        "urls": "https://news.example.org/a",
        "extract_depth": "basic",
        "format": "markdown",
        "timeout": 7.25,
        "query": "Title",
        "chunks_per_source": 1,
    }
    assert list(payload) == ["urls", "extract_depth", "format", "timeout", "query", "chunks_per_source"]
    assert isinstance(payload["urls"], str)


@pytest.mark.parametrize(
    "selected, body",
    [(0.001, 1.0), (0.5, 1.0), (1.0, 1.0), (29.5, 29.5), (120.0, 120.0), (500.0, 120.0)],
)
def test_extract_payload_timeout_is_clamped_to_tavily_range(selected, body):
    assert tavily_transport.extract_payload_timeout(selected) == body


@pytest.mark.parametrize("bad", [0, -1.0, float("nan"), True, "5"])
def test_extract_payload_timeout_rejects_invalid_values(bad):
    with pytest.raises((TypeError, ValueError)):
        tavily_transport.extract_payload_timeout(bad)


@pytest.mark.parametrize(
    "response",
    [[], "text", None, {"results": "x"}, {"results": {"a": 1}}, {"results": [1]}, {"results": [["x"]]}],
    ids=["list", "str", "none", "results_str", "results_dict", "item_int", "item_list"],
)
def test_validate_response_rejects_malformed_shapes(response):
    with pytest.raises(TavilyResponseInvalid) as caught:
        tavily_transport.validate_response(response)

    assert str(caught.value) == TavilyResponseInvalid.MESSAGE


def test_validate_response_defaults_missing_results_like_the_sdk():
    assert tavily_transport.validate_response({"answer": None}) == {"answer": None, "results": []}


def test_post_json_refuses_other_endpoints_and_blank_credentials():
    with pytest.raises(ValueError):
        tavily_transport.post_json(
            "https://api.openai.com/v1/responses", {}, api_key="k", settings=TAVILY_TRANSPORT_LIMITS,
            budget=budget(), timeout=1.0, request_deadline_selected=False, max_bytes=10,
        )

    for key in ("", "   ", None):
        with pytest.raises(ValueError):
            tavily_transport.post_json(
                tavily_transport.SEARCH_URL, {}, api_key=key, settings=TAVILY_TRANSPORT_LIMITS,
                budget=budget(), timeout=1.0, request_deadline_selected=False, max_bytes=10,
            )

    assert tavily_transport.existing_holder() is None


# --- feature-off and budgeted payloads match what tavily-python sends ----------------------------


class _SDKResponse:
    status_code = 200

    def __init__(self, data):
        self._data = data

    def json(self):
        return copy.deepcopy(self._data)


@pytest.fixture
def sdk(monkeypatch):
    """The real tavily-python client with a capturing session in place of
    requests.Session: its own payload construction runs, nothing is sent."""
    import tavily

    real_client = tavily.TavilyClient
    state = {"script": [], "posts": [], "clients": []}

    class CapturingSession:
        def __init__(self):
            self.headers = {}
            self.proxies = {}

        def post(self, url, data=None, timeout=None, **kwargs):
            state["posts"].append({"url": url, "body": data, "timeout": timeout, "kwargs": kwargs})
            return _SDKResponse(state["script"].pop(0))

        def close(self):
            pass

    def client(api_key=None, **kwargs):
        assert kwargs == {}
        state["clients"].append(api_key)
        return real_client(api_key=api_key, session=CapturingSession())

    monkeypatch.setattr(tavily, "TavilyClient", client)
    return state


@pytest.mark.parametrize("variant", list(SEARCH_VARIANTS))
def test_budgeted_search_body_equals_the_sdk_body(sdk, tavily, monkeypatch, variant):
    query, domains, body = SEARCH_VARIANTS[variant]
    sdk["script"] = [results()]
    monkeypatch.setattr(app, "_request_limits", None)

    app.run_web_search(query, include_domains=domains)          # flag off: SDK

    enable(monkeypatch)
    tavily.script = [results()]

    with budget_scope(budget()):
        app.run_web_search(query, include_domains=domains)      # budgeted: bounded

    sdk_post = sdk["posts"][0]
    bounded = tavily.posts[0]
    assert sdk_post["url"] == bounded["url"] == "https://api.tavily.com/search"
    assert json.dumps(bounded["payload"]) == sdk_post["body"]
    assert bounded["payload"] == body
    assert sdk_post["timeout"] == app.SEARCH_TIMEOUT_SECONDS
    assert len(sdk["posts"]) == len(tavily.posts) == 1


def test_budgeted_extract_body_equals_the_sdk_body_except_the_timeout(sdk, tavily, monkeypatch):
    sdk["script"] = [results(article(1)), LONG]
    app.run_web_search("latest AI news")                        # flag off: SDK

    enable(monkeypatch)
    tavily.script = [results(article(1)), LONG]

    with budget_scope(budget(seconds=60.0)):                    # 30 s cap selected
        app.run_web_search("latest AI news")

    sdk_body = json.loads(sdk["posts"][1]["body"])
    bounded = tavily.posts[1]
    assert sdk["posts"][1]["url"] == bounded["url"] == "https://api.tavily.com/extract"
    assert list(bounded["payload"]) == list(sdk_body)
    assert bounded["payload"] == sdk_body == {
        "urls": article(1)["url"],
        "extract_depth": "basic",
        "format": "markdown",
        "timeout": 30,
        "query": article(1)["title"],
        "chunks_per_source": 1,
    }
    assert sdk["posts"][1]["timeout"] == 30                     # SDK default, unchanged
    assert bounded["timeout"] == 30.0


def test_feature_off_keeps_the_sdk_path_and_creates_no_transport(sdk, monkeypatch):
    calls = []
    import tavily

    wrapped = tavily.TavilyClient

    def recording_client(api_key=None):
        client = wrapped(api_key=api_key)
        real_search, real_extract = client.search, client.extract

        def search(**kwargs):
            calls.append(("search", kwargs))
            return real_search(**kwargs)

        def extract(**kwargs):
            calls.append(("extract", kwargs))
            return real_extract(**kwargs)

        client.search, client.extract = search, extract
        return client

    monkeypatch.setattr(tavily, "TavilyClient", recording_client)
    sdk["script"] = [results(article(1)), LONG]

    status, found = app.run_web_search("latest AI news")

    assert status == "ok" and len(found) == 1
    assert sdk["clients"] == [TAVILY_KEY]
    assert calls == [
        ("search", {"query": "latest AI news", "search_depth": "advanced", "max_results": 5,
                    "timeout": app.SEARCH_TIMEOUT_SECONDS, "chunks_per_source": 1, "topic": "news"}),
        ("extract", {"urls": article(1)["url"], "query": article(1)["title"],
                     "chunks_per_source": 1, "extract_depth": "basic", "format": "markdown"}),
    ]
    assert tavily_transport.existing_holder() is None
    assert app._OPENAI_TRANSPORT.existing() is None


def test_feature_off_remote_failure_is_still_unavailable(sdk):
    sdk["script"] = []          # the capturing session raises IndexError: an SDK failure

    assert app.run_web_search("latest AI news") == ("unavailable", [])
    assert tavily_transport.existing_holder() is None


# --- holder separation and quarantine isolation ------------------------------------------------


def test_search_and_extraction_use_tavilys_own_holder(tavily, openai):
    tavily.script = [results(article(1)), LONG]

    with budget_scope(budget()):
        status, found = app.run_web_search("latest AI news")

    holder = tavily_transport.existing_holder()
    assert status == "ok"
    assert holder is tavily.slot.existing()
    assert holder is not app._OPENAI_TRANSPORT
    assert len(tavily.transports) == 1
    assert [post["url"] for post in tavily.posts] == [
        tavily_transport.SEARCH_URL, tavily_transport.EXTRACT_URL,
    ]
    assert tavily.transports[0].settings == {
        "max_outstanding": TAVILY_TRANSPORT_LIMITS.max_outstanding,
        "dns_threads": TAVILY_TRANSPORT_LIMITS.dns_threads,
        "max_pending_dns": TAVILY_TRANSPORT_LIMITS.max_pending_dns,
        "cancel_poll_interval": TAVILY_TRANSPORT_LIMITS.cancel_poll_interval_seconds,
        "backstop_grace": TAVILY_TRANSPORT_LIMITS.backstop_grace_seconds,
        "cleanup_grace": TAVILY_TRANSPORT_LIMITS.cleanup_grace_seconds,
    }
    assert openai["posts"] == []
    assert app._OPENAI_TRANSPORT.existing() is None


def test_tavily_headers_carry_only_the_tavily_credential(tavily):
    tavily.script = [results()]

    with budget_scope(budget()):
        app.run_web_search("history of roman aqueducts")

    assert tavily.posts[0]["headers"] == {
        "Authorization": f"Bearer {TAVILY_KEY}",
        "Content-Type": "application/json",
    }


def test_tavily_quarantine_does_not_quarantine_openai(tavily, openai, capsys):
    tavily.script = [TransportCleanupUnconfirmed("connection")]
    openai["script"] = [{"output": [{"type": "message", "role": "assistant",
                                     "content": [{"type": "output_text", "text": "ok"}]}],
                         "status": "completed"}]

    with budget_scope(budget()):
        with pytest.raises(app.SearchTransportUnavailable):
            app.run_web_search("history of roman aqueducts")

        app._post_openai_for_attempt({"model": "m"})

    assert tavily_transport.quarantined() is True
    assert app._OPENAI_TRANSPORT.quarantined is False
    assert len(openai["posts"]) == 1
    assert "WARN: SEARCH_TRANSPORT_QUARANTINED cleanup_unconfirmed" in capsys.readouterr().out


def test_openai_quarantine_does_not_quarantine_tavily(tavily, openai):
    openai["holder"].quarantine()
    tavily.script = [results(page(1))]

    with budget_scope(budget()):
        status, found = app.run_web_search("history of roman aqueducts")

    assert status == "ok"
    assert len(tavily.posts) == 1
    assert tavily_transport.quarantined() is False


# --- admissions, timeouts and ceilings ----------------------------------------------------------


def test_each_post_consumes_exactly_one_search_admission_and_no_model_admission(tavily):
    tavily.script = [results(article(1), article(2), article(3)), LONG, LONG, LONG]
    request_budget = budget()

    with budget_scope(request_budget):
        status, found = app.run_web_search("latest AI news")

    assert status == "ok" and len(found) == 3
    assert len(tavily.posts) == request_budget.search_attempts == 4
    assert request_budget.model_attempts == 0


def test_news_search_admits_one_search_plus_five_extractions(tavily):
    tavily.script = [
        results(*(article(n) for n in range(5))),
        SHORT, LONG, LONG, LONG, LONG,
    ]
    request_budget = budget(searches=6)

    with budget_scope(request_budget):
        status, found = app.run_web_search("latest AI news")

    assert status == "ok" and len(found) == 4
    assert len(search_posts(tavily)) == 1
    assert len(extract_posts(tavily)) == 5
    assert request_budget.search_attempts == 6


def test_a_seventh_attempt_is_rejected_without_a_request(tavily):
    tavily.script = [
        results(*(article(n) for n in range(5))),
        SHORT, LONG, LONG, LONG, LONG,
    ]
    request_budget = budget(searches=6)

    with budget_scope(request_budget):
        app.run_web_search("latest AI news")

        with pytest.raises(CallBudgetExhausted) as caught:
            app.run_web_search("latest AI news")

    assert caught.value.kind == "search"
    assert len(tavily.posts) == 6
    assert request_budget.search_attempts == 6


def test_exhaustion_mid_extraction_stops_without_a_request(tavily):
    tavily.script = [results(article(1), article(2), article(3)), LONG]

    with budget_scope(budget(searches=2)):
        with pytest.raises(CallBudgetExhausted):
            app.run_web_search("latest AI news")

    assert [post["url"] for post in tavily.posts] == [
        tavily_transport.SEARCH_URL, tavily_transport.EXTRACT_URL,
    ]


def test_named_domain_retry_is_a_separate_admission(tavily):
    off_host = results({
        "title": "Python docs page",
        "url": "https://docs.python.org/3/whatsnew/",
        "content": "Docs content " * 10,
        "published_date": "",
        "score": 0.5,
    })
    tavily.script = [off_host, off_host]
    request_budget = budget()

    with budget_scope(request_budget):
        app.run_web_search("python release notes", include_domains=["python.org"])

    assert len(search_posts(tavily)) == request_budget.search_attempts == 2
    assert tavily.posts[0]["payload"]["include_domains"] == ["python.org"]
    assert tavily.posts[1]["payload"]["include_domains"] == ["www.python.org"]


def test_search_and_extraction_use_their_own_byte_ceilings(tavily):
    tavily.script = [results(article(1)), LONG]

    with budget_scope(budget()):
        app.run_web_search("latest AI news")

    assert [post["max_bytes"] for post in tavily.posts] == [SEARCH_MAX_BYTES, EXTRACT_MAX_BYTES]
    assert SEARCH_MAX_BYTES != app._request_limits.openai_max_bytes


def test_extraction_body_floor_never_extends_the_local_deadline(tavily):
    tavily.script = [results(article(1)), LONG]
    request_budget = budget(seconds=0.5, clock=lambda: 100.0)      # 0.5 s left, frozen

    with budget_scope(request_budget):
        app.run_web_search("latest AI news")

    search, extract = tavily.posts
    assert search["timeout"] == 0.5
    assert extract["timeout"] == 0.5                             # local HTTP deadline
    assert extract["payload"]["timeout"] == 1.0                  # Tavily's minimum


def test_timeout_and_provenance_come_from_one_observation(tavily, monkeypatch):
    tavily.script = [results(article(1), article(2)), LONG, LONG]
    reads = []
    now = [100.0]

    def clock():
        reads.append(now[0])
        now[0] += 0.25          # every read moves time: a second read would differ
        return reads[-1]

    request_budget = budget(seconds=12.0, clock=clock)
    selections = []
    real_select = request_budget.select_call_timeout

    def spy(cap):
        result = real_select(cap)
        selections.append((cap, result))
        return result

    monkeypatch.setattr(request_budget, "select_call_timeout", spy)
    monkeypatch.setattr(request_budget, "call_timeout", lambda cap: pytest.fail("second read"))
    reads.clear()

    with budget_scope(request_budget):
        app.run_web_search("latest AI news")

    assert len(selections) == len(tavily.posts) == 3
    # One read for the admission and one for the selection, per request.
    assert len(reads) == 2 * len(tavily.posts)

    for (cap, (timeout, selected)), post in zip(selections, tavily.posts):
        assert post["timeout"] == timeout

        if post["url"] == tavily_transport.EXTRACT_URL:
            assert cap == app.TAVILY_EXTRACT_TIMEOUT_SECONDS
            assert selected is True                    # under 12 s remain, cap 30
            assert post["payload"]["timeout"] == max(1.0, min(120.0, timeout))
        else:
            assert cap == app.SEARCH_TIMEOUT_SECONDS
            assert selected is False                   # 12 s remain, cap 10
            assert timeout == app.SEARCH_TIMEOUT_SECONDS


def test_a_deadline_tie_is_request_selected(tavily):
    tavily.script = [TransportDeadlineExceeded()]
    request_budget = budget(seconds=app.SEARCH_TIMEOUT_SECONDS, clock=lambda: 100.0)

    assert request_budget.select_call_timeout(app.SEARCH_TIMEOUT_SECONDS) == (
        app.SEARCH_TIMEOUT_SECONDS, True,
    )

    with budget_scope(request_budget):
        with pytest.raises(RequestDeadlineExceeded):
            app.run_web_search("history of roman aqueducts")

    assert len(tavily.posts) == 1


def test_a_cap_selected_timeout_is_an_ordinary_remote_failure(tavily, monkeypatch):
    tavily.script = [TransportDeadlineExceeded()]
    monkeypatch.setattr(app, "SEARCH_TIMEOUT_SECONDS", 1.0)

    with budget_scope(budget(seconds=30.0)):
        assert app.run_web_search("history of roman aqueducts") == ("unavailable", [])

    assert len(tavily.posts) == 1


def test_missing_key_is_unavailable_before_any_admission(tavily, monkeypatch):
    monkeypatch.setattr(app, "TAVILY_API_KEY", None)
    request_budget = budget()

    with budget_scope(request_budget):
        assert app.run_web_search("latest AI news") == ("unavailable", [])

    assert request_budget.search_attempts == 0
    assert tavily.posts == []
    assert tavily_transport.existing_holder() is None


# --- remote failures keep today's user-level behavior --------------------------------------------


REMOTE_FAILURES = {
    "connection": lambda: TransportConnectionError(),
    "http_400": lambda: TransportHTTPError(400),
    "http_401": lambda: TransportHTTPError(401),
    "http_429": lambda: TransportHTTPError(429),
    "http_432": lambda: TransportHTTPError(432),
    "http_500": lambda: TransportHTTPError(500),
    "redirect_302": lambda: TransportHTTPError(302),
    "invalid_json": lambda: InvalidJSONResponse(),
    "unsupported_encoding": lambda: UnsupportedContentEncoding(),
    "too_large": lambda: ResponseTooLarge(),
    "not_an_object": lambda: ["not", "an", "object"],
    "results_not_a_list": lambda: {"results": "x"},
    "result_not_an_object": lambda: {"results": [1]},
}


@pytest.mark.parametrize("failure", list(REMOTE_FAILURES))
def test_remote_search_failure_is_unavailable_after_one_request(tavily, failure):
    tavily.script = [REMOTE_FAILURES[failure]()]
    request_budget = budget()

    with budget_scope(request_budget):
        assert app.run_web_search("latest AI news") == ("unavailable", [])

    assert len(tavily.posts) == request_budget.search_attempts == 1   # no retry


@pytest.mark.parametrize("failure", list(REMOTE_FAILURES))
def test_remote_extraction_failure_skips_only_that_extraction(tavily, failure):
    tavily.script = [results(article(1), article(2), article(3)), LONG, REMOTE_FAILURES[failure](), LONG]

    with budget_scope(budget()):
        status, found = app.run_web_search("latest AI news")

    assert status == "ok"
    assert [item["url"] for item in found] == [article(1)["url"], article(3)["url"]]
    assert len(extract_posts(tavily)) == 3


@pytest.mark.parametrize("failure", ["http_500", "too_large", "results_not_a_list"])
def test_legacy_route_keeps_its_fixed_unavailable_reply(tavily, monkeypatch, failure):
    enable(monkeypatch)
    web_search_chat(monkeypatch)
    tavily.script = [REMOTE_FAILURES[failure]()]

    status, body, _ = post_chat()

    assert status == 200
    assert body["reply"] == LEGACY_UNAVAILABLE
    assert len(tavily.posts) == 1


# --- stop precedence at the API boundary ---------------------------------------------------------


def test_cancellation_wins_with_499(tavily, monkeypatch):
    enable(monkeypatch)
    web_search_chat(monkeypatch)

    def cancelled_then_failed():
        current_budget().cancel()
        return TransportConnectionError()

    tavily.script = [cancelled_then_failed]

    status, body, response = post_chat()

    assert (status, body) == (499, {"error": "request_cancelled"})
    assert response.headers["cache-control"] == "no-store"
    assert len(tavily.posts) == 1


def test_request_deadline_wins_with_504(tavily, monkeypatch):
    enable(monkeypatch)                       # 5 s request, 10 s search cap
    web_search_chat(monkeypatch)
    tavily.script = [TransportDeadlineExceeded()]

    status, body, _ = post_chat()

    assert (status, body) == (504, {"error": "request_timeout"})
    assert len(tavily.posts) == 1


@pytest.mark.parametrize("stop", ["cancel", "deadline"])
def test_request_stop_beats_unconfirmed_cleanup_and_still_quarantines(tavily, stop):
    now = [100.0]
    request_budget = budget(seconds=5.0, clock=lambda: now[0])

    def stopped_then_unconfirmed():
        if stop == "cancel":
            request_budget.cancel()
        else:
            now[0] += 10.0
        return TransportCleanupUnconfirmed("connection")

    tavily.script = [stopped_then_unconfirmed]

    with budget_scope(request_budget):
        with pytest.raises(RequestCancelled if stop == "cancel" else RequestDeadlineExceeded):
            app.run_web_search("history of roman aqueducts")

    assert tavily_transport.quarantined() is True


LOCAL_FAILURES = {
    "overloaded": lambda: TransportOverloaded(),
    "quarantined_transport": lambda: TransportQuarantined(),
    "closed_transport": lambda: TransportClosed(),
    "startup_failure": lambda: TransportError(),
    "cleanup_unconfirmed": lambda: TransportCleanupUnconfirmed("connection"),
    "cancelled_without_request_stop": lambda: TransportCleanupUnconfirmed("cancelled"),
}


@pytest.mark.parametrize("failure", list(LOCAL_FAILURES))
def test_local_transport_failure_is_service_unavailable(tavily, monkeypatch, capsys, failure):
    enable(monkeypatch)
    web_search_chat(monkeypatch)
    tavily.script = [LOCAL_FAILURES[failure]()]

    status, body, response = post_chat()

    assert (status, body) == SERVICE_UNAVAILABLE
    assert (status, body) != PROVIDER_UNAVAILABLE
    assert response.headers["cache-control"] == "no-store"
    assert len(tavily.posts) == 1
    assert_nothing_leaked(capsys, body)


@pytest.mark.parametrize("state", ["holder_quarantined", "slot_closed"])
def test_quarantined_or_closed_holder_is_service_unavailable_without_a_request(
    tavily, monkeypatch, state,
):
    enable(monkeypatch)
    web_search_chat(monkeypatch)

    if state == "holder_quarantined":
        tavily.slot.holder().quarantine()
    else:
        tavily.slot.close()

    status, body, _ = post_chat()

    assert (status, body) == SERVICE_UNAVAILABLE
    assert tavily.posts == []


def test_local_failure_in_extraction_is_not_skipped(tavily):
    tavily.script = [results(article(1), article(2)), TransportOverloaded(), LONG]

    with budget_scope(budget()):
        with pytest.raises(app.SearchTransportUnavailable):
            app.run_web_search("latest AI news")

    assert len(tavily.posts) == 2


def test_search_exhaustion_is_service_unavailable(tavily, monkeypatch):
    enable(monkeypatch, max_search_attempts=1)
    web_search_chat(monkeypatch)
    tavily.script = [results(article(1), article(2))]

    status, body, _ = post_chat()

    assert (status, body) == SERVICE_UNAVAILABLE
    assert len(tavily.posts) == 1


@pytest.mark.parametrize(
    "outcome, expected",
    [
        (CallBudgetExhausted("search"), SERVICE_UNAVAILABLE),
        (CallBudgetExhausted("model"), PROVIDER_UNAVAILABLE),
        ("search_transport", SERVICE_UNAVAILABLE),
        (RequestCancelled(), (499, {"error": "request_cancelled"})),
        (RequestDeadlineExceeded(), (504, {"error": "request_timeout"})),
    ],
    ids=["search_exhausted", "model_exhausted", "search_transport", "cancelled", "deadline"],
)
def test_boundary_codes(monkeypatch, outcome, expected):
    enable(monkeypatch)

    def failing_chat(message, history, request=None, session_id=None):
        if outcome == "search_transport":
            raise app.SearchTransportUnavailable()
        raise outcome

    monkeypatch.setattr(app, "chat", failing_chat)

    status, body, _ = post_chat()

    assert (status, body) == expected


@pytest.mark.parametrize("defect", ["type_error", "value_error", "incompatible_settings", "invalid_max_bytes"])
def test_defects_are_internal_errors_without_leaks(tavily, monkeypatch, capsys, defect):
    enable(monkeypatch)
    web_search_chat(monkeypatch)

    if defect == "type_error":
        tavily.script = [TypeError(SECRET)]
    elif defect == "value_error":
        tavily.script = [ValueError(SECRET)]
    elif defect == "incompatible_settings":
        tavily.slot.holder().get_or_create(
            TransportLimits(**{**TAVILY_TRANSPORT_LIMITS.__dict__, "max_outstanding": 99})
        )
    else:
        # A real bounded transport validates max_bytes before any startup.
        monkeypatch.setattr(tavily_transport, "_SLOT", TavilyTransportSlot())
        enable(monkeypatch, tavily_search_max_bytes=0)

    status, body, response = post_chat()

    assert (status, body) == INTERNAL_ERROR
    assert response.headers["cache-control"] == "no-store"
    assert_nothing_leaked(capsys, body)

    if defect == "invalid_max_bytes":
        report = tavily_transport.close_transport()
        assert report is None or report.clean


def test_missing_limits_under_a_budget_is_an_internal_error(tavily, monkeypatch):
    monkeypatch.setattr(app, "_request_limits", None)

    with budget_scope(budget()):
        with pytest.raises(app.ChatInternalError):
            app.run_web_search("latest AI news")

    assert tavily.posts == []


# --- native-tool path -----------------------------------------------------------------------------


def _function_call(query):
    return {"output": [{
        "type": "function_call",
        "call_id": "call_search",
        "name": "search_web",
        "arguments": json.dumps({"query": query}),
    }]}


def _final(text):
    return {"status": "completed", "output": [{
        "type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }]}


def _tool_outputs(payload):
    return [
        json.loads(item["output"])
        for item in payload["input"]
        if isinstance(item, dict) and item.get("type") == "function_call_output"
    ]


@pytest.mark.parametrize("failure", ["http_500", "connection", "results_not_a_list"])
def test_v31_receives_unavailable_on_a_remote_failure(tavily, openai, failure):
    openai["script"] = [_function_call("latest AI news"), _final("Live search was unavailable.")]
    tavily.script = [REMOTE_FAILURES[failure]()]
    request_budget = budget()

    with budget_scope(request_budget):
        reply = app._run_v31_native_tool_chat(
            "search the web for the latest AI news", [], {"memory": [], "search_times": []},
        )

    assert _tool_outputs(openai["posts"][1]["payload"]) == [{"status": "unavailable", "results": []}]
    assert "**Sources**" not in reply
    assert request_budget.search_attempts == 1
    assert request_budget.model_attempts == 2


def test_v31_local_search_outage_ends_the_request(tavily, openai, monkeypatch):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", True)
    monkeypatch.setattr(app, "classify_request", lambda message, history: "web_search")
    monkeypatch.setattr(app, "requires_web_verification", lambda message, history: False)
    legacy = []
    monkeypatch.setattr(app, "invoke_llm", lambda *a, **k: legacy.append(1))
    openai["script"] = [_function_call("latest AI news")]
    tavily.script = [TransportOverloaded()]

    with budget_scope(budget()):
        with pytest.raises(app.SearchTransportUnavailable):
            app.chat("search the web for the latest AI news", [], session_id="tavily-native-local")

    assert legacy == []
    assert len(openai["posts"]) == 1


def test_v31_policy_discloses_unavailable_or_limited_search():
    policy = app.V31_NATIVE_TOOL_POLICY

    rule = (
        '- If search_web returns status "unavailable" or "limited", say plainly that\n'
        "  live web search could not be used, or was limited, for this request. Do\n"
        "  not claim that any current information was verified, and do not cite or\n"
        "  invent sources. You may add clearly qualified general knowledge when it is\n"
        "  useful.\n"
    )
    assert rule in policy
    assert "tavily" not in policy.lower()


# --- lifecycle ------------------------------------------------------------------------------------


def test_holder_is_created_only_by_a_bounded_operation(tavily):
    assert tavily_transport.existing_holder() is None
    assert tavily_transport.quarantined() is False

    with budget_scope(budget()):
        tavily.script = [results()]
        app.run_web_search("history of roman aqueducts")

    assert tavily_transport.existing_holder() is not None


def test_shutdown_never_creates_a_holder_and_closes_the_slot():
    with TestClient(app.api):
        pass

    assert tavily_transport.existing_holder() is None

    with pytest.raises(TransportUnavailable) as caught:
        tavily_transport._SLOT.holder()

    assert caught.value.reason == "closed"
    assert tavily_transport.existing_holder() is None


def test_shutdown_is_idempotent_and_closes_only_tavily(tavily, openai):
    openai_transport = openai["holder"].get_or_create(OPENAI_TRANSPORT_LIMITS)
    transport = tavily.slot.holder().get_or_create(TAVILY_TRANSPORT_LIMITS)

    app._close_tavily_transport()
    app._close_tavily_transport()

    assert transport.closes == [TAVILY_TRANSPORT_LIMITS.close_timeout_seconds] * 2
    assert tavily.slot.existing().closed is True
    assert openai["holder"].closed is False
    assert openai_transport is not transport


# --- real bounded transport against a loopback server ---------------------------------------------


class LoopbackTavily:
    """A loopback HTTP server standing in for Tavily's endpoints."""

    def __init__(self, status=200, body=b'{"results": []}', headers=None):
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                outer.requests.append({
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": self.rfile.read(length),
                })
                self.send_response(status)

                for name, value in (headers or {}).items():
                    self.send_header(name, value)

                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(10)


@pytest.fixture
def loopback(monkeypatch):
    servers = []
    monkeypatch.setattr(tavily_transport, "_SLOT", TavilyTransportSlot())

    def start(**kwargs):
        server = LoopbackTavily(**kwargs)
        servers.append(server)
        monkeypatch.setattr(tavily_transport, "SEARCH_URL", server.url + "/search")
        monkeypatch.setattr(tavily_transport, "EXTRACT_URL", server.url + "/extract")
        return server

    yield start

    report = tavily_transport.close_transport()

    for server in servers:
        server.close()

    assert report is None or report.clean


def test_loopback_round_trip_through_the_real_bounded_transport(loopback, monkeypatch):
    server = loopback(body=json.dumps(results(page(1))).encode())
    enable(monkeypatch)

    with budget_scope(budget()):
        status, found = app.run_web_search("history of roman aqueducts")

    assert status == "ok" and [item["url"] for item in found] == [page(1)["url"]]
    (request,) = server.requests
    assert request["path"] == "/search"
    assert json.loads(request["body"]) == SEARCH_VARIANTS["basic"][2]
    assert request["headers"]["authorization"] == f"Bearer {TAVILY_KEY}"
    assert request["headers"]["content-type"] == "application/json"
    assert request["headers"]["accept-encoding"] == "identity"
    assert "x-client-source" not in request["headers"]


def test_loopback_redirect_is_not_followed(loopback, monkeypatch):
    server = loopback(status=302, body=b"{}", headers={"Location": "/elsewhere"})
    enable(monkeypatch)
    request_budget = budget()

    with budget_scope(request_budget):
        assert app.run_web_search("history of roman aqueducts") == ("unavailable", [])

    assert [request["path"] for request in server.requests] == ["/search"]
    assert request_budget.search_attempts == 1


def test_loopback_oversized_response_fails_safely(loopback, monkeypatch, capsys):
    body = json.dumps(results(page(1), page(2), page(3))).encode()
    server = loopback(body=body)
    enable(monkeypatch, tavily_search_max_bytes=len(body) - 1)

    with budget_scope(budget()):
        assert app.run_web_search("history of roman aqueducts") == ("unavailable", [])

    assert len(server.requests) == 1
    assert "SEARCH ERROR: ResponseTooLarge" in capsys.readouterr().out


def test_loopback_malformed_json_fails_safely(loopback, monkeypatch):
    server = loopback(body=b"{not json")
    enable(monkeypatch)

    with budget_scope(budget()):
        assert app.run_web_search("history of roman aqueducts") == ("unavailable", [])

    assert len(server.requests) == 1


# --- the external-network guard (conftest.py) -----------------------------------------------------


EXTERNAL = ("192.0.2.10", 443)          # TEST-NET-1: never routed


def test_guard_blocks_raw_sockets_and_name_resolution(external_network_guard):
    with socket.socket() as sock:
        with pytest.raises(ConnectionRefusedError):
            sock.connect(EXTERNAL)

    with pytest.raises(socket.gaierror):
        socket.getaddrinfo("api.tavily.com", 443)

    with pytest.raises(OSError):
        socket.create_connection(EXTERNAL, timeout=1)

    assert [kind for kind, _ in external_network_guard.take()] == [
        "connect", "getaddrinfo", "getaddrinfo",
    ]


def test_guard_blocks_asyncio_connections(external_network_guard):
    async def attempt():
        loop = asyncio.get_running_loop()

        with socket.socket() as sock:
            sock.setblocking(False)
            await loop.sock_connect(sock, EXTERNAL)

    with pytest.raises(ConnectionRefusedError):
        asyncio.run(attempt())

    assert [kind for kind, _ in external_network_guard.take()] == ["sock_connect"]


def test_guard_blocks_the_tavily_sdk_with_a_placeholder_key(external_network_guard):
    import requests
    import tavily

    with pytest.raises(requests.exceptions.ConnectionError):
        tavily.TavilyClient(api_key="test-tavily-key").search("anything")

    # The feature-off app path swallows the error as "unavailable", but the
    # guard still records (and would fail the test for) the attempt.
    assert app.run_web_search("history of roman aqueducts") == ("unavailable", [])
    assert [kind for kind, _ in external_network_guard.take()] == ["requests", "requests"]


def test_guard_blocks_the_direct_bounded_tavily_path(external_network_guard, monkeypatch):
    monkeypatch.setattr(tavily_transport, "_SLOT", TavilyTransportSlot())
    enable(monkeypatch)

    try:
        with budget_scope(budget()):
            assert app.run_web_search("history of roman aqueducts") == ("unavailable", [])
    finally:
        report = tavily_transport.close_transport()

    assert report is None or report.clean
    attempts = external_network_guard.take()
    assert attempts and all(host == "api.tavily.com" for _, host in attempts)


def test_guard_leaves_loopback_usable(external_network_guard):
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(4)
        server.settimeout(5)

        for target in (server.getsockname(), ("localhost", server.getsockname()[1])):
            with socket.create_connection(target, timeout=5):
                accepted, _ = server.accept()
                accepted.close()

    assert external_network_guard.take() == []
