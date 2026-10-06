"""Request-budget integration in /api/chat and the provider call sites.

Every provider is faked (or a loopback server); no external network
access. Async boundary tests drive the ASGI app directly so disconnects and
queue contention are deterministic.
"""

from __future__ import annotations

import asyncio
import gc
import io
import json
import os
from pathlib import Path
import sys
import threading
import time

import pytest


CANDIDATE_DIR = Path(__file__).resolve().parents[1]

if str(CANDIDATE_DIR) not in sys.path:
    sys.path.insert(0, str(CANDIDATE_DIR))

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

import app_fastapi_candidate as app
from kalillac_routing.request_budget import (
    CallBudgetExhausted,
    RequestBudget,
    RequestCancelled,
    RequestDeadlineExceeded,
    budget_scope,
    current_budget,
)
from kalillac_routing import provider_transport
from kalillac_routing.request_limits import RequestLimits, TransportLimits


MESSAGES = [SystemMessage(content="system"), HumanMessage(content="hello")]
TRANSPORT_LIMITS = TransportLimits(
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
    "transport": TRANSPORT_LIMITS,
    # The owner-selected deployment value (2 MiB).
    "openai_max_bytes": 2097152,
}
ORIGINAL_POST_OPENAI_RESPONSES = app._post_openai_responses


class UnexpectedTransport(BaseException):
    """A test reached the real bounded transport without installing a
    fake. BaseException, so no broad provider handler can hide it."""


def _refuse_transport(**kwargs):
    raise UnexpectedTransport()
JOIN = 10.0


# --- fixtures -------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(app, "_request_limits", None)
    monkeypatch.setattr(app, "_chat_semaphore", None)
    monkeypatch.setattr(app, "_chat_waiting", 0)
    monkeypatch.setattr(app, "_session_locks", {})
    monkeypatch.setattr(app, "_usage_meter", None)
    monkeypatch.setattr(app, "_lookups_outstanding", 0, raising=False)
    monkeypatch.setattr(app, "_lookup_tasks", set(), raising=False)
    monkeypatch.setattr(app, "_chats_admitted", 0, raising=False)
    monkeypatch.setattr(
        app, "_OPENAI_TRANSPORT",
        provider_transport.TransportHolder(factory=_refuse_transport),
        raising=False,
    )
    monkeypatch.setattr(app, "OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", False)


def enable(monkeypatch, **overrides):
    limits = RequestLimits(**{**BASE_LIMITS, **overrides})
    monkeypatch.setattr(app, "_request_limits", limits)
    return limits


def budget(models=6, searches=6, seconds=5.0, clock=time.monotonic):
    return RequestBudget(
        duration_seconds=seconds,
        max_model_attempts=models,
        max_search_attempts=searches,
        clock=clock,
    )


def openai_reply(text, cut_off=False):
    data = {
        "status": "incomplete" if cut_off else "completed",
        "output": [{
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        }],
    }

    if cut_off:
        data["incomplete_details"] = {"reason": app.OUTPUT_TOKEN_LIMIT_REASON}

    return data


class _FakeHTTPResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def providers(monkeypatch):
    """Fakes for the only model provider (OpenAI); records each network
    attempt. Any other outbound urllib request fails the test."""

    calls = []
    state = {"openai": []}

    def fake_post(payload, **kwargs):
        calls.append(("openai", kwargs))
        result = state["openai"].pop(0)
        result = result() if callable(result) else result

        if isinstance(result, BaseException):
            raise result

        return result

    # Budgeted OpenAI requests go through the bounded transport seam; the
    # scripted transport replays the same OpenAI script and call log.
    transport_posts = []

    class ScriptedTransport:
        def __init__(self, **settings):
            self.settings = settings
            self.closes = []
            state["transport"] = self

        def post_json(self, url, payload, *, headers, timeout, max_bytes, cancelled=None):
            transport_posts.append({
                "url": url, "payload": payload, "headers": headers,
                "timeout": timeout, "max_bytes": max_bytes, "cancelled": cancelled,
            })
            return fake_post(payload, timeout=timeout)

        def close(self, timeout):
            self.closes.append(timeout)

    state["transport_posts"] = transport_posts
    state["holder"] = provider_transport.TransportHolder(factory=ScriptedTransport)
    monkeypatch.setattr(app, "_OPENAI_TRANSPORT", state["holder"], raising=False)
    monkeypatch.setattr(app, "_request_limits", RequestLimits(**BASE_LIMITS))

    monkeypatch.setattr(app, "_post_openai_responses", fake_post)

    def no_other_network(*args, **kwargs):
        raise AssertionError("unexpected outbound request")

    monkeypatch.setattr(app.urllib.request, "urlopen", no_other_network)

    return calls, state


def names(calls):
    return [name for name, _ in calls]


# --- flag-off compatibility ----------------------------------------------------------


def test_flag_off_request_creates_no_budget(monkeypatch):
    seen = []

    def no_budget(*args, **kwargs):
        raise AssertionError("budget created with the flag off")

    def fake_chat(message, history, request=None, session_id=None):
        seen.append(current_budget())
        return "plain reply"

    monkeypatch.setattr(app, "RequestBudget", no_budget)
    monkeypatch.setattr(app, "chat", fake_chat)

    with TestClient(app.api) as client:
        response = client.post("/api/chat", json={"message": "hi", "history": []})

    assert response.status_code == 200
    assert response.json()["reply"] == "plain reply"
    assert seen == [None]


def test_flag_off_openai_failure_is_provider_unavailable_without_fallback(providers):
    calls, state = providers
    state["openai"] = [ConnectionError("openai down")]

    with pytest.raises(app.ModelProviderUnavailable) as caught:
        app.invoke_llm(MESSAGES)

    # Exactly one OpenAI request, no timeout override, no other provider.
    assert calls == [("openai", {})]
    assert type(caught.value) is app.ModelProviderUnavailable


# --- model admissions -------------------------------------------------------------------


def test_continuation_is_admitted_with_a_bounded_timeout(providers):
    calls, state = providers
    state["openai"] = [openai_reply("Partial", cut_off=True), openai_reply(" rest")]
    request_budget = budget(models=2)

    with budget_scope(request_budget):
        response = app.invoke_llm(MESSAGES)

    assert response.incomplete is False
    assert request_budget.model_attempts == 2
    assert names(calls) == ["openai", "openai"]
    assert all(0 < kwargs["timeout"] <= 90 for _, kwargs in calls)


def test_continuation_stop_is_not_swallowed(providers):
    calls, state = providers
    state["openai"] = [openai_reply("Partial", cut_off=True)]

    with budget_scope(budget(models=1)):
        with pytest.raises(CallBudgetExhausted):
            app.invoke_llm(MESSAGES)

    # No partial answer returned, and no other provider tried.
    assert names(calls) == ["openai"]


def test_cancellation_during_a_call_stops_the_provider_chain(providers):
    calls, state = providers
    request_budget = budget()

    def cancelled_then_failed():
        request_budget.cancel()
        return ConnectionError("connection dropped")

    state["openai"] = [cancelled_then_failed]

    with budget_scope(request_budget):
        with pytest.raises(RequestCancelled):
            app.invoke_llm(MESSAGES)

    assert names(calls) == ["openai"]


def test_remote_failure_admits_one_attempt_and_tries_no_other_provider(providers):
    calls, state = providers
    state["openai"] = [ConnectionError("openai down")]
    request_budget = budget(models=4)

    with budget_scope(request_budget):
        with pytest.raises(app.ModelProviderUnavailable):
            app.invoke_llm(MESSAGES)

    assert names(calls) == ["openai"]
    assert request_budget.model_attempts == 1


def test_attempt_cap_stops_before_a_further_openai_request(providers):
    calls, state = providers
    state["openai"] = [openai_reply("first answer")]

    with budget_scope(budget(models=1)):
        app.invoke_llm(MESSAGES)

        with pytest.raises(CallBudgetExhausted):
            app.invoke_llm(MESSAGES)

    assert names(calls) == ["openai"]


def test_repair_is_an_admitted_model_attempt(providers):
    calls, state = providers
    state["openai"] = [openai_reply("first answer")]

    with budget_scope(budget(models=1)):
        app.invoke_llm(MESSAGES)

        with pytest.raises(CallBudgetExhausted):
            app.repair_python_output("write code", "print(1", ["syntax error"])

    assert names(calls) == ["openai"]


def test_native_tool_round_is_an_admitted_model_attempt(providers):
    calls, state = providers
    state["openai"] = [openai_reply("tool round")]
    request_budget = budget(models=1)

    with budget_scope(request_budget):
        app._invoke_openai_native_tools([{"role": "user", "content": "x"}], "rules")

    assert request_budget.model_attempts == 1
    assert "timeout" in calls[0][1]


# --- search admissions --------------------------------------------------------------------


@pytest.fixture
def tavily(monkeypatch):
    calls = []
    state = {"search": []}

    class FakeTavily:
        def __init__(self, api_key=None, **kwargs):
            pass

        def search(self, **kwargs):
            calls.append(("search", kwargs))
            return state["search"].pop(0)

        def extract(self, **kwargs):
            calls.append(("extract", kwargs))
            return {"results": [{"raw_content": "Article text. " * 30}]}

    monkeypatch.setattr("tavily.TavilyClient", FakeTavily)
    monkeypatch.setattr(app, "TAVILY_API_KEY", "test-tavily-key")
    return calls, state


def news_results(count):
    return {"results": [
        {
            "title": f"Story {n} about AI models",
            "url": f"https://news.example.org/2026/10/05/story-{n}",
            "content": "summary",
            "published_date": "2026-10-05",
            "score": 0.9,
        }
        for n in range(count)
    ]}


def test_search_and_each_extraction_are_admitted(tavily):
    calls, state = tavily
    state["search"] = [news_results(3)]
    request_budget = budget(searches=6)

    with budget_scope(request_budget):
        status, results = app.run_web_search("latest AI news")

    assert status == "ok"
    assert [name for name, _ in calls] == ["search", "extract", "extract", "extract"]
    assert request_budget.search_attempts == 4
    assert 0 < calls[0][1]["timeout"] <= app.SEARCH_TIMEOUT_SECONDS
    assert all(0 < kwargs["timeout"] <= 30 for name, kwargs in calls if name == "extract")


def test_search_stop_during_extraction_propagates(tavily):
    calls, state = tavily
    state["search"] = [news_results(3)]

    with budget_scope(budget(searches=2)):
        with pytest.raises(CallBudgetExhausted):
            app.run_web_search("latest AI news")

    # One search and one extraction; the loop did not continue.
    assert [name for name, _ in calls] == ["search", "extract"]


def test_domain_retry_search_is_admitted(tavily):
    calls, state = tavily
    off_host = {"results": [{
        "title": "Python docs page",
        "url": "https://docs.python.org/3/whatsnew/",
        "content": "Docs content " * 10,
        "published_date": "",
        "score": 0.5,
    }]}
    state["search"] = [off_host, off_host]
    request_budget = budget(searches=6)

    with budget_scope(request_budget):
        app.run_web_search("python release notes", include_domains=["python.org"])

    assert [name for name, _ in calls] == ["search", "search"]
    assert request_budget.search_attempts == 2


def test_flag_off_search_keeps_existing_arguments(tavily):
    calls, state = tavily
    state["search"] = [news_results(1)]

    app.run_web_search("latest AI news")

    assert calls[0][1]["timeout"] == app.SEARCH_TIMEOUT_SECONDS
    assert "timeout" not in calls[1][1]   # extract keeps the SDK default


# --- terminal stops bypass broad handlers -------------------------------------------------


