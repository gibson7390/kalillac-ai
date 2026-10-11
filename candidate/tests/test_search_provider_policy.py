"""Brave-first search policy, shared budgets, and default Tavily compatibility.

Providers are scripted; conftest and the external runner prohibit all external
sockets. Pipeline tests exercise real search routing and response formatting.
"""
import copy
from contextlib import nullcontext
import json
from types import SimpleNamespace

import pytest
import app_fastapi_candidate as app
from kalillac_routing import search_providers as sp, tavily_transport
from kalillac_routing.bounded_transport import (
    TransportConnectionError, TransportDeadlineExceeded, TransportHTTPError,
    TransportCleanupUnconfirmed,
)
from kalillac_routing.request_budget import (
    RequestBudget, RequestCancelled, RequestDeadlineExceeded, CallBudgetExhausted, budget_scope,
)
from kalillac_routing.request_limits import RequestLimits, TransportLimits

BRAVE_KEY = "brave-secret-must-not-leak"
TAVILY_KEY = "tavily-secret-must-not-leak"
SOURCE = "https://docs.example.test/reference"
TRANSPORT = TransportLimits(3, 1, 3, 0.02, 0.5, 1.0, 2.0)
LIMITS = RequestLimits(45.0, 2.0, 6, 6, TRANSPORT, 2097152, TRANSPORT, 262144, 524288)


def brave_result(url=SOURCE, content="Tavily API documentation evidence.", age=None):
    return {"grounding": {"generic": [{"url": url, "title": "Reference", "snippets": [content]}]},
            "sources": {url: {"title": "Reference", **({"age": age} if age else {})}}}


def tavily_result(url=SOURCE, content="Tavily API documentation evidence."):
    return {"results": [{"url": url, "title": "Reference", "content": content, "score": 0.8}]}


class UnscriptedRequest(BaseException):
    pass


@pytest.fixture
def providers(monkeypatch):
    state = {"brave": [], "tavily": [], "calls": [], "model": []}

    def play(url, payload, headers, timeout, max_bytes):
        provider = "brave" if url == sp.BRAVE_CONTEXT_URL else "tavily"
        state["calls"].append({"provider": provider, "url": url, "payload": copy.deepcopy(payload),
                               "headers": dict(headers), "timeout": timeout, "max_bytes": max_bytes})
        if not state[provider]:
            raise UnscriptedRequest("Unexpected search attempt")
        response = state[provider].pop(0)
        response = response() if callable(response) else response
        if isinstance(response, BaseException):
            raise response
        return copy.deepcopy(response)

    class ScriptTransport:
        def __init__(self, **kwargs):
            pass

        def post_json(self, url, payload, *, headers, timeout, max_bytes, cancelled=None):
            return play(url, payload, headers, timeout, max_bytes)

        def close(self, timeout):
            return SimpleNamespace(clean=True)

    class ScriptSDK:
        def __init__(self, **kwargs):
            pass

        def search(self, **kwargs):
            return play(tavily_transport.SEARCH_URL, kwargs, {}, kwargs["timeout"], 262144)

        def extract(self, **kwargs):
            raise UnscriptedRequest("Extraction forbidden in bounded fallback")

    import tavily
    monkeypatch.setattr(tavily, "TavilyClient", ScriptSDK)
    monkeypatch.setattr(tavily_transport, "_SLOT", tavily_transport.TavilyTransportSlot(factory=ScriptTransport))
    monkeypatch.setattr(sp, "post_json_unbudgeted", lambda url, payload, headers, timeout, max_bytes=262144:
                        play(url, payload, headers, timeout, max_bytes))
    monkeypatch.setattr(app, "_request_limits", LIMITS)
    monkeypatch.setattr(app, "BRAVE_API_KEY", BRAVE_KEY)
    monkeypatch.setattr(app, "TAVILY_API_KEY", TAVILY_KEY)
    monkeypatch.setattr(app, "SEARCH_PROVIDER_POLICY", sp.SearchProviderPolicy("brave", "tavily"))
    monkeypatch.setattr(app, "SESSION_STATE", type(app.SESSION_STATE)())
    monkeypatch.setattr(app.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(UnscriptedRequest()))
    return state


@pytest.mark.parametrize("primary,fallback", [("tavily", "none"), ("brave", "none"), ("brave", "tavily")])
def test_explicit_configuration_and_default(primary, fallback):
    policy = sp.load_policy({"KALILLAC_SEARCH_PRIMARY": primary, "KALILLAC_SEARCH_FALLBACK": fallback})
    assert (policy.primary, policy.fallback) == (primary, fallback)
    assert sp.load_policy({}) == sp.SearchProviderPolicy("tavily", "none")


@pytest.mark.parametrize("primary,fallback", [("", "none"), ("other-secret", "none"), ("tavily", "brave"),
                                              ("tavily", "tavily"), ("brave", ""), ("brave", "brave")])
def test_invalid_policy_is_fixed_clear_error(primary, fallback):
    with pytest.raises(sp.SearchProviderConfigError) as caught:
        sp.load_policy({"KALILLAC_SEARCH_PRIMARY": primary, "KALILLAC_SEARCH_FALLBACK": fallback})
    assert "KALILLAC_SEARCH_PRIMARY" in str(caught.value)
    assert "other-secret" not in str(caught.value)


@pytest.mark.parametrize("budgeted", [False, True])
@pytest.mark.parametrize("fallback", ["none", "tavily"])
def test_brave_success_has_zero_tavily_attempts(providers, monkeypatch, budgeted, fallback):
    monkeypatch.setattr(app, "SEARCH_PROVIDER_POLICY", sp.SearchProviderPolicy("brave", fallback))
    monkeypatch.setattr(app, "TAVILY_API_KEY", None)
    providers["brave"] = [brave_result()]
    budget = RequestBudget(duration_seconds=45, max_model_attempts=6, max_search_attempts=6)
    with budget_scope(budget) if budgeted else nullcontext():
        status, results = app.run_web_search("reference documentation", ["docs.example.test"])
    assert status == "ok" and len(results) == 1
    assert set(results[0]) == {"title", "url", "content", "published", "score"}
    assert results[0]["url"] == SOURCE and results[0]["published"] == ""
    assert [c["provider"] for c in providers["calls"]] == ["brave"]
    assert budget.search_attempts == (1 if budgeted else 0)
    assert budget.model_attempts == 0
    assert providers["calls"][0]["payload"]["goggles"] == "$discard\n$site=docs.example.test"


