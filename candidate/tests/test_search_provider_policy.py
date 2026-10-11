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
