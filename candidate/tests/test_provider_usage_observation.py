"""Content-free observation at actual scripted provider seams, never live APIs."""
import asyncio
import copy
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import json
import threading
import time

import pytest
import app_fastapi_candidate as app
from kalillac_routing.request_budget import RequestBudget, RequestCancelled, budget_scope
from test_search_provider_policy import providers, brave_result, tavily_result

USAGE = {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 10, "cache_write_tokens": 20},
         "output_tokens": 40, "output_tokens_details": {"reasoning_tokens": 5}}


def response(text="A supported explanation.", **changes):
    result = {"status": "completed", "model": "gpt-5.6-luna", "service_tier": "default",
              "usage": copy.deepcopy(USAGE),
              "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}]}
    result.update(changes)
    return result


def wire_response(**changes):
    import httpx
    data = json.dumps(response(**changes)).encode("utf-8")
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield data
    return httpx.Response(200, stream=Stream(), headers={"Content-Type": "application/json"})


def tool_response(call_id="synthetic-tool"):
    return response(output=[{"type": "function_call", "name": "search_web", "call_id": call_id,
                             "arguments": json.dumps({"query": "site:docs.example.test opening hours"})}])


@pytest.fixture
def model(monkeypatch):
    from datetime import datetime, timezone
    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime(2026, 10, 11, 2, 28, 1, tzinfo=timezone.utc)
            return value if tz is not None else value.replace(tzinfo=None)
    monkeypatch.setattr(app, "datetime", FixedClock)
    state = {"responses": [], "payloads": []}
    def post(payload, *args):
        state["payloads"].append(copy.deepcopy(payload))
        assert state["responses"], "Unscripted model attempt"
        value = state["responses"].pop(0)
        if isinstance(value, BaseException): raise value
        return value() if callable(value) else copy.deepcopy(value)
    monkeypatch.setattr(app, "OPENAI_API_KEY", "synthetic-key-never-export")
    monkeypatch.setattr(app, "_post_openai_responses", post)
    monkeypatch.setattr(app, "_post_openai_bounded", post)
    monkeypatch.setattr(app, "_request_limits", None)
    monkeypatch.setattr(app, "SESSION_STATE", type(app.SESSION_STATE)())
    return state


def capture(work, case=1):
    # Before integration, run the real pipeline and demonstrate absent records.
    module = getattr(app, "provider_usage", None)
    if module is None:
        return work(), {"requests": []}
    collector = module.EvaluationCollector()
    with module.observation_scope(collector, case_number=case):
        value = work()
    return value, collector.snapshot()


@pytest.mark.parametrize("native", [False, True])
def test_pipeline_captures_usage_before_output_cleanup(model, monkeypatch, native):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    model["responses"] = [response()]
    value, data = capture(lambda: app.chat("Explain public key encryption simply.", [], session_id="usage-test"))
    assert value == "A supported explanation."
    assert len(data["requests"]) == 1
    record = data["requests"][0]["attempts"][0]
    assert record["input_tokens"] == 100 and record["output_tokens"] == 40
    assert record["cached_tokens"] == 10 and record["cache_write_tokens"] == 20
    assert record["reasoning_tokens"] == 5 and record["usage_status"] == "complete"
    assert record["provider"] == "openai" and record["returned_model"] == "gpt-5.6-luna"
    assert record["admitted"] is True and record["dispatched"] is True
    assert model["payloads"][0]["store"] is False


@pytest.mark.parametrize("native", [False, True])
def test_pipeline_counts_automatic_continuation_once_per_call(model, monkeypatch, native):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    model["responses"] = [response("Public keys ", status="incomplete", incomplete_details={"reason": "max_output_tokens"}),
                           response("are shared.")]
    value, data = capture(lambda: app.chat("Explain public key encryption simply.", [], session_id="usage-test"))
    assert "are shared." in value and len(model["payloads"]) == 2
    assert len(data["requests"]) == 1 and len(data["requests"][0]["attempts"]) == 2
    assert sum(a["input_tokens"] for a in data["requests"][0]["attempts"]) == 200


@pytest.mark.parametrize("native", [False, True])
def test_pipeline_manual_code_remainder_keeps_task_and_usage(model, monkeypatch, native):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    task = "Write a short Python example combining two bit masks with XOR, then print the result."
    history = [{"role": "user", "content": task}, {"role": "assistant", "content": app.mark_incomplete_reply("```\nmask = 1 ^ 2")}]
    model["responses"] = [response("print(mask)")]
    value, data = capture(lambda: app.chat("continue", history, session_id="usage-test"))
    assert "print(mask)" in value
    assert task in str(model["payloads"])
    assert len(data["requests"]) == 1 and len(data["requests"][0]["attempts"]) == 1


@pytest.mark.parametrize("native", [False, True])
def test_pipeline_actual_search_and_tool_rounds(model, providers, monkeypatch, native):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    providers["brave"] = [brave_result(content="Opening hours: 9am to 5pm.")]
    model["responses"] = ([tool_response(), response("The listed hours are 9am to 5pm.")] if native
                           else [response("The listed hours are 9am to 5pm.")])
    value, data = capture(lambda: app.chat("Search https://docs.example.test/reference for opening hours.", [], session_id="usage-test"))
    assert "9am to 5pm" in value and len(providers["calls"]) == 1
    assert len(data["requests"]) == 1
    attempts = data["requests"][0]["attempts"]
    assert [a["provider"] for a in attempts].count("brave") == 1
    assert [a["provider"] for a in attempts].count("tavily") == 0
    assert [a["provider"] for a in attempts].count("openai") == (2 if native else 1)


@pytest.mark.parametrize("native", [False, True])
def test_pipeline_payloads_and_replies_identical_when_observed(model, monkeypatch, native):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    model["responses"] = [response(), response()]
    expected = app.chat("Explain public key encryption simply.", [], session_id="usage-control")
    payload = copy.deepcopy(model["payloads"][-1])
    value, data = capture(lambda: app.chat("Explain public key encryption simply.", [], session_id="usage-test"))
    assert value == expected and model["payloads"][-1] == payload
    assert len(data["requests"]) == 1


from kalillac_routing import provider_usage as usage
from kalillac_routing.request_budget import CallBudgetExhausted, RequestDeadlineExceeded
from kalillac_routing.bounded_transport import TransportHTTPError


@pytest.mark.parametrize("native", [False, True])
def test_actual_native_or_legacy_failure_fallback(model, providers, monkeypatch, native):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    providers["brave"] = [TransportHTTPError(503)]
    providers["tavily"] = [tavily_result(content="Opening hours: 9am to 5pm.")]
    model["responses"] = ([tool_response(), response()] if native else [response()])
    budget = RequestBudget(duration_seconds=45, max_model_attempts=6, max_search_attempts=6)
    with budget_scope(budget):
        _, data = capture(lambda: app.chat("Search https://docs.example.test/reference for opening hours.", [], session_id="usage-test"))
    attempts = data["requests"][0]["attempts"]
    search = [a for a in attempts if a["provider"] != "openai"]
    assert [a["provider"] for a in search] == ["brave", "tavily"]
    assert search[0]["phase"] == "failed" and search[0]["dispatched"] is True
    assert search[1]["depth"] in {"basic", "advanced"}
    assert budget.search_attempts == 2


def test_actual_native_followup_each_provider_once(model, providers, monkeypatch):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", True)
    providers["brave"] = [brave_result(content="An older observation; current hours uncertain.")]
    providers["tavily"] = [tavily_result(content="Confirmed public opening hours.")]
    model["responses"] = [tool_response(), tool_response("synthetic-tool-2"), response()]
    budget = RequestBudget(duration_seconds=45, max_model_attempts=6, max_search_attempts=6)
    with budget_scope(budget):
        _, data = capture(lambda: app.chat("Search https://docs.example.test/reference for opening hours.", [], session_id="usage-test"))
    attempts = data["requests"][0]["attempts"]
    assert [a["provider"] for a in attempts] == ["openai", "brave", "openai", "tavily", "openai"]
    assert budget.model_attempts == 3 and budget.search_attempts == 2
    assert sum(a["input_tokens"] for a in attempts if a["provider"] == "openai") == 300


@pytest.mark.parametrize("kind", ["cancelled", "deadline", "allowance"])
def test_stops_do_not_dispatch_another_model_call(model, kind):
    clock = [0.0]
    budget = RequestBudget(duration_seconds=1, max_model_attempts=1, max_search_attempts=1, clock=lambda: clock[0])
    if kind == "cancelled": budget.cancel()
    if kind == "deadline": clock[0] = 2
    if kind == "allowance": budget.admit_model_attempt()
    expected = {"cancelled": RequestCancelled, "deadline": RequestDeadlineExceeded, "allowance": CallBudgetExhausted}[kind]
    collector = usage.EvaluationCollector()
    with usage.observation_scope(collector, case_number=1), budget_scope(budget):
        with pytest.raises(expected):
            app._post_openai_for_attempt({"model": "gpt-5.6-luna"})
    record = collector.snapshot()["requests"][0]["attempts"][0]
    assert record["phase"] == "rejected" and record["admitted"] is False
    assert record["dispatched"] is False and model["payloads"] == []
    assert record["input_tokens"] is None


def test_missing_search_key_is_not_a_dispatch(providers, monkeypatch):
    monkeypatch.setattr(app, "BRAVE_API_KEY", None)
    monkeypatch.setattr(app, "TAVILY_API_KEY", None)
    result, data = capture(lambda: app.run_web_search("public reference"))
    assert result == ("unavailable", []) and providers["calls"] == []
    record = data["requests"][0]["attempts"][0]
    assert record["failure"] == "missing_credential" and record["dispatched"] is False


def test_legacy_unbudgeted_sdk_has_explicit_unknown_coverage(providers, monkeypatch):
    from kalillac_routing.search_providers import SearchProviderPolicy
    monkeypatch.setattr(app, "SEARCH_PROVIDER_POLICY", SearchProviderPolicy("tavily", "none"))
    providers["tavily"] = [tavily_result()]
    _, data = capture(lambda: app.run_web_search("public reference"))
    assert len(providers["calls"]) == 1
    assert data["requests"][0]["unobserved_operations"] is True


@pytest.mark.parametrize("bad_usage,status", [(None, "missing"), ({"input_tokens": 100, "output_tokens": 40}, "partial"),
                                            ({**USAGE, "input_tokens": True}, "invalid"),
                                            ({**USAGE, "output_tokens": -1}, "invalid"),
                                            ({**USAGE, "input_tokens": 1}, "invalid"),
                                            ({**USAGE, "output_tokens": 2}, "invalid"),
                                            ({**USAGE, "input_tokens": 10**20}, "invalid")])
def test_absent_or_invalid_usage_is_not_zero_cost(model, bad_usage, status):
    model["responses"] = [response(usage=bad_usage)]
    _, data = capture(lambda: app._invoke_openai([app.HumanMessage(content="synthetic")]))
    record = data["requests"][0]["attempts"][0]
    assert record["usage_status"] == status
    if status != "partial": assert record["input_tokens"] is None


def test_sentinel_contents_and_arbitrary_metadata_never_export(model):
    secret = "SENTINEL-never-copy-prompt-history-key-url-cookie-email"
    model["responses"] = [response(secret, model=secret, service_tier=secret * 1000, status=secret)]
    _, data = capture(lambda: app._invoke_openai([app.HumanMessage(content=secret)]))
    exported = json.dumps(data)
    assert secret not in exported and len(exported) < 3000
    record = data["requests"][0]["attempts"][0]
    assert record["returned_model"] == record["service_tier"] == record["provider_status"] == "unknown"
    assert record["input_tokens"] == 100


def test_collector_and_attempt_capacity_are_explicit_loss(model):
    collector = usage.EvaluationCollector(max_requests=1, max_attempts=1)
    model["responses"] = [response(), response(), response()]
    with usage.observation_scope(collector, case_number=1):
        app._invoke_openai([app.HumanMessage(content="one")])
        app._invoke_openai([app.HumanMessage(content="two")])
    with usage.observation_scope(collector, case_number=2):
        app._invoke_openai([app.HumanMessage(content="three")])
    data = collector.snapshot()
    assert data["observation_lost"] and len(data["requests"]) == 1
    assert data["requests"][0]["observation_lost"] and len(data["requests"][0]["attempts"]) == 1
    assert len(model["payloads"]) == 3


@pytest.mark.parametrize("case", [True, 0, 21, "private arbitrary label", "X" * 100000], ids=["boolean", "zero", "out-of-range", "arbitrary", "overlength"])
def test_case_labels_are_numeric_allowlisted_and_bounded(case):
    with pytest.raises(ValueError, match="synthetic case number"):
        with usage.observation_scope(usage.EvaluationCollector(), case_number=case): pass


@pytest.mark.parametrize("capacity", [True, 0, 65, 1000000, "64"])
def test_collector_rejects_unbounded_configuration(capacity):
    with pytest.raises(ValueError): usage.EvaluationCollector(max_requests=capacity)


def test_slow_sink_is_not_an_accepted_interface():
    class SlowSink:
        def __call__(self, value): raise AssertionError("A sink must never be invoked")
    with pytest.raises(TypeError):
        with usage.observation_scope(SlowSink(), case_number=1): pass


def test_contended_collector_never_waits_or_changes_reply(model):
    collector = usage.EvaluationCollector()
    collector._lock.acquire()
    model["responses"] = [response()]
    started = time.monotonic()
    try:
        with usage.observation_scope(collector, case_number=1):
            assert app._invoke_openai([app.HumanMessage(content="synthetic")]).content == "A supported explanation."
    finally:
        collector._lock.release()
    assert time.monotonic() - started < 0.5
    assert collector.snapshot()["observation_lost"]


def test_export_revalidates_categories_and_ignores_extra_fields(model):
    collector = usage.EvaluationCollector();model["responses"] = [response()]
    with usage.observation_scope(collector, case_number=1): app._invoke_openai([app.HumanMessage(content="synthetic")])
    record = collector._requests[0].attempts[0]
    record.update(service_tier="private raw provider string", unexpected="private extra field", credits="not-a-number")
    data = collector.snapshot()["requests"][0]["attempts"][0]
    assert data["service_tier"] == "unknown" and "unexpected" not in data and data["credits"] is None


def test_deterministic_reply_has_no_provider_attempts(model):
    value, data = capture(lambda: app.chat("2 + 2", [], session_id="deterministic"))
    assert "4" in value and model["payloads"] == []
    assert data["requests"][0]["attempts"] == [] and not data["requests"][0]["unobserved_operations"]



def test_concurrent_request_contexts_do_not_mix(model, monkeypatch):
    barrier = threading.Barrier(2)
    def post(payload, *args):
        value = int(payload["input"][0]["content"])
        barrier.wait(timeout=3)
        tokens = {**USAGE, "input_tokens": value}
        return response(usage=tokens)
    monkeypatch.setattr(app, "_post_openai_responses", post)
    collector = usage.EvaluationCollector()
    def work(case, tokens):
        with usage.observation_scope(collector, case_number=case):
            app._post_openai_for_attempt({"model": "gpt-5.6-luna", "input": [{"content": str(tokens)}]})
    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = [pool.submit(work, 1, 100), pool.submit(work, 2, 200)]
        for task in tasks: task.result(timeout=5)
    result = {r["case_number"]: r["attempts"][0]["input_tokens"] for r in collector.snapshot()["requests"]}
    assert result == {1: 100, 2: 200}


def test_closed_context_late_completion_cannot_attach_to_next_request(model, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def post(payload, *args):
        calls.append(payload)
        if len(calls) == 1:
            entered.set();assert release.wait(3)
            return response("A partial answer", status="incomplete", incomplete_details={"reason": "max_output_tokens"})
        return response(usage={**USAGE, "input_tokens": 200})
    monkeypatch.setattr(app, "_post_openai_bounded", post)
    collector = usage.EvaluationCollector()
    budget = RequestBudget(duration_seconds=10, max_model_attempts=6, max_search_attempts=6)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with usage.observation_scope(collector, case_number=1), budget_scope(budget):
            context = copy_context()
            job = pool.submit(context.run, app._invoke_openai, [app.HumanMessage(content="first")])
            assert entered.wait(3)
            budget.cancel()
        first = collector.snapshot()["requests"][0]
        assert first["attempts"][0]["phase"] == "unobserved"
        assert first["attempts"][0]["input_tokens"] is None
        with usage.observation_scope(collector, case_number=2), budget_scope(RequestBudget(duration_seconds=10, max_model_attempts=6, max_search_attempts=6)):
            app._invoke_openai([app.HumanMessage(content="second")])
        release.set()
        with pytest.raises(RequestCancelled): job.result(timeout=4)
    data = collector.snapshot()
    assert data["requests"][0] == first
    assert data["requests"][1]["attempts"][0]["input_tokens"] == 200
    assert len(calls) == 2 and budget.model_attempts == 1


def test_actual_api_cancelled_request_seals_usage_and_preserves_next_reply(model, monkeypatch):
    import httpx
    from test_search_provider_policy import LIMITS
    monkeypatch.setattr(app, "_request_limits", LIMITS)
    monkeypatch.setattr(app, "_chat_semaphore", None)
    monkeypatch.setattr(app, "_chat_waiting", 0)
    monkeypatch.setattr(app, "_session_locks", {})
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def post(payload, *args):
        calls.append(payload)
        if len(calls) == 1:
            entered.set();assert release.wait(4);completed.set()
            return response(usage={**USAGE, "input_tokens": 999})
        return response(usage={**USAGE, "input_tokens": 200})
    monkeypatch.setattr(app, "_post_openai_bounded", post)
    collector = usage.EvaluationCollector()
    async def run():
        transport = httpx.ASGITransport(app=app.api)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            async def first():
                with usage.observation_scope(collector, case_number=1):
                    return await client.post("/api/chat", json={"message": "Explain public key encryption simply.", "history": []})
            task = asyncio.create_task(first())
            for _ in range(300):
                if entered.is_set(): break
                await asyncio.sleep(.01)
            assert entered.is_set()
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
            sealed = collector.snapshot()["requests"][0]
            with usage.observation_scope(collector, case_number=2):
                reply = await client.post("/api/chat", json={"message": "Explain public key encryption simply.", "history": []})
            assert reply.status_code == 200 and reply.headers["cache-control"] == "no-store"
            release.set()
            for _ in range(300):
                if completed.is_set() and not app._session_locks: break
                await asyncio.sleep(.01)
            assert completed.is_set()
            assert collector.snapshot()["requests"][0] == sealed
    try: asyncio.run(run())
    finally: release.set()
    assert len(calls) == 2
    rows = collector.snapshot()["requests"]
    assert rows[0]["outcome"] == "cancelled" and rows[0]["attempts"][0]["input_tokens"] is None
    assert rows[1]["attempts"][0]["input_tokens"] == 200


def test_actual_api_unusable_response_retains_usage_and_typed_error(model):
    from fastapi.testclient import TestClient
    model["responses"] = [response(output=[])]
    collector = usage.EvaluationCollector()
    with usage.observation_scope(collector, case_number=1), TestClient(app.api) as client:
        result = client.post("/api/chat", json={"message": "Explain public key encryption simply.", "history": []})
    assert result.status_code == 503 and result.json() == {"error": "model_provider_unavailable"}
    assert result.headers["cache-control"] == "no-store"
    assert collector.snapshot()["requests"][0]["attempts"][0]["input_tokens"] == 100


def test_failed_observer_does_not_change_actual_api_reply(model, monkeypatch):
    from fastapi.testclient import TestClient
    collector = usage.EvaluationCollector();model["responses"] = [response()]
    def fail(*args, **kwargs): raise RuntimeError("synthetic observer fault")
    with usage.observation_scope(collector, case_number=1):
        monkeypatch.setattr(collector._requests[0], "begin", fail)
        with TestClient(app.api) as client:
            result = client.post("/api/chat", json={"message": "Explain public key encryption simply.", "history": []})
    assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
    assert result.json()["reply"] == "A supported explanation."
    assert collector.snapshot()["requests"][0]["observation_lost"]


def test_real_http_client_context_and_response_capture(monkeypatch):
    import httpx
    from kalillac_routing import bounded_transport, provider_transport
    from test_search_provider_policy import LIMITS
    original = httpx.AsyncClient
    calls = []
    async def handle(request):
        calls.append(request)
        return wire_response()
    def client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handle)
        return original(**kwargs)
    monkeypatch.setattr(bounded_transport.httpx, "AsyncClient", client)
    monkeypatch.setattr(app, "OPENAI_API_KEY", "synthetic-never-export")
    monkeypatch.setattr(app, "_request_limits", LIMITS)
    holder = provider_transport.TransportHolder()
    monkeypatch.setattr(app, "_OPENAI_TRANSPORT", holder)
    collector = usage.EvaluationCollector()
    try:
        with usage.observation_scope(collector, case_number=1), budget_scope(RequestBudget(duration_seconds=5, max_model_attempts=1, max_search_attempts=1)):
            result = app._post_openai_for_attempt({"model": "gpt-5.6-luna", "input": []})
    finally: holder.close()
    assert result["usage"] == USAGE and len(calls) == 1
    record = collector.snapshot()["requests"][0]["attempts"][0]
    assert record["admitted"] and record["dispatched"] and record["input_tokens"] == 100
    assert json.loads(calls[0].content) == {"model": "gpt-5.6-luna", "input": []}



def test_shared_http_transport_preserves_concurrent_contexts(monkeypatch):
    import httpx
    from kalillac_routing import bounded_transport, provider_transport
    from test_search_provider_policy import LIMITS
    original = httpx.AsyncClient
    calls = []
    async def handle(request):
        calls.append(request)
        for _ in range(300):
            if len(calls) == 2: break
            await asyncio.sleep(.005)
        assert len(calls) == 2
        tokens = int(json.loads(request.content)["input"][0]["content"])
        return wire_response(usage={**USAGE, "input_tokens": tokens})
    def client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handle)
        return original(**kwargs)
    monkeypatch.setattr(bounded_transport.httpx, "AsyncClient", client)
    monkeypatch.setattr(app, "OPENAI_API_KEY", "synthetic-never-export")
    monkeypatch.setattr(app, "_request_limits", LIMITS)
    holder = provider_transport.TransportHolder();monkeypatch.setattr(app, "_OPENAI_TRANSPORT", holder)
    collector = usage.EvaluationCollector()
    def work(case, tokens):
        with usage.observation_scope(collector, case_number=case), budget_scope(RequestBudget(duration_seconds=5, max_model_attempts=1, max_search_attempts=1)):
            app._post_openai_for_attempt({"model": "gpt-5.6-luna", "input": [{"content": str(tokens)}]})
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(work, 1, 100), pool.submit(work, 2, 200)]
            for job in jobs: job.result(timeout=7)
    finally: holder.close()
    data = {r["case_number"]: r["attempts"][0] for r in collector.snapshot()["requests"]}
    assert data[1]["input_tokens"] == 100 and data[2]["input_tokens"] == 200
    assert all(a["admitted"] and a["dispatched"] and a["response_returned"] for a in data.values())