FAILURES = [TransportHTTPError(401), TransportHTTPError(403), TransportHTTPError(429), TransportHTTPError(500),
            TransportConnectionError(), TransportDeadlineExceeded(), [], {"grounding": {"generic": "bad"}},
            {"grounding": {"generic": []}}, brave_result(content="")]


@pytest.mark.parametrize("budgeted", [False, True])
@pytest.mark.parametrize("failure", FAILURES)
def test_eligible_failure_has_only_one_tavily_attempt(providers, budgeted, failure):
    providers["brave"] = [failure]
    providers["tavily"] = [tavily_result()]
    budget = RequestBudget(duration_seconds=45, max_model_attempts=6, max_search_attempts=6)
    with budget_scope(budget) if budgeted else nullcontext():
        status, results = app.run_web_search("AI news today", ["example.test"])
    assert status == "ok" and results[0]["url"] == SOURCE
    assert [c["provider"] for c in providers["calls"]] == ["brave", "tavily"]
    assert budget.search_attempts == (2 if budgeted else 0)
    assert budget.model_attempts == 0
    assert all(c["timeout"] <= app.SEARCH_TIMEOUT_SECONDS for c in providers["calls"])
    assert all(c["max_bytes"] == LIMITS.tavily_search_max_bytes for c in providers["calls"])
    assert providers["calls"][-1]["url"] == tavily_transport.SEARCH_URL


@pytest.mark.parametrize("budgeted", [False, True])
def test_missing_brave_credential_counts_only_fallback(providers, monkeypatch, budgeted):
    monkeypatch.setattr(app, "BRAVE_API_KEY", None)
    providers["tavily"] = [tavily_result()]
    budget = RequestBudget(duration_seconds=45, max_model_attempts=6, max_search_attempts=6)
    with budget_scope(budget) if budgeted else nullcontext():
        status, results = app.run_web_search("reference", ["example.test"])
    assert status == "ok"
    assert [c["provider"] for c in providers["calls"]] == ["tavily"]
    assert budget.search_attempts == (1 if budgeted else 0)


@pytest.mark.parametrize("stop", ["cancel", "deadline", "exhaustion"])
@pytest.mark.parametrize("missing", [False, True])
def test_request_stop_prevents_fallback(providers, monkeypatch, stop, missing):
    clock = [100.0]
    budget = RequestBudget(duration_seconds=30, max_model_attempts=6, max_search_attempts=1, clock=lambda: clock[0])
    if missing:
        monkeypatch.setattr(app, "BRAVE_API_KEY", None)
        if stop == "cancel":
            budget.cancel()
        elif stop == "deadline":
            clock[0] = 131
        else:
            budget.admit_search_attempt()
    else:
        def fail_after_stop():
            if stop == "cancel":
                budget.cancel()
            elif stop == "deadline":
                clock[0] = 131
            return {"grounding": {"generic": []}}
        providers["brave"] = [fail_after_stop]
    error = {"cancel": RequestCancelled, "deadline": RequestDeadlineExceeded, "exhaustion": CallBudgetExhausted}[stop]
    with budget_scope(budget), pytest.raises(error):
        app.run_web_search("reference documentation")
    assert not any(c["provider"] == "tavily" for c in providers["calls"])
    assert len(providers["calls"]) == (0 if missing else 1)


def test_unconfirmed_cleanup_does_not_run_parallel_fallback(providers):
    providers["brave"] = [TransportCleanupUnconfirmed("deadline")]
    budget = RequestBudget(duration_seconds=45, max_model_attempts=6, max_search_attempts=6)
    with budget_scope(budget), pytest.raises(app.SearchTransportUnavailable):
        app.run_web_search("reference")
    assert [c["provider"] for c in providers["calls"]] == ["brave"]
    assert tavily_transport.quarantined()


@pytest.mark.parametrize("response", [{"results": []}, {}, {"results": "bad"}, TransportHTTPError(500)])
def test_empty_or_failed_fallback_stops_after_one_attempt(providers, response):
    providers["brave"] = [{"grounding": {"generic": []}}]
    providers["tavily"] = [response]
    status, results = app.run_web_search("reference", ["example.test"])
    assert (status, results) == ("unavailable", [])
    assert [c["provider"] for c in providers["calls"]] == ["brave", "tavily"]


def test_no_quality_or_age_fallback(providers):
    providers["brave"] = [brave_result(content="Different topic, but usable source evidence.", age=["2001-01-01"])]
    status, results = app.run_web_search("latest technical news today")
    assert status == "ok" and results[0]["published"] == ""
    assert "not observation time" in results[0]["content"]
    assert [c["provider"] for c in providers["calls"]] == ["brave"]


def test_restrictions_and_normalization_bounds(providers):
    response = brave_result(content="Z" * 10000)
    response["grounding"]["generic"].insert(0, {"url": "https://outside.test/leak", "title": "Outside", "snippets": ["Outside"]})
    response["grounding"]["generic"] *= 8
    response["sources"][SOURCE]["fetched_content_timestamp"] = "2026-10-10T23:00:00Z"
    providers["brave"] = [response]
    status, results = app.run_web_search("reference", ["docs.example.test"])
    assert status == "ok" and len(results) == 1
    assert results[0]["url"] == SOURCE and len(results[0]["content"]) <= 800
    assert "fetch timestamp (not observation time)" in results[0]["content"]
    assert results[0]["published"] == ""
    assert providers["calls"][0]["payload"]["maximum_number_of_urls"] == app.MAX_SEARCH_RESULTS