def test_search_stop_in_chat_is_not_an_unavailable_reply(monkeypatch):
    monkeypatch.setattr(app, "classify_request", lambda message, history: "web_search")
    monkeypatch.setattr(app, "requires_web_verification", lambda message, history: False)

    def stop(*args, **kwargs):
        raise CallBudgetExhausted("search")

    monkeypatch.setattr(app, "run_web_search", stop)

    with pytest.raises(CallBudgetExhausted):
        app.chat("latest AI news", [], session_id="budget-search-stop")


@pytest.mark.parametrize(
    "stop", [RequestCancelled(), RequestDeadlineExceeded(), CallBudgetExhausted("model")],
)
def test_native_tool_stop_never_falls_through_to_legacy(monkeypatch, stop):
    legacy = []
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", True)
    monkeypatch.setattr(app, "classify_request", lambda message, history: "general")
    monkeypatch.setattr(app, "requires_web_verification", lambda message, history: False)

    def native(*args):
        raise stop

    monkeypatch.setattr(app, "_run_v31_native_tool_chat", native)
    monkeypatch.setattr(app, "invoke_llm", lambda *a, **k: legacy.append(1))

    with pytest.raises(type(stop)):
        app.chat("hello there", [], session_id="budget-native-stop")

    assert legacy == []


def test_native_tool_protocol_failure_falls_through_once(monkeypatch):
    legacy = []
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", True)
    monkeypatch.setattr(app, "classify_request", lambda message, history: "general")
    monkeypatch.setattr(app, "requires_web_verification", lambda message, history: False)

    def native(*args):
        raise app.ToolLoopProtocolError("Maximum tool rounds exceeded.")

    def legacy_llm(*args, **kwargs):
        legacy.append(1)
        return AIMessage(content="legacy answer")

    monkeypatch.setattr(app, "_run_v31_native_tool_chat", native)
    monkeypatch.setattr(app, "invoke_llm", legacy_llm)

    assert app.chat("hello there", [], session_id="budget-native-fail") == "legacy answer"
    assert legacy == [1]


def test_native_tool_internal_defect_is_internal_error_without_legacy(monkeypatch):
    legacy = []
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", True)
    monkeypatch.setattr(app, "classify_request", lambda message, history: "general")
    monkeypatch.setattr(app, "requires_web_verification", lambda message, history: False)

    def native(*args):
        raise RuntimeError("native path broke")

    monkeypatch.setattr(app, "_run_v31_native_tool_chat", native)
    monkeypatch.setattr(app, "invoke_llm", lambda *a, **k: legacy.append(1))

    with pytest.raises(app.ChatInternalError):
        app.chat("hello there", [], session_id="budget-native-defect")

    assert legacy == []


def test_chat_catch_all_passes_stops_and_still_maps_errors(monkeypatch):
    monkeypatch.setattr(app, "classify_request", lambda message, history: "general")
    monkeypatch.setattr(app, "requires_web_verification", lambda message, history: False)

    def exhausted(*args, **kwargs):
        raise CallBudgetExhausted("model")

    monkeypatch.setattr(app, "invoke_llm", exhausted)

    with pytest.raises(CallBudgetExhausted):
        app.chat("hello there", [], session_id="budget-catch-all")

    def broken(*args, **kwargs):
        raise RuntimeError("other failure")

    monkeypatch.setattr(app, "invoke_llm", broken)

    with pytest.raises(app.ChatInternalError):
        app.chat("hello there", [], session_id="budget-catch-all")


def test_worker_rechecks_budget_after_chat_returns(monkeypatch):
    now = [100.0]
    request_budget = budget(seconds=5.0, clock=lambda: now[0])

    def late_chat(message, history, request=None, session_id=None):
        now[0] += 10.0  # the reply (or its post-processing) finished late
        return "late reply"

    monkeypatch.setattr(app, "chat", late_chat)

    with budget_scope(request_budget):
        with pytest.raises(RequestDeadlineExceeded):
            app.chat_core("hi", [], "sid-late", resolved=True)

    # Without a budget the same call returns normally.
    assert app.chat_core("hi", [], "sid-late", resolved=True)[0] == "late reply"


# --- HTTP boundary ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "outcome, status, code",
    [
        (CallBudgetExhausted("model"), 503, "model_provider_unavailable"),
        (app.ModelProviderUnavailable(), 503, "model_provider_unavailable"),
        (RequestDeadlineExceeded(), 504, "request_timeout"),
        (RequestCancelled(), 499, "request_cancelled"),
        (RuntimeError("boom"), 500, "internal_error"),
    ],
)
def test_terminal_outcomes_have_fixed_codes(monkeypatch, outcome, status, code):
    enable(monkeypatch)

    def fake_chat(message, history, request=None, session_id=None):
        raise outcome

    monkeypatch.setattr(app, "chat", fake_chat)

    with TestClient(app.api) as client:
        response = client.post("/api/chat", json={"message": "hi", "history": []})

    assert response.status_code == status
    assert response.json() == {"error": code}
    assert response.headers["cache-control"] == "no-store"


def test_enabled_success_runs_under_the_request_budget(monkeypatch):
    enable(monkeypatch, max_model_attempts=3)
    seen = []

    def fake_chat(message, history, request=None, session_id=None):
        request_budget = current_budget()
        seen.append(request_budget)
        current_budget().admit_model_attempt()
        return "budgeted reply"

    monkeypatch.setattr(app, "chat", fake_chat)

    with TestClient(app.api) as client:
        response = client.post("/api/chat", json={"message": "hi", "history": []})

    assert response.status_code == 200
    assert response.json()["reply"] == "budgeted reply"
    assert isinstance(seen[0], RequestBudget)
    assert seen[0].model_attempts == 1


def test_late_success_after_deadline_is_504_and_capacity_follows_worker(monkeypatch):
    enable(monkeypatch, deadline_seconds=0.4, queue_wait_seconds=0.2)
    finished = threading.Event()
    release = threading.Event()

    def slow_chat(message, history, request=None, session_id=None):
        release.wait(JOIN)
        finished.set()
        return "late reply"

    monkeypatch.setattr(app, "chat", slow_chat)

    with TestClient(app.api) as client:
        start = time.perf_counter()
        response = client.post("/api/chat", json={"message": "hi", "history": []})
        elapsed = time.perf_counter() - start

        assert response.status_code == 504
        assert response.json() == {"error": "request_timeout"}
        assert "late reply" not in response.text
        assert elapsed < 0.4 + 1.5

        # The worker still owns its slot until it actually exits.
        assert app._chat_semaphore._value == app.MAX_CONCURRENT_CHATS - 1
        release.set()
        assert finished.wait(JOIN)
        assert wait_until(
            lambda: app._chat_semaphore._value == app.MAX_CONCURRENT_CHATS
        )
        assert wait_until(lambda: app._session_locks == {})


def wait_until(predicate, timeout=JOIN):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# --- direct ASGI driving: disconnects, queues, watcher ------------------------------------


class Exchange:
    """Drive app.api directly. After the body, receive() blocks until the
    test signals a disconnect."""

    def __init__(self, payload):
        self.body = json.dumps(payload).encode()
        self.body_sent = False
        self.disconnect = asyncio.Event()
        self.receives_after_body = 0
        self.messages = []

    async def receive(self):
        if not self.body_sent:
            self.body_sent = True
            return {"type": "http.request", "body": self.body, "more_body": False}

        self.receives_after_body += 1
        await self.disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(self, message):
        self.messages.append(message)

    async def run(self):
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "POST", "scheme": "http", "path": "/api/chat",
            "raw_path": b"/api/chat", "query_string": b"", "root_path": "",
            "headers": [(b"content-type", b"application/json"), (b"host", b"testserver")],
            "client": ("127.0.0.1", 50000), "server": ("testserver", 80),
        }
        await app.api(scope, self.receive, self.send)
        status = next(m["status"] for m in self.messages if m["type"] == "http.response.start")
        body = b"".join(m.get("body", b"") for m in self.messages if m["type"] == "http.response.body")
        return status, json.loads(body)


def known_session(sid):
    app.get_session_state_by_id(sid)
    return sid


class FakeMeter:
    def __init__(self, delay=0.0):
        self.delay = delay
        self.metered = []

    async def resolve_request_account(self, request):
        await asyncio.sleep(self.delay)
        return "account-1"

    def background_for_chat(self, *args):
        self.metered.append(1)
        return None


@pytest.mark.parametrize("held", ["session_lock", "slot"])
def test_disconnect_during_each_queue_wait_cancels_without_leaks(monkeypatch, held):
    enable(monkeypatch)
    sid = known_session(f"queue-disconnect-{held}")
    chat_calls = []
    monkeypatch.setattr(app, "chat", lambda *a, **k: chat_calls.append(1) or "x")

    async def scenario():
        entry = app._session_lock_ref(sid)
        sem = app._get_chat_semaphore()

        if held == "session_lock":
            await entry.lock.acquire()
        else:
            for _ in range(app.MAX_CONCURRENT_CHATS):
                await sem.acquire()

        exchange = Exchange({"message": "hi", "history": [], "session_id": sid})
        task = asyncio.ensure_future(exchange.run())
        await asyncio.sleep(0.2)
        assert not task.done()      # really waiting in the queue
        exchange.disconnect.set()
        status, body = await asyncio.wait_for(task, 2.0)

        # Nothing the request touched is still held or counted.
        if held == "session_lock":
            entry.lock.release()
            assert app._get_chat_semaphore()._value == app.MAX_CONCURRENT_CHATS
        else:
            assert not entry.lock.locked()
            for _ in range(app.MAX_CONCURRENT_CHATS):
                sem.release()

        assert entry.refs == 1      # only the test's own reference
        app._session_lock_unref(sid, entry)
        assert app._chat_waiting == 0
        return status, body

    status, body = asyncio.run(scenario())

    assert (status, body) == (499, {"error": "request_cancelled"})
    assert chat_calls == []
    assert app._session_locks == {}


def test_disconnect_during_worker_keeps_capacity_until_worker_exits(monkeypatch):
    enable(monkeypatch)
    meter = FakeMeter()
    monkeypatch.setattr(app, "_usage_meter", meter)
    started = threading.Event()
    release = threading.Event()
    seen = []

    def blocking_chat(message, history, request=None, session_id=None):
        seen.append(current_budget())
        started.set()
        release.wait(JOIN)
        return "reply nobody receives"

    monkeypatch.setattr(app, "chat", blocking_chat)

    async def scenario():
        exchange = Exchange({"message": "hi", "history": []})
        task = asyncio.ensure_future(exchange.run())

        while not started.is_set():
            await asyncio.sleep(0.01)

        exchange.disconnect.set()
        status, body = await asyncio.wait_for(task, 2.0)

        # The handler returned, but the worker still owns its capacity.
        sem = app._get_chat_semaphore()
        assert sem._value == app.MAX_CONCURRENT_CHATS - 1
        assert seen[0].cancelled is True
        assert len(app._session_locks) == 1

        release.set()

        for _ in range(500):
            if sem._value == app.MAX_CONCURRENT_CHATS and not app._session_locks:
                break
            await asyncio.sleep(0.01)

        assert sem._value == app.MAX_CONCURRENT_CHATS
        assert app._session_locks == {}
        return status, body

    status, body = asyncio.run(scenario())

    assert (status, body) == (499, {"error": "request_cancelled"})
    assert meter.metered == []


