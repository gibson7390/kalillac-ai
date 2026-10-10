"""Backend /api/chat response contract.

- Every /api/chat response is Cache-Control: no-store, budgets on or off.
- Model-attempt exhaustion is 422 processing_limit_reached; a genuine
  OpenAI failure stays 503 model_provider_unavailable.
- Search-attempt exhaustion after usable results keeps exactly those
  results, marks the native tool result coverage "limited", and appends one
  fixed application-written notice before Sources.

Every provider is scripted; conftest.py blocks all non-loopback network
access and refuses a real bounded Tavily transport.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
import re
import sys
import time

import pytest
from fastapi.testclient import TestClient


CANDIDATE_DIR = Path(__file__).resolve().parents[1]

if str(CANDIDATE_DIR) not in sys.path:
    sys.path.insert(0, str(CANDIDATE_DIR))

import app_fastapi_candidate as app
from kalillac_routing import provider_transport, tavily_transport
from kalillac_routing.bounded_transport import TransportHTTPError
from kalillac_routing.request_budget import (
    CallBudgetExhausted,
    RequestBudget,
    RequestCancelled,
    RequestDeadlineExceeded,
    budget_scope,
    current_budget,
)
from kalillac_routing.request_limits import RequestLimits, TransportLimits
from kalillac_routing.tavily_transport import TavilyTransportSlot


TAVILY_KEY = "test-tavily-key-must-not-leak"
OPENAI_KEY = "test-openai-key-must-not-leak"
NOTICE = (
    "Live search was limited for this request. This answer uses only the "
    "sources listed below."
)
TRANSPORT = TransportLimits(
    max_outstanding=4,
    dns_threads=1,
    max_pending_dns=4,
    cancel_poll_interval_seconds=0.02,
    backstop_grace_seconds=0.5,
    cleanup_grace_seconds=1.0,
    close_timeout_seconds=3.0,
)
BASE_LIMITS = {
    "deadline_seconds": 5.0,
    "queue_wait_seconds": 2.0,
    "max_model_attempts": 6,
    "max_search_attempts": 6,
    "transport": TRANSPORT,
    "openai_max_bytes": 2097152,
    "tavily_transport": TRANSPORT,
    "tavily_search_max_bytes": 262144,
    "tavily_extract_max_bytes": 524288,
}
SERVICE_UNAVAILABLE = (503, {"error": "service_unavailable"})
PROVIDER_UNAVAILABLE = (503, {"error": "model_provider_unavailable"})
PROCESSING_LIMIT = (422, {"error": "processing_limit_reached"})


class ScriptExhausted(BaseException):
    """A scripted provider received more requests than the test planned."""


def _refuse(*args, **kwargs):
    raise ScriptExhausted()


# --- fixtures and helpers ------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(app, "_request_limits", None)
    monkeypatch.setattr(app, "_chat_semaphore", None)
    monkeypatch.setattr(app, "_chat_waiting", 0)
    monkeypatch.setattr(app, "_session_locks", {})
    monkeypatch.setattr(app, "_usage_meter", None)
    monkeypatch.setattr(app, "_chats_admitted", 0)
    monkeypatch.setattr(app.urllib.request, "urlopen", _refuse)
    monkeypatch.setattr(app, "_OPENAI_TRANSPORT", provider_transport.TransportHolder(factory=_refuse))
    monkeypatch.setattr(app, "OPENAI_API_KEY", OPENAI_KEY)
    monkeypatch.setattr(app, "OPENAI_MODEL", "configured-test-model")
    monkeypatch.setattr(app, "OPENAI_REASONING_EFFORT", "low")
    monkeypatch.setattr(app, "TAVILY_API_KEY", TAVILY_KEY)
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", False)
    monkeypatch.setattr(app, "requires_web_verification", lambda message, history: False)


def enable(monkeypatch, **overrides):
    monkeypatch.setattr(app, "_request_limits", RequestLimits(**{**BASE_LIMITS, **overrides}))


def budget(searches=6, models=6, seconds=30.0, clock=time.monotonic):
    return RequestBudget(
        duration_seconds=seconds,
        max_model_attempts=models,
        max_search_attempts=searches,
        clock=clock,
    )


def post_chat(message="latest AI news"):
    with TestClient(app.api) as client:
        return client.post("/api/chat", json={"message": message, "history": []})


def result(response):
    return response.status_code, response.json()


@pytest.fixture
def providers(monkeypatch):
    """Scripted budgeted OpenAI and Tavily transports, each in its own holder."""
    state = {"openai": [], "openai_posts": [], "tavily": [], "tavily_posts": []}

    def play(script, value):
        value = value() if callable(value) else value

        if isinstance(value, BaseException):
            raise value

        return copy.deepcopy(value)

    class OpenAITransport:
        def __init__(self, **settings):
            pass

        def post_json(self, url, payload, *, headers, timeout, max_bytes, cancelled=None):
            state["openai_posts"].append(copy.deepcopy(payload))

            if not state["openai"]:
                raise ScriptExhausted()

            return play(state["openai"], state["openai"].pop(0))

        def close(self, timeout):
            return None

    class TavilyTransport:
        def __init__(self, **settings):
            pass

        def post_json(self, url, payload, *, headers, timeout, max_bytes, cancelled=None):
            state["tavily_posts"].append({"url": url, "payload": copy.deepcopy(payload)})

            if not state["tavily"]:
                raise ScriptExhausted()

            return play(state["tavily"], state["tavily"].pop(0))

        def close(self, timeout):
            return None

    monkeypatch.setattr(app, "_OPENAI_TRANSPORT", provider_transport.TransportHolder(factory=OpenAITransport))
    monkeypatch.setattr(tavily_transport, "_SLOT", TavilyTransportSlot(factory=TavilyTransport))
    enable(monkeypatch)
    return state


def article(n):
    return {
        "title": f"Story {n} about AI models",
        "url": f"https://news.example.org/2026/10/05/story-{n}",
        "content": f"RAW-SNIPPET-{n}",
        "published_date": "2026-10-05",
        "score": 0.9,
    }


def page(n):
    return {
        "title": f"Python docs page {n}",
        "url": f"https://docs.python.org/3/page-{n}",
        "content": f"Docs content {n} " * 10,
        "published_date": "",
        "score": 0.5,
    }


def fetched(n):
    return {"results": [{"raw_content": f"FETCHED-ARTICLE-{n} " + "Article text. " * 30}]}


def reply(text, cut_off=False):
    data = {
        "status": "incomplete" if cut_off else "completed",
        "output": [{
            "type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        }],
    }

    if cut_off:
        data["incomplete_details"] = {"reason": app.OUTPUT_TOKEN_LIMIT_REASON}

    return data


def search_call(query):
    return {"output": [{
        "type": "function_call",
        "call_id": "call_search",
        "name": "search_web",
        "arguments": json.dumps({"query": query}),
    }]}


def tool_outputs(payload):
    return [
        json.loads(item["output"])
        for item in payload["input"]
        if isinstance(item, dict) and item.get("type") == "function_call_output"
    ]


def sources_of(text):
    head, _, tail = text.partition("\n\n**Sources**\n\n")
    return head, [line for line in tail.splitlines() if line.startswith("- [")]


def legacy_web_search(monkeypatch, domains=None):
    monkeypatch.setattr(app, "classify_request", lambda message, history: "web_search")
    monkeypatch.setattr(app, "get_search_domain_filters", lambda message: list(domains or []))


def native_web_search(monkeypatch):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", True)
    monkeypatch.setattr(app, "classify_request", lambda message, history: "web_search")
    monkeypatch.setattr(app, "get_search_domain_filters", lambda message: [])
    monkeypatch.setattr(app, "get_authoritative_search_domains", lambda message, query: [])


# --- A. Cache-Control: no-store on every /api/chat response ------------------------------------


def _fake_chat(outcome):
    def chat(message, history, request=None, session_id=None):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return chat


@pytest.mark.parametrize("budgets", ["off", "on"])
@pytest.mark.parametrize(
    "outcome, expected",
    [
        ("plain reply", 200),
        (RuntimeError("boom"), (500, {"error": "internal_error"})),
        (app.OpenAIConfigurationUnavailable(), SERVICE_UNAVAILABLE),
        (app.ModelProviderUnavailable(), PROVIDER_UNAVAILABLE),
    ],
    ids=["success", "internal_error", "service_unavailable", "model_provider_unavailable"],
)
def test_every_chat_outcome_is_no_store(monkeypatch, budgets, outcome, expected):
    if budgets == "on":
        enable(monkeypatch)
    monkeypatch.setattr(app, "chat", _fake_chat(outcome))

    response = post_chat("hello")

    if expected == 200:
        assert response.status_code == 200
        assert set(response.json()) == {"reply", "session_id"}
    else:
        assert result(response) == expected

    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "outcome, expected",
    [
        (RequestDeadlineExceeded(), (504, {"error": "request_timeout"})),
        (RequestCancelled(), (499, {"error": "request_cancelled"})),
        (CallBudgetExhausted("model"), PROCESSING_LIMIT),
        (CallBudgetExhausted("search"), SERVICE_UNAVAILABLE),
    ],
    ids=["request_timeout", "request_cancelled", "processing_limit_reached", "search_exhausted"],
)
def test_budget_outcomes_are_no_store(monkeypatch, outcome, expected):
    enable(monkeypatch)
    monkeypatch.setattr(app, "chat", _fake_chat(outcome))

    response = post_chat("hello")

    assert result(response) == expected
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("budgets", ["off", "on"])
@pytest.mark.parametrize(
    "body, expected",
    [
        (b"not json", (400, {"error": "invalid_json"})),
        (b"[1, 2]", (400, {"error": "invalid_body"})),
        (b'{"message": "   "}', (422, {"error": "empty_message"})),
    ],
    ids=["invalid_json", "invalid_body", "empty_message"],
)
def test_validation_errors_are_no_store(monkeypatch, budgets, body, expected):
    if budgets == "on":
        enable(monkeypatch)

    with TestClient(app.api) as client:
        response = client.post(
            "/api/chat", content=body, headers={"Content-Type": "application/json"},
        )

    assert result(response) == expected
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("budgets", ["off", "on"])
def test_busy_is_no_store(monkeypatch, budgets):
    if budgets == "on":
        enable(monkeypatch)
        monkeypatch.setattr(app, "_chats_admitted", app.MAX_CONCURRENT_CHATS + app.MAX_QUEUED_CHATS)
    else:
        monkeypatch.setattr(app, "_chat_semaphore", asyncio.Semaphore(0))
        monkeypatch.setattr(app, "_chat_waiting", app.MAX_QUEUED_CHATS)

    monkeypatch.setattr(app, "chat", _fake_chat("never reached"))
    response = post_chat("hello")

    assert result(response) == (429, {"error": "busy"})
    assert response.headers["cache-control"] == "no-store"


def test_no_store_is_limited_to_the_chat_route():
    with TestClient(app.api) as client:
        response = client.get("/api/health")

    assert response.json() == {"status": "ok"}
    assert "cache-control" not in response.headers


# --- B. model-attempt exhaustion -----------------------------------------------------------------


def test_model_attempt_exhaustion_is_processing_limit_reached(providers, monkeypatch, capsys):
    enable(monkeypatch, max_model_attempts=1)
    monkeypatch.setattr(app, "classify_request", lambda message, history: "general")
    # The first answer is cut off; its one continuation cannot be admitted.
    providers["openai"] = [reply("Partial answer", cut_off=True)]

    response = post_chat("Explain rivers in detail.")

    assert result(response) == PROCESSING_LIMIT
    assert result(response) != PROVIDER_UNAVAILABLE
    assert response.headers["cache-control"] == "no-store"
    assert len(providers["openai_posts"]) == 1           # no further request, no other provider
    out = capsys.readouterr()
    assert "Explain rivers" not in out.out + out.err


def test_genuine_openai_failure_stays_model_provider_unavailable(providers, monkeypatch):
    enable(monkeypatch)
    monkeypatch.setattr(app, "classify_request", lambda message, history: "general")
    providers["openai"] = [TransportHTTPError(500)]

    response = post_chat("Explain rivers.")

    assert result(response) == PROVIDER_UNAVAILABLE
    assert len(providers["openai_posts"]) == 1


def test_unknown_exhaustion_kind_is_an_internal_error(monkeypatch):
    enable(monkeypatch)
    monkeypatch.setattr(app, "chat", _fake_chat(CallBudgetExhausted("unexpected")))

    assert result(post_chat("hello")) == (500, {"error": "internal_error"})


# --- C. partial search preservation: search layer -------------------------------------------------


def test_news_exhaustion_keeps_only_fetched_articles(providers):
    providers["tavily"] = [{"results": [article(1), article(2), article(3)]}, fetched(1)]
    request_budget = budget(searches=2)

    with budget_scope(request_budget):
        status, results = app.run_web_search("latest AI news")

    assert status == "partial"
    assert [item["url"] for item in results] == [article(1)["url"]]
    assert "FETCHED-ARTICLE-1" in results[0]["content"]
    assert all("RAW-SNIPPET" not in item["content"] for item in results)
    assert len(providers["tavily_posts"]) == request_budget.search_attempts == 2


def test_named_site_exhaustion_keeps_first_pass_results(providers):
    providers["tavily"] = [{"results": [page(1), page(2)]}]
    request_budget = budget(searches=1)

    with budget_scope(request_budget):
        status, results = app.run_web_search("python release notes", include_domains=["python.org"])

    assert status == "partial"
    assert [item["url"] for item in results] == [page(1)["url"], page(2)["url"]]
    assert len(providers["tavily_posts"]) == 1                    # the retry was never sent


@pytest.mark.parametrize("first", ["none_fetched_yet", "short_article_skipped"])
def test_exhaustion_before_any_usable_result_still_stops_the_request(providers, first):
    script = [{"results": [article(1), article(2)]}]
    searches = 1

    if first == "short_article_skipped":
        script.append({"results": [{"raw_content": "short"}]})
        searches = 2

    providers["tavily"] = script

    with budget_scope(budget(searches=searches)):
        with pytest.raises(CallBudgetExhausted):
            app.run_web_search("latest AI news")

    assert len(providers["tavily_posts"]) == searches


def test_exhausted_initial_search_still_stops_the_request(providers):
    request_budget = budget(searches=1)
    request_budget.admit_search_attempt()

    with budget_scope(request_budget):
        with pytest.raises(CallBudgetExhausted):
            app.run_web_search("latest AI news")

    assert providers["tavily_posts"] == []


@pytest.mark.parametrize("stop", ["cancel", "deadline"])
def test_cancellation_and_deadline_override_partial_results(providers, stop):
    now = [100.0]
    request_budget = budget(searches=2, seconds=5.0, clock=lambda: now[0])

    def fetched_then_stopped():
        if stop == "cancel":
            request_budget.cancel()
        else:
            now[0] += 10.0
        return fetched(1)

    providers["tavily"] = [{"results": [article(1), article(2)]}, fetched_then_stopped]

    with budget_scope(request_budget):
        with pytest.raises(RequestCancelled if stop == "cancel" else RequestDeadlineExceeded):
            app.run_web_search("latest AI news")

    assert len(providers["tavily_posts"]) == 2


def test_complete_search_is_ok(providers):
    providers["tavily"] = [{"results": [article(1)]}, fetched(1)]

    with budget_scope(budget()):
        status, results = app.run_web_search("latest AI news")

    assert status == "ok" and len(results) == 1


# --- C. legacy route --------------------------------------------------------------------------


def test_legacy_news_partial_answers_from_fetched_articles_only(providers, monkeypatch, capsys):
    enable(monkeypatch, max_search_attempts=2)
    legacy_web_search(monkeypatch)
    providers["tavily"] = [{"results": [article(1), article(2), article(3)]}, fetched(1)]
    providers["openai"] = [reply("Here is the news.")]

    response = post_chat("latest AI news")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"reply", "session_id"}            # no public coverage field
    answer, sources = sources_of(body["reply"])
    assert answer == f"Here is the news.\n\n{NOTICE}"
    assert body["reply"].count(NOTICE) == 1
    assert sources == [f"- [{article(1)['title']}]({article(1)['url']})"]
    assert len(providers["tavily_posts"]) == 2             # nothing after exhaustion
    prompt = json.dumps(providers["openai_posts"][0])
    assert "FETCHED-ARTICLE-1" in prompt
    assert article(2)["url"] not in prompt and "RAW-SNIPPET" not in prompt
    out = capsys.readouterr()
    for secret in (TAVILY_KEY, OPENAI_KEY, "latest AI news", "FETCHED-ARTICLE"):
        assert secret not in out.out + out.err


def test_legacy_named_site_partial_keeps_first_pass_sources(providers, monkeypatch):
    enable(monkeypatch, max_search_attempts=1)
    legacy_web_search(monkeypatch, domains=["python.org"])
    providers["tavily"] = [{"results": [page(1), page(2)]}]
    providers["openai"] = [reply("Python notes.")]

    body = post_chat("python release notes").json()

    answer, sources = sources_of(body["reply"])
    assert answer == f"Python notes.\n\n{NOTICE}"
    assert sources == [
        f"- [{page(1)['title']}]({page(1)['url']})",
        f"- [{page(2)['title']}]({page(2)['url']})",
    ]
    assert len(providers["tavily_posts"]) == 1


def test_legacy_complete_search_has_no_notice(providers, monkeypatch):
    enable(monkeypatch)
    legacy_web_search(monkeypatch)
    providers["tavily"] = [{"results": [article(1)]}, fetched(1)]
    providers["openai"] = [reply("Here is the news.")]

    body = post_chat("latest AI news").json()

    assert NOTICE not in body["reply"]
    assert "**Sources**" in body["reply"]


def test_legacy_zero_usable_results_stays_service_unavailable(providers, monkeypatch):
    enable(monkeypatch, max_search_attempts=1)
    legacy_web_search(monkeypatch)
    providers["tavily"] = [{"results": [article(1), article(2)]}]

    response = post_chat("latest AI news")

    assert result(response) == SERVICE_UNAVAILABLE
    assert NOTICE not in response.text and "Sources" not in response.text
    assert providers["openai_posts"] == []


def test_legacy_partial_still_yields_to_cancellation(providers, monkeypatch):
    enable(monkeypatch, max_search_attempts=2)
    legacy_web_search(monkeypatch)

    def fetched_then_cancelled():
        current_budget().cancel()
        return fetched(1)

    providers["tavily"] = [{"results": [article(1), article(2)]}, fetched_then_cancelled]

    response = post_chat("latest AI news")

    assert result(response) == (499, {"error": "request_cancelled"})
    assert providers["openai_posts"] == []


def test_model_copy_of_the_notice_is_not_duplicated(providers, monkeypatch):
    enable(monkeypatch, max_search_attempts=2)
    legacy_web_search(monkeypatch)
    providers["tavily"] = [{"results": [article(1), article(2)]}, fetched(1)]
    providers["openai"] = [reply(f"Here is the news.\n\n{NOTICE}")]

    body = post_chat("latest AI news").json()

    assert body["reply"].count(NOTICE) == 1
    assert sources_of(body["reply"])[0] == f"Here is the news.\n\n{NOTICE}"


def test_feature_off_search_is_unchanged_except_no_store(monkeypatch):
    import tavily

    calls = []

    class FakeTavily:
        def __init__(self, api_key=None, **kwargs):
            pass

        def search(self, **kwargs):
            calls.append(("search", kwargs))
            return {"results": [article(1)]}

        def extract(self, **kwargs):
            calls.append(("extract", kwargs))
            return fetched(1)

    monkeypatch.setattr(tavily, "TavilyClient", FakeTavily)
    legacy_web_search(monkeypatch)
    monkeypatch.setattr(app, "invoke_llm", lambda messages, max_tokens=None: app.SimpleNamespace(
        content="Here is the news.", incomplete=False, incomplete_reason=None,
    ))

    response = post_chat("latest AI news")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert NOTICE not in response.json()["reply"]
    assert [name for name, _ in calls] == ["search", "extract"]
    assert calls[0][1]["timeout"] == app.SEARCH_TIMEOUT_SECONDS
    assert "timeout" not in calls[1][1]
    assert tavily_transport.existing_holder() is None


# --- C. native path ---------------------------------------------------------------------------


def test_native_complete_search_reports_complete_coverage(providers, monkeypatch):
    enable(monkeypatch)
    native_web_search(monkeypatch)
    providers["openai"] = [search_call("latest AI news"), reply("The news.")]
    providers["tavily"] = [{"results": [article(1)]}, fetched(1)]

    body = post_chat("search the latest AI news").json()

    (output,) = tool_outputs(providers["openai_posts"][1])
    assert output["status"] == "ok" and output["coverage"] == "complete"
    assert NOTICE not in body["reply"]
    assert set(body) == {"reply", "session_id"}


def test_native_partial_search_reports_limited_coverage(providers, monkeypatch):
    enable(monkeypatch, max_search_attempts=2)
    native_web_search(monkeypatch)
    providers["openai"] = [search_call("latest AI news"), reply(f"The news.\n\n{NOTICE}")]
    providers["tavily"] = [{"results": [article(1), article(2), article(3)]}, fetched(1)]

    response = post_chat("search the latest AI news")
    body = response.json()

    (output,) = tool_outputs(providers["openai_posts"][1])
    assert output["status"] == "ok" and output["coverage"] == "limited"
    assert [item["url"] for item in output["results"]] == [article(1)["url"]]
    assert "RAW-SNIPPET" not in json.dumps(output)
    assert set(body) == {"reply", "session_id"}
    assert "coverage" not in response.text
    answer, sources = sources_of(body["reply"])
    assert answer == f"The news.\n\n{NOTICE}"
    assert body["reply"].count(NOTICE) == 1
    assert sources == [f"- [{article(1)['title']}]({article(1)['url']})"]
    assert len(providers["tavily_posts"]) == 2


def test_session_limiter_keeps_its_no_results_meaning(providers, monkeypatch):
    enable(monkeypatch)
    native_web_search(monkeypatch)
    monkeypatch.setattr(app, "session_search_allowed", lambda state: False)
    providers["openai"] = [search_call("latest AI news"), reply("Search was limited.")]

    body = post_chat("search the latest AI news").json()

    assert tool_outputs(providers["openai_posts"][1]) == [{"status": "limited", "results": []}]
    assert NOTICE not in body["reply"] and "**Sources**" not in body["reply"]
    assert providers["tavily_posts"] == []


def test_native_remote_failure_stays_unavailable(providers, monkeypatch):
    enable(monkeypatch)
    native_web_search(monkeypatch)
    providers["openai"] = [search_call("latest AI news"), reply("Search was unavailable.")]
    providers["tavily"] = [TransportHTTPError(500)]

    body = post_chat("search the latest AI news").json()

    assert tool_outputs(providers["openai_posts"][1]) == [{"status": "unavailable", "results": []}]
    assert NOTICE not in body["reply"]


def test_native_policy_describes_limited_coverage():
    rule = (
        '- When a successful search_web result has coverage "limited", use only the\n'
        "  returned results for claims requiring current verification, do not imply\n"
        "  that the search was exhaustive, and cite only the returned sources. Do not\n"
        "  add a separate coverage notice; Kalillac appends the fixed notice.\n"
    )

    assert rule in app.V31_NATIVE_TOOL_POLICY
    assert "tavily" not in app.V31_NATIVE_TOOL_POLICY.lower()
    assert app.LIMITED_SEARCH_NOTICE == NOTICE


# --- round 2: the notice is one standalone paragraph; answer text is kept -----------------------


FENCE = "`" * 3


def standalone_notices(text):
    """Paragraphs that are exactly the notice (not raw substring matches)."""
    return [part for part in re.split(r"\n[ \t]*\n", text) if part.strip() == NOTICE]


def test_trailing_standalone_copy_is_deduplicated():
    reply_text = app.with_limited_search_notice(f"Answer.\n\n{NOTICE}")

    assert reply_text == f"Answer.\n\n{NOTICE}"
    assert len(standalone_notices(reply_text)) == 1


def test_multiple_standalone_copies_become_one():
    reply_text = app.with_limited_search_notice(
        f"{NOTICE}\n\nAnswer.\n\n{NOTICE}\n\n  {NOTICE}  \n\n{NOTICE}"
    )

    assert reply_text == f"Answer.\n\n{NOTICE}"


def test_embedded_occurrences_are_answer_content_and_are_kept():
    quoted = f"> {NOTICE}"
    inline = f"The page said: {NOTICE} That was all."
    code = f"{FENCE}\nbanner\n\n{NOTICE}\n\nmore\n{FENCE}"
    original = f"{inline}\n\n{quoted}\n\n{code}\n\nEnd."

    reply_text = app.with_limited_search_notice(original)

    assert reply_text == f"{original}\n\n{NOTICE}"
    # Four raw occurrences, but only the application's is a standalone paragraph
    # outside a code block.
    assert reply_text.count(NOTICE) == 4
    assert reply_text.endswith(f"End.\n\n{NOTICE}")


def test_paragraph_breaks_are_preserved():
    original = "First line\n  \nSecond paragraph\n\n\nThird"

    assert app.with_limited_search_notice(original) == f"{original}\n\n{NOTICE}"


def test_notice_follows_the_cut_off_notice():
    marked = app.mark_incomplete_reply("Partial answer")
    reply_text = app.with_limited_search_notice(marked)

    assert reply_text == f"Partial answer\n\n{app.INCOMPLETE_RESPONSE_NOTICE}\n\n{NOTICE}"


def test_embedded_model_text_survives_end_to_end(providers, monkeypatch):
    enable(monkeypatch, max_search_attempts=2)
    legacy_web_search(monkeypatch)
    providers["tavily"] = [{"results": [article(1), article(2)]}, fetched(1)]
    embedded = f"The site banner reads: {NOTICE} Nothing else changed."
    providers["openai"] = [reply(f"{embedded}\n\n{NOTICE}")]

    body = post_chat("latest AI news").json()

    answer, sources = sources_of(body["reply"])
    assert answer == f"{embedded}\n\n{NOTICE}"
    assert len(standalone_notices(answer)) == 1
    assert sources == [f"- [{article(1)['title']}]({article(1)['url']})"]


# --- round 2: route-local final boundary --------------------------------------------------------


def test_unexpected_route_defect_is_a_fixed_no_store_500(monkeypatch, capsys):
    async def broken(request):
        raise RuntimeError("SECRET-DEFECT-DETAIL")

    monkeypatch.setattr(app, "_api_chat_response", broken)

    with TestClient(app.api) as client:
        response = client.post("/api/chat", json={"message": "SUBMITTED-USER-TEXT", "history": []})

    assert response.status_code == 500
    assert response.json() == {"error": "internal_error"}
    assert response.headers["cache-control"] == "no-store"
    out = capsys.readouterr()
    logs = out.out + out.err
    assert "ERROR: CHAT_ROUTE_UNEXPECTED RuntimeError" in logs

    for secret in ("SECRET-DEFECT-DETAIL", "SUBMITTED-USER-TEXT"):
        assert secret not in response.text
        assert secret not in logs


def test_task_cancellation_is_not_converted(monkeypatch):
    async def cancelled(request):
        raise asyncio.CancelledError()

    monkeypatch.setattr(app, "_api_chat_response", cancelled)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(app.api_chat(object()))


# --- explicit code continuation: fence markers are not a usable remainder ---

@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("budgeted", [False, True])
@pytest.mark.parametrize("model_text", [
    "```text\n\n```",
    "```python\n \t \n```",
    "```\n\n```",
    "```rust\n\n```",
    "```python\n```",
    "```python",
    "```python\n \t",
    "```text\n\n```\n\n```python\n \t\n```",
    "````python\n \t\n````",
    "~~~python\n \t\n~~~",
    "```python\r\n \t\r\n```",
    "",
    " \t\n ",
])
def test_empty_code_continuation_is_typed_provider_failure_without_retry(
    providers, monkeypatch, capsys, native, budgeted, model_text,
):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    if not budgeted:
        monkeypatch.setattr(app, "_request_limits", None)

        def fake_post(payload, timeout=90):
            providers["openai_posts"].append(copy.deepcopy(payload))
            return providers["openai"].pop(0)

        monkeypatch.setattr(app, "_post_openai_responses", fake_post)
    task = "Write Python code combining two bit masks with XOR, then display the result."
    history = [{"role": "user", "content": task},
               {"role": "assistant", "content": app.mark_incomplete_reply("```\nmask = 1 ^ 2")}]
    assert app.classify_request("continue", history) == "code_continuation"
    providers["openai"] = [reply(model_text)]

    with TestClient(app.api) as client:
        response = client.post("/api/chat", json={"message": "continue", "history": history})

    assert result(response) == PROVIDER_UNAVAILABLE
    assert response.headers["cache-control"] == "no-store"
    assert "reply" not in response.json()
    assert len(providers["openai_posts"]) == 1
    assert providers["tavily_posts"] == []
    out = capsys.readouterr()
    assert task not in out.out + out.err
    assert OPENAI_KEY not in out.out + out.err