def test_site_operator_is_not_broadened(providers):
    response = brave_result()
    response["grounding"]["generic"].insert(0, {"url": "https://outside.test/a", "title": "Outside", "snippets": ["outside"]})
    providers["brave"] = [response]
    status, results = app.run_web_search("site:docs.example.test reference")
    assert status == "ok" and [r["url"] for r in results] == [SOURCE]
    assert "$site=docs.example.test" in providers["calls"][0]["payload"]["goggles"]


def test_sanitized_selection_diagnostics(providers, monkeypatch, capsys):
    monkeypatch.setattr(app, "DEBUG_MODE", True)
    providers["brave"] = [RuntimeError(BRAVE_KEY + " RAW-ERROR-BODY")]
    providers["tavily"] = [tavily_result()]
    app.run_web_search("reference")
    logs = capsys.readouterr().out
    assert "SEARCH_PROVIDER selected=brave" in logs
    assert "brave_failed reason=provider_or_transport_failure" in logs
    assert "evidence=tavily fallback_used=true" in logs
    assert BRAVE_KEY not in logs and TAVILY_KEY not in logs and "RAW-ERROR-BODY" not in logs


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("fallback_used", [False, True])
def test_chat_paths_use_same_provider_policy_and_preserve_source_text(providers, monkeypatch, native, fallback_used):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    providers["brave"] = [TransportHTTPError(429) if fallback_used else brave_result()]
    if fallback_used:
        providers["tavily"] = [tavily_result()]

    def native_model(items, instructions):
        providers["model"].append((items, instructions))
        if not any(item.get("type") == "function_call_output" for item in items):
            return {"output": [{"type": "function_call", "name": "search_web", "call_id": "s1",
                                "arguments": json.dumps({"query": "Tavily API documentation"})}]}
        assert "Tavily API documentation evidence." in json.dumps(items)
        assert "Brave" in instructions
        return {"output": [{"type": "message", "content": [{"type": "output_text", "text": "Supported answer.\n\nSources: [Reference]"}]}]}

    def legacy_model(messages, max_tokens=None):
        providers["model"].append(messages)
        assert "Tavily API documentation evidence." in messages[-1].content
        assert "Brave" in messages[0].content
        return SimpleNamespace(content="Supported answer.\n\nSources: [Reference]", incomplete=False)

    monkeypatch.setattr(app, "_invoke_openai_native_tools", native_model)
    monkeypatch.setattr(app, "invoke_llm", legacy_model)
    answer = app.chat("Search the web for Tavily API reference on docs.example.test", [], session_id="provider-policy-pipeline")
    assert answer == "Supported answer.\n\n**Sources**\n\n- [Reference](" + SOURCE + ")"
    assert [c["provider"] for c in providers["calls"]] == (["brave", "tavily"] if fallback_used else ["brave"])
    assert len(providers["model"]) == (2 if native else 1)


@pytest.mark.parametrize("primary,fallback", [("tavily", "none"), ("brave", "none"), ("brave", "tavily")])
def test_runtime_and_canonical_facts_are_configuration_only(providers, monkeypatch, primary, fallback):
    monkeypatch.setattr(app, "SEARCH_PROVIDER_POLICY", sp.SearchProviderPolicy(primary, fallback))
    facts = app._v31_runtime_facts()
    assert facts["web_search_provider"] == primary.title()
    assert facts["web_search_policy"]["fallback"] == (None if fallback == "none" else "Tavily")
    assert facts["automatic_model_fallback"] is False and facts["configured_fallback_chain"] == []
    _, answer = app.get_canonical_self_knowledge_response("can you search the web?")
    assert primary.title() in answer
    if primary == "brave" and fallback == "none":
        assert "Tavily" not in answer
    assert "{search_provider_label()}" not in app.render_kalillac_facts()
    assert "fallback ran" not in answer or "do not claim fallback ran" in answer
    assert BRAVE_KEY not in json.dumps(facts) and TAVILY_KEY not in answer
    assert providers["calls"] == []


@pytest.mark.parametrize("budgeted", [False, True])
def test_tavily_only_never_uses_brave(providers, monkeypatch, budgeted):
    monkeypatch.setattr(app, "SEARCH_PROVIDER_POLICY", sp.SearchProviderPolicy())
    providers["tavily"] = [tavily_result()]
    budget = RequestBudget(duration_seconds=45, max_model_attempts=6, max_search_attempts=6)
    with budget_scope(budget) if budgeted else nullcontext():
        status, results = app.run_web_search("history of aqueducts")
    assert status == "ok" and results[0]["url"] == SOURCE
    assert [c["provider"] for c in providers["calls"]] == ["tavily"]
    assert budget.search_attempts == (1 if budgeted else 0)


@pytest.mark.parametrize("credential", [None, "", "   "])
def test_brave_only_missing_key_does_not_substitute_tavily(providers, monkeypatch, credential):
    monkeypatch.setattr(app, "SEARCH_PROVIDER_POLICY", sp.SearchProviderPolicy("brave", "none"))
    monkeypatch.setattr(app, "BRAVE_API_KEY", credential)
    assert app.run_web_search("reference") == ("unavailable", [])
    assert providers["calls"] == []


def test_request_selected_transport_timeout_prevents_fallback(providers):
    budget = RequestBudget(duration_seconds=2.5, max_model_attempts=6, max_search_attempts=6)
    providers["brave"] = [TransportDeadlineExceeded()]
    with budget_scope(budget), pytest.raises(RequestDeadlineExceeded):
        app.run_web_search("reference")
    assert [c["provider"] for c in providers["calls"]] == ["brave"]
    assert providers["calls"][0]["timeout"] <= 2.5