def test_usage_survives_cleanup_failure_without_claiming_no_dispatch(monkeypatch):
    import httpx
    from kalillac_routing import bounded_transport, provider_transport
    from test_search_provider_policy import LIMITS
    original = httpx.AsyncClient
    def client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(lambda request: wire_response())
        return original(**kwargs)
    original_close = bounded_transport.BoundedTransport._close_client
    async def unconfirmed(self, client):
        await original_close(self, client)
        return False
    monkeypatch.setattr(bounded_transport.httpx, "AsyncClient", client)
    monkeypatch.setattr(bounded_transport.BoundedTransport, "_close_client", unconfirmed)
    monkeypatch.setattr(app, "OPENAI_API_KEY", "synthetic-never-export")
    monkeypatch.setattr(app, "_request_limits", LIMITS)
    holder = provider_transport.TransportHolder();monkeypatch.setattr(app, "_OPENAI_TRANSPORT", holder)
    collector = usage.EvaluationCollector()
    try:
        with usage.observation_scope(collector, case_number=1), budget_scope(RequestBudget(duration_seconds=5, max_model_attempts=1, max_search_attempts=1)):
            with pytest.raises(app.ProviderTransportUnavailable):
                app._post_openai_for_attempt({"model": "gpt-5.6-luna", "input": []})
    finally: holder.close()
    record = collector.snapshot()["requests"][0]["attempts"][0]
    assert record["phase"] == "failed" and record["dispatched"] is True
    assert record["response_returned"] and record["input_tokens"] == 100


def test_actual_repair_helper_uses_same_observed_provider_seam(model):
    model["responses"] = [response("```python\nprint('repaired')\n```")]
    _, data = capture(lambda: app.repair_python_output("Write a short Python example.", "print('broken'", ["Syntax error"]))
    assert len(model["payloads"]) == 1 and model["payloads"][0]["max_output_tokens"] == app.CODE_RESPONSE_TOKENS
    assert data["requests"][0]["attempts"][0]["input_tokens"] == 100


def test_tavily_returned_credits_and_depth_are_numeric_only(providers):
    providers["tavily"] = [{**tavily_result(), "usage": {"credits": 2}}]
    budget = RequestBudget(duration_seconds=5, max_model_attempts=1, max_search_attempts=1)
    with budget_scope(budget):
        _, data = capture(lambda: app._post_single_search("tavily", {"query": "synthetic", "search_depth": "advanced"}))
    attempts = data["requests"][0]["attempts"]
    assert len(attempts) == 1 and attempts[0]["credits"] == 2 and attempts[0]["depth"] == "advanced"
    assert "docs.example.test" not in json.dumps(data)
