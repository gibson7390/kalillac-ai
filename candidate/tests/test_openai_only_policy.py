"""OpenAI-only provider policy.

OpenAI is Kalillac's only model provider, with or without request
budgeting. Every test fakes the OpenAI seam; an autouse guard makes any
other outbound request, or any real bounded transport, fail the test.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error

import pytest
from fastapi.testclient import TestClient


CANDIDATE_DIR = Path(__file__).resolve().parents[1]

if str(CANDIDATE_DIR) not in sys.path:
    sys.path.insert(0, str(CANDIDATE_DIR))


import app_fastapi_candidate as app
from kalillac_routing import provider_transport
from kalillac_routing.bounded_transport import (
    InvalidJSONResponse,
    TransportConnectionError,
    TransportHTTPError,
)
from kalillac_routing.openai_tool_loop import (
    ToolLoopError,
    ToolLoopOutputError,
    ToolLoopProtocolError,
)
from kalillac_routing.request_budget import (
    CallBudgetExhausted,
    RequestBudget,
    RequestCancelled,
    RequestDeadlineExceeded,
    budget_scope,
)
from kalillac_routing.request_limits import RequestLimits, TransportLimits
from kalillac_routing.tool_contract import ToolValidationError
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage


MESSAGES = [SystemMessage(content="system"), HumanMessage(content="hello")]
SECRET_KEY = "sk-test-secret-value-must-not-leak"
LIMITS = RequestLimits(
    deadline_seconds=5.0,
    queue_wait_seconds=2.0,
    max_model_attempts=6,
    max_search_attempts=6,
    transport=TransportLimits(
        max_outstanding=4,
        dns_threads=1,
        max_pending_dns=4,
        cancel_poll_interval_seconds=0.02,
        backstop_grace_seconds=0.5,
        cleanup_grace_seconds=1.0,
        close_timeout_seconds=3.0,
    ),
    openai_max_bytes=2097152,
)
SERVICE_UNAVAILABLE = {"error": "service_unavailable"}
PROVIDER_UNAVAILABLE = {"error": "model_provider_unavailable"}


class UnexpectedProviderCall(BaseException):
    """A test reached a real provider path. BaseException, so no broad
    handler in the app can hide it."""


def _refuse(*args, **kwargs):
    raise UnexpectedProviderCall()


@pytest.fixture(autouse=True)
def no_real_providers(monkeypatch):
    # Any urllib request (OpenAI flag-off seam unfaked, or any other
    # provider) and any real bounded transport fail the test.
    monkeypatch.setattr(app.urllib.request, "urlopen", _refuse)
    monkeypatch.setattr(
        app, "_OPENAI_TRANSPORT", provider_transport.TransportHolder(factory=_refuse),
    )
    monkeypatch.setattr(app, "OPENAI_API_KEY", SECRET_KEY)
    monkeypatch.setattr(app, "OPENAI_MODEL", "gpt-5.6-luna")
    monkeypatch.setattr(app, "OPENAI_REASONING_EFFORT", "low")
    monkeypatch.setattr(app, "_request_limits", None)
    monkeypatch.setattr(app, "_chat_semaphore", None)
    monkeypatch.setattr(app, "_chat_waiting", 0)
    monkeypatch.setattr(app, "_session_locks", {})
    monkeypatch.setattr(app, "_chats_admitted", 0)
    monkeypatch.setattr(app, "_usage_meter", None)
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", False)
    monkeypatch.setattr(app, "classify_request", lambda message, history: "general")
    monkeypatch.setattr(app, "requires_web_verification", lambda message, history: False)


def reply(text, cut_off=False):
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


@pytest.fixture
def openai(monkeypatch):
    """Scripted OpenAI seam for both paths: flag-off urllib function and the
    budgeted transport. Each outbound OpenAI request is one recorded call."""

    state = {"script": [], "calls": [], "payloads": []}

    def respond(payload):
        state["calls"].append(1)
        state["payloads"].append(payload)
        result = state["script"].pop(0)
        result = result() if callable(result) else result

        if isinstance(result, BaseException):
            raise result

        return result

    class ScriptedTransport:
        def __init__(self, **settings):
            pass

        def post_json(self, url, payload, *, headers, timeout, max_bytes, cancelled=None):
            return respond(payload)

        def close(self, timeout):
            return None

    monkeypatch.setattr(app, "_post_openai_responses", lambda payload, **kw: respond(payload))
    monkeypatch.setattr(
        app, "_OPENAI_TRANSPORT", provider_transport.TransportHolder(factory=ScriptedTransport),
    )
    return state


def budget(models=6, seconds=5.0, clock=time.monotonic):
    return RequestBudget(
        duration_seconds=seconds,
        max_model_attempts=models,
        max_search_attempts=6,
        clock=clock,
    )


def post_chat(message="Explain rivers."):
    with TestClient(app.api) as client:
        return client.post("/api/chat", json={"message": message, "history": []})


# --- 1. success ---------------------------------------------------------------------------


def test_openai_success_is_unchanged(openai):
    openai["script"] = [reply("Rivers flow downhill.")]

    response = app.invoke_llm(MESSAGES, max_tokens=123)

    assert response.content == "Rivers flow downhill."
    assert response.incomplete is False
    payload = openai["payloads"][0]
    assert payload["model"] == "gpt-5.6-luna"
    assert payload["store"] is False
    assert payload["reasoning"] == {"effort": "low"}
    assert payload["max_output_tokens"] == 123
    assert payload["input"] == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
    ]
    assert openai["calls"] == [1]


# --- 2-3. remote failure and empty output: one request, no other provider ------------------


# What each path's OpenAI seam can raise for a remote failure: urllib (and
# json.loads) when the flag is off; the bounded transport's remote errors when
# budgeted. (A raw ValueError from the transport seam is an internal defect.)
REMOTE_FAILURES = [
    (False, urllib.error.URLError("down")),
    (False, urllib.error.HTTPError("u", 500, "err", {}, None)),
    (False, TimeoutError()),
    (False, json.JSONDecodeError("bad", "", 0)),
    (True, TransportConnectionError()),
    (True, TransportHTTPError(503)),
    (True, InvalidJSONResponse()),
]


@pytest.mark.parametrize(
    "budgeted, error",
    REMOTE_FAILURES,
    ids=[("budgeted-" if b else "flag_off-") + type(e).__name__ for b, e in REMOTE_FAILURES],
)
def test_remote_failure_makes_one_openai_request_and_no_fallback(openai, monkeypatch, error, budgeted):
    openai["script"] = [error]
    monkeypatch.setattr(app, "_request_limits", LIMITS)

    with pytest.raises(app.ModelProviderUnavailable) as caught:
        if budgeted:
            with budget_scope(budget()):
                app.invoke_llm(MESSAGES)
        else:
            app.invoke_llm(MESSAGES)

    assert type(caught.value) is app.ModelProviderUnavailable
    assert caught.value.__cause__ is None
    assert openai["calls"] == [1]


@pytest.mark.parametrize("text", ["", "   \n"])
def test_empty_output_raises_without_any_further_request(openai, text):
    openai["script"] = [reply(text)]

    with pytest.raises(app.ModelProviderUnavailable):
        app.invoke_llm(MESSAGES)

    assert openai["calls"] == [1]


def test_remote_failure_logs_class_and_status_only(openai, capsys):
    openai["script"] = [urllib.error.HTTPError("u", 429, SECRET_KEY, {}, None)]

    with pytest.raises(app.ModelProviderUnavailable):
        app.invoke_llm(MESSAGES)

    out = capsys.readouterr().out
    assert "WARN: OPENAI_PRIMARY_UNAVAILABLE HTTPError status=429" in out
    assert SECRET_KEY not in out


# --- 4. missing or blank configuration ------------------------------------------------------


BLANK_SETTINGS = [
    ("OPENAI_API_KEY", None),
    ("OPENAI_API_KEY", ""),
    ("OPENAI_API_KEY", "   "),
    ("OPENAI_MODEL", ""),
    ("OPENAI_MODEL", "  "),
    ("OPENAI_REASONING_EFFORT", ""),
    ("OPENAI_REASONING_EFFORT", None),
]


@pytest.mark.parametrize("name, value", BLANK_SETTINGS)
def test_blank_configuration_is_local_unavailability_without_admission(
    openai, monkeypatch, capsys, name, value,
):
    monkeypatch.setattr(app, name, value)
    request_budget = budget()

    with budget_scope(request_budget):
        with pytest.raises(app.OpenAIConfigurationUnavailable):
            app.invoke_llm(MESSAGES)

        with pytest.raises(app.OpenAIConfigurationUnavailable):
            app._invoke_openai_native_tools([{"role": "user", "content": "x"}], "rules")

    assert openai["calls"] == []
    assert request_budget.model_attempts == 0
    out = capsys.readouterr().out
    assert "WARN: OPENAI_CONFIGURATION_UNAVAILABLE" in out
    assert SECRET_KEY not in out


@pytest.mark.parametrize("budgeted", [False, True], ids=["flag_off", "budgeted"])
@pytest.mark.parametrize("name, value", [("OPENAI_API_KEY", ""), ("OPENAI_MODEL", " ")])
def test_blank_configuration_is_503_service_unavailable_at_the_api(
    openai, monkeypatch, capsys, name, value, budgeted,
):
    monkeypatch.setattr(app, name, value)

    if budgeted:
        monkeypatch.setattr(app, "_request_limits", LIMITS)

    response = post_chat()

    assert response.status_code == 503
    assert response.json() == SERVICE_UNAVAILABLE
    assert response.headers["cache-control"] == "no-store"
    assert openai["calls"] == []
    assert SECRET_KEY not in capsys.readouterr().out


# --- 5-8. native-tool classification ---------------------------------------------------------


@pytest.fixture
def native(monkeypatch):
    legacy = []
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", True)

    def legacy_llm(*args, **kwargs):
        legacy.append(1)
        return AIMessage(content="legacy answer")

    monkeypatch.setattr(app, "invoke_llm", legacy_llm)
    return legacy


def _chat():
    return app.chat("hello there", [], session_id="openai-only-native")


def test_native_remote_failure_never_retries_through_legacy(openai, native):
    openai["script"] = [ConnectionError("down")]

    with pytest.raises(app.ModelProviderUnavailable) as caught:
        _chat()

    assert type(caught.value) is app.ModelProviderUnavailable
    assert openai["calls"] == [1]
    assert native == []


def test_native_unusable_output_never_retries_through_legacy(openai, native):
    openai["script"] = [reply("")]

    with pytest.raises(app.ModelProviderUnavailable):
        _chat()

    assert openai["calls"] == [1]
    assert native == []


@pytest.mark.parametrize(
    "error, outcome",
    [
        (ToolLoopProtocolError("Maximum tool rounds exceeded."), "legacy"),
        (ToolValidationError("bad tool"), "legacy"),
        (ToolLoopOutputError("Model completed without visible output text."), "provider"),
        (ToolLoopError("Tool result is not JSON serializable."), "internal"),
        (RuntimeError("bug"), "internal"),
        (KeyError("bug"), "internal"),
    ],
    ids=lambda v: type(v).__name__ if isinstance(v, BaseException) else v,
)
def test_only_protocol_and_validation_errors_enter_legacy_once(monkeypatch, native, error, outcome):
    def loop(**kwargs):
        raise error

    monkeypatch.setattr(app, "run_tool_loop", loop)

    if outcome == "legacy":
        assert _chat() == "legacy answer"
        assert native == [1]
        return

    expected = app.ModelProviderUnavailable if outcome == "provider" else app.ChatInternalError

    with pytest.raises(expected):
        _chat()

    assert native == []


def _tool_call():
    return {"output": [{
        "type": "function_call",
        "call_id": "call_1",
        "name": "get_kalillac_runtime_facts",
        "arguments": '{"topic": "models"}',
    }]}


@pytest.mark.parametrize("defect", ["execution", "serialization", "cleanup"])
def test_tool_and_cleanup_defects_are_internal_errors_without_legacy(
    openai, native, monkeypatch, defect,
):
    if defect == "execution":
        openai["script"] = [_tool_call()]
        monkeypatch.setattr(app, "_v31_runtime_facts", lambda: (_ for _ in ()).throw(RuntimeError("bug")))
    elif defect == "serialization":
        openai["script"] = [_tool_call()]
        monkeypatch.setattr(app, "_v31_runtime_facts", lambda: object())
    else:
        openai["script"] = [reply("answer")]

        def broken(text):
            raise RuntimeError("bug")

        monkeypatch.setattr(app, "clean_ai_reply", broken)

    with pytest.raises(app.ChatInternalError):
        _chat()

    assert native == []
    assert openai["calls"] == [1]


# --- 9. request stop precedence ------------------------------------------------------------------


@pytest.mark.parametrize("stop", ["cancel", "deadline"])
@pytest.mark.parametrize("path", ["legacy", "native"])
def test_request_stop_wins_over_a_remote_failure(openai, monkeypatch, stop, path):
    now = [100.0]
    request_budget = budget(seconds=5.0, clock=lambda: now[0])

    def stopped_then_failed():
        if stop == "cancel":
            request_budget.cancel()
        else:
            now[0] += 10.0
        return ConnectionError("dropped")

    openai["script"] = [stopped_then_failed]
    monkeypatch.setattr(app, "_request_limits", LIMITS)
    expected = RequestCancelled if stop == "cancel" else RequestDeadlineExceeded

    with budget_scope(request_budget):
        with pytest.raises(expected):
            if path == "legacy":
                app.invoke_llm(MESSAGES)
            else:
                monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", True)
                _chat()

    assert openai["calls"] == [1]


def test_attempt_exhaustion_stops_before_another_request(openai, monkeypatch):
    openai["script"] = [reply("Partial", cut_off=True)]
    monkeypatch.setattr(app, "_request_limits", LIMITS)

    with budget_scope(budget(models=1)):
        with pytest.raises(CallBudgetExhausted):
            app.invoke_llm(MESSAGES)

    assert openai["calls"] == [1]


# --- 10. continuation and repairs keep their own admission ----------------------------------------


def test_continuation_and_repairs_each_admit_their_own_attempts(openai, monkeypatch):
    monkeypatch.setattr(app, "_request_limits", LIMITS)
    openai["script"] = [
        reply("Partial", cut_off=True), reply(" rest"),   # primary + continuation
        reply("```python\nprint(1)\n```"),                  # python repair
        reply("<!doctype html><html><body>ok</body></html>"),  # html repair
    ]
    request_budget = budget(models=6)

    with budget_scope(request_budget):
        app.invoke_llm(MESSAGES)
        assert request_budget.model_attempts == 2

        app.repair_python_output("write code", "print(1", ["syntax error"])
        assert request_budget.model_attempts == 3

        app.repair_html_output("make a page", "<html>", ["missing body"])
        assert request_budget.model_attempts == 4

    assert len(openai["calls"]) == 4


def test_remote_continuation_failure_keeps_the_typed_partial(openai):
    openai["script"] = [reply("Partial", cut_off=True), ConnectionError("down")]

    response = app.invoke_llm(MESSAGES)

    assert response.content == "Partial"
    assert response.incomplete is True
    assert openai["calls"] == [1, 1]


def test_repair_failure_propagates_as_provider_unavailable(openai):
    openai["script"] = [ConnectionError("down")]

    with pytest.raises(app.ModelProviderUnavailable):
        app.repair_python_output("write code", "print(1", ["syntax error"])


# --- 11. flag-off generation is OpenAI-only ---------------------------------------------------------


def test_flag_off_openai_failure_is_503_without_any_other_provider(openai):
    openai["script"] = [ConnectionError("down")]

    response = post_chat()

    assert response.status_code == 503
    assert response.json() == PROVIDER_UNAVAILABLE
    assert openai["calls"] == [1]


def test_flag_off_success_comes_from_openai(openai):
    openai["script"] = [reply("Rivers flow downhill.")]

    response = post_chat()

    assert response.status_code == 200
    assert response.json()["reply"] == "Rivers flow downhill."
    assert openai["calls"] == [1]


# --- 12. no Groq or Workers AI in the runtime -------------------------------------------------------


REMOVED = [
    "GROQ_MODEL", "FALLBACK_GROQ_MODEL", "GROQ_API_KEY", "CLOUDFLARE_MODEL",
    "CLOUDFLARE_AI_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_CALL_TIMEOUT_SECONDS",
    "llm", "fallback_llm", "ChatGroq", "RateLimitError", "_no_retry_groq",
    "_NO_RETRY_GROQ", "_groq_client_for_attempt", "_invoke_cloudflare",
    "_invoke_final_groq_fallback", "_invoke_cross_provider_fallback",
    "_invoke_existing_provider_chain", "_groq_response_with_finish_reason",
    "_chat_completions_response", "CHAT_COMPLETIONS_LENGTH_FINISH_REASON",
    "_admit_model_call", "_cloudflare_message_payload",
]


def test_removed_provider_symbols_are_gone():
    assert [name for name in REMOVED if hasattr(app, name)] == []


def test_app_imports_without_groq_or_workers_ai_settings():
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("GROQ", "CLOUDFLARE", "KALILLAC_"))
    }
    script = (
        "import json, sys, app_fastapi_candidate as a; "
        "print(json.dumps([n for n in %r if hasattr(a, n)]))" % (REMOVED,)
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(CANDIDATE_DIR), env=env, capture_output=True, text=True, timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == []


# --- 13. local outage vs provider unavailability, both handlers ------------------------------------


@pytest.mark.parametrize("budgeted", [False, True], ids=["flag_off", "budgeted"])
@pytest.mark.parametrize(
    "raised, expected",
    [
        ("OpenAIConfigurationUnavailable", SERVICE_UNAVAILABLE),
        ("ProviderTransportUnavailable", SERVICE_UNAVAILABLE),
        ("LocalModelServiceUnavailable", SERVICE_UNAVAILABLE),
        ("ModelProviderUnavailable", PROVIDER_UNAVAILABLE),
    ],
)
def test_both_handlers_keep_local_outage_distinct(monkeypatch, raised, expected, budgeted):
    def failing_chat(message, history, request=None, session_id=None):
        raise getattr(app, raised)()

    monkeypatch.setattr(app, "chat", failing_chat)

    if budgeted:
        monkeypatch.setattr(app, "_request_limits", LIMITS)

    response = post_chat()

    assert response.status_code == 503
    assert response.json() == expected

    if expected == SERVICE_UNAVAILABLE:
        assert response.headers["cache-control"] == "no-store"


def test_exception_hierarchy():
    assert issubclass(app.LocalModelServiceUnavailable, app.ModelProviderUnavailable)
    assert issubclass(app.ProviderTransportUnavailable, app.LocalModelServiceUnavailable)
    assert issubclass(app.OpenAIConfigurationUnavailable, app.LocalModelServiceUnavailable)
    assert app.ModelProviderUnavailable in app._OPENAI_PATH_STOPS
    assert app.ChatInternalError in app._OPENAI_PATH_STOPS


# --- 14. disclosure ------------------------------------------------------------------------------------


def _surfaces():
    return {
        "system_prompt": app.SYSTEM_PROMPT,
        "facts": app.render_kalillac_facts(),
        "code_reference": app.render_kalillac_code_reference_facts(),
        "diagram": app.kalillac_ascii_diagram(),
        "identity": app.KALILLAC_CANONICAL_IDENTITY,
        "how_it_works": app.KALILLAC_CANONICAL_HOW_IT_WORKS,
        "memory": app.KALILLAC_CANONICAL_MEMORY,
        "model": app.KALILLAC_CANONICAL_MODEL,
        "runtime_facts": json.dumps(app._v31_runtime_facts()),
    }


@pytest.mark.parametrize("surface", list(_surfaces()))
def test_disclosure_names_no_removed_provider(surface):
    text = _surfaces()[surface]

    for removed in ("Groq", "Workers AI", "gpt-oss", "@cf/"):
        assert removed not in text, (surface, removed)


@pytest.mark.parametrize(
    "message",
    ["what model do you use?", "what is kalillac ai", "how does Kalillac work?"],
)
def test_self_knowledge_states_the_openai_only_policy(message):
    _family, answer = app.get_canonical_self_knowledge_response(message)

    assert app.OPENAI_MODEL in answer
    assert "no automatic fallback" in answer


def test_runtime_facts_are_openai_only():
    facts = app._v31_runtime_facts()

    assert facts["configured_primary"]["provider"] == "OpenAI"
    assert facts["configured_fallback_chain"] == []
    assert facts["automatic_model_fallback"] is False
    assert facts["web_search_provider"] == "Tavily"
    assert facts["request_handling"]["legacy_pipeline_provider_chain"] == [
        {"provider": "OpenAI", "model": app.OPENAI_MODEL},
    ]
    assert facts["per_message_provider_identity_available"] is False


def test_cloudflare_cdn_facts_are_kept():
    facts = app.render_kalillac_facts()
    diagram = app.kalillac_ascii_diagram()

    assert "Cloudflare" in facts
    assert "Cloudflare" in diagram
    assert "Full (strict)" in app.KALILLAC_CANONICAL_HOW_IT_WORKS


def test_api_failure_wording_is_accurate():
    facts = app.render_kalillac_facts()

    assert "model_provider_unavailable" in facts
    assert "chat then returns a friendly" not in facts


# --- 15. Tavily is unchanged ------------------------------------------------------------------------


# SHA-256 of each function's source on origin/main 4f56ded0 (before this slice).
TAVILY_PATH_SOURCE = {
    "run_web_search": "5511214d2768d2ceb26d89e4d588ea370577e2b09781fce6098eef4f4c1edf9e",
    "_admit_search_call": "1fcc8b4d3f0b1a65ed46ba60e64c7bc060816d128beacd4615829543f98ef0e5",
    "session_search_allowed": "72f8e0ddb4b565727df13f43e81a4d9fd5a84e3f4d5ab92b163b94edec364cc6",
}


@pytest.mark.parametrize("name", list(TAVILY_PATH_SOURCE))
def test_tavily_path_source_is_unchanged(name):
    source = inspect.getsource(getattr(app, name))

    assert hashlib.sha256(source.encode()).hexdigest() == TAVILY_PATH_SOURCE[name]


# --- 16. the tests themselves cannot reach a provider ----------------------------------------------


def test_guard_blocks_unfaked_openai_and_any_other_request():
    with pytest.raises(UnexpectedProviderCall):
        app._post_openai_responses({"model": "m"})

    with pytest.raises(UnexpectedProviderCall):
        app.urllib.request.urlopen("https://example.invalid")


# --- review round 2: stop precedence for empty output and continuation failure ---------------


def _stopping_budget(stop):
    """A budget plus a callable that establishes `stop` (cancel or expired
    deadline) using the budget's own state and clock."""
    now = [100.0]
    request_budget = budget(seconds=5.0, clock=lambda: now[0])

    def establish():
        if stop == "cancel":
            request_budget.cancel()
        else:
            now[0] += 10.0

    return request_budget, establish