@pytest.mark.parametrize("code", [401, 403, 429])
def test_auth_permission_quota_reason_is_sanitized(providers, monkeypatch, capsys, code):
    monkeypatch.setattr(app, "DEBUG_MODE", True)
    providers["brave"] = [TransportHTTPError(code)]
    providers["tavily"] = [tavily_result()]
    app.run_web_search("reference")
    output = capsys.readouterr().out
    assert "brave_failed reason=auth_permission_quota" in output
    assert BRAVE_KEY not in output and TAVILY_KEY not in output


def test_bounded_result_count_and_technical_context(providers):
    response = {"grounding": {"generic": [
        {"url": f"https://docs.example.test/ref/{i}", "title": f"Reference {i}", "snippets": ["X" * 9000]}
        for i in range(8)]}, "sources": {}}
    providers["brave"] = [response]
    status, results = app.run_web_search("current Python release", ["docs.example.test"])
    assert status == "ok" and len(results) == app.MAX_SEARCH_RESULTS
    assert all(len(r["content"]) <= 1600 for r in results)


def test_explicit_restriction_cannot_be_broadened_by_site_operator():
    assert sp.domains_for_query(["docs.example.test"], "site:outside.test subject") == ["docs.example.test"]
    assert not sp.allowed_url("https://outside.test/ref", ["docs.example.test"])
    assert not sp.allowed_url("https://docs.example.test.evil.test/ref", ["docs.example.test"])
    assert not sp.allowed_url("javascript:alert(1)", [])
    assert not sp.allowed_url("http://[invalid", [])


def test_wire_request_is_single_post_with_no_redirect(monkeypatch):
    import http.server
    import threading
    calls = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            calls.append((self.path, json.loads(body), self.headers.get("X-Subscription-Token")))
            self.send_response(302)
            self.send_header("Location", "/would-be-second-attempt")
            self.end_headers()

        def do_GET(self):
            raise AssertionError("redirect must not be followed")

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        import urllib.error
        url = f"http://127.0.0.1:{server.server_port}/context"
        with pytest.raises(urllib.error.HTTPError) as caught:
            sp.post_json_unbudgeted(url, {"q": "subject"}, {"X-Subscription-Token": BRAVE_KEY}, 2)
        assert caught.value.code == 302
        assert calls == [("/context", {"q": "subject"}, BRAVE_KEY)]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.mark.parametrize("body,encoding", [(b"{" + b"X" * 30, "identity"), (b"[]", "gzip"), (b"invalid", "identity")])
def test_wire_size_encoding_and_json_are_bounded(monkeypatch, body, encoding):
    sizes = []

    class Response:
        headers = {"Content-Encoding": encoding}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, size):
            sizes.append(size)
            return body[:size]

    class Opener:
        def open(self, request, timeout):
            assert request.get_method() == "POST" and timeout == 2
            assert request.get_header("X-subscription-token") == BRAVE_KEY
            return Response()

    monkeypatch.setattr(sp.urllib.request, "build_opener", lambda *args: Opener())
    with pytest.raises(sp.SearchResponseInvalid):
        sp.post_json_unbudgeted(sp.BRAVE_CONTEXT_URL, {"q": "subject"}, {"X-Subscription-Token": BRAVE_KEY}, 2, max_bytes=16)
    assert sizes in ([], [17])


@pytest.mark.parametrize("budgeted", [False, True])
def test_site_operator_restriction_reaches_tavily_fallback(providers, budgeted):
    providers["brave"] = [TransportHTTPError(500)]
    providers["tavily"] = [tavily_result()]
    budget = RequestBudget(duration_seconds=45, max_model_attempts=6, max_search_attempts=6)
    with budget_scope(budget) if budgeted else nullcontext():
        status, results = app.run_web_search("Search the web for current API reference site:docs.example.test")
    assert status == "ok" and results[0]["url"] == SOURCE
    assert [c["provider"] for c in providers["calls"]] == ["brave", "tavily"]
    assert providers["calls"][0]["payload"]["goggles"] == "$discard\n$site=docs.example.test"
    assert providers["calls"][1]["payload"]["include_domains"] == ["docs.example.test"]
    assert budget.search_attempts == (2 if budgeted else 0)


@pytest.mark.parametrize("failure", ["redirect", "size", "deadline"])
def test_budgeted_brave_wire_failure_uses_existing_transport(providers, monkeypatch, failure):
    from dataclasses import replace
    import http.server
    import threading
    calls, release = [], threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            calls.append((self.path, json.loads(body), self.headers.get("X-Subscription-Token")))
            if failure == "deadline":
                release.wait(5)
                return
            self.send_response(302 if failure == "redirect" else 200)
            if failure == "redirect":
                self.send_header("Location", "/forbidden-second-call")
            else:
                self.send_header("Content-Length", "1000")
            self.end_headers()
            if failure == "size":
                try:
                    self.wfile.write(b"X" * 1000)
                except (BrokenPipeError, ConnectionResetError):
                    pass
        def do_GET(self):
            calls.append(("unexpected-get", {}, None))
            self.send_response(500)
            self.end_headers()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(sp, "BRAVE_CONTEXT_URL", f"http://127.0.0.1:{server.server_port}/context")
    slot = tavily_transport.TavilyTransportSlot()
    monkeypatch.setattr(tavily_transport, "_SLOT", slot)
    monkeypatch.setattr(app, "_request_limits", replace(LIMITS, tavily_search_max_bytes=128))
    monkeypatch.setattr(app, "SEARCH_PROVIDER_POLICY", sp.SearchProviderPolicy("brave", "none"))
    budget = RequestBudget(duration_seconds=0.8 if failure == "deadline" else 10,
                           max_model_attempts=6, max_search_attempts=6)
    try:
        with budget_scope(budget):
            if failure == "deadline":
                with pytest.raises(RequestDeadlineExceeded):
                    app.run_web_search("reference")
            else:
                assert app.run_web_search("reference") == ("unavailable", [])
        assert budget.search_attempts == 1 and budget.model_attempts == 0
        assert len(calls) == 1 and calls[0][0] == "/context"
        assert calls[0][1] == sp.brave_payload("reference", [], app.MAX_SEARCH_RESULTS)
        assert calls[0][2] == BRAVE_KEY
        assert providers["calls"] == []
    finally:
        release.set()
        report = tavily_transport.close_transport()
        assert report is None or report.clean
        server.shutdown()
        server.server_close()
        thread.join(2)
        assert not thread.is_alive()