def test_shared_queue_deadline_spans_session_lock_and_slot(monkeypatch):
    enable(monkeypatch, deadline_seconds=5.0, queue_wait_seconds=0.6)
    sid = known_session("queue-shared-deadline")
    monkeypatch.setattr(app, "chat", lambda *a, **k: "never")

    async def scenario():
        entry = app._session_lock_ref(sid)
        await entry.lock.acquire()
        sem = app._get_chat_semaphore()

        for _ in range(app.MAX_CONCURRENT_CHATS):
            await sem.acquire()

        # The session lock frees after 0.4 s; the slot never does. A
        # per-wait timeout would allow 0.4 + 0.6 s; the shared one 0.6 s.
        asyncio.get_running_loop().call_later(0.4, entry.lock.release)
        exchange = Exchange({"message": "hi", "history": [], "session_id": sid})
        start = time.perf_counter()
        status, body = await exchange.run()
        elapsed = time.perf_counter() - start

        assert not entry.lock.locked()  # the request released what it took
        for _ in range(app.MAX_CONCURRENT_CHATS):
            sem.release()
        app._session_lock_unref(sid, entry)
        return status, body, elapsed

    status, body, elapsed = asyncio.run(scenario())

    assert (status, body) == (429, {"error": "busy"})
    assert 0.55 <= elapsed < 0.9
    assert app._chat_waiting == 0
    assert app._session_locks == {}


def test_queue_wait_reaching_the_request_deadline_is_504(monkeypatch):
    enable(monkeypatch, deadline_seconds=0.5, queue_wait_seconds=0.4)
    monkeypatch.setattr(app, "_usage_meter", FakeMeter(delay=0.3))
    monkeypatch.setattr(app, "chat", lambda *a, **k: "never")

    async def scenario():
        sem = app._get_chat_semaphore()

        for _ in range(app.MAX_CONCURRENT_CHATS):
            await sem.acquire()

        status, body = await Exchange({"message": "hi", "history": []}).run()

        for _ in range(app.MAX_CONCURRENT_CHATS):
            sem.release()
        return status, body

    # 0.3 s identity + queue capped at the 0.5 s request deadline.
    assert asyncio.run(scenario()) == (504, {"error": "request_timeout"})


def test_watcher_is_the_single_receiver_and_is_always_cleaned_up(monkeypatch):
    enable(monkeypatch)
    monkeypatch.setattr(app, "chat", lambda *a, **k: "fine")

    async def scenario():
        exchange = Exchange({"message": "hi", "history": []})
        status, body = await exchange.run()
        await asyncio.sleep(0)
        leftovers = [
            task for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
        ]
        return status, body, exchange.receives_after_body, leftovers

    status, body, receives, leftovers = asyncio.run(scenario())

    assert status == 200 and body["reply"] == "fine"
    assert receives == 1        # only the watcher read after the body
    assert leftovers == []      # watcher and helpers were awaited


def test_concurrent_requests_have_isolated_budgets(monkeypatch):
    enable(monkeypatch, max_model_attempts=2)
    seen = []
    lock = threading.Lock()

    def admitting_chat(message, history, request=None, session_id=None):
        # Two attempts each: a shared budget would exhaust on the third.
        current_budget().admit_model_attempt()
        time.sleep(0.05)
        current_budget().admit_model_attempt()
        with lock:
            seen.append(current_budget())
        return "ok"

    monkeypatch.setattr(app, "chat", admitting_chat)

    async def scenario():
        first = Exchange({"message": "one", "history": []})
        second = Exchange({"message": "two", "history": []})
        return await asyncio.gather(first.run(), second.run())

    results = asyncio.run(scenario())

    assert [status for status, _ in results] == [200, 200]
    assert len({id(b) for b in seen}) == 2
    assert all(b.model_attempts == 2 for b in seen)


def test_detached_worker_failure_is_consumed(monkeypatch):
    enable(monkeypatch, deadline_seconds=0.3, queue_wait_seconds=0.1)
    release = threading.Event()
    reported = []

    def failing_late(message, history, request=None, session_id=None):
        release.wait(JOIN)
        raise RuntimeError("late failure")

    monkeypatch.setattr(app, "chat", failing_late)

    async def scenario():
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, context: reported.append(context))
        status, body = await Exchange({"message": "hi", "history": []}).run()
        release.set()
        sem = app._get_chat_semaphore()

        for _ in range(500):
            if sem._value == app.MAX_CONCURRENT_CHATS:
                break
            await asyncio.sleep(0.01)

        gc.collect()
        await asyncio.sleep(0.05)
        return status, body, sem._value

    status, body, value = asyncio.run(scenario())

    assert (status, body) == (504, {"error": "request_timeout"})
    assert value == app.MAX_CONCURRENT_CHATS
    assert reported == []


# --- bounded acquisition races --------------------------------------------------------------


def test_acquire_race_after_stop_releases_what_it_got():
    released = []

    async def scenario():
        stop = asyncio.Event()

        async def racing_acquire():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                return True  # acquired just as the cancellation landed

        loop = asyncio.get_running_loop()
        loop.call_later(0.05, stop.set)
        return await app._acquire_bounded(
            racing_acquire, lambda: released.append(1), time.monotonic() + 5, stop,
        )

    assert asyncio.run(scenario()) == "stopped"
    assert released == [1]


def test_acquire_timeout_and_external_cancel_leave_lock_free():
    async def scenario():
        lock = asyncio.Lock()
        await lock.acquire()
        stop = asyncio.Event()

        outcome = await app._acquire_bounded(
            lock.acquire, lock.release, time.monotonic() + 0.1, stop,
        )
        assert outcome == "timeout"

        waiter = asyncio.ensure_future(app._acquire_bounded(
            lock.acquire, lock.release, time.monotonic() + 5, stop,
        ))
        await asyncio.sleep(0.05)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

        lock.release()
        # Nobody else holds it now: a fresh acquire succeeds immediately.
        assert await asyncio.wait_for(lock.acquire(), 0.5) is True
        lock.release()
        return outcome

    assert asyncio.run(scenario()) == "timeout"


# --- review round 2: acquisition ownership ------------------------------------------------


