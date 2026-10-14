"""Aggregate ceilings and temporary-session guards; scripted HTTP only."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import threading
from types import SimpleNamespace

import pytest
import app_fastapi_candidate as app
from fastapi.testclient import TestClient
from sqlalchemy import select
from account_test_db import make_sqlite_file_engine, make_session_factory
from kalillac_routing.request_budget import RequestBudget, budget_scope

NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
MESSAGE = "Explain public key encryption simply."
SENTINEL = "private-content-sentinel-do-not-persist"


def create_service(tmp_path, total=3, anonymous=2, period=60, abuse=None):
    from kalillac_routing.provider_limits import ControlConfig, ProviderLimit
    from kalillac_routing.provider_controls import ProviderControls
    from kalillac_db.models import Base
    engine = make_sqlite_file_engine(str(tmp_path))
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    clock = {"now": NOW}
    config = ControlConfig({p: ProviderLimit(total, anonymous, period) for p in ("openai", "brave", "tavily")}, abuse)
    service = ProviderControls(config, session_factory=lambda: factory, clock=lambda: clock["now"])
    return SimpleNamespace(service=service, factory=factory, engine=engine, clock=clock, config=config)


def counts(data, provider="openai"):
    from kalillac_db.models import ProviderUsagePool
    with data.factory() as session:
        row = session.get(ProviderUsagePool, provider)
        return (row.total_attempts, row.anonymous_attempts) if row else (0, 0)


def test_default_off_ignores_incomplete_settings_without_database_work(monkeypatch):
    from kalillac_routing.provider_limits import load_controls
    monkeypatch.delenv("KALILLAC_PROVIDER_CEILINGS_ENABLED", raising=False)
    monkeypatch.delenv("KALILLAC_ANONYMOUS_ABUSE_ENABLED", raising=False)
    monkeypatch.setenv("KALILLAC_OPENAI_TOTAL_ATTEMPTS", "not-a-number-private-sentinel")
    assert load_controls() is None


def test_atomic_last_slot_survives_new_service_and_keeps_anonymous_subpool(tmp_path):
    from kalillac_routing.provider_controls import CeilingBusy, ProviderControls
    data = create_service(tmp_path, total=2, anonymous=1)
    barrier = threading.Barrier(6)
    def reserve(_):
        service = ProviderControls(data.config, session_factory=lambda: data.factory, clock=lambda: NOW)
        barrier.wait(3)
        try: return service.reserve("openai", True)
        except CeilingBusy: return None
    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            leases = list(pool.map(reserve, range(6)))
        assert sum(x is not None for x in leases) == 1
        assert counts(data) == (1, 1)
        restarted = ProviderControls(data.config, session_factory=lambda: data.factory, clock=lambda: NOW)
        with pytest.raises(CeilingBusy): restarted.reserve("openai", True)
        restarted.reserve("openai", False)
        assert counts(data) == (2, 1)
        with pytest.raises(CeilingBusy): restarted.reserve("openai", False)
    finally: data.engine.dispose()


def test_period_recovery_does_not_refund_crash_or_old_release_into_new_period(tmp_path):
    data = create_service(tmp_path, total=1, anonymous=1)
    from kalillac_routing.provider_controls import CeilingBusy
    try:
        old = data.service.reserve("openai", True)
        with pytest.raises(CeilingBusy): data.service.reserve("openai", True)
        # A crash with unknown dispatch keeps the charge until the UTC period.
        data.clock["now"] += timedelta(seconds=60)
        new = data.service.reserve("openai", True)
        data.service.release(old)
        data.service.release(old)
        assert counts(data) == (1, 1)
        data.service.release(new)
        data.service.release(new)
        assert counts(data) == (0, 0)
    finally: data.engine.dispose()


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    from dataclasses import replace
    from kalillac_routing import provider_controls as controls, tavily_transport
    from kalillac_routing.provider_transport import TransportHolder
    from test_search_provider_policy import LIMITS
    from test_provider_usage_observation import wire_response
    from kalillac_routing.search_providers import SearchProviderPolicy
    import httpx
    data = create_service(tmp_path)
    data.calls, data.responses = [], []
    monkeypatch.setattr(controls, "SERVICE", data.service)
    monkeypatch.setattr(app, "_request_limits", replace(LIMITS, deadline_seconds=5))
    monkeypatch.setattr(app, "_usage_quota", None)
    monkeypatch.setattr(app, "_usage_meter", None)
    monkeypatch.setattr(app, "SESSION_STATE", {})
    monkeypatch.setattr(app, "OPENAI_API_KEY", "synthetic-openai-key")
    monkeypatch.setattr(app, "BRAVE_API_KEY", "synthetic-brave-key")
    monkeypatch.setattr(app, "TAVILY_API_KEY", "synthetic-tavily-key")
    monkeypatch.setattr(app, "SEARCH_PROVIDER_POLICY", SearchProviderPolicy("brave", "tavily"))
    holder = TransportHolder()
    slot = tavily_transport.TavilyTransportSlot()
    monkeypatch.setattr(app, "_OPENAI_TRANSPORT", holder)
    monkeypatch.setattr(tavily_transport, "_SLOT", slot)
    async def handle(_self, request):
        provider = "openai" if request.url.host == "api.openai.com" else "brave" if request.url.host == "api.search.brave.com" else "tavily"
        data.calls.append(provider)
        assert data.responses, "unexpected provider dispatch"
        value = data.responses.pop(0)
        if isinstance(value, Exception): raise value
        if callable(value): return await value(request)
        return wire_response(**value)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)
    yield data
    report = holder.close()
    assert report is None or report.clean
    report = slot.close()
    assert report is None or report.clean
    data.engine.dispose()


@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
def test_actual_chat_pipeline_anonymous_ceiling_survives_session_replacement(pipeline, monkeypatch, native):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    pipeline.responses = [{}, {}]
    with TestClient(app.api) as client:
        first = client.post("/api/chat", json={"message": MESSAGE + SENTINEL})
        second = client.post("/api/chat", json={"message": MESSAGE})
        rejected = client.post("/api/chat", json={"message": MESSAGE})
    assert first.status_code == second.status_code == 200
    assert rejected.status_code == 429 and rejected.json() == {"error": "busy"}
    assert rejected.headers["cache-control"] == "no-store"
    assert pipeline.calls == ["openai", "openai"]
    assert counts(pipeline) == (2, 2)
    from kalillac_db.models import ProviderUsagePool
    with pipeline.factory() as session:
        row = session.get(ProviderUsagePool, "openai")
        assert set(row.__table__.columns.keys()) == {"provider", "period_start", "period_seconds", "total_limit", "anonymous_limit", "total_attempts", "anonymous_attempts"}
        assert SENTINEL not in str(row.__dict__)


def test_local_rejection_before_dispatch_does_not_consume_aggregate(pipeline, monkeypatch):
    from kalillac_routing.provider_transport import TransportUnavailable
    monkeypatch.setattr(app, "_post_openai_bounded", lambda *args: (_ for _ in ()).throw(TransportUnavailable("closed")))
    with TestClient(app.api) as client:
        result = client.post("/api/chat", json={"message": MESSAGE})
    assert result.status_code != 200
    assert counts(pipeline) == (0, 0) and pipeline.calls == []


def test_provider_failure_keeps_attempt_and_database_failure_denies_dispatch(pipeline):
    from kalillac_routing.provider_controls import CeilingUnavailable
    pipeline.responses = [RuntimeError("synthetic failure")]
    with TestClient(app.api) as client:
        result = client.post("/api/chat", json={"message": MESSAGE})
        assert result.status_code == 503
        assert counts(pipeline) == (1, 1)
        pipeline.service._factory = lambda: (_ for _ in ()).throw(RuntimeError("database outage"))
        result = client.post("/api/chat", json={"message": MESSAGE})
    assert result.status_code == 503 and result.json() == {"error": "service_unavailable"}
    assert result.headers["cache-control"] == "no-store"
    assert pipeline.calls == ["openai"]


@pytest.mark.parametrize("provider", ["openai", "brave", "tavily"])
def test_each_provider_has_independent_total_and_anonymous_pools(tmp_path, provider):
    from kalillac_routing.provider_controls import CeilingBusy
    data = create_service(tmp_path, total=2, anonymous=1)
    try:
        data.service.reserve(provider, True)
        with pytest.raises(CeilingBusy): data.service.reserve(provider, True)
        data.service.reserve(provider, False)
        with pytest.raises(CeilingBusy): data.service.reserve(provider, False)
        assert counts(data, provider) == (2, 1)
        assert all(counts(data, other) == (0, 0) for other in ["openai", "brave", "tavily"] if other != provider)
    finally: data.engine.dispose()


def explicit_environment(monkeypatch):
    monkeypatch.setenv("KALILLAC_PROVIDER_CEILINGS_ENABLED", "true")
    monkeypatch.setenv("KALILLAC_DB_ENABLED", "true")
    monkeypatch.setenv("KALILLAC_REQUEST_BUDGET_ENABLED", "true")
    monkeypatch.delenv("KALILLAC_ACCOUNTS_ENABLED", raising=False)
    monkeypatch.delenv("KALILLAC_ANONYMOUS_ABUSE_ENABLED", raising=False)
    for provider in ("OPENAI", "BRAVE", "TAVILY"):
        for suffix, value in [("TOTAL_ATTEMPTS", "3"), ("ANONYMOUS_ATTEMPTS", "1"), ("PERIOD_SECONDS", "60")]:
            monkeypatch.setenv("KALILLAC_" + provider + "_" + suffix, value)


@pytest.mark.parametrize("provider", ["OPENAI", "BRAVE", "TAVILY"])
@pytest.mark.parametrize("suffix", ["TOTAL_ATTEMPTS", "ANONYMOUS_ATTEMPTS", "PERIOD_SECONDS"])
def test_all_provider_limits_and_periods_are_required(monkeypatch, provider, suffix):
    from kalillac_routing.provider_limits import load_controls, ControlConfigError
    explicit_environment(monkeypatch)
    name = "KALILLAC_" + provider + "_" + suffix
    monkeypatch.delenv(name)
    with pytest.raises(ControlConfigError, match=name): load_controls()


@pytest.mark.parametrize("bad", ["-1", "0-private", "1.5", "2147483648", "??"])
def test_invalid_config_never_echoes_value(monkeypatch, bad):
    from kalillac_routing.provider_limits import load_controls, ControlConfigError
    explicit_environment(monkeypatch)
    monkeypatch.setenv("KALILLAC_OPENAI_TOTAL_ATTEMPTS", bad)
    with pytest.raises(ControlConfigError) as error: load_controls()
    assert bad not in str(error.value)


def test_abuse_requires_aggregate_ceiling_and_explicit_bounds(monkeypatch):
    from kalillac_routing.provider_limits import load_controls, ControlConfigError
    explicit_environment(monkeypatch)
    monkeypatch.setenv("KALILLAC_ANONYMOUS_ABUSE_ENABLED", "true")
    with pytest.raises(ControlConfigError, match="SESSION_BURST"): load_controls()
    for suffix, value in [("BURST", "2"), ("WINDOW_SECONDS", "60"), ("CONCURRENT", "1")]:
        monkeypatch.setenv("KALILLAC_ANONYMOUS_SESSION_" + suffix, value)
    assert load_controls().abuse.concurrent == 1
    monkeypatch.setenv("KALILLAC_PROVIDER_CEILINGS_ENABLED", "false")
    with pytest.raises(ControlConfigError, match="PROVIDER_CEILINGS_ENABLED"): load_controls()


def test_zero_limit_denies_without_dispatch_and_disagreeing_workers_fail_closed(tmp_path):
    from kalillac_routing.provider_controls import CeilingBusy, CeilingUnavailable, ProviderControls
    from kalillac_routing.provider_limits import ControlConfig, ProviderLimit
    data = create_service(tmp_path, total=0, anonymous=0)
    try:
        with pytest.raises(CeilingBusy): data.service.reserve("brave", True)
        config = ControlConfig({p: ProviderLimit(1, 1, 60) for p in ("openai", "brave", "tavily")})
        # A denied transaction rolls back row creation. First establish a
        # durable zero pool explicitly, then verify mismatched workers reject.
        from kalillac_db.repositories.provider_usage import locked_pool
        with data.factory() as session, session.begin():
            locked_pool(session, "brave", data.config.providers["brave"], NOW)
        other = ProviderControls(config, session_factory=lambda: data.factory, clock=lambda: NOW)
        with pytest.raises(CeilingUnavailable): other.reserve("brave", False)
        assert counts(data, "brave") == (0, 0)
    finally: data.engine.dispose()


@pytest.mark.parametrize("native", [False, True])
def test_automatic_continuation_uses_two_slots_and_never_regenerates_after_ceiling(pipeline, monkeypatch, native):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    pipeline.responses = [{"text": "Public keys ", "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}, {"text": "are shared."}]
    with TestClient(app.api) as client:
        first = client.post("/api/chat", json={"message": MESSAGE})
        denied = client.post("/api/chat", json={"message": MESSAGE})
    assert first.status_code == 200 and "are shared." in first.json()["reply"]
    assert denied.status_code == 429
    assert pipeline.calls == ["openai", "openai"] and counts(pipeline) == (2, 2)


@pytest.mark.parametrize("native", [False, True])
def test_manual_code_remainder_keeps_task_under_controls(pipeline, monkeypatch, native):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    task = "Write a short Python example combining two bit masks with XOR, then print the result."
    history = [{"role": "user", "content": task}, {"role": "assistant", "content": app.mark_incomplete_reply("```\nmask = 1 ^ 2")}]
    pipeline.responses = [{"text": "print(mask)"}]
    with TestClient(app.api) as client:
        result = client.post("/api/chat", json={"message": "continue", "history": history})
    assert result.status_code == 200 and "print(mask)" in result.json()["reply"]
    assert pipeline.calls == ["openai"] and counts(pipeline) == (1, 1)


def test_cancelled_inflight_transport_retains_charge_and_session_capacity_until_worker_exit(pipeline, monkeypatch):
    import httpx
    from kalillac_routing.provider_limits import AbuseLimit, ControlConfig
    pipeline.service.config = ControlConfig(pipeline.config.providers, AbuseLimit(3, 60, 1))
    sid = "existing-temporary-session"
    app.get_session_state_by_id(sid)
    entered, finished = threading.Event(), threading.Event()
    async def stalled(request):
        entered.set()
        try:
            await asyncio.sleep(10)
        finally: finished.set()
    pipeline.responses = [stalled]
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.api), base_url="https://testserver") as client:
            task = asyncio.create_task(client.post("/api/chat", json={"message": MESSAGE, "session_id": sid}))
            assert await asyncio.to_thread(entered.wait, 3)
            blocked = await client.post("/api/chat", json={"message": MESSAGE, "session_id": sid})
            assert blocked.status_code == 429 and blocked.headers["cache-control"] == "no-store"
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
            assert await asyncio.to_thread(finished.wait, 3)
            for _ in range(200):
                if app.get_session_state_by_id(sid)["anonymous_active"] == 0: break
                await asyncio.sleep(0.01)
            else: pytest.fail("late worker did not release session capacity")
    asyncio.run(scenario())
    assert pipeline.calls == ["openai"] and counts(pipeline) == (1, 1)


def test_cancelled_database_admission_rolls_back_before_any_provider(pipeline, monkeypatch):
    import httpx
    entered, finish = threading.Event(), threading.Event()
    real_factory = pipeline.service._factory
    def delayed_factory():
        entered.set()
        assert finish.wait(3)
        return real_factory()
    pipeline.service._factory = delayed_factory
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.api), base_url="https://testserver") as client:
            task = asyncio.create_task(client.post("/api/chat", json={"message": MESSAGE}))
            assert await asyncio.to_thread(entered.wait, 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
            finish.set()
            for _ in range(200):
                if not app._session_locks: break
                await asyncio.sleep(0.01)
            else: pytest.fail("database worker did not finish")
    asyncio.run(scenario())
    assert counts(pipeline) == (0, 0) and pipeline.calls == []


def test_temporary_burst_limit_and_window_do_not_become_person_identity(monkeypatch, tmp_path):
    from kalillac_routing import provider_controls as controls
    from kalillac_routing.provider_limits import AbuseLimit, ControlConfig
    data = create_service(tmp_path)
    data.service.config = ControlConfig(data.config.providers, AbuseLimit(2, 10, 1))
    clock = {"tick": 100.0}
    monkeypatch.setattr(controls, "time", SimpleNamespace(monotonic=lambda: clock["tick"]))
    state, lock = {}, threading.RLock()
    try:
        for _ in range(2):
            req = controls.ControlRequest(data.service)
            req.admit_session(state, lock)
            req.close()
        with pytest.raises(controls.CeilingBusy): controls.ControlRequest(data.service).admit_session(state, lock)
        assert set(state) == {"anonymous_burst", "anonymous_active"}
        assert len(state["anonymous_burst"]) == 2
        clock["tick"] += 10
        req = controls.ControlRequest(data.service)
        req.admit_session(state, lock);req.close()
        assert len(state["anonymous_burst"]) == 1
        # A new temporary session is distinct, never a inferred person.
        req = controls.ControlRequest(data.service)
        req.admit_session({}, lock);req.close()
    finally: data.engine.dispose()


def test_unbudgeted_sdk_and_direct_paths_fail_closed_without_provider_work(pipeline, monkeypatch):
    from kalillac_routing.provider_controls import CeilingUnavailable
    with pytest.raises(CeilingUnavailable): app.run_web_search("public documentation")
    with pytest.raises(CeilingUnavailable): app._post_openai_for_attempt({})
    assert pipeline.calls == [] and counts(pipeline) == (0, 0)


def test_pool_survives_engine_restart_and_utc_boundary(tmp_path):
    from kalillac_routing.provider_controls import ProviderControls, CeilingBusy
    data = create_service(tmp_path, total=1, anonymous=1, period=86400)
    try:
        data.clock["now"] = NOW.replace(hour=23, minute=59, second=59)
        data.service.reserve("tavily", True)
        data.engine.dispose()
        engine = make_sqlite_file_engine(str(tmp_path))
        factory = make_session_factory(engine)
        restored = ProviderControls(data.config, session_factory=lambda: factory, clock=lambda: data.clock["now"])
        with pytest.raises(CeilingBusy): restored.reserve("tavily", True)
        data.clock["now"] += timedelta(seconds=1)
        restored.reserve("tavily", True)
        assert counts(data, "tavily") == (1, 1)
        engine.dispose()
    finally: data.engine.dispose()



def larger_pools(pipeline):
    from kalillac_routing.provider_limits import ControlConfig, ProviderLimit
    pipeline.service.config = ControlConfig({p: ProviderLimit(6, 6, 60) for p in ("openai", "brave", "tavily")})


@pytest.mark.parametrize("native", [False, True])
def test_real_search_fallback_keeps_domains_and_counts_both_providers(pipeline, monkeypatch, native):
    from test_search_provider_policy import brave_result, tavily_result, _lookup_tool
    from test_provider_usage_observation import wire_response
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    larger_pools(pipeline)
    seen = []
    async def failed_brave(request):
        seen.append(json.loads(request.content))
        result = wire_response()
        result.status_code = 500
        return result
    async def found_tavily(request):
        body = json.loads(request.content)
        assert body["include_domains"] == ["docs.example.test"]
        seen.append(body)
        return wire_response(**tavily_result())
    pipeline.responses = ([_lookup_tool("API behavior site:docs.example.test")] if native else []) + [failed_brave, found_tavily, {"text": "Supported API behavior."}]
    with TestClient(app.api) as client:
        result = client.post("/api/chat", json={"message": "Search https://docs.example.test/reference for API behavior. Restrict sources to docs.example.test."})
    assert result.status_code == 200 and "docs.example.test" in result.json()["reply"]
    assert pipeline.calls == (["openai"] if native else []) + ["brave", "tavily", "openai"]
    assert counts(pipeline, "brave") == counts(pipeline, "tavily") == (1, 1)
    assert counts(pipeline) == ((2, 2) if native else (1, 1))
    assert "$site=docs.example.test" in seen[0]["goggles"]


def test_real_native_followup_uses_one_tavily_and_three_model_slots(pipeline, monkeypatch):
    from test_search_provider_policy import brave_result, tavily_result, _lookup_tool
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", True)
    larger_pools(pipeline)
    pipeline.responses = [_lookup_tool(), brave_result(content="A stale observation with an earlier date."), _lookup_tool("reliable observation time", call_id="followup"), tavily_result(), {"text": "Supported evidence is available."}]
    with TestClient(app.api) as client:
        result = client.post("/api/chat", json={"message": "Search the web for weather today at the reported location."})
    assert result.status_code == 200
    assert pipeline.calls == ["openai", "brave", "openai", "tavily", "openai"]
    assert counts(pipeline) == (3, 3)
    assert counts(pipeline, "brave") == counts(pipeline, "tavily") == (1, 1)


@pytest.mark.parametrize("native", [False, True])
def test_service_search_ceiling_is_not_a_fallback_signal(pipeline, monkeypatch, native):
    from kalillac_routing.provider_limits import ControlConfig, ProviderLimit
    from test_search_provider_policy import _lookup_tool
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    limits = dict(pipeline.config.providers)
    limits["brave"] = ProviderLimit(0, 0, 60)
    pipeline.service.config = ControlConfig(limits)
    pipeline.responses = [_lookup_tool()] if native else []
    with TestClient(app.api) as client:
        result = client.post("/api/chat", json={"message": "Search https://docs.example.test/reference for API behavior."})
    assert result.status_code == 429 and result.json() == {"error": "busy"}
    assert pipeline.calls == (["openai"] if native else [])
    assert counts(pipeline, "brave") == counts(pipeline, "tavily") == (0, 0)


def test_actual_repair_helpers_share_ceiling_and_keep_existing_output_contract(pipeline):
    from kalillac_routing import provider_controls as controls
    pipeline.responses = [{"text": "```python\nprint(1)\n```"}, {"text": "<!doctype html><html><body>ok</body></html>"}]
    state, token = controls.begin_request()
    try:
        with budget_scope(RequestBudget(duration_seconds=5, max_model_attempts=6, max_search_attempts=6)):
            assert "print(1)" in app.repair_python_output("write code", "print(1", ["syntax error"])
            assert "<body>ok</body>" in app.repair_html_output("make a page", "<html>", ["missing body"])
            with pytest.raises(controls.CeilingBusy): app.repair_python_output("write code", "print(1", ["syntax error"])
    finally: controls.end_request(state, token)
    assert pipeline.calls == ["openai", "openai"] and counts(pipeline) == (2, 2)


def test_default_off_keeps_provider_calls_and_temporary_session_shape(pipeline, monkeypatch):
    from kalillac_routing import provider_controls as controls
    monkeypatch.setattr(controls, "SERVICE", None)
    pipeline.service._factory = lambda: (_ for _ in ()).throw(AssertionError("disabled control DB work"))
    pipeline.responses = [{}, {}, {}]
    with TestClient(app.api) as client:
        responses = [client.post("/api/chat", json={"message": MESSAGE}) for _ in range(3)]
    assert [r.status_code for r in responses] == [200, 200, 200]
    assert pipeline.calls == ["openai"] * 3
    assert all(set(state) == {"memory", "search_times"} for state in app.SESSION_STATE.values())
    assert counts(pipeline) == (0, 0)


def test_closed_or_expired_dispatch_lease_cannot_send_after_local_refund(pipeline):
    from kalillac_routing import provider_controls as controls
    state, token = controls.begin_request()
    budget = RequestBudget(duration_seconds=5, max_model_attempts=6, max_search_attempts=6)
    try:
        with budget_scope(budget):
            lease = pipeline.service.reserve("openai", True)
            attempt = controls._LEASE.set(lease)
            try:
                pipeline.service.release(lease)
                with pytest.raises(controls.CeilingBusy): controls.dispatch()
                other = pipeline.service.reserve("openai", True)
                controls._LEASE.set(other)
                other.dispatch_deadline = 0
                with pytest.raises(controls.CeilingBusy): controls.dispatch()
                pipeline.service.release(other)
            finally: controls._LEASE.reset(attempt)
    finally: controls.end_request(state, token)
    assert counts(pipeline) == (0, 0) and pipeline.calls == []


def test_private_search_target_never_reaches_a_search_provider(pipeline):
    # OpenAI is the configured answer model and may receive the request; the
    # private material must never reach Brave or Tavily.
    explanation = ("I didn't send your private notes to an external search provider. "
                   "Paste the relevant text here and I can work with it.")
    pipeline.responses = [{"text": explanation}]
    with TestClient(app.api) as client:
        response = client.post("/api/chat", json={"message": "Search my private notes for " + SENTINEL})
    assert response.status_code == 200
    assert response.json()["reply"] == explanation
    assert pipeline.calls == ["openai"]
    assert counts(pipeline) == (1, 1)
    assert counts(pipeline, "brave") == (0, 0) and counts(pipeline, "tavily") == (0, 0)


def test_postgresql_pool_lock_and_conflict_insert_are_rendered_not_executed():
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.dialects.postgresql import insert
    from kalillac_db.models import ProviderUsagePool
    sql = str(select(ProviderUsagePool).where(ProviderUsagePool.provider == "openai").with_for_update().compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in sql
    sql = str(insert(ProviderUsagePool).values(provider="openai").on_conflict_do_nothing(index_elements=["provider"]).compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT (provider) DO NOTHING" in sql



def test_concurrent_actual_requests_admit_only_last_slot_without_transaction_during_http(pipeline):
    import httpx
    from kalillac_routing.provider_limits import ControlConfig, ProviderLimit
    from test_provider_usage_observation import wire_response
    from kalillac_db.models import ProviderUsagePool
    pipeline.service.config = ControlConfig({p: ProviderLimit(1, 1, 60) for p in ("openai", "brave", "tavily")})
    entered, finish = threading.Event(), threading.Event()
    async def held(request):
        # Another connection can obtain a write transaction during HTTP work.
        with pipeline.factory() as session, session.begin():
            assert session.get(ProviderUsagePool, "openai").total_attempts == 1
        entered.set()
        while not finish.is_set(): await asyncio.sleep(0.01)
        return wire_response()
    pipeline.responses = [held]
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.api), base_url="https://testserver") as client:
            task = asyncio.create_task(client.post("/api/chat", json={"message": MESSAGE}))
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                denied = await client.post("/api/chat", json={"message": MESSAGE})
                assert denied.status_code == 429 and denied.json() == {"error": "busy"}
            finally: finish.set()
            assert (await task).status_code == 200
    asyncio.run(scenario())
    assert pipeline.calls == ["openai"] and counts(pipeline) == (1, 1)


def test_authoritative_authenticated_identity_uses_total_pool_and_signout_cannot_reset_it(pipeline, monkeypatch):
    import uuid
    from kalillac_accounts.router import AccountSettings
    from kalillac_accounts.tokens import hash_session_token
    from kalillac_accounts.usage import UsageMeter
    from kalillac_accounts.quota import UsageQuota
    from kalillac_accounts.usage_limits import UsageLimits
    from kalillac_db.models import User, AccountSession, AccountUsageDaily
    from kalillac_db.repositories.entitlements import create_default_entitlement
    token, user_id = "synthetic-account-token", uuid.uuid4()
    settings = AccountSettings(cookie_secure=True)
    with pipeline.factory() as session, session.begin():
        session.add(User(id=user_id, email="synthetic@example.test", password_hash="synthetic"))
        session.flush()
        create_default_entitlement(session, user_id)
        session.add(AccountSession(user_id=user_id, token_hash=hash_session_token(token), expires_at=NOW+timedelta(days=1)))
    monkeypatch.setattr(app, "_usage_meter", UsageMeter(session_factory=lambda: pipeline.factory, settings=settings, clock=lambda: NOW))
    monkeypatch.setattr(app, "_usage_quota", UsageQuota(UsageLimits(3, 4), session_factory=lambda: pipeline.factory, settings=settings, clock=lambda: NOW))
    pipeline.responses = [{}, {}, {}]
    with TestClient(app.api) as client:
        signed = client.post("/api/chat", json={"message": MESSAGE}, headers={"Cookie": settings.cookie_name+"="+token})
        anon = client.post("/api/chat", json={"message": MESSAGE})
        another = client.post("/api/chat", json={"message": MESSAGE}, headers={"Cookie": settings.cookie_name+"="+token})
        cleared = client.post("/api/chat", json={"message": MESSAGE})
    assert signed.status_code == anon.status_code == another.status_code == 200
    assert cleared.status_code == 429
    assert counts(pipeline) == (3, 1) and len(pipeline.calls) == 3
    with pipeline.factory() as session:
        row = session.scalar(select(AccountUsageDaily))
        assert row.successful_chats == 2 and row.request_chars == 2*len(MESSAGE)
    assert all("account" not in column and "user" not in column for column in __import__("kalillac_db.models",fromlist=["ProviderUsagePool"]).ProviderUsagePool.__table__.columns.keys())


@pytest.mark.parametrize("prerequisite", ["KALILLAC_DB_ENABLED", "KALILLAC_REQUEST_BUDGET_ENABLED"])
def test_enabling_controls_requires_supported_infrastructure(monkeypatch, prerequisite):
    from kalillac_routing.provider_limits import load_controls, ControlConfigError
    explicit_environment(monkeypatch)
    monkeypatch.setenv(prerequisite, "false")
    with pytest.raises(ControlConfigError, match=prerequisite): load_controls()


def test_enabled_accounts_require_authoritative_metering_identity(monkeypatch):
    from kalillac_routing.provider_limits import load_controls, ControlConfigError
    explicit_environment(monkeypatch)
    monkeypatch.setenv("KALILLAC_ACCOUNTS_ENABLED", "true")
    monkeypatch.setenv("KALILLAC_USAGE_METERING_ENABLED", "false")
    with pytest.raises(ControlConfigError, match="METERING_ENABLED"): load_controls()


def test_fresh_process_default_off_does_not_initialize_database_or_controls():
    import os, subprocess, sys
    from pathlib import Path
    env = {k:v for k,v in os.environ.items() if not k.startswith(("KALILLAC_", "OPENAI_", "TAVILY_", "BRAVE_"))}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    script = """
import socket,json
attempts=[]
def forbidden(*args,**kwargs): attempts.append(1);raise AssertionError('external blocked')
socket.socket.connect=socket.socket.connect_ex=socket.getaddrinfo=forbidden
import app_fastapi_candidate as app
from kalillac_db import engine
print(json.dumps({'controls':app.provider_controls.SERVICE is not None,'db':engine._ENGINE is not None,'outbound':len(attempts)}))
"""
    result = subprocess.run([sys.executable,"-B","-c",script],cwd=str(Path(app.__file__).parent),env=env,capture_output=True,text=True,timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"controls":False,"db":False,"outbound":0}