# --- one model-requested native evidence follow-up, through both transport seams ---

@pytest.fixture
def native_lookup(providers, monkeypatch):
    from kalillac_routing.provider_transport import TransportHolder
    state = providers
    state.update(turns=[], session_checks=0)
    real_session_allowed = app.session_search_allowed

    def model_post(payload):
        state["model"].append(copy.deepcopy(payload))
        assert payload["model"] == app.OPENAI_MODEL and payload["store"] is False
        assert payload["reasoning"]["effort"] == app.OPENAI_REASONING_EFFORT
        assert payload["max_output_tokens"] == app.MAX_RESPONSE_TOKENS
        if not state["turns"]:
            raise UnscriptedRequest("Unexpected model attempt")
        response = state["turns"].pop(0)
        return response(payload) if callable(response) else copy.deepcopy(response)

    class ModelTransport:
        def __init__(self, **kwargs):
            pass
        def post_json(self, url, payload, *, headers, timeout, max_bytes, cancelled=None):
            assert url == app.OPENAI_RESPONSES_URL and 0 < timeout <= app.OPENAI_CALL_TIMEOUT_SECONDS
            assert max_bytes == LIMITS.openai_max_bytes
            return model_post(payload)
        def close(self, timeout):
            return SimpleNamespace(clean=True)

    def session_allowed(session):
        state["session_checks"] += 1
        return real_session_allowed(session)

    monkeypatch.setattr(app, "OPENAI_API_KEY", "offline-model-key")
    monkeypatch.setattr(app, "_post_openai_responses", model_post)
    monkeypatch.setattr(app, "_OPENAI_TRANSPORT", TransportHolder(factory=ModelTransport))
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", True)
    monkeypatch.setattr(app, "session_search_allowed", session_allowed)
    return state


def _lookup_tool(query="weather reported location today", call_id="lookup-1", **extra):
    return {"output":[{"type":"function_call", "name":"search_web", "call_id":call_id,
                       "arguments":json.dumps({"query":query, **extra})}]}


def _lookup_reply(text="The supported evidence is limited; current conditions remain unverified."):
    return {"output":[{"type":"message", "content":[{"type":"output_text", "text":text}]}]}


def _lookup_outputs(state):
    return [json.loads(i["output"]) for i in state["model"][-1]["input"]
            if i.get("type")=="function_call_output"]


def _run_lookup(state, budgeted=True, message="Search the web for weather today at the reported location.", budget=None):
    policy = app.SEARCH_PROVIDER_POLICY
    budget = budget or RequestBudget(duration_seconds=45, max_model_attempts=6, max_search_attempts=6)
    with budget_scope(budget) if budgeted else nullcontext():
        answer = app.chat(message, [], session_id="native-evidence-lookup")
    assert app.SEARCH_PROVIDER_POLICY is policy
    assert state["session_checks"] == 1
    return answer, budget


@pytest.mark.parametrize("budgeted", [False, True])
def test_native_adequate_brave_evidence_uses_no_tavily(native_lookup, budgeted):
    state = native_lookup
    state["brave"] = [brave_result(content="An applicable observation with a stated time.")]
    state["turns"] = [_lookup_tool(), _lookup_reply("Supported answer.")]
    answer, budget = _run_lookup(state, budgeted)
    assert [c["provider"] for c in state["calls"]] == ["brave"]
    assert len(state["model"]) == 2 and budget.search_attempts == (1 if budgeted else 0)
    result = _lookup_outputs(state)[0]
    assert result["provider_provenance"]["attempted_providers"] == ["brave"]
    assert result["provider_provenance"]["evidence_provider"] == "brave"
    assert "adequate" in state["model"][0]["instructions"]
    assert "one follow-up" in state["model"][0]["instructions"]
    assert answer == "Supported answer.\n\n**Sources**\n\n- [Reference](" + SOURCE + ")"


@pytest.mark.parametrize("budgeted", [False, True])
@pytest.mark.parametrize("insufficiency", ["expired-weather", "wrong-location"])
def test_native_requested_followup_uses_tavily_once(native_lookup, budgeted, insufficiency):
    state = native_lookup
    content = ("Observation September 15, 2026. Forecast expires September 17, 2026."
               if insufficiency=="expired-weather" else "Weather for a different location, not the requested location.")
    state["brave"] = [brave_result(content=content)]
    state["tavily"] = [tavily_result(url="https://weather.test/followup", content="Requested location: a separately dated observation.")]
    def ask_followup(payload):
        first = next(json.loads(i["output"]) for i in payload["input"] if i.get("type")=="function_call_output")
        assert first["results"][0]["content"] == content
        return _lookup_tool("weather reported location latest observation", "lookup-2")
    state["turns"] = [_lookup_tool(), ask_followup, _lookup_reply()]
    answer, budget = _run_lookup(state, budgeted)
    assert [c["provider"] for c in state["calls"]] == ["brave", "tavily"]
    assert budget.search_attempts == (2 if budgeted else 0)
    assert budget.model_attempts == (3 if budgeted else 0) and len(state["model"]) == 3
    outputs = _lookup_outputs(state)
    assert outputs[0]["provider_provenance"]["attempted_providers"] == ["brave"]
    assert outputs[1]["provider_provenance"] == {"selected_provider":"tavily", "attempted_providers":["brave","tavily"], "evidence_provider":"tavily"}
    assert outputs[0]["results"][0]["content"] == content
    assert outputs[1]["results"][0]["content"] == "Requested location: a separately dated observation."
    assert "expired forecasts" in state["model"][0]["instructions"] and "wrong-location" in state["model"][0]["instructions"]
    assert SOURCE in answer and "https://weather.test/followup" in answer
    assert state["calls"][1]["url"] == tavily_transport.SEARCH_URL
    assert all(c["max_bytes"] == LIMITS.tavily_search_max_bytes for c in state["calls"])


