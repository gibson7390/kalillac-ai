"""The anonymous Free Chat boundary: with the Spaces code present (and its
router installed, as when the feature is on), an ordinary anonymous
/api/chat request behaves exactly as without it.

A deterministic transcript drives validation, ordinary questions, a
follow-up, search, the temporary RAM memory ("remember" and recall), a
previous-chat reference, End & erase and the ended-session refusal through
the real app with the model transport, the native tool path and search
replaced, and the clock pinned. The transcript records every response, every
model payload byte for byte, every search query and the RAM session state
after each step; it must be identical with and without Spaces installed.
The same runner is used offline to compare this module with the base
commit (see the implementation review artifacts).

The allowance is exercised with the existing anonymous-allowance fixture,
and every Spaces database entry point is watched. No real model, search,
database server or network is used."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import re

import pytest
from fastapi.testclient import TestClient

import app_fastapi_candidate as app
from spaces_test_support import build_app, make_world, token_for, COOKIE

FIXED_NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
END = "/api/chat/session/end"
PLAIN = "Explain how encryption uses keys."


def _scenario(module):
    long_message = "x" * (module.MAX_INPUT_CHARS + 1)
    long_history = [{"role": "user", "content": "q"}] * (module.MAX_HISTORY_TURNS + 1)
    earlier = [{"role": "user", "content": PLAIN}, {"role": "assistant", "content": "Keys lock and unlock data."}]
    return [
        ("invalid_missing_message", "chat", {"json": {}}, None),
        ("invalid_empty_message", "chat", {"json": {"message": "   "}}, None),
        ("invalid_too_long", "chat", {"json": {"message": long_message}}, None),
        ("invalid_history_too_long", "chat", {"json": {"message": "hi", "history": long_history}}, None),
        ("invalid_not_json", "chat", {"content": b"{oops", "headers": {"Content-Type": "application/json"}}, None),
        ("plain", "chat", {"json": {"message": PLAIN, "history": []}}, None),
        ("follow_up", "chat", {"json": {"message": "Can you give a shorter version?", "history": earlier}}, "plain"),
        ("search", "chat", {"json": {"message": "Search the web for the latest Python release notes."}}, "plain"),
        ("remember", "chat", {"json": {"message": "Remember that my project codename is Falcon."}}, "plain"),
        ("recall", "chat", {"json": {"message": "What is my project codename?"}}, "plain"),
        ("previous_chat", "chat", {"json": {"message": "What did we discuss in our previous chat?"}}, None),
        ("code", "chat", {"json": {"message": 'Why does `if (a < b && c > d) { return "<ok>"; }` fail?'}}, None),
        ("calculator", "chat", {"json": {"message": "What is 48151 * 62342?"}}, None),
        # The fixed self-knowledge and memory replies a Space adapts.
        ("retention", "chat", {"json": {"message": "Will you remember this after I refresh?"}}, "plain"),
        ("memory_privacy", "chat", {"json": {"message": "Do you store my chats?"}}, "plain"),
        ("how_it_works", "chat", {"json": {"message": "How does Kalillac AI work?"}}, "plain"),
        ("direct_memory", "chat", {"json": {"message": "What is my favorite language?"}}, None),
        ("difference", "chat", {"json": {"message": "What makes Kalillac different?"}}, None),
        ("end_and_erase", "end", {}, "plain"),
        ("after_end", "chat", {"json": {"message": PLAIN}}, "plain"),
        ("end_again", "end", {}, "plain"),
    ]


class _Patcher:
    def __init__(self):
        self._undo = []

    def set(self, target, name, value):
        self._undo.append((target, name, getattr(target, name)))
        setattr(target, name, value)

    def undo(self):
        while self._undo:
            target, name, value = self._undo.pop()
            setattr(target, name, value)


def run_transcript(module, *, budget, native, install=None):
    """Drive the scenario through module.api; return a JSON-able transcript.
    install(module.api) may add routers first (it returns an undo)."""
    from kalillac_routing.request_limits import RequestLimits
    from test_request_budget_integration import BASE_LIMITS

    payloads, queries = [], []

    def completed(text):
        return {"status": "completed", "output": [{"type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": text}]}]}

    def bounded(payload, budget, timeout, request_selected):
        payloads.append(json.dumps(payload, sort_keys=True, ensure_ascii=False))
        return completed("A synthetic model answer.")

    def unbounded(payload, timeout=90, **kwargs):
        payloads.append(json.dumps(payload, sort_keys=True, ensure_ascii=False))
        return completed("A synthetic model answer.")

    def native_tools(items, instructions):
        payloads.append(json.dumps({"native": items, "instructions": instructions}, sort_keys=True, ensure_ascii=False))
        return completed("A synthetic native answer.")

    def search(query, include_domains=None):
        queries.append(query)
        return "unavailable", []

    original_time = module.render_current_time_context
    p = _Patcher()
    p.set(module, "OPENAI_API_KEY", "test-only-key")
    p.set(module, "V31_NATIVE_TOOL_ROUTING", native)
    p.set(module, "_request_limits", RequestLimits(**BASE_LIMITS) if budget else None)
    p.set(module, "_post_openai_bounded", bounded)
    p.set(module, "_post_openai_responses", unbounded)
    p.set(module, "_invoke_openai_native_tools", native_tools)
    p.set(module, "run_web_search", search)
    p.set(module, "render_current_time_context",
          lambda now=None, message="", results=None: original_time(FIXED_NOW, message, results))
    p.set(module, "SESSION_STATE", type(module.SESSION_STATE)())
    for name, value in (("_SESSION_WORK", {}), ("_ENDED_SESSIONS", {}), ("_chat_semaphore", None),
                        ("_chat_waiting", 0), ("_session_locks", {}), ("_usage_meter", None),
                        ("_usage_quota", None), ("_anonymous_quota", None)):
        p.set(module, name, value)
    p.set(module.provider_controls, "SERVICE", None)
    undo_install = install(module.api) if install else None

    sids, transcript = {}, []

    def alias(text):
        for real, name in sids.items():
            text = text.replace(real, name)
        return text

    try:
        with TestClient(module.api, base_url="https://testserver") as client:
            for name, kind, request, session_from in _scenario(module):
                before = (len(payloads), len(queries))
                sid = sids_by_step.get(session_from) if session_from else None
                if kind == "end":
                    response = client.post(END, json={"session_id": sid})
                else:
                    kwargs = {k: (dict(v) if isinstance(v, dict) else v) for k, v in request.items()}
                    if sid is not None:
                        kwargs["json"] = {**kwargs["json"], "session_id": sid}
                    response = client.post("/api/chat", **kwargs)
                body = response.text
                returned = response.json().get("session_id") if response.headers.get(
                    "content-type", "").startswith("application/json") and isinstance(response.json(), dict) else None
                if returned and returned not in sids:
                    sids[returned] = f"<sid{len(sids) + 1}>"
                if returned:
                    sids_by_step[name] = returned
                state = {
                    sids.get(key, "<other>"): {
                        # A saved fact's wall-clock stamp is the one
                        # nondeterministic field; its presence is kept.
                        "memory": [{**m, "time": "<time>"} if "time" in m else m for m in value["memory"]],
                        "searches": len(value["search_times"]),
                        "keys": sorted(value),
                    }
                    for key, value in module.SESSION_STATE.items()
                }
                transcript.append({
                    "step": name,
                    "status": response.status_code,
                    "cache_control": response.headers.get("cache-control"),
                    "set_cookie": response.headers.get("set-cookie"),
                    "body": alias(body),
                    "payloads": [alias(p_) for p_ in payloads[before[0]:]],
                    "queries": queries[before[1]:],
                    "state": json.loads(alias(json.dumps(state, sort_keys=True))),
                    "ended": sorted(sids.get(k, "<other>") for k in module._ENDED_SESSIONS),
                })
    finally:
        if undo_install:
            undo_install()
        p.undo()
    return transcript


sids_by_step: dict[str, str] = {}

CONFIGURATIONS = [
    pytest.param({"budget": True, "native": False}, id="budget-legacy"),
    pytest.param({"budget": True, "native": True}, id="budget-native"),
    pytest.param({"budget": False, "native": False}, id="unbudgeted-legacy"),
]


@pytest.fixture
def world(tmp_path):
    w = make_world(tmp_path)
    yield w
    w.engine.dispose()


def install_spaces(world, opened):
    """Install the Spaces router on the real app, as KALILLAC_SPACES_ENABLED
    does, with every database session it opens recorded."""
    factory = world.factory

    def watched():
        opened.append(1)
        return factory()

    world.factory = watched

    def install(api):
        before = list(api.router.routes)
        api.include_router(build_app(world, generate=app._space_generate).router)

        def undo():
            api.router.routes[:] = before
            world.factory = factory
        return undo
    return install


@pytest.fixture(autouse=True)
def _fresh_steps():
    sids_by_step.clear()
    yield
    sids_by_step.clear()


@pytest.mark.parametrize("configuration", CONFIGURATIONS)
def test_anonymous_transcript_is_identical_with_spaces_installed(world, configuration):
    without = run_transcript(app, **configuration)
    sids_by_step.clear()
    opened = []
    with_spaces = run_transcript(app, install=install_spaces(world, opened), **configuration)

    assert with_spaces == without
    assert opened == []                                   # no Spaces database access at all

    steps = {entry["step"]: entry for entry in without}
    # The scenario really exercised what it claims.
    assert [steps[s]["status"] for s in ("invalid_missing_message", "invalid_empty_message",
                                         "invalid_too_long", "invalid_history_too_long",
                                         "invalid_not_json")] == [422, 422, 422, 422, 400]
    assert steps["plain"]["status"] == 200 and steps["plain"]["payloads"]
    assert steps["remember"]["state"]["<sid1>"]["memory"]
    assert steps["end_and_erase"]["status"] == 200 and "<sid1>" not in steps["end_and_erase"]["state"]
    assert steps["after_end"]["status"] == 409
    if not configuration["native"]:
        assert steps["search"]["queries"] == ["Search the web for the latest Python release notes."]
    for entry in without:
        for payload in entry["payloads"]:
            assert "WORKSPACE CONTEXT" not in payload and "space_instructions" not in payload
    assert app._SPACE_REQUEST.get() is None and app._SPACE_SEARCH_STATE == {}


def test_without_a_workspace_the_model_input_is_built_exactly_as_before(monkeypatch):
    # The pre-Spaces construction was exactly:
    #   _responses_input_items([SystemMessage(CURRENT_INTERFACE_CAPABILITIES), *messages])
    from langchain_core.messages import HumanMessage, SystemMessage
    seen = []
    monkeypatch.setattr(app, "OPENAI_API_KEY", "test-only-key")
    monkeypatch.setattr(app, "_request_limits", None)
    monkeypatch.setattr(app, "_post_openai_responses",
                        lambda payload, timeout=90, **kw: seen.append(payload) or {
                            "status": "completed", "output": [{"type": "message", "role": "assistant",
                                                               "content": [{"type": "output_text", "text": "ok"}]}]})
    messages = [SystemMessage(content="route prompt"), HumanMessage(content='code: a < b && "x" > y')]
    app._invoke_openai(messages)
    assert seen[0]["input"] == app._responses_input_items(
        [SystemMessage(content=app.CURRENT_INTERFACE_CAPABILITIES), *messages])


def test_an_account_cookie_never_attaches_anonymous_chat_to_spaces(world, monkeypatch):
    opened = []
    monkeypatch.setattr(app, "OPENAI_API_KEY", "test-only-key")
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", False)
    monkeypatch.setattr(app, "_request_limits", None)
    monkeypatch.setattr(app, "SESSION_STATE", type(app.SESSION_STATE)())
    monkeypatch.setattr(app, "_post_openai_responses", lambda payload, timeout=90, **kw: {
        "status": "completed", "output": [{"type": "message", "role": "assistant",
                                           "content": [{"type": "output_text", "text": "ok"}]}]})
    undo = install_spaces(world, opened)(app.api)
    try:
        with TestClient(app.api, base_url="https://testserver") as client:
            response = client.post("/api/chat", json={"message": PLAIN},
                                   headers={"Cookie": f"{COOKIE}={token_for('a')}"})
    finally:
        undo()
    assert response.status_code == 200 and set(response.json()) == {"reply", "session_id"}
    assert opened == []
    from kalillac_db.models import Conversation, Message, Space
    assert world.count(Space) == world.count(Conversation) == world.count(Message) == 0
    assert list(app.SESSION_STATE) == [response.json()["session_id"]]


def test_a_failed_anonymous_request_leaves_no_binding(monkeypatch):
    monkeypatch.setattr(app, "OPENAI_API_KEY", "test-only-key")
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", False)
    monkeypatch.setattr(app, "_request_limits", None)
    monkeypatch.setattr(app, "SESSION_STATE", type(app.SESSION_STATE)())
    monkeypatch.setattr(app, "_session_locks", {})

    def fail(payload, timeout=90, **kw):
        raise RuntimeError("synthetic provider failure")

    monkeypatch.setattr(app, "_post_openai_responses", fail)
    with TestClient(app.api, base_url="https://testserver") as client:
        response = client.post("/api/chat", json={"message": PLAIN})
    assert response.status_code != 200
    assert app._SPACE_REQUEST.get() is None
    assert app._SPACE_SEARCH_STATE == {} and app._session_locks == {}


def test_the_wrapper_keeps_the_original_anonymous_lifecycle_text():
    """_run_chat_request is the former api_chat body. The only differences
    are the three parameters it takes; with /api/chat's arguments each one
    reduces to the original expression."""
    import inspect
    source = inspect.getsource(app._run_chat_request)
    assert "if anonymous_allowed and _anonymous_quota is not None and not _account_cookie_present(request):" in source
    assert "response = await produce(request)" in source
    assert "reply_chars(response)" in source
    assert inspect.getsource(app._chat_reply_chars).strip().endswith('return len(json.loads(response.body)["reply"])')
    assert re.search(r"return await _run_chat_request\(request, _api_chat_response\)\s*$",
                     inspect.getsource(app.api_chat).strip())


# --- the allowance, with Spaces installed --------------------------------------------------------


from test_anonymous_chat import (  # noqa: E402,F401  (fixture and helpers)
    COOKIE_VALUE,
    DAY,
    REPLY,
    anon,
    client as anonymous_client,
    post,
    states,
    visitor_cookie,
)


def test_the_allowance_is_unchanged_with_spaces_installed(anon, world):  # noqa: F811  (pytest fixtures)
    opened = []
    undo = install_spaces(world, opened)(app.api)
    try:
        with anonymous_client() as c:
            for _ in range(10):
                response = post(c)
                assert response.status_code == 200 and response.json()["reply"] == REPLY
                assert visitor_cookie(response) == COOKIE_VALUE
            refused = post(c)
    finally:
        undo()
    assert refused.status_code == 429
    assert refused.content == b'{"error":"anonymous_daily_limit_reached"}'
    assert len(anon["calls"]) == 10
    assert states(anon) == [(DAY, "completed")] * 10
    assert opened == []