class CountingLock:
    """An asyncio.Lock whose acquire() factory calls are counted."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.acquire_calls = 0
        self.releases = 0

    def acquire(self):
        self.acquire_calls += 1
        return self.lock.acquire()

    def release(self):
        self.releases += 1
        self.lock.release()


def test_already_stopped_never_acquires_a_free_lock():
    async def scenario():
        lock = CountingLock()
        stop = asyncio.Event()
        stop.set()
        outcome = await app._acquire_bounded(
            lock.acquire, lock.release, time.monotonic() + 5, stop,
        )
        await asyncio.sleep(0)
        return outcome, lock.acquire_calls, lock.lock.locked()

    assert asyncio.run(scenario()) == ("stopped", 0, False)


def test_already_expired_never_acquires_a_free_lock():
    async def scenario():
        lock = CountingLock()
        outcome = await app._acquire_bounded(
            lock.acquire, lock.release, time.monotonic() - 1, asyncio.Event(),
        )
        await asyncio.sleep(0)
        return outcome, lock.acquire_calls, lock.lock.locked()

    assert asyncio.run(scenario()) == ("timeout", 0, False)


def test_stop_set_while_acquiring_wins_and_releases():
    async def scenario():
        lock = CountingLock()
        stop = asyncio.Event()

        async def acquire_as_stop_lands():
            stop.set()                      # stop and acquisition coincide
            return await lock.acquire()

        outcome = await app._acquire_bounded(
            acquire_as_stop_lands, lock.release, time.monotonic() + 5, stop,
        )
        for _ in range(3):
            await asyncio.sleep(0)
        return outcome, lock.releases, lock.lock.locked()

    assert asyncio.run(scenario()) == ("stopped", 1, False)


def test_cancellation_at_any_point_after_acquisition_never_leaks():
    """Cancel the helper k loop iterations after it starts, for each k.
    Whenever the caller did not receive "acquired", the lock must end up
    free."""

    cancelled_after_acquiring = 0

    for offset in range(12):
        async def scenario(offset=offset):
            lock = CountingLock()
            helper = asyncio.ensure_future(app._acquire_bounded(
                lock.acquire, lock.release, time.monotonic() + 5, asyncio.Event(),
            ))

            for _ in range(offset):
                await asyncio.sleep(0)

            helper.cancel()

            try:
                outcome = await helper
            except asyncio.CancelledError:
                outcome = "cancelled"

            for _ in range(3):
                await asyncio.sleep(0)

            held = lock.lock.locked()

            if outcome == "acquired":
                assert held                # the caller owns it
                lock.release()
            else:
                assert not held, f"leaked at offset {offset}"

            return outcome, lock.acquire_calls

        outcome, calls = asyncio.run(scenario())

        if outcome == "cancelled" and calls:
            cancelled_after_acquiring += 1

    # The sweep really exercised cancellation after acquisition began.
    assert cancelled_after_acquiring >= 1


# --- review round 2: worker ownership at every cancellation point ---------------------------


def test_handler_cancelled_at_any_point_keeps_capacity_until_worker_exits(monkeypatch):
    """After a disconnect, cancel the handler k loop iterations later (and
    keep cancelling). Wherever the cancellation lands, the worker keeps the
    session lock and slot until it really exits, then releases them once."""

    enable(monkeypatch)
    landed = set()

    for offset in range(10):
        started = threading.Event()
        release_worker = threading.Event()
        sid = known_session(f"cancel-sweep-{offset}")

        def held_chat(message, history, request=None, session_id=None,
                      _started=started, _release=release_worker):
            _started.set()
            _release.wait(JOIN)
            return "never delivered"

        monkeypatch.setattr(app, "chat", held_chat)
        monkeypatch.setattr(app, "_chat_semaphore", None)

        async def scenario(offset=offset, started=started,
                           release_worker=release_worker, sid=sid):
            exchange = Exchange({"message": "hi", "history": [], "session_id": sid})
            handler = asyncio.ensure_future(exchange.run())

            while not started.is_set():
                await asyncio.sleep(0.005)

            exchange.disconnect.set()

            for _ in range(offset):
                await asyncio.sleep(0)

            while not handler.done():
                handler.cancel()            # repeated cancellation
                await asyncio.sleep(0)

            try:
                await handler
                result = "returned"
            except asyncio.CancelledError:
                result = "cancelled"

            sem = app._get_chat_semaphore()
            entry = app._session_locks.get(sid)
            # The worker still runs: its capacity is still held.
            assert sem._value == app.MAX_CONCURRENT_CHATS - 1, offset
            assert entry is not None and entry.lock.locked(), offset

            release_worker.set()

            for _ in range(500):
                if sem._value == app.MAX_CONCURRENT_CHATS and sid not in app._session_locks:
                    break
                await asyncio.sleep(0.01)

            # Released exactly once: a double release would exceed the max.
            await asyncio.sleep(0.05)
            assert sem._value == app.MAX_CONCURRENT_CHATS, offset
            assert sid not in app._session_locks, offset
            assert app._chat_waiting == 0
            return result

        landed.add(asyncio.run(scenario()))

    assert "cancelled" in landed


class _CancelOnFirstGather:
    """Stands in for the app module's `asyncio`: the first gather() call
    cancels the calling task, so the CancelledError lands exactly on that
    cleanup await. Everything else is the real asyncio."""

    def __init__(self):
        self.gathers = 0

    def __getattr__(self, name):
        return getattr(asyncio, name)

    def gather(self, *args, **kwargs):
        if self.gathers == 0:
            asyncio.current_task().cancel()
        self.gathers += 1
        return asyncio.gather(*args, **kwargs)


@pytest.mark.parametrize("trigger", ["deadline", "disconnect"])
def test_handler_cancelled_during_cleanup_await_keeps_capacity_until_worker_exits(
    monkeypatch, trigger,
):
    """The wait ends (deadline or disconnect), then the handler is cancelled
    during the first cleanup await that follows. The worker, held by an
    event, must keep the session lock and slot until it actually exits, then
    release them once.

    On the deadline path the stopper task is still pending, so its cleanup
    await really suspends and the cancellation lands there."""

    if trigger == "deadline":
        enable(monkeypatch, deadline_seconds=0.4, queue_wait_seconds=0.2)
    else:
        enable(monkeypatch)

    started = threading.Event()
    release_worker = threading.Event()
    sid = known_session("cancel-during-cleanup")

    def held_chat(message, history, request=None, session_id=None):
        started.set()
        release_worker.wait(JOIN)
        return "never delivered"

    monkeypatch.setattr(app, "chat", held_chat)
    proxy = _CancelOnFirstGather()

    async def scenario():
        exchange = Exchange({"message": "hi", "history": [], "session_id": sid})
        handler = asyncio.ensure_future(exchange.run())

        while not started.is_set():
            await asyncio.sleep(0.005)

        monkeypatch.setattr(app, "asyncio", proxy)

        if trigger == "disconnect":
            exchange.disconnect.set()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(handler), 5.0)

        assert proxy.gathers >= 1      # the cancellation really landed there
        sem = app._get_chat_semaphore()
        entry = app._session_locks.get(sid)
        assert sem._value == app.MAX_CONCURRENT_CHATS - 1
        assert entry is not None and entry.lock.locked()

        release_worker.set()

        for _ in range(500):
            if sem._value == app.MAX_CONCURRENT_CHATS and sid not in app._session_locks:
                break
            await asyncio.sleep(0.01)

        await asyncio.sleep(0.05)
        assert sem._value == app.MAX_CONCURRENT_CHATS   # released exactly once
        assert sid not in app._session_locks
        assert app._chat_waiting == 0

    asyncio.run(scenario())


# --- review round 2: account-resolution wait ------------------------------------------------


class BlockedLookupMeter:
    """resolve_request_account runs a synchronous lookup in a worker
    thread, like UsageMeter, and blocks until released."""

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.metered = []

    async def resolve_request_account(self, request):
        return await app.run_in_threadpool(self._lookup)

    def _lookup(self):
        self.started.set()
        self.release.wait(JOIN)
        self.finished.set()
        return "account-1"

    def background_for_chat(self, *args):
        self.metered.append(1)
        return None


@pytest.mark.parametrize("stop", ["deadline", "disconnect"])
def test_blocked_account_lookup_is_bounded_and_never_queues(monkeypatch, stop):
    enable(monkeypatch, deadline_seconds=0.6, queue_wait_seconds=0.3)
    meter = BlockedLookupMeter()
    monkeypatch.setattr(app, "_usage_meter", meter)
    chat_calls = []
    monkeypatch.setattr(app, "chat", lambda *a, **k: chat_calls.append(1) or "x")
    reported = []

    async def scenario():
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, context: reported.append(context)
        )
        exchange = Exchange({"message": "hi", "history": []})
        handler = asyncio.ensure_future(exchange.run())

        while not meter.started.is_set():
            await asyncio.sleep(0.005)

        if stop == "disconnect":
            exchange.disconnect.set()

        status, body = await asyncio.wait_for(handler, 3.0)

        # Stopped before the queue: nothing entered, nothing created.
        assert app._chat_waiting == 0
        assert app._session_locks == {}
        assert app._chat_semaphore is None
        assert not meter.finished.is_set()   # the lookup thread still runs

        meter.release.set()

        # The stopped request left its lookup running; its thread only now
        # completes. Wait for the thread itself.
        for _ in range(500):
            if meter.finished.is_set():
                break
            await asyncio.sleep(0.01)

        assert meter.finished.is_set()
        gc.collect()
        await asyncio.sleep(0.05)
        return status, body

    status, body = asyncio.run(scenario())

    if stop == "deadline":
        assert (status, body) == (504, {"error": "request_timeout"})
    else:
        assert (status, body) == (499, {"error": "request_cancelled"})

    assert chat_calls == []
    assert meter.metered == []
    assert reported == []


# --- review round 3: account-lookup capacity ownership ------------------------------------


import anyio.to_thread


class CountingLookupMeter:
    """A meter whose synchronous lookup runs via run_in_threadpool (as the
    real UsageMeter does) and blocks until released. Counts lookups that
    are actually running, not merely tasks."""

    def __init__(self):
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.running = 0
        self.max_running = 0
        self.started = 0
        self.resolve_calls = 0
        self.metered = []

    async def resolve_request_account(self, request):
        self.resolve_calls += 1
        return await app.run_in_threadpool(self._lookup)

    def _lookup(self):
        with self.lock:
            self.started += 1
            self.running += 1
            self.max_running = max(self.max_running, self.running)

        try:
            self.release.wait(JOIN)
        finally:
            with self.lock:
                self.running -= 1

        return "account-1"

    def background_for_chat(self, *args):
        self.metered.append(1)
        return None


async def _wait_for(predicate, timeout=JOIN):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


def test_installed_threadpool_returns_capacity_when_its_await_is_cancelled():
    """Documents the installed Starlette/AnyIO behavior this slice must not
    rely on: cancelling the awaiting task returns its limiter token while
    the synchronous function is still running."""

    meter = CountingLookupMeter()

    async def scenario():
        limiter = anyio.to_thread.current_default_thread_limiter()
        original = limiter.total_tokens
        limiter.total_tokens = 1

        try:
            first = asyncio.ensure_future(app.run_in_threadpool(meter._lookup))
            assert await _wait_for(lambda: meter.running == 1)
            before = (limiter.borrowed_tokens, meter.running)

            first.cancel()
            await asyncio.sleep(0.05)
            after = (first.done(), limiter.borrowed_tokens, meter.running)

            second = asyncio.ensure_future(app.run_in_threadpool(meter._lookup))
            assert await _wait_for(lambda: meter.started == 2, timeout=2.0)
            during_second = (limiter.borrowed_tokens, meter.running)
            return before, after, during_second, second
        finally:
            meter.release.set()
            await _wait_for(lambda: meter.running == 0)
            limiter.total_tokens = original

    before, after, during_second, second = asyncio.run(scenario())

    assert before == (1, 1)
    assert after == (True, 0, 1)           # token returned, thread still running
    assert during_second == (1, 2)         # two running lookups, one token
    assert meter.max_running == 2


def test_uncancelled_lookup_keeps_its_capacity_until_the_thread_returns():
    meter = CountingLookupMeter()

    async def scenario():
        limiter = anyio.to_thread.current_default_thread_limiter()
        original = limiter.total_tokens
        limiter.total_tokens = 1

        try:
            first = asyncio.ensure_future(app.run_in_threadpool(meter._lookup))
            assert await _wait_for(lambda: meter.running == 1)
            second = asyncio.ensure_future(app.run_in_threadpool(meter._lookup))
            await asyncio.sleep(0.2)
            waiting = (limiter.borrowed_tokens, meter.running, meter.started)

            meter.release.set()
            await asyncio.gather(first, second)
            return waiting
        finally:
            meter.release.set()
            await _wait_for(lambda: meter.running == 0)
            limiter.total_tokens = original

    waiting = asyncio.run(scenario())

    assert waiting == (1, 1, 1)            # the second waits for the token
    assert meter.started == 2
    assert meter.max_running == 1


@pytest.mark.parametrize("stop", ["deadline", "disconnect", "handler_cancelled"])
def test_stopped_requests_never_exceed_lookup_capacity(monkeypatch, stop):
    """With one thread token: a request stopped while its lookup runs must
    leave that token held until the lookup thread returns, so a second
    request's lookup cannot run concurrently; detached lookups are bounded,
    and no chat worker or metering ever starts."""

    enable(monkeypatch, deadline_seconds=0.6, queue_wait_seconds=0.3)
    monkeypatch.setattr(app, "MAX_QUEUED_CHATS", 2)
    meter = CountingLookupMeter()
    monkeypatch.setattr(app, "_usage_meter", meter)
    chat_calls = []
    monkeypatch.setattr(app, "chat", lambda *a, **k: chat_calls.append(1) or "x")
    observed = {}

    async def stop_first(exchange, handler):
        if stop == "disconnect":
            exchange.disconnect.set()
        elif stop == "handler_cancelled":
            handler.cancel()

        try:
            return await asyncio.wait_for(asyncio.shield(handler), 3.0)
        except asyncio.CancelledError:
            return "cancelled"

    async def scenario():
        limiter = anyio.to_thread.current_default_thread_limiter()
        original = limiter.total_tokens
        limiter.total_tokens = 1

        try:
            first = Exchange({"message": "one", "history": []})
            first_handler = asyncio.ensure_future(first.run())
            assert await _wait_for(lambda: meter.running == 1)

            observed["first"] = await stop_first(first, first_handler)

            # The stopped request's lookup still owns the only token.
            observed["after_first"] = (limiter.borrowed_tokens, meter.running)

            # A second request: its lookup must wait, not run alongside.
            observed["second"] = await asyncio.wait_for(
                Exchange({"message": "two", "history": []}).run(), 3.0,
            )
            observed["after_second"] = (
                limiter.borrowed_tokens, meter.running, meter.started,
            )
            observed["detached"] = app._lookups_outstanding

            # At the bound: refused before any lookup is attempted.
            observed["third"] = await asyncio.wait_for(
                Exchange({"message": "three", "history": []}).run(), 3.0,
            )
            observed["resolve_calls"] = meter.resolve_calls
        finally:
            meter.release.set()
            await _wait_for(
                lambda: meter.running == 0 and app._lookups_outstanding == 0
            )
            observed["recovered"] = (
                limiter.borrowed_tokens, meter.running, app._lookups_outstanding,
            )
            limiter.total_tokens = original

    asyncio.run(scenario())

    expected_first = {
        "deadline": (504, {"error": "request_timeout"}),
        "disconnect": (499, {"error": "request_cancelled"}),
        "handler_cancelled": "cancelled",
    }[stop]

    assert observed["first"] == expected_first
    # Capacity: still held by the running lookup after the request stopped.
    assert observed["after_first"] == (1, 1)
    assert observed["second"] == (504, {"error": "request_timeout"})
    assert observed["after_second"] == (1, 1, 1)
    assert meter.max_running == 1
    # Outstanding detached lookups are counted and bounded.
    assert observed["detached"] == 2
    assert observed["third"] == (429, {"error": "busy"})
    assert observed["resolve_calls"] == 2
    # Everything recovers once the real work finishes.
    assert observed["recovered"] == (0, 0, 0)
    assert meter.started == 2              # the waiting lookup ran normally
    assert chat_calls == []
    assert meter.metered == []


# --- review round 4: lookup capacity is reserved at admission ------------------------------


def _expected_stop(stop):
    return {
        "deadline": (504, {"error": "request_timeout"}),
        "disconnect": (499, {"error": "request_cancelled"}),
        "handler_cancelled": "cancelled",
    }[stop]


@pytest.mark.parametrize("stop", ["completed", "deadline", "disconnect", "handler_cancelled"])
def test_concurrent_burst_admits_no_more_lookups_than_the_capacity(monkeypatch, stop):
    """Five handlers start together, before any lookup can finish and before
    any handler stops waiting. With capacity two, exactly two lookups are
    admitted and the rest are refused 429 busy. The two then complete or are
    stopped; stopped ones keep their capacity until the lookup really ends,
    and everything recovers afterwards."""

    enable(
        monkeypatch,
        deadline_seconds=0.8 if stop == "deadline" else 8.0,
        queue_wait_seconds=0.4,
    )
    monkeypatch.setattr(app, "MAX_QUEUED_CHATS", 2)
    meter = CountingLookupMeter()
    monkeypatch.setattr(app, "_usage_meter", meter)
    chat_calls = []
    monkeypatch.setattr(app, "chat", lambda *a, **k: chat_calls.append(1) or "ok")
    observed = {}
    reported = []

    async def scenario():
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, context: reported.append(context)
        )
        exchanges = [Exchange({"message": f"m{i}", "history": []}) for i in range(5)]
        handlers = [asyncio.ensure_future(e.run()) for e in exchanges]

        try:
            # No lookup can finish (release is unset) and no handler can stop
            # (no disconnect; the deadline is later than this wait) while the
            # burst is admitted.
            # Every handler has either started its lookup or been answered.
            await _wait_for(
                lambda: meter.running + sum(h.done() for h in handlers) == 5
            )
            await asyncio.sleep(0.1)
            observed["split"] = (meter.running, sum(h.done() for h in handlers))
            assert observed["split"] == (2, 3)    # running lookups, answered

            refused = [h for h in handlers if h.done()]
            admitted = [h for h in handlers if not h.done()]
            observed["refused"] = [h.result() for h in refused]
            observed["burst"] = (
                meter.resolve_calls, meter.started, app._lookups_outstanding,
                len(app._lookup_tasks),
            )

            if stop == "completed":
                meter.release.set()
                observed["admitted"] = await asyncio.wait_for(
                    asyncio.gather(*admitted), 3.0,
                )
            else:
                for exchange, handler in zip(exchanges, handlers):
                    if handler not in admitted:
                        continue
                    if stop == "disconnect":
                        exchange.disconnect.set()
                    elif stop == "handler_cancelled":
                        handler.cancel()

                outcomes = await asyncio.wait_for(
                    asyncio.gather(*admitted, return_exceptions=True), 3.0,
                )
                observed["admitted"] = [
                    "cancelled" if isinstance(o, asyncio.CancelledError) else o
                    for o in outcomes
                ]

                # Handlers gone; lookups still running and still counted,
                # held only by the module's strong references.
                gc.collect()
                observed["after_stop"] = (
                    meter.running, app._lookups_outstanding,
                    len(app._lookup_tasks),
                    all(not t.done() for t in app._lookup_tasks),
                )
                observed["while_held"] = await asyncio.wait_for(
                    Exchange({"message": "late", "history": []}).run(), 3.0,
                )
                observed["calls_while_held"] = meter.resolve_calls

                meter.release.set()
        finally:
            meter.release.set()
            await _wait_for(
                lambda: meter.running == 0 and app._lookups_outstanding == 0
            )
            observed["recovered"] = (
                meter.running, app._lookups_outstanding, len(app._lookup_tasks),
            )

        # Full recovery: a new request is admitted and served.
        observed["fresh"] = await asyncio.wait_for(
            Exchange({"message": "fresh", "history": []}).run(), 3.0,
        )
        observed["final_calls"] = meter.resolve_calls
        observed["final_outstanding"] = app._lookups_outstanding
        await asyncio.sleep(0.05)

    asyncio.run(scenario())

    assert observed["refused"] == [(429, {"error": "busy"})] * 3
    assert observed["burst"] == (2, 2, 2, 2)
    assert meter.max_running == 2

    if stop == "completed":
        assert [status for status, _ in observed["admitted"]] == [200, 200]
        assert len(chat_calls) == 3      # the two admitted, then the fresh one
    else:
        assert observed["admitted"] == [_expected_stop(stop)] * 2
        assert observed["after_stop"] == (2, 2, 2, True)
        assert observed["while_held"] == (429, {"error": "busy"})
        assert observed["calls_while_held"] == 2
        assert len(chat_calls) == 1      # only the fresh request

    assert observed["recovered"] == (0, 0, 0)
    assert observed["fresh"][0] == 200
    assert observed["final_calls"] == 3
    assert observed["final_outstanding"] == 0
    assert reported == []


class _LookupFailed(Exception):
    pass


class FailingLookupMeter:
    def __init__(self, synchronous=False):
        self.synchronous = synchronous
        self.calls = 0
        self.metered = []

    def _fail(self):
        raise _LookupFailed("lookup failed")

    def resolve_request_account(self, request):
        self.calls += 1

        if self.synchronous:
            # Fails while the lookup task is being created.
            self._fail()

        return app.run_in_threadpool(self._fail)

    def background_for_chat(self, *args):
        self.metered.append(1)
        return None


@pytest.mark.parametrize("synchronous", [False, True], ids=["lookup_fails", "creation_fails"])
def test_failed_lookup_releases_its_reservation_exactly_once(monkeypatch, synchronous):
    enable(monkeypatch)
    monkeypatch.setattr(app, "MAX_QUEUED_CHATS", 1)
    meter = FailingLookupMeter(synchronous=synchronous)
    monkeypatch.setattr(app, "_usage_meter", meter)
    chat_calls = []
    monkeypatch.setattr(app, "chat", lambda *a, **k: chat_calls.append(1) or "ok")

    async def attempt():
        try:
            return await asyncio.wait_for(
                Exchange({"message": "hi", "history": []}).run(), 3.0,
            )
        except _LookupFailed:
            return "raised"

    async def scenario():
        results = []

        # Capacity one: each attempt is admitted only if the previous
        # reservation was released, and never released twice.
        for _ in range(3):
            results.append(await attempt())
            await asyncio.sleep(0.02)
            results.append((app._lookups_outstanding, len(app._lookup_tasks)))

        return results

    results = asyncio.run(scenario())

    assert results == ["raised", (0, 0)] * 3
    assert meter.calls == 3
    assert chat_calls == []
    assert meter.metered == []


# --- review round 5: admitted-chat reservation --------------------------------------------


class GatedWorker:
    """Replaces chat_core. Runs in the worker thread, blocks until released,
    and counts workers that are really executing."""

    def __init__(self, fail=None):
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.fail = fail
        self.calls = 0
        self.running = 0
        self.max_running = 0

    def __call__(self, message, history, session_id, resolved=False):
        with self.lock:
            self.calls += 1
            self.running += 1
            self.max_running = max(self.max_running, self.running)

        try:
            self.release.wait(JOIN)

            if self.fail is not None:
                raise self.fail

            return "ok", session_id
        finally:
            with self.lock:
                self.running -= 1


class AdmissionSampler:
    """Samples the admission counters on every event-loop iteration. They
    only change in event-loop steps, so no persisted value is missed."""

    def __init__(self, worker):
        self.worker = worker
        self.max_admitted = 0
        self.min_admitted = 0
        self.max_waiting = 0
        self.min_waiting = 0
        self.max_sessions = 0
        self.max_running = 0
        self.task = None

    def sample(self):
        admitted = getattr(app, "_chats_admitted", 0)
        self.max_admitted = max(self.max_admitted, admitted)
        self.min_admitted = min(self.min_admitted, admitted)
        self.max_waiting = max(self.max_waiting, app._chat_waiting)
        self.min_waiting = min(self.min_waiting, app._chat_waiting)
        self.max_sessions = max(self.max_sessions, len(app._session_locks))
        self.max_running = max(self.max_running, self.worker.running)

    async def _run(self):
        while True:
            self.sample()
            await asyncio.sleep(0)

    def start(self):
        self.task = asyncio.ensure_future(self._run())

    async def stop(self):
        self.sample()
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


async def _free_slots(limit):
    """How many global slots can be taken right now, without private
    semaphore state: acquire until one would block, then give them back."""
    sem = app._get_chat_semaphore()
    taken = 0

    while taken < limit + 2:
        try:
            await asyncio.wait_for(sem.acquire(), 0.05)
        except asyncio.TimeoutError:
            break
        taken += 1

    for _ in range(taken):
        sem.release()

    return taken


async def _recovered(worker):
    """Wait for every worker to exit and every counter to settle, then
    report (admitted, waiting, session entries, running, free slots)."""
    await _wait_for(
        lambda: worker.running == 0
        and getattr(app, "_chats_admitted", 0) == 0
        and app._chat_waiting == 0
        and not app._session_locks
    )
    await asyncio.sleep(0.05)
    return (
        getattr(app, "_chats_admitted", 0),
        app._chat_waiting,
        len(app._session_locks),
        worker.running,
        await _free_slots(app.MAX_CONCURRENT_CHATS),
    )


def _outcome(result):
    if isinstance(result, asyncio.CancelledError):
        return "cancelled"
    if isinstance(result, BaseException):
        return f"raised {type(result).__name__}"
    return result


def test_distinct_session_burst_never_admits_more_than_total_capacity(monkeypatch):
    """Twelve distinct-session handlers start together, with no account
    lookup in front of admission (_usage_meter is None). Each runs to its
    first real await before any scheduled acquisition task runs, so a
    semaphore-state check sees free slots for all of them. Exactly
    MAX_CONCURRENT_CHATS + MAX_QUEUED_CHATS may be admitted."""

    enable(monkeypatch, deadline_seconds=8.0, queue_wait_seconds=6.0)
    monkeypatch.setattr(app, "_usage_meter", None)
    monkeypatch.setattr(app, "MAX_CONCURRENT_CHATS", 2)
    monkeypatch.setattr(app, "MAX_QUEUED_CHATS", 3)
    capacity = 2 + 3
    burst = 12
    worker = GatedWorker()
    monkeypatch.setattr(app, "chat_core", worker)
    observed = {}
    reported = []

    async def scenario():
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, context: reported.append(context)
        )
        sampler = AdmissionSampler(worker)
        sampler.start()

        exchanges = [
            Exchange({"message": f"burst {i}", "history": []}) for i in range(burst)
        ]
        # All created before any await: every handler's first step runs
        # before any acquisition task one of them schedules.
        handlers = [asyncio.ensure_future(e.run()) for e in exchanges]

        try:
            assert await _wait_for(lambda: worker.running == 2)
            await asyncio.sleep(0.3)

            pending = [h for h in handlers if not h.done()]
            observed["admitted"] = (len(pending), len(app._session_locks))
            observed["refused"] = [h.result() for h in handlers if h.done()]
            observed["counters_full"] = (
                getattr(app, "_chats_admitted", None), app._chat_waiting,
                worker.running,
            )

            # Full: one more distinct session is refused at once.
            observed["late"] = await asyncio.wait_for(
                Exchange({"message": "late", "history": []}).run(), 3.0,
            )

            worker.release.set()
            observed["served"] = await asyncio.wait_for(asyncio.gather(*pending), 5.0)
        finally:
            worker.release.set()
            await asyncio.gather(*handlers, return_exceptions=True)
            observed["recovered"] = await _recovered(worker)

        observed["fresh"] = await asyncio.wait_for(
            Exchange({"message": "fresh", "history": []}).run(), 3.0,
        )
        observed["after_fresh"] = await _recovered(worker)
        await sampler.stop()
        gc.collect()
        await asyncio.sleep(0.05)
        return sampler

    sampler = asyncio.run(scenario())

    # Exactly the combined capacity is admitted; every excess request is refused.
    assert observed["admitted"] == (capacity, capacity)
    assert observed["refused"] == [(429, {"error": "busy"})] * (burst - capacity)
    assert observed["counters_full"] == (capacity, 3, 2)
    assert observed["late"] == (429, {"error": "busy"})
    assert [status for status, _ in observed["served"]] == [200] * capacity

    # The invariants held at every sampled point.
    assert sampler.max_admitted == capacity
    assert sampler.max_running <= 2 and worker.max_running == 2
    assert sampler.max_waiting <= 3
    assert sampler.max_sessions <= capacity
    assert sampler.min_admitted == 0 and sampler.min_waiting == 0

    # Complete recovery, before and after a fresh request.
    assert observed["recovered"] == (0, 0, 0, 0, 2)
    assert observed["fresh"][0] == 200
    assert observed["after_fresh"] == (0, 0, 0, 0, 2)
    assert worker.calls == capacity + 1
    assert reported == []


BUSY = (429, {"error": "busy"})
TIMEOUT = (504, {"error": "request_timeout"})
CANCELLED = (499, {"error": "request_cancelled"})
INTERNAL = (500, {"error": "internal_error"})

OWNERSHIP_CASES = [
    "completed",
    "worker_exception",
    "cancel_queued",
    "queue_timeout",
    "deadline_queued",
    "disconnect_queued",
    "disconnect_worker",
    "cancel_worker",
    "deadline_worker",
    "detached_worker_fails",
    "session_ref_fails",
    "session_lock_acquire_fails",
    "slot_acquire_fails",
    "worker_creation_fails",
]


def _fail_first_call(original, make_failure):
    calls = []

    def wrapper(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return make_failure(*args, **kwargs)
        return original(*args, **kwargs)

    return wrapper


@pytest.mark.parametrize("case", OWNERSHIP_CASES)
def test_admitted_chat_reservation_is_released_exactly_once(monkeypatch, case):
    """One slot, one waiting place: capacity 2. Each case ends one request's
    lifecycle a different way and proves its admitted-chat reservation was
    released exactly once, at the right moment, followed by full recovery."""

    if case == "queue_timeout":
        enable(monkeypatch, deadline_seconds=5.0, queue_wait_seconds=0.4)
    elif case == "deadline_queued":
        enable(monkeypatch, deadline_seconds=0.8, queue_wait_seconds=0.7)
        monkeypatch.setattr(app, "_usage_meter", FakeMeter(delay=0.3))
    elif case == "deadline_worker":
        enable(monkeypatch, deadline_seconds=0.6, queue_wait_seconds=0.3)
    else:
        enable(monkeypatch, deadline_seconds=8.0, queue_wait_seconds=6.0)

    if case != "deadline_queued":
        monkeypatch.setattr(app, "_usage_meter", None)

    monkeypatch.setattr(app, "MAX_CONCURRENT_CHATS", 1)
    monkeypatch.setattr(app, "MAX_QUEUED_CHATS", 1)

    failure = RuntimeError("worker failed")
    worker = GatedWorker(
        fail=failure if case in ("worker_exception", "detached_worker_fails") else None
    )
    monkeypatch.setattr(app, "chat_core", worker)

    if case == "session_ref_fails":
        def broken_ref(sid):
            raise RuntimeError("session ref failed")

        monkeypatch.setattr(
            app, "_session_lock_ref",
            _fail_first_call(app._session_lock_ref, broken_ref),
        )

    if case in ("session_lock_acquire_fails", "slot_acquire_fails"):
        original_acquire = app._acquire_bounded
        acquire_calls = []
        fail_on = 1 if case == "session_lock_acquire_fails" else 2

        async def failing_acquire_bounded(acquire, release, deadline, stop):
            acquire_calls.append(1)

            if len(acquire_calls) == fail_on:
                async def broken():
                    raise RuntimeError("acquire failed")

                return await original_acquire(broken, release, deadline, stop)

            return await original_acquire(acquire, release, deadline, stop)

        monkeypatch.setattr(app, "_acquire_bounded", failing_acquire_bounded)

    if case == "worker_creation_fails":
        def broken_pool(*args, **kwargs):
            raise RuntimeError("worker creation failed")

        monkeypatch.setattr(
            app, "run_in_threadpool",
            _fail_first_call(app.run_in_threadpool, broken_pool),
        )

    observed = {}
    reported = []

    def start(message):
        exchange = Exchange({"message": message, "history": []})
        return exchange, asyncio.ensure_future(exchange.run())

    async def finish(task, timeout=3.0):
        (result,) = await asyncio.wait_for(
            asyncio.gather(task, return_exceptions=True), timeout,
        )
        return _outcome(result)

    def snapshot():
        sem = app._get_chat_semaphore()
        entries = list(app._session_locks.values())
        return (
            app._chats_admitted,
            app._chat_waiting,
            worker.running,
            sem.locked(),
            len(entries),
            all(e.lock.locked() for e in entries) if entries else None,
        )

    async def a_running():
        assert await _wait_for(lambda: worker.running == 1)

    async def b_queued():
        assert await _wait_for(
            lambda: app._chats_admitted == 2 and app._chat_waiting == 1
        )

    async def replacements():
        # The detached worker still holds one admitted place and the slot:
        # one replacement may queue, the next is refused.
        r1_exchange, r1 = start("replacement 1")
        r2_exchange, r2 = start("replacement 2")
        observed["r2"] = await finish(r2)
        await asyncio.sleep(0.05)
        observed["during_replacement"] = (
            app._chats_admitted, app._chat_waiting, worker.running, r1.done(),
        )
        worker.release.set()
        observed["r1"] = await finish(r1)

    async def scenario():
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, context: reported.append(context)
        )
        sampler = AdmissionSampler(worker)
        sampler.start()
        a_exchange, a = start("A")

        try:
            if case in ("completed", "worker_exception"):
                await a_running()
                observed["while_running"] = snapshot()
                worker.release.set()
                observed["a"] = await finish(a)

            elif case in ("cancel_queued", "queue_timeout", "disconnect_queued"):
                await a_running()
                b_exchange, b = start("B")
                await b_queued()
                observed["queued"] = snapshot()

                if case == "cancel_queued":
                    b.cancel()
                elif case == "disconnect_queued":
                    b_exchange.disconnect.set()

                observed["b"] = await finish(b)
                observed["after_b"] = snapshot()
                worker.release.set()
                observed["a"] = await finish(a)

            elif case == "deadline_queued":
                await a_running()
                b_exchange, b = start("B")
                observed["a"] = await finish(a)
                observed["b"] = await finish(b)
                observed["held"] = snapshot()
                worker.release.set()

            elif case in ("disconnect_worker", "cancel_worker", "deadline_worker",
                          "detached_worker_fails"):
                await a_running()

                if case in ("disconnect_worker", "detached_worker_fails"):
                    a_exchange.disconnect.set()
                elif case == "cancel_worker":
                    a.cancel()

                observed["a"] = await finish(a)
                await asyncio.sleep(0.05)
                # The handler is gone; its worker still owns everything.
                observed["held"] = snapshot()

                if case == "detached_worker_fails":
                    worker.release.set()
                else:
                    await replacements()

            else:
                # Failures between admission and worker creation.
                observed["a"] = await finish(a)

            observed["recovered"] = await _recovered(worker)
        finally:
            worker.release.set()
            await asyncio.gather(a, return_exceptions=True)

        worker.fail = None
        observed["fresh"] = await finish(start("fresh")[1])
        observed["after_fresh"] = await _recovered(worker)
        await sampler.stop()
        gc.collect()
        await asyncio.sleep(0.05)
        return sampler

    sampler = asyncio.run(scenario())

    expected_a = {
        "worker_exception": INTERNAL,
        "deadline_queued": TIMEOUT,
        "disconnect_worker": CANCELLED,
        "cancel_worker": "cancelled",
        "deadline_worker": TIMEOUT,
        "detached_worker_fails": CANCELLED,
        "session_ref_fails": "raised RuntimeError",
        "session_lock_acquire_fails": INTERNAL,
        "slot_acquire_fails": INTERNAL,
        "worker_creation_fails": "raised RuntimeError",
    }

    if case in expected_a:
        assert observed["a"] == expected_a[case]
    else:
        assert observed["a"][0] == 200

    if case in ("completed", "worker_exception"):
        # (admitted, waiting, running, slot taken, sessions, session locked)
        assert observed["while_running"] == (1, 0, 1, True, 1, True)

    if case in ("cancel_queued", "queue_timeout", "disconnect_queued"):
        # B holds its own (distinct) session lock and waits for the slot.
        assert observed["queued"] == (2, 1, 1, True, 2, True)
        assert observed["b"] == {
            "cancel_queued": "cancelled",
            "queue_timeout": BUSY,
            "disconnect_queued": CANCELLED,
        }[case]
        # B's place, and only B's, was released.
        assert observed["after_b"] == (1, 0, 1, True, 1, True)

    if case == "deadline_queued":
        # B's queue deadline is selected by its request deadline (0.3 s
        # lookup + 0.7 s queue wait > 0.8 s), so its queue timeout is the
        # request timing out, however early the wait returns.
        assert observed["b"] == TIMEOUT

    if case in ("deadline_queued", "disconnect_worker", "cancel_worker",
                "deadline_worker", "detached_worker_fails"):
        # The worker outlived its handler: reservation, slot and session
        # lock are all still held by it.
        assert observed["held"] == (1, 0, 1, True, 1, True)

    if case in ("disconnect_worker", "cancel_worker", "deadline_worker"):
        assert observed["r2"] == BUSY
        assert observed["during_replacement"] == (2, 1, 1, False)
        assert observed["r1"][0] == 200

    # Exactly once: never negative, back to zero, no extra slot.
    assert sampler.min_admitted == 0 and sampler.min_waiting == 0
    assert sampler.max_admitted <= 2
    assert sampler.max_waiting <= 1
    assert sampler.max_running <= 1 and worker.max_running <= 1
    assert observed["recovered"] == (0, 0, 0, 0, 1)
    assert observed["fresh"][0] == 200
    assert observed["after_fresh"] == (0, 0, 0, 0, 1)
    assert reported == []


# --- review round 6: deterministic queue-timeout status mapping ---------------------------


import math


class ControlledTime:
    """Stands in for the app module's `time` name only. monotonic() returns
    a value the test sets; everything else is the real time module. The
    event loop, asyncio and the test's own waits keep the real clock."""

    def __init__(self, start):
        self.now = start

    def monotonic(self):
        return self.now

    def __getattr__(self, name):
        return getattr(time, name)