@pytest.mark.parametrize("failure", [TransportHTTPError(429), TransportConnectionError(), {"grounding":{"generic":[]}}])
def test_native_initial_failure_fallback_closes_followup(native_lookup, failure):
    state = native_lookup; state["brave"] = [failure];state["tavily"] = [tavily_result()]
    state["turns"] = [_lookup_tool(), _lookup_tool(call_id="lookup-2"), _lookup_reply()]
    _,budget = _run_lookup(state)
    assert [c["provider"] for c in state["calls"]] == ["brave", "tavily"]
    assert budget.search_attempts == 2
    outputs = _lookup_outputs(state)
    assert outputs[0]["provider_provenance"]["attempted_providers"] == ["brave", "tavily"]
    assert outputs[0]["provider_provenance"]["evidence_provider"] == "tavily"
    assert outputs[1]["status"] == "rejected" and "already" in outputs[1]["reason"]


def test_native_missing_brave_key_counts_only_actual_tavily_attempt(native_lookup, monkeypatch):
    state=native_lookup;monkeypatch.setattr(app,"BRAVE_API_KEY",None)
    state["tavily"]=[tavily_result()]
    state["turns"]=[_lookup_tool(),_lookup_tool(call_id="lookup-2"),_lookup_reply()]
    _,budget=_run_lookup(state)
    assert [c["provider"] for c in state["calls"]]==["tavily"] and budget.search_attempts==1
    outputs=_lookup_outputs(state)
    assert outputs[0]["provider_provenance"]["attempted_providers"]==["tavily"]
    assert outputs[1]["status"]=="rejected"


def test_native_third_search_is_rejected(native_lookup):
    state=native_lookup;state["brave"]=[brave_result()];state["tavily"]=[tavily_result()]
    state["turns"]=[_lookup_tool(),_lookup_tool(call_id="lookup-2"),_lookup_tool(call_id="lookup-3"),_lookup_reply()]
    _,budget=_run_lookup(state)
    assert [c["provider"] for c in state["calls"]]==["brave","tavily"] and budget.search_attempts==2
    assert [o["status"] for o in _lookup_outputs(state)]==["ok","ok","rejected"]
    assert budget.model_attempts==4


@pytest.mark.parametrize("primary",["tavily","brave"])
def test_native_single_provider_policy_still_rejects_second_search(native_lookup,monkeypatch,primary):
    state=native_lookup;monkeypatch.setattr(app,"SEARCH_PROVIDER_POLICY",sp.SearchProviderPolicy(primary,"none"))
    state[primary]=[tavily_result() if primary=="tavily" else brave_result()]
    state["turns"]=[_lookup_tool(),_lookup_tool(call_id="lookup-2"),_lookup_reply()]
    _,budget=_run_lookup(state)
    assert [c["provider"] for c in state["calls"]]==[primary] and budget.search_attempts==1
    assert _lookup_outputs(state)[1]["status"]=="rejected"


def test_legacy_does_not_follow_up_on_nonempty_stale_brave(native_lookup,monkeypatch):
    state=native_lookup;monkeypatch.setattr(app,"V31_NATIVE_TOOL_ROUTING",False)
    state["brave"]=[brave_result(content="Old observation; forecast expired.")]
    state["turns"]=[_lookup_reply()]
    _,budget=_run_lookup(state)
    assert [c["provider"] for c in state["calls"]]==["brave"] and budget.search_attempts==1
    assert len(state["model"])==1


@pytest.mark.parametrize("stop",["cancel","deadline","exhaustion","quarantine"])
def test_native_followup_respects_request_and_transport_stops(native_lookup,stop):
    state=native_lookup;clock=[100.0]
    budget=RequestBudget(duration_seconds=45,max_model_attempts=6,max_search_attempts=1 if stop=="exhaustion" else 6,clock=lambda:clock[0])
    state["brave"]=[brave_result()]
    def stopped_followup(payload):
        if stop=="cancel":budget.cancel()
        if stop=="deadline":clock[0]=145.0
        if stop=="quarantine":tavily_transport.search_holder().quarantine()
        return _lookup_tool(call_id="lookup-2")
    state["turns"]=[_lookup_tool(),stopped_followup]
    error={"cancel":RequestCancelled,"deadline":RequestDeadlineExceeded,"exhaustion":CallBudgetExhausted,"quarantine":app.SearchTransportUnavailable}[stop]
    with pytest.raises(error):_run_lookup(state,budget=budget)
    assert [c["provider"] for c in state["calls"]]==["brave"] and budget.search_attempts==1
    assert len(state["model"])==2


@pytest.mark.parametrize("result",[{"results":[]},TransportHTTPError(500)])
def test_native_failed_followup_is_truthful_and_preserves_primary_sources(native_lookup,result):
    state=native_lookup;state["brave"]=[brave_result()];state["tavily"]=[result]
    state["turns"]=[_lookup_tool(),_lookup_tool(call_id="lookup-2"),_lookup_reply()]
    answer,budget=_run_lookup(state)
    assert [c["provider"] for c in state["calls"]]==["brave","tavily"] and budget.search_attempts==2
    output=_lookup_outputs(state)[1]
    assert output["status"]=="unavailable" and output["results"]==[]
    assert output["provider_provenance"]["evidence_provider"] is None
    assert "current conditions remain unverified" in answer and SOURCE in answer


def test_native_followup_preserves_user_domains_and_guards_added_date(native_lookup):
    state=native_lookup;state["brave"]=[brave_result(url="https://docs.example.com/reference")];state["tavily"]=[tavily_result(url="https://docs.example.com/reference")]
    message="Search https://docs.example.com for weather today."
    state["turns"]=[_lookup_tool("weather today site:outside.test"),_lookup_tool("weather October 11, 2026 site:outside.test","lookup-2"),_lookup_reply()]
    _run_lookup(state,message=message)
    assert state["calls"][0]["payload"]["goggles"]=="$discard\n$site=docs.example.com"
    assert state["calls"][1]["payload"]["include_domains"]==["docs.example.com"]
    assert _lookup_outputs(state)[1]["query"]==message
    assert "October 11" not in state["calls"][1]["payload"]["query"]