STOPS = {"cancel": RequestCancelled, "deadline": RequestDeadlineExceeded}


@pytest.mark.parametrize("stop", list(STOPS))
def test_stop_wins_over_empty_output_classification(openai, monkeypatch, stop):
    request_budget, establish = _stopping_budget(stop)

    def stopped_then_empty():
        establish()
        return reply("")

    openai["script"] = [stopped_then_empty]
    monkeypatch.setattr(app, "_request_limits", LIMITS)

    with budget_scope(request_budget):
        with pytest.raises(STOPS[stop]):
            app.invoke_llm(MESSAGES)

    assert openai["calls"] == [1]


def test_empty_output_with_an_open_request_is_provider_unavailable(openai, monkeypatch):
    openai["script"] = [reply("")]
    monkeypatch.setattr(app, "_request_limits", LIMITS)

    with budget_scope(budget()):
        with pytest.raises(app.ModelProviderUnavailable) as caught:
            app.invoke_llm(MESSAGES)

    assert type(caught.value) is app.ModelProviderUnavailable
    assert openai["calls"] == [1]


@pytest.mark.parametrize("error", ["transport_error", "other_remote_error"])
def test_remote_continuation_failure_with_an_open_request_keeps_the_partial(
    openai, monkeypatch, error,
):
    failure = TransportConnectionError() if error == "transport_error" else ConnectionError("x")
    openai["script"] = [reply("Partial", cut_off=True), failure]
    monkeypatch.setattr(app, "_request_limits", LIMITS)
    request_budget = budget()

    with budget_scope(request_budget):
        response = app.invoke_llm(MESSAGES)

    assert response.content == "Partial"
    assert response.incomplete is True
    assert response.incomplete_reason == app.OUTPUT_TOKEN_LIMIT_REASON
    assert openai["calls"] == [1, 1]           # no further request
    assert request_budget.model_attempts == 2