QUEUE_CLOCK_START = 1000.0
QUEUE_DEADLINE_SECONDS = 10.0
QUEUE_WAIT_SECONDS = 4.0

# case: (clock advance before the queue starts, where the timed-out wait
# resumes, expected response). The request deadline is START + 10.
#   elapsed 7: queue-wait deadline START + 11, request deadline selects START + 10
#   elapsed 6: both are START + 10 (tie)
#   elapsed 1: queue-wait deadline START + 5 selects; request deadline START + 10
QUEUE_MAPPING_CASES = {
    "request_selects": (7.0, "at_effective", TIMEOUT),
    "request_selects_early_wakeup": (7.0, "before_effective", TIMEOUT),
    "tie": (6.0, "at_effective", TIMEOUT),
    "tie_early_wakeup": (6.0, "before_effective", TIMEOUT),
    "queue_wait_selects_request_open": (1.0, "at_effective", BUSY),
    "queue_wait_selects_resumes_after_request_deadline": (1.0, "after_request", TIMEOUT),
    "disconnect_wins": (7.0, "disconnect_after_request", CANCELLED),
}


@pytest.mark.parametrize("stage", ["session_lock", "slot"])
@pytest.mark.parametrize("case", list(QUEUE_MAPPING_CASES))
def test_queue_timeout_status_follows_the_deadline_that_selected_it(monkeypatch, case, stage):
    """The queue wait for the session lock or the slot ends without the
    resource. The status depends on which configured deadline selected the
    effective queue deadline, and on whether the request deadline has really
    passed when a shorter queue wait expires -- never on how early the event
    loop happened to wake. The clock is controlled; nothing here sleeps to a
    deadline."""

    elapsed, resume, expected = QUEUE_MAPPING_CASES[case]
    enable(
        monkeypatch,
        deadline_seconds=QUEUE_DEADLINE_SECONDS,
        queue_wait_seconds=QUEUE_WAIT_SECONDS,
    )
    monkeypatch.setattr(app, "_usage_meter", None)
    clock = ControlledTime(QUEUE_CLOCK_START)
    monkeypatch.setattr(app, "time", clock)

    class ClockedBudget(RequestBudget):
        def __init__(self, **kwargs):
            super().__init__(clock=clock.monotonic, **kwargs)

    monkeypatch.setattr(app, "RequestBudget", ClockedBudget)

    # The queue starts right after the session id is resolved.
    original_resolve = app.resolve_session_id

    def resolve_late(session_id):
        clock.now += elapsed
        return original_resolve(session_id)

    monkeypatch.setattr(app, "resolve_session_id", resolve_late)

    worker = GatedWorker()
    worker.release.set()
    monkeypatch.setattr(app, "chat_core", worker)

    original_acquire = app._acquire_bounded
    acquire_calls = []
    observed = {}
    reported = []
    request_deadline = QUEUE_CLOCK_START + QUEUE_DEADLINE_SECONDS

    async def queue_wait_ends(acquire, release, deadline, stop):
        """The wait for this stage returns without the resource."""
        observed["effective_deadline"] = deadline

        if resume == "at_effective":
            clock.now = deadline
        elif resume == "before_effective":
            # The closest representable instant before the deadline: the
            # loop woke early, by any amount.
            clock.now = math.nextafter(deadline, -math.inf)
        elif resume == "after_request":
            clock.now = math.nextafter(request_deadline, math.inf)
        else:
            observed["exchange"].disconnect.set()
            await asyncio.wait_for(stop.wait(), 3.0)
            clock.now = math.nextafter(request_deadline, math.inf)
            return "stopped"

        return "timeout"

    async def staged_acquire(acquire, release, deadline, stop):
        acquire_calls.append(1)
        timed_out_call = 1 if stage == "session_lock" else 2

        if len(acquire_calls) == timed_out_call:
            return await queue_wait_ends(acquire, release, deadline, stop)

        return await original_acquire(acquire, release, deadline, stop)

    monkeypatch.setattr(app, "_acquire_bounded", staged_acquire)

    async def scenario():
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, context: reported.append(context)
        )
        exchange = Exchange({"message": "queued", "history": []})
        observed["exchange"] = exchange
        observed["result"] = await asyncio.wait_for(exchange.run(), 3.0)
        observed["headers"] = next(
            dict(m["headers"]) for m in exchange.messages
            if m["type"] == "http.response.start"
        )
        observed["recovered"] = await _recovered(worker)
        gc.collect()
        await asyncio.sleep(0.05)

    asyncio.run(scenario())

    queue_wait_deadline = QUEUE_CLOCK_START + elapsed + QUEUE_WAIT_SECONDS
    assert observed["effective_deadline"] == min(queue_wait_deadline, request_deadline)
    assert observed["result"] == expected
    assert observed["headers"][b"cache-control"] == b"no-store"
    assert len(acquire_calls) == (1 if stage == "session_lock" else 2)
    assert worker.calls == 0
    # (admitted, waiting, session entries, running workers, free slots)
    assert observed["recovered"] == (0, 0, 0, 0, app.MAX_CONCURRENT_CHATS)
    assert reported == []