def test_native_followup_retains_authoritative_domain_policy(native_lookup):
    state=native_lookup;url="https://developers.openai.com/api/docs/pricing"
    state["brave"]=[brave_result(url=url)];state["tavily"]=[tavily_result(url=url)]
    state["turns"]=[_lookup_tool("current OpenAI API pricing official"),_lookup_tool("current OpenAI API pricing official","lookup-2"),_lookup_reply()]
    _run_lookup(state,message="Search the web for current OpenAI API pricing.")
    assert state["calls"][1]["payload"]["include_domains"]==["developers.openai.com","openai.com"]


def test_native_private_followup_never_reaches_provider(native_lookup):
    state=native_lookup;state["brave"]=[brave_result()]
    state["turns"]=[_lookup_tool(),_lookup_tool("search my session memory for weather","lookup-2"),_lookup_reply()]
    _,budget=_run_lookup(state)
    assert [c["provider"] for c in state["calls"]]==["brave"] and budget.search_attempts==1
    assert _lookup_outputs(state)[1]["status"]=="rejected"


@pytest.mark.parametrize("duplicate",[False,True])
def test_native_evidence_records_survive_and_display_urls_are_deduplicated(native_lookup,duplicate):
    state=native_lookup;url=SOURCE if duplicate else "https://weather.test/second"
    first="Source A: Current 61, Tonight 57; observed September 15."
    second="Source B: Current 66, Tonight 59; observed October 10."
    state["brave"]=[brave_result(content=first)];state["tavily"]=[tavily_result(url=url,content=second)]
    state["turns"]=[_lookup_tool(),_lookup_tool(call_id="lookup-2"),_lookup_reply("Qualified answer.\n\nSources: [Wrong]")]
    answer,_=_run_lookup(state)
    outputs=_lookup_outputs(state)
    assert outputs[0]["results"][0]["content"]==first and outputs[1]["results"][0]["content"]==second
    assert answer.count("**Sources**")==1 and answer.count("]("+SOURCE+")")==1
    assert answer.count("- [Reference]")== (1 if duplicate else 2)
    assert "Wrong" not in answer


def test_native_followup_preserves_each_provider_context_limits(native_lookup):
    state=native_lookup
    state["brave"]=[{"grounding":{"generic":[{"url":f"https://weather.test/a{i}","title":f"A{i}","snippets":["A"*9000]} for i in range(8)]},"sources":{}}]
    state["tavily"]=[{"results":[{"url":f"https://weather.test/b{i}","title":f"B{i}","content":"B"*9000} for i in range(8)]}]
    state["turns"]=[_lookup_tool(),_lookup_tool(call_id="lookup-2"),_lookup_reply()]
    answer,_=_run_lookup(state)
    outputs=_lookup_outputs(state)
    assert [len(o["results"]) for o in outputs]==[app.MAX_SEARCH_RESULTS,app.MAX_SEARCH_RESULTS]
    assert all(len(r["content"])<=800 for o in outputs for r in o["results"])
    assert answer.count("- [")==2*app.MAX_SEARCH_RESULTS


def test_native_lookup_cannot_bypass_session_allowance(native_lookup,monkeypatch):
    state=native_lookup
    def limited(session):state["session_checks"]+=1;return False
    monkeypatch.setattr(app,"session_search_allowed",limited)
    state["turns"]=[_lookup_tool(),_lookup_tool(call_id="lookup-2"),_lookup_reply()]
    _run_lookup(state)
    assert state["calls"]==[] and [o["status"] for o in _lookup_outputs(state)]==["limited","rejected"]


def test_native_model_cannot_override_provider_in_tool_arguments(native_lookup):
    state=native_lookup;state["turns"]=[_lookup_tool(provider="tavily")]
    with pytest.raises(app.ToolValidationError):
        with budget_scope(RequestBudget(duration_seconds=45,max_model_attempts=6,max_search_attempts=6)):
            app._run_v31_native_tool_chat("Search the web for weather today.",[],{"memory":[],"search_times":[]})
    assert state["calls"]==[]


def test_native_followup_requires_a_later_model_round(native_lookup):
    state=native_lookup;state["brave"]=[brave_result()]
    batch={"output":_lookup_tool()["output"]+_lookup_tool(call_id="lookup-2")["output"]}
    state["turns"]=[batch,_lookup_reply()]
    _run_lookup(state)
    assert [c["provider"] for c in state["calls"]]==["brave"]
    assert _lookup_outputs(state)[1]["status"]=="rejected"


def test_native_lookup_round_limit_does_not_replay_providers_via_legacy(native_lookup):
    state=native_lookup;state["brave"]=[brave_result()];state["tavily"]=[tavily_result()]
    state["turns"]=[_lookup_tool(call_id=f"lookup-{i}") for i in range(4)]
    with pytest.raises(app.ModelProviderUnavailable):_run_lookup(state)
    assert [c["provider"] for c in state["calls"]]==["brave","tavily"]
    assert len(state["model"])==4


def test_native_followup_does_not_expand_model_attempt_allowance(native_lookup):
    state=native_lookup;state["brave"]=[brave_result()];state["tavily"]=[tavily_result()]
    state["turns"]=[_lookup_tool(),_lookup_tool(call_id="lookup-2")]
    budget=RequestBudget(duration_seconds=45,max_model_attempts=2,max_search_attempts=6)
    with pytest.raises(CallBudgetExhausted):_run_lookup(state,budget=budget)
    assert budget.model_attempts==2 and budget.search_attempts==2
    assert [c["provider"] for c in state["calls"]]==["brave","tavily"]