# A transport error is already mapped by the adapter (rows 1-2); a
# non-transport remote error reaches the continuation catch itself.
CONTINUATION_ERRORS = {
    "transport_error": TransportConnectionError,
    "other_remote_error": lambda: ConnectionError("dropped"),
}


@pytest.mark.parametrize("error", list(CONTINUATION_ERRORS))
@pytest.mark.parametrize("stop", list(STOPS))
def test_stop_wins_over_a_simultaneous_continuation_failure(openai, monkeypatch, stop, error):
    request_budget, establish = _stopping_budget(stop)

    def stopped_then_failed():
        establish()
        return CONTINUATION_ERRORS[error]()

    openai["script"] = [reply("Partial", cut_off=True), stopped_then_failed]
    monkeypatch.setattr(app, "_request_limits", LIMITS)

    with budget_scope(request_budget):
        with pytest.raises(STOPS[stop]):
            app.invoke_llm(MESSAGES)

    assert openai["calls"] == [1, 1]           # no partial returned, no further request


# --- review round 2: configuration-driven model disclosure ------------------------------------


CONFIGURED_MODEL = "configured-test-model-7"

DISCLOSURE_PROBE = r"""
import json
import app_fastapi_candidate as a

captured = []

def capture(messages, max_tokens=None):
    captured.append(chr(10).join(str(m.content) for m in messages))
    raise a.ModelProviderUnavailable()

a.invoke_llm = capture

for call in (
    lambda: a.repair_python_output("write code", "print(1", ["syntax error"]),
    lambda: a.repair_html_output("make a page", "<html>", ["missing body"]),
):
    try:
        call()
    except a.ModelProviderUnavailable:
        pass

def prompt(message, route):
    return chr(10).join(str(m.content) for m in a.build_messages(message, [], route, []))

surfaces = {
    "model_constant": a.OPENAI_MODEL,
    "system_prompt": a.SYSTEM_PROMPT,
    "facts": a.render_kalillac_facts(),
    "code_reference": a.render_kalillac_code_reference_facts(),
    "diagram": a.kalillac_ascii_diagram(),
    "identity": a.KALILLAC_CANONICAL_IDENTITY,
    "how_it_works": a.KALILLAC_CANONICAL_HOW_IT_WORKS,
    "memory": a.KALILLAC_CANONICAL_MEMORY,
    "model": a.KALILLAC_CANONICAL_MODEL,
    "difference": a.KALILLAC_CANONICAL_DIFFERENCE,
    "search": a.KALILLAC_CANONICAL_SEARCH,
    "native_policy": a.V31_NATIVE_TOOL_POLICY,
    "runtime_facts": json.dumps(a._v31_runtime_facts()),
    "self_knowledge_prompt": prompt("what model do you use?", "self_knowledge"),
    "general_prompt": prompt("hello there", "general"),
    "code_prompt": prompt("build a router like kalillac", "code"),
    "python_repair_prompt": captured[0],
    "html_repair_prompt": captured[1],
}
print(json.dumps(surfaces))
"""


def test_disclosure_uses_the_configured_model_not_a_hard_coded_one():
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("GROQ", "CLOUDFLARE", "KALILLAC_", "OPENAI_"))
    }
    env["OPENAI_MODEL"] = CONFIGURED_MODEL
    env["OPENAI_REASONING_EFFORT"] = "medium"

    result = subprocess.run(
        [sys.executable, "-c", DISCLOSURE_PROBE],
        cwd=str(CANDIDATE_DIR), env=env, capture_output=True, text=True, timeout=180,
    )

    assert result.returncode == 0, result.stderr
    surfaces = json.loads(result.stdout.strip().splitlines()[-1])

    assert surfaces["model_constant"] == CONFIGURED_MODEL

    for name, text in surfaces.items():
        lowered = text.lower()
        assert "luna" not in lowered, name
        assert "gpt-5.6" not in lowered, name

    # Surfaces that state the model name the configured value.
    for name in (
        "system_prompt", "facts", "code_reference", "diagram", "identity",
        "how_it_works", "model", "search", "runtime_facts",
        "self_knowledge_prompt", "python_repair_prompt",
    ):
        assert CONFIGURED_MODEL in surfaces[name], name

    assert "medium" in surfaces["model"]