# --- review round 7: acquisition-error integrity ------------------------------------------


ACQUIRE_FAILURE_DETAIL = "acquire-detail-must-not-leak"

# case: (clock advance before the queue starts, how the acquisition ends,
# expected response). Request deadline START + 10, queue wait 4 s.
#   elapsed 1: the queue-wait deadline (START + 5) selects; request open
#   elapsed 7: the request deadline (START + 10) selects
ACQUIRE_ERROR_CASES = {
    "raises_queue_wait_selected": (1.0, "raise", INTERNAL),
    "raises_request_deadline_selected": (7.0, "raise", INTERNAL),
    "task_cancelled_queue_wait_selected": (1.0, "cancelled", INTERNAL),
    "task_cancelled_request_deadline_selected": (7.0, "cancelled", INTERNAL),
    "disconnect_wins": (7.0, "disconnect_then_raise", CANCELLED),
    "request_deadline_wins": (1.0, "deadline_then_raise", TIMEOUT),
    "handler_cancelled": (1.0, "block", "cancelled"),
}


@pytest.mark.parametrize("stage", ["session_lock", "slot"])
@pytest.mark.parametrize("case", list(ACQUIRE_ERROR_CASES))
def test_acquisition_error_is_not_reported_as_busy_or_timeout(monkeypatch, case, stage):
    """The session-lock or slot acquisition itself fails. That is neither
    congestion nor a deadline: the answer is a fixed 500 internal_error,
    unless a disconnect or the real request deadline won first. Handler
    cancellation stays cancellation. Nothing about the failure is exposed,
    and every resource recovers. The clock is controlled."""

    elapsed, ending, expected = ACQUIRE_ERROR_CASES[case]
    enable(
        monkeypatch,
        deadline_seconds=QUEUE_DEADLINE_SECONDS,
        queue_wait_seconds=QUEUE_WAIT_SECONDS,
    )
    monkeypatch.setattr(app, "_usage_meter", None)
    clock = ControlledTime(QUEUE_CLOCK_START)
    monkeypatch.setattr(app, "time", clock)

    class ClockedBudget(RequestBudget):
        def __init__(self, **kwargs):
            super().__init__(clock=clock.monotonic, **kwargs)

    monkeypatch.setattr(app, "RequestBudget", ClockedBudget)

    original_resolve = app.resolve_session_id

    def resolve_late(session_id):
        clock.now += elapsed
        return original_resolve(session_id)

    monkeypatch.setattr(app, "resolve_session_id", resolve_late)

    worker = GatedWorker()
    worker.release.set()
    monkeypatch.setattr(app, "chat_core", worker)

    original_acquire = app._acquire_bounded
    acquire_calls = []
    observed = {}
    reported = []
    request_deadline = QUEUE_CLOCK_START + QUEUE_DEADLINE_SECONDS

    def failing_acquire(stop):
        async def acquire():
            if ending == "cancelled":
                raise asyncio.CancelledError()

            if ending == "disconnect_then_raise":
                observed["exchange"].disconnect.set()
                await asyncio.wait_for(stop.wait(), 3.0)
            elif ending == "deadline_then_raise":
                clock.now = math.nextafter(request_deadline, math.inf)
            elif ending == "block":
                observed["blocked"].set()
                await asyncio.Event().wait()

            raise RuntimeError(ACQUIRE_FAILURE_DETAIL)

        return acquire

    async def staged_acquire(acquire, release, deadline, stop):
        acquire_calls.append(1)
        failing_call = 1 if stage == "session_lock" else 2

        if len(acquire_calls) == failing_call:
            return await original_acquire(failing_acquire(stop), release, deadline, stop)

        return await original_acquire(acquire, release, deadline, stop)

    monkeypatch.setattr(app, "_acquire_bounded", staged_acquire)

    async def scenario():
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, context: reported.append(context)
        )
        observed["blocked"] = asyncio.Event()
        exchange = Exchange({"message": "queued", "history": []})
        observed["exchange"] = exchange
        handler = asyncio.ensure_future(exchange.run())

        if ending == "block":
            await asyncio.wait_for(observed["blocked"].wait(), 3.0)
            handler.cancel()

        (result,) = await asyncio.wait_for(
            asyncio.gather(handler, return_exceptions=True), 3.0,
        )
        observed["result"] = _outcome(result)
        observed["starts"] = [m for m in exchange.messages if m["type"] == "http.response.start"]
        observed["raw_body"] = b"".join(
            m.get("body", b"") for m in exchange.messages if m["type"] == "http.response.body"
        )
        observed["leftover_tasks"] = [
            t for t in asyncio.all_tasks()
            if t is not asyncio.current_task() and not t.done()
        ]
        observed["recovered"] = await _recovered(worker)

        observed["fresh"] = await asyncio.wait_for(
            Exchange({"message": "fresh", "history": []}).run(), 3.0,
        )
        observed["after_fresh"] = await _recovered(worker)
        gc.collect()
        await asyncio.sleep(0.05)

    asyncio.run(scenario())

    assert observed["result"] == expected
    assert len(acquire_calls) >= (1 if stage == "session_lock" else 2)

    if expected == "cancelled":
        # Handler cancellation propagated; no response was produced for it.
        assert observed["starts"] == []
    else:
        # Exactly one fixed response, and nothing else in it.
        assert len(observed["starts"]) == 1
        headers = dict(observed["starts"][0]["headers"])
        assert headers[b"cache-control"] == b"no-store"
        assert json.loads(observed["raw_body"]) == expected[1]
        assert ACQUIRE_FAILURE_DETAIL.encode() not in observed["raw_body"]
        assert b"Traceback" not in observed["raw_body"]
        assert b"RuntimeError" not in observed["raw_body"]
        assert b"session_id" not in observed["raw_body"]
        assert b"reply" not in observed["raw_body"]

    # The watcher and every helper task were cleaned up with the request.
    assert observed["leftover_tasks"] == []
    # No worker started; (admitted, waiting, session entries, running, free slots)
    assert worker.calls == 1                    # the fresh request only
    assert observed["recovered"] == (0, 0, 0, 0, app.MAX_CONCURRENT_CHATS)
    assert observed["fresh"][0] == 200
    assert observed["after_fresh"] == (0, 0, 0, 0, app.MAX_CONCURRENT_CHATS)
    assert reported == []