def test_native_followup_facts_are_scoped_to_native_brave_tavily(native_lookup,monkeypatch):
    facts=app._v31_runtime_facts()
    recovery=facts["web_search_policy"]["native_evidence_followup"]
    assert recovery["available_when_native_routing_enabled"] is True
    assert recovery["requires_no_prior_tavily_attempt"] is True and recovery["legacy_eligible"] is False
    assert facts["automatic_model_fallback"] is False and facts["configured_fallback_chain"]==[]
    for policy in [sp.SearchProviderPolicy(),sp.SearchProviderPolicy("brave","none")]:
        monkeypatch.setattr(app,"SEARCH_PROVIDER_POLICY",policy)
        assert app._v31_runtime_facts()["web_search_policy"]["native_evidence_followup"]["available_when_native_routing_enabled"] is False



def test_native_followup_provenance_scope_resets_before_legacy_search(native_lookup):
    state=native_lookup;state["brave"]=[brave_result()];state["tavily"]=[tavily_result()]
    state["turns"]=[_lookup_tool(),_lookup_tool(call_id="lookup-2"),_lookup_reply()]
    _run_lookup(state)
    assert app._NATIVE_SEARCH_CONTEXT.get() is None
    state["brave"]=[brave_result()]
    assert app.run_web_search("weather today")[0]=="ok"
    assert [c["provider"] for c in state["calls"]]==["brave","tavily","brave"]


def test_native_followup_keeps_explicit_user_dates(native_lookup):
    state=native_lookup;state["brave"]=[brave_result()];state["tavily"]=[tavily_result()]
    message="Search the web for weather today and October 12, 2026."
    state["turns"]=[_lookup_tool("weather today October 12, 2026"),_lookup_tool("weather October 12, 2026","lookup-2"),_lookup_reply()]
    _run_lookup(state,message=message)
    assert _lookup_outputs(state)[1]["query"]=="weather October 12, 2026"
    assert "October 12, 2026" in state["calls"][1]["payload"]["query"]


def test_native_unconfirmed_followup_cleanup_stops_without_more_model_or_provider_calls(native_lookup):
    state=native_lookup;state["brave"]=[brave_result()];state["tavily"]=[TransportCleanupUnconfirmed("deadline")]
    state["turns"]=[_lookup_tool(),_lookup_tool(call_id="lookup-2")]
    with pytest.raises(app.SearchTransportUnavailable):_run_lookup(state)
    assert [c["provider"] for c in state["calls"]]==["brave","tavily"]
    assert len(state["model"])==2 and tavily_transport.quarantined()
    assert app._NATIVE_SEARCH_CONTEXT.get() is None


def test_native_followup_missing_key_is_not_reported_as_tavily_attempt(native_lookup,monkeypatch):
    state=native_lookup;state["brave"]=[brave_result()];monkeypatch.setattr(app,"TAVILY_API_KEY",None)
    state["turns"]=[_lookup_tool(),_lookup_tool(call_id="lookup-2"),_lookup_reply()]
    _,budget=_run_lookup(state)
    assert [c["provider"] for c in state["calls"]]==["brave"] and budget.search_attempts==1
    followup=_lookup_outputs(state)[1]
    assert followup["status"]=="unavailable" and followup["provider_provenance"]=={
        "selected_provider":"tavily","attempted_providers":["brave"],"evidence_provider":None}


def test_native_private_origin_never_starts_a_lookup(native_lookup):
    state=native_lookup;state["turns"]=[_lookup_tool(),_lookup_reply()]
    with budget_scope(RequestBudget(duration_seconds=45,max_model_attempts=6,max_search_attempts=6)):
        app._run_v31_native_tool_chat("Search my memory for weather.",[],{"memory":[],"search_times":[]})
    assert state["calls"]==[] and state["session_checks"]==0
    assert _lookup_outputs(state)[0]["status"]=="rejected"


def test_native_followup_runtime_facts_include_actual_routing_flag(native_lookup,monkeypatch):
    assert app._v31_runtime_facts()["web_search_policy"]["native_evidence_followup"]["enabled_in_current_process"] is True
    monkeypatch.setattr(app,"V31_NATIVE_TOOL_ROUTING",False)
    assert app._v31_runtime_facts()["web_search_policy"]["native_evidence_followup"]["enabled_in_current_process"] is False
    assert app.current_runtime_configuration()["web_search_policy"]["native_evidence_followup"]["enabled_in_current_process"] is False



def test_native_third_search_after_private_rejection_still_cannot_use_tavily(native_lookup):
    state=native_lookup;state["brave"]=[brave_result()];state["tavily"]=[tavily_result()]
    state["turns"]=[_lookup_tool(),_lookup_tool("search my session memory for weather","lookup-2"),
                    _lookup_tool(call_id="lookup-3"),_lookup_reply()]
    _run_lookup(state)
    assert [c["provider"] for c in state["calls"]]==["brave"]
    assert [o["status"] for o in _lookup_outputs(state)]==["ok","rejected","rejected"]



@pytest.mark.parametrize("budgeted",[False,True])
def test_native_news_followup_never_extracts_or_retries(native_lookup,budgeted):
    state=native_lookup;state["brave"]=[brave_result(content="A"*2000)]
    state["tavily"]=[{"results":[{"title":"Dated article","url":"https://news.test/article",
                                 "published_date":"2026-09-01","content":"B"*2000}]}]
    state["turns"]=[_lookup_tool("AI news today"),_lookup_tool("latest AI news developments","lookup-2"),_lookup_reply()]
    _run_lookup(state,budgeted,message="Search the web for AI news today.")
    assert [c["provider"] for c in state["calls"]]==["brave","tavily"]
    assert state["calls"][1]["url"]==tavily_transport.SEARCH_URL
    outputs=_lookup_outputs(state)
    assert len(outputs[0]["results"][0]["content"])==1600
    assert len(outputs[1]["results"][0]["content"])==1600
    assert outputs[1]["results"][0]["published"]=="2026-09-01"