# --- bounded provider transport: OpenAI Responses (slice 1) --------------------------------


from kalillac_routing.bounded_transport import (
    InvalidJSONResponse,
    ResponseTooLarge,
    TransportCancelled,
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

SECRET_PROMPT = "PROMPT-TEXT-THAT-MUST-NOT-LEAK"
SERVICE_UNAVAILABLE = (503, {"error": "service_unavailable"})
PROVIDERS_UNAVAILABLE = (503, {"error": "model_provider_unavailable"})
INTERNAL_ERROR = (500, {"error": "internal_error"})


class _CapturedResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_flag_off_openai_keeps_urllib_and_never_creates_a_transport(monkeypatch):
    captured = []

    def fake_urlopen(request, timeout=None):
        captured.append((request, timeout))
        return _CapturedResponse(json.dumps(openai_reply("plain")).encode())

    monkeypatch.setattr(app.urllib.request, "urlopen", fake_urlopen)
    payload = {"model": app.OPENAI_MODEL, "input": [{"role": "user", "content": "hi"}]}

    data = app._post_openai_for_attempt(payload)

    assert data == openai_reply("plain")
    request, timeout = captured[0]
    assert request.full_url == app.OPENAI_RESPONSES_URL == "https://api.openai.com/v1/responses"
    assert request.data == json.dumps(payload).encode()
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer test-openai-key"
    assert request.get_header("Content-type") == "application/json"
    assert timeout == 90
    assert app._OPENAI_TRANSPORT.existing() is None


@pytest.mark.parametrize("path", ["primary", "continuation", "native_tool_rounds"])
def test_every_budgeted_openai_request_uses_the_bounded_transport(providers, monkeypatch, path):
    calls, state = providers
    urllib_calls = []
    monkeypatch.setattr(
        app.urllib.request, "urlopen", lambda *a, **k: urllib_calls.append(1),
    )
    request_budget = budget(models=6)

    with budget_scope(request_budget):
        if path == "primary":
            state["openai"] = [openai_reply("answer")]
            app.invoke_llm(MESSAGES)
            expected = 1
        elif path == "continuation":
            state["openai"] = [openai_reply("Partial", cut_off=True), openai_reply(" rest")]
            app.invoke_llm(MESSAGES)
            expected = 2
        else:
            state["openai"] = [
                {"output": [{
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "get_kalillac_runtime_facts",
                    "arguments": '{"topic": "models"}',
                }]},
                openai_reply("final answer"),
            ]
            app._run_v31_native_tool_chat("what model are you", [], {"memory": [], "search_times": []})
            expected = 2

    # One outbound request, one admission, one transport post: no more.
    assert urllib_calls == []
    assert names(calls) == ["openai"] * expected
    assert len(state["transport_posts"]) == expected
    assert request_budget.model_attempts == expected


def test_budgeted_payload_headers_and_limits_match_the_urllib_request(providers, monkeypatch):
    calls, state = providers
    captured = []

    def fake_urlopen(request, timeout=None):
        captured.append(request)
        return _CapturedResponse(json.dumps(openai_reply("plain")).encode())

    monkeypatch.setattr(app.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(app, "_post_openai_responses", ORIGINAL_POST_OPENAI_RESPONSES)
    payload = {"model": app.OPENAI_MODEL, "input": [{"role": "user", "content": "hé \"q\""}]}

    app._post_openai_for_attempt(payload)                     # flag off: urllib

    state["openai"] = [openai_reply("bounded")]

    with budget_scope(budget()):
        app._post_openai_for_attempt(payload)                 # budgeted: transport

    request = captured[0]
    post = state["transport_posts"][0]
    assert post["url"] == request.full_url
    assert json.dumps(post["payload"], allow_nan=False).encode() == request.data
    assert {k.lower(): v for k, v in post["headers"].items()} == {
        k.lower(): v for k, v in request.header_items()
    }
    assert post["max_bytes"] == 2097152
    assert state["transport"].settings == {
        "max_outstanding": TRANSPORT_LIMITS.max_outstanding,
        "dns_threads": TRANSPORT_LIMITS.dns_threads,
        "max_pending_dns": TRANSPORT_LIMITS.max_pending_dns,
        "cancel_poll_interval": TRANSPORT_LIMITS.cancel_poll_interval_seconds,
        "backstop_grace": TRANSPORT_LIMITS.backstop_grace_seconds,
        "cleanup_grace": TRANSPORT_LIMITS.cleanup_grace_seconds,
    }


def test_budgeted_timeout_and_provenance_come_from_one_selection(providers, monkeypatch):
    calls, state = providers
    state["openai"] = [openai_reply("answer")]
    selections = []
    request_budget = budget(seconds=5.0)
    real_select = request_budget.select_call_timeout

    def spy(cap):
        result = real_select(cap)
        selections.append((cap, result))
        return result

    monkeypatch.setattr(request_budget, "select_call_timeout", spy)
    monkeypatch.setattr(request_budget, "call_timeout", lambda cap: pytest.fail("second read"))

    with budget_scope(request_budget):
        app._post_openai_for_attempt({"model": "m"})

    (cap, (timeout, selected)), = selections
    assert cap == app.OPENAI_CALL_TIMEOUT_SECONDS
    assert selected is True                      # 5 s remain, cap 90
    assert state["transport_posts"][0]["timeout"] == timeout


def _chat_through_invoke_llm(message, history, request=None, session_id=None):
    return app.invoke_llm(
        [SystemMessage(content="system"), HumanMessage(content=SECRET_PROMPT)]
    ).content


def _handler_run(monkeypatch, *, cap=None):
    """One budgeted /api/chat request whose chat runs the real provider chain."""
    enable(monkeypatch)
    monkeypatch.setattr(app, "chat", _chat_through_invoke_llm)

    if cap is not None:
        monkeypatch.setattr(app, "OPENAI_CALL_TIMEOUT_SECONDS", cap)

    async def scenario():
        exchange = Exchange({"message": SECRET_PROMPT, "history": []})
        result = await asyncio.wait_for(exchange.run(), 5.0)
        start = next(m for m in exchange.messages if m["type"] == "http.response.start")
        return result, dict(start["headers"])

    return asyncio.run(scenario())


LOCAL_FAILURES = {
    "overloaded": lambda: TransportOverloaded(),
    "quarantined": lambda: TransportQuarantined(),
    "closed": lambda: TransportClosed(),
    "base_transport_error": lambda: TransportError(),
    "cancelled_by_transport": lambda: TransportCancelled(),
    "cleanup_connection": lambda: TransportCleanupUnconfirmed("connection"),
    "cleanup_cancelled_without_request_stop": lambda: TransportCleanupUnconfirmed("cancelled"),
}


@pytest.mark.parametrize("failure", list(LOCAL_FAILURES))
def test_local_transport_failure_is_service_unavailable_without_fallback(
    providers, monkeypatch, capsys, failure,
):
    calls, state = providers
    state["openai"] = [LOCAL_FAILURES[failure]()]

    (status, body), headers = _handler_run(monkeypatch)

    assert (status, body) == SERVICE_UNAVAILABLE
    assert headers[b"cache-control"] == b"no-store"
    # No other provider and no second OpenAI request.
    assert names(calls) == ["openai"]
    assert len(state["transport_posts"]) == 1
    assert state["holder"].quarantined is failure.startswith("cleanup")
    out = capsys.readouterr()
    for secret in (SECRET_PROMPT, "test-openai-key", "Bearer", app.OPENAI_RESPONSES_URL):
        assert secret not in out.out + out.err
        assert secret not in json.dumps(body)


def test_cap_selected_unconfirmed_cleanup_is_service_unavailable_and_quarantines(
    providers, monkeypatch,
):
    calls, state = providers
    state["openai"] = [TransportCleanupUnconfirmed("deadline")]

    # A 1 s per-call cap is shorter than the 5 s request: cap-selected.
    (status, body), headers = _handler_run(monkeypatch, cap=1.0)

    assert (status, body) == SERVICE_UNAVAILABLE
    assert names(calls) == ["openai"]
    assert state["holder"].quarantined is True


def test_clean_cap_selected_timeout_is_provider_unavailable_without_fallback(
    providers, monkeypatch,
):
    calls, state = providers
    state["openai"] = [TransportDeadlineExceeded()]
    monkeypatch.setattr(app, "OPENAI_CALL_TIMEOUT_SECONDS", 1.0)

    with budget_scope(budget(seconds=5.0)):
        with pytest.raises(app.ModelProviderUnavailable) as caught:
            app.invoke_llm(MESSAGES)

    assert not isinstance(caught.value, app.LocalModelServiceUnavailable)
    assert names(calls) == ["openai"]
    assert state["holder"].quarantined is False


@pytest.mark.parametrize(
    "error",
    [TransportConnectionError(), TransportHTTPError(500), InvalidJSONResponse(),
     UnsupportedContentEncoding(), ResponseTooLarge()],
    ids=lambda e: type(e).__name__,
)
def test_remote_openai_failures_are_provider_unavailable_without_fallback(providers, error):
    calls, state = providers
    state["openai"] = [error]

    with budget_scope(budget()):
        with pytest.raises(app.ModelProviderUnavailable) as caught:
            app.invoke_llm(MESSAGES)

    assert not isinstance(caught.value, app.LocalModelServiceUnavailable)
    assert names(calls) == ["openai"]
    assert state["holder"].quarantined is False


@pytest.mark.parametrize(
    "case, expected",
    [("cancelled", (499, {"error": "request_cancelled"})),
     ("request_deadline", (504, {"error": "request_timeout"}))],
)
def test_request_stop_beats_unconfirmed_cleanup_and_still_quarantines(
    providers, monkeypatch, case, expected,
):
    calls, state = providers

    def stop_then_unconfirmed():
        if case == "cancelled":
            current_budget().cancel()
            return TransportCleanupUnconfirmed("cancelled")

        # 5 s request, 90 s cap: the request deadline selected the timeout.
        return TransportCleanupUnconfirmed("deadline")

    state["openai"] = [stop_then_unconfirmed, openai_reply("never sent")]

    (status, body), headers = _handler_run(monkeypatch)

    assert (status, body) == expected
    assert headers[b"cache-control"] == b"no-store"
    assert names(calls) == ["openai"]
    assert state["holder"].quarantined is True

    # The quarantine persists: the next request is a local 503 with no post.
    (status, body), _ = _handler_run(monkeypatch)

    assert (status, body) == SERVICE_UNAVAILABLE
    assert len(state["transport_posts"]) == 1


@pytest.mark.parametrize("defect", ["type_error", "value_error", "incompatible_settings"])
def test_transport_defects_are_internal_errors_without_fallback(
    providers, monkeypatch, capsys, defect,
):
    calls, state = providers

    if defect == "type_error":
        state["openai"] = [TypeError(SECRET_PROMPT)]
    elif defect == "value_error":
        state["openai"] = [ValueError(SECRET_PROMPT)]
    else:
        other = TransportLimits(**{**TRANSPORT_LIMITS.__dict__, "max_outstanding": 99})
        state["holder"].get_or_create(other)

    (status, body), headers = _handler_run(monkeypatch)

    assert (status, body) == INTERNAL_ERROR
    assert headers[b"cache-control"] == b"no-store"
    assert names(calls) == ([] if defect == "incompatible_settings" else ["openai"])
    out = capsys.readouterr()
    assert SECRET_PROMPT not in out.out + out.err


@pytest.mark.parametrize(
    "error, raised",
    [(TransportOverloaded(), "ProviderTransportUnavailable"), (ValueError("x"), "ChatInternalError")],
)
def test_continuation_cannot_swallow_a_local_transport_failure(providers, error, raised):
    calls, state = providers
    state["openai"] = [openai_reply("Partial", cut_off=True), error]

    with budget_scope(budget()):
        with pytest.raises(getattr(app, raised)):
            app.invoke_llm(MESSAGES)

    assert names(calls) == ["openai", "openai"]


@pytest.mark.parametrize(
    "error, raised",
    [(TransportOverloaded(), "ProviderTransportUnavailable"), (ValueError("x"), "ChatInternalError")],
)
def test_native_routing_cannot_fall_through_after_a_local_transport_failure(
    providers, monkeypatch, error, raised,
):
    calls, state = providers
    legacy = []
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", True)
    monkeypatch.setattr(app, "classify_request", lambda message, history: "general")
    monkeypatch.setattr(app, "requires_web_verification", lambda message, history: False)
    monkeypatch.setattr(app, "invoke_llm", lambda *a, **k: legacy.append(1))
    state["openai"] = [error]

    with budget_scope(budget()):
        with pytest.raises(getattr(app, raised)):
            app.chat("hello there", [], session_id="bounded-native-local")

    assert legacy == []
    assert names(calls) == ["openai"]


@pytest.mark.parametrize(
    "raised, expected",
    [("ProviderTransportUnavailable", SERVICE_UNAVAILABLE),
     ("ModelProviderUnavailable", PROVIDERS_UNAVAILABLE),
     ("CallBudgetExhausted", PROVIDERS_UNAVAILABLE)],
)
def test_handler_keeps_local_transport_and_provider_exhaustion_distinct(
    monkeypatch, raised, expected,
):
    """Fails if the local-transport handler is moved after, or merged into,
    the broader ModelProviderUnavailable handler."""
    enable(monkeypatch)

    def failing_chat(message, history, request=None, session_id=None):
        if raised == "CallBudgetExhausted":
            raise CallBudgetExhausted("model")
        raise getattr(app, raised)()

    monkeypatch.setattr(app, "chat", failing_chat)

    async def scenario():
        exchange = Exchange({"message": "hi", "history": []})
        result = await asyncio.wait_for(exchange.run(), 5.0)
        start = next(m for m in exchange.messages if m["type"] == "http.response.start")
        return result, dict(start["headers"])

    result, headers = asyncio.run(scenario())

    assert issubclass(app.ProviderTransportUnavailable, app.ModelProviderUnavailable)
    assert result == expected
    assert headers[b"cache-control"] == b"no-store"


def test_shutdown_never_constructs_a_transport(monkeypatch):
    with TestClient(app.api):
        pass

    assert app._OPENAI_TRANSPORT.existing() is None
    assert app._OPENAI_TRANSPORT.closed is True


def test_shutdown_boundedly_closes_an_existing_transport_once_per_call(providers):
    calls, state = providers
    transport = state["holder"].get_or_create(TRANSPORT_LIMITS)

    with TestClient(app.api):
        pass

    app._close_openai_transport()                  # repeated shutdown is safe

    assert transport.closes == [TRANSPORT_LIMITS.close_timeout_seconds] * 2
    assert state["holder"].closed is True
