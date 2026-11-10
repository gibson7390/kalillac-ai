"""End & erase: POST /api/chat/session/end and the session lifecycle behind it.

No provider, database or network access: chat is faked, every database entry
point fails the test if reached, and concurrency is driven through the ASGI app
with gates and condition polling (no fixed sleeps decide an outcome).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re
import sys
import threading
import time

import pytest


CANDIDATE_DIR = Path(__file__).resolve().parents[1]
if str(CANDIDATE_DIR) not in sys.path:
    sys.path.insert(0, str(CANDIDATE_DIR))

from fastapi.testclient import TestClient

import app_fastapi_candidate as app
from kalillac_routing.request_budget import current_budget
from kalillac_routing.request_limits import RequestLimits
from test_request_budget_integration import BASE_LIMITS
from test_search_provider_policy import brave_result, providers  # noqa: F401  (fixture)


END = "/api/chat/session/end"
JOIN = 10.0
APP_TSX = Path(__file__).resolve().parents[2] / "site" / "src" / "App.tsx"


# --- fixtures ------------------------------------------------------------------------------------


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(app, "SESSION_STATE", {})
    monkeypatch.setattr(app, "_SESSION_WORK", {})
    monkeypatch.setattr(app, "_ENDED_SESSIONS", {})
    monkeypatch.setattr(app, "_request_limits", None)
    monkeypatch.setattr(app, "_chat_semaphore", None)
    monkeypatch.setattr(app, "_chat_waiting", 0)
    monkeypatch.setattr(app, "_session_locks", {})
    monkeypatch.setattr(app, "_usage_meter", None)
    monkeypatch.setattr(app, "_usage_quota", None)
    monkeypatch.setattr(app, "_anonymous_quota", None)
    monkeypatch.setattr(app, "_chats_admitted", 0, raising=False)
    monkeypatch.setattr(app, "_lookups_outstanding", 0, raising=False)
    monkeypatch.setattr(app, "_lookup_tasks", set(), raising=False)
    monkeypatch.setattr(app.provider_controls, "SERVICE", None)
    clock = Clock()
    monkeypatch.setattr(app, "_ended_now", clock)

    # Ending a session never touches a database or a provider.
    def forbidden(*args, **kwargs):
        raise AssertionError("database or provider reached")

    import kalillac_db.engine as engine
    monkeypatch.setattr(engine, "get_session_factory", forbidden)
    monkeypatch.setattr(engine, "get_engine", forbidden)
    monkeypatch.setattr(app, "_post_openai_responses", forbidden)
    monkeypatch.setattr(app.urllib.request, "urlopen", forbidden)
    return clock


def enable_budget(monkeypatch):
    monkeypatch.setattr(app, "_request_limits", RequestLimits(**BASE_LIMITS))


def seed(sid, fact="a saved fact"):
    state = app.get_session_state_by_id(sid)
    state["memory"].append({"type": "user_fact", "fact": fact})
    state["search_times"].append(time.monotonic())
    return sid


def end_http(client, sid, **kwargs):
    return client.post(END, json={"session_id": sid}, **kwargs)


# --- endpoint contract ---------------------------------------------------------------------------


def test_end_removes_the_session_state_and_reports_it():
    sid = seed("end-removed")

    with TestClient(app.api) as client:
        response = end_http(client, sid)

    assert response.status_code == 200
    assert response.json() == {
        "ended": True, "kalillac_session_state": "removed",
        "active_requests": 0, "cancellation": "none",
    }
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers
    assert sid not in app.SESSION_STATE
    assert app._ENDED_SESSIONS[sid] is not None        # idle tombstone, expiry clock running


def test_end_is_idempotent_and_unknown_ids_are_not_found():
    sid = seed("end-twice")

    with TestClient(app.api) as client:
        first = end_http(client, sid)
        second = end_http(client, sid)
        unknown = end_http(client, "never-seen-before")

    assert first.json()["kalillac_session_state"] == "removed"
    for response in (second, unknown):
        assert response.status_code == 200
        assert response.json() == {
            "ended": True, "kalillac_session_state": "not_found",
            "active_requests": 0, "cancellation": "none",
        }
        assert response.headers["cache-control"] == "no-store"


def test_arbitrary_unknown_ids_never_fill_the_tombstone_registry():
    with TestClient(app.api) as client:
        for i in range(300):
            assert end_http(client, f"probe-{i:04d}").json()["kalillac_session_state"] == "not_found"

    assert app._ENDED_SESSIONS == {}
    assert app._SESSION_WORK == {}


@pytest.mark.parametrize("body, headers, status, code", [
    ("{", {"content-type": "application/json"}, 400, "invalid_json"),
    ("[1, 2]", {"content-type": "application/json"}, 400, "invalid_body"),
    ('"text"', {"content-type": "application/json"}, 400, "invalid_body"),
    ('{"session_id": "abc"}', {"content-type": "text/plain"}, 415, "unsupported_media_type"),
    ('{"session_id": "abc"}', {"content-type": "application/x-www-form-urlencoded"}, 415,
     "unsupported_media_type"),
    ('{"session_id": "abc"}', {}, 415, "unsupported_media_type"),
    ("{}", {"content-type": "application/json"}, 422, "invalid_request"),
    ('{"session_id": null}', {"content-type": "application/json"}, 422, "invalid_request"),
    ('{"session_id": 42}', {"content-type": "application/json"}, 422, "invalid_request"),
    ('{"session_id": ""}', {"content-type": "application/json"}, 422, "invalid_request"),
    ('{"session_id": "has space"}', {"content-type": "application/json"}, 422, "invalid_request"),
    ('{"session_id": "../etc"}', {"content-type": "application/json"}, 422, "invalid_request"),
    (json.dumps({"session_id": "x" * 129}), {"content-type": "application/json"}, 422, "invalid_request"),
])
def test_end_rejects_bad_requests_with_fixed_codes(body, headers, status, code):
    sid = seed("abc")

    with TestClient(app.api) as client:
        response = client.post(END, content=body, headers=headers)

    assert response.status_code == status
    assert response.json() == {"error": code}
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers
    assert sid in app.SESSION_STATE                    # nothing was ended


def test_json_content_type_with_charset_is_accepted():
    sid = seed("charset")

    with TestClient(app.api) as client:
        response = client.post(END, content=json.dumps({"session_id": sid}),
                               headers={"content-type": "application/json; charset=utf-8"})

    assert response.status_code == 200
    assert response.json()["kalillac_session_state"] == "removed"


def test_end_never_sets_or_clears_cookies_or_reads_quota(monkeypatch):
    """Account and visitor cookies, and the allowance machinery, are untouched."""
    class Untouchable:
        def __getattr__(self, name):
            raise AssertionError(f"quota touched: {name}")

    monkeypatch.setattr(app, "_anonymous_quota", Untouchable())
    monkeypatch.setattr(app, "_usage_quota", Untouchable())
    sid = seed("cookies")

    with TestClient(app.api) as client:
        client.cookies.set("__Host-kalillac_visitor", "visitor-value")
        client.cookies.set("kalillac_session", "account-value")
        response = end_http(client, sid)

    assert response.status_code == 200
    assert "set-cookie" not in response.headers


def test_account_and_quota_records_are_unchanged(monkeypatch):
    """Rows in every account, usage, quota and billing table are identical
    before and after ending a session, and the database is never opened."""
    from datetime import date, datetime, timezone
    import uuid
    from account_test_db import make_session_factory, make_sqlite_engine
    from kalillac_db.models import AccountSession, AnonymousUsageDay, AnonymousUsageSlot, Base, User

    engine = make_sqlite_engine()
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    now = datetime(2026, 10, 28, tzinfo=timezone.utc)
    with factory() as db, db.begin():
        user = User(id=uuid.uuid4(), email="a@example.test", password_hash="hash")
        db.add(user)
        db.flush()
        db.add(AccountSession(user_id=user.id, token_hash="t" * 64, created_at=now, expires_at=now))
        db.add(AnonymousUsageDay(visitor_digest="d" * 64, usage_date=date(2026, 10, 28)))
        db.add(AnonymousUsageSlot(visitor_digest="d" * 64, usage_date=date(2026, 10, 28), slot=1,
                                  expires_at=now, state="completed", completed_date=date(2026, 10, 28)))

    def snapshot():
        with engine.connect() as connection:
            return {name: sorted(map(tuple, connection.execute(table.select()).all()), key=repr)
                    for name, table in Base.metadata.tables.items()}

    before = snapshot()
    sid = seed("records")

    with TestClient(app.api) as client:
        assert end_http(client, sid).status_code == 200

    assert snapshot() == before


def test_another_session_is_never_affected():
    keep = seed("keep-me", fact="kept fact")
    gone = seed("end-me")

    with TestClient(app.api) as client:
        assert end_http(client, gone).json()["kalillac_session_state"] == "removed"

    assert gone not in app.SESSION_STATE
    assert app.SESSION_STATE[keep]["memory"] == [{"type": "user_fact", "fact": "kept fact"}]
    assert keep not in app._ENDED_SESSIONS
    assert app.resolve_session_id(keep) == keep


def test_end_is_not_gated_by_chat_concurrency(monkeypatch):
    """A saturated chat queue cannot block ending a session."""
    sid = seed("saturated")

    async def scenario():
        sem = app._get_chat_semaphore()
        for _ in range(app.MAX_CONCURRENT_CHATS):
            await sem.acquire()
        monkeypatch.setattr(app, "_chat_waiting", app.MAX_QUEUED_CHATS)
        try:
            return await asgi(END, {"session_id": sid})
        finally:
            for _ in range(app.MAX_CONCURRENT_CHATS):
                sem.release()

    status, body = asyncio.run(scenario())
    assert status == 200 and body["kalillac_session_state"] == "removed"


# --- lifecycle registry: tombstones --------------------------------------------------------------


def test_stale_request_with_an_ended_id_gets_session_ended(isolated):
    sid = seed("stale")
    with TestClient(app.api) as client:
        end_http(client, sid)
        response = client.post("/api/chat", json={"message": "hi", "history": [], "session_id": sid})

    assert response.status_code == 409
    assert response.json() == {"error": "session_ended"}
    assert response.headers["cache-control"] == "no-store"
    assert sid not in app.SESSION_STATE


def test_after_the_tombstone_expires_the_id_is_simply_unknown(monkeypatch, isolated):
    """Expiry is honoured on the chat request itself: nothing else (no other
    end, no release) has to run first to prune the tombstone."""
    sid = seed("expired")
    monkeypatch.setattr(app, "chat", lambda *a, **k: "fresh reply")

    with TestClient(app.api) as client:
        end_http(client, sid)
        assert app._ENDED_SESSIONS[sid] is not None
        isolated.now += app.ENDED_SESSION_TTL_SECONDS
        assert sid in app._ENDED_SESSIONS              # nothing pruned it yet
        response = client.post("/api/chat", json={"message": "hi", "history": [], "session_id": sid})

    assert response.status_code == 200
    new_sid = response.json()["session_id"]
    assert new_sid and new_sid != sid                  # a newly minted session, never the ended one
    assert sid not in app._ENDED_SESSIONS
    assert sid not in app.SESSION_STATE


def test_an_idle_tombstone_is_honoured_until_its_ttl_then_expires_on_access(isolated):
    for accessor in ("resolve", "state", "begin"):
        sid = seed(f"lazy-{accessor}")
        app.end_chat_session(sid)

        def access():
            if accessor == "resolve":
                return app.resolve_session_id(sid)
            if accessor == "state":
                return app.get_session_state_by_id(sid)
            resolved, work = app._begin_session_work(sid, None)
            app._release_session_work(work)
            return resolved

        isolated.now += app.ENDED_SESSION_TTL_SECONDS - 1
        with pytest.raises(app.SessionEnded):
            access()

        isolated.now += 1
        result = access()
        assert sid not in app._ENDED_SESSIONS
        if accessor == "state":
            assert result == {"memory": [], "search_times": []}   # empty: nothing was restored
        else:
            assert result != sid                       # an unknown id gets a fresh one
        app.SESSION_STATE.pop(sid, None)

    assert app._SESSION_WORK == {}


def test_a_tombstone_is_never_removed_while_its_work_is_registered(isolated):
    sid = seed("held")
    _, work = app._begin_session_work(sid, None)
    result = app.end_chat_session(sid)
    assert result["active_requests"] == 1 and result["cancellation"] == "unavailable"
    assert app._ENDED_SESSIONS[sid] is None

    # Far past the TTL, and over the cap: the tombstone stays while work exists,
    # whether pruned by other ends or checked lazily on access.
    isolated.now += 10 * app.ENDED_SESSION_TTL_SECONDS
    for i in range(app.MAX_ENDED_SESSIONS + 5):
        app.end_chat_session(seed(f"filler-{i}"))
    assert sid in app._ENDED_SESSIONS
    with pytest.raises(app.SessionEnded):
        app.get_session_state_by_id(sid)
    with pytest.raises(app.SessionEnded):
        app.resolve_session_id(sid)
    with pytest.raises(app.SessionEnded):
        app._begin_session_work(sid, None)
    assert app._ENDED_SESSIONS[sid] is None

    # Once the work drains, the tombstone expires normally, on access.
    app._release_session_work(work)
    assert app._SESSION_WORK == {}
    assert app._ENDED_SESSIONS[sid] == isolated.now
    isolated.now += app.ENDED_SESSION_TTL_SECONDS
    assert app.resolve_session_id(sid) != sid
    assert sid not in app._ENDED_SESSIONS


def test_ending_an_expired_id_again_does_not_revive_its_tombstone(isolated):
    sid = seed("re-end")
    app.end_chat_session(sid)
    isolated.now += app.ENDED_SESSION_TTL_SECONDS

    assert app.end_chat_session(sid)["kalillac_session_state"] == "not_found"
    assert sid not in app._ENDED_SESSIONS


def test_idle_tombstones_are_capped_oldest_first(monkeypatch, isolated):
    monkeypatch.setattr(app, "MAX_ENDED_SESSIONS", 3)
    ids = []
    for i in range(6):
        isolated.now += 1
        ids.append(seed(f"capped-{i}"))
        app.end_chat_session(ids[-1])

    assert sorted(app._ENDED_SESSIONS) == sorted(ids[-3:])


def test_repeated_end_during_work_stays_safe():
    sid = seed("repeat")
    _, work = app._begin_session_work(sid, None)

    first = app.end_chat_session(sid)
    second = app.end_chat_session(sid)

    assert first["kalillac_session_state"] == "removed"
    assert second["kalillac_session_state"] == "not_found"
    assert first["active_requests"] == second["active_requests"] == 1
    assert app._ENDED_SESSIONS[sid] is None
    app._release_session_work(work)
    app._release_session_work(work)                    # an extra release is a no-op
    assert app._SESSION_WORK == {}


# --- concurrency through the ASGI app ------------------------------------------------------------


class Client:
    """Drive app.api directly; after the body, receive() waits until told to disconnect."""

    def __init__(self, path, payload, content_type=b"application/json"):
        self.path = path
        self.body = json.dumps(payload).encode()
        self.content_type = content_type
        self.sent = False
        self.disconnect = asyncio.Event()
        self.messages = []

    async def receive(self):
        if not self.sent:
            self.sent = True
            return {"type": "http.request", "body": self.body, "more_body": False}
        await self.disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(self, message):
        self.messages.append(message)

    async def run(self):
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "POST", "scheme": "http", "path": self.path,
            "raw_path": self.path.encode(), "query_string": b"", "root_path": "",
            "headers": [(b"content-type", self.content_type), (b"host", b"testserver")],
            "client": ("127.0.0.1", 50000), "server": ("testserver", 80),
        }
        await app.api(scope, self.receive, self.send)
        start = next(m for m in self.messages if m["type"] == "http.response.start")
        body = b"".join(m.get("body", b"") for m in self.messages if m["type"] == "http.response.body")
        self.headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
        return start["status"], json.loads(body)


async def asgi(path, payload):
    return await Client(path, payload).run()


async def until(predicate, timeout=JOIN):
    """Poll an observable condition; the timeout only bounds a failing test."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not reached")


class GatedChat:
    """A stand-in for chat(): fetches its session state exactly as chat() does,
    waits at a gate, then writes to that state and returns a reply."""

    def __init__(self, refetch=False):
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.calls = []
        self.budgets = []
        self.refetch = refetch

    def __call__(self, message, history, request=None, session_id=None):
        self.calls.append(message)
        state = app.get_session_state_by_id(session_id)
        self.budgets.append(current_budget())
        self.started.set()
        try:
            self.release.wait(JOIN)
            if self.refetch:
                state = app.get_session_state_by_id(session_id)   # raises once ended
            state["memory"].append({"type": "user_fact", "fact": "late write"})
            return "late reply that must be discarded"
        finally:
            self.finished.set()


@pytest.mark.parametrize("budgeted", [True, False])
def test_in_flight_work_is_cancelled_and_its_late_result_discarded(monkeypatch, budgeted):
    if budgeted:
        enable_budget(monkeypatch)
    sid = seed("in-flight")
    chat = GatedChat()
    monkeypatch.setattr(app, "chat", chat)

    async def scenario():
        request = asyncio.ensure_future(asgi("/api/chat", {"message": "m", "history": [], "session_id": sid}))
        await until(chat.started.is_set)

        status, ended = await asgi(END, {"session_id": sid})
        assert status == 200
        if budgeted:
            assert chat.budgets[0].cancelled is True       # the registered budget was cancelled

        chat.release.set()
        return ended, await asyncio.wait_for(request, JOIN)

    ended, (status, body) = asyncio.run(scenario())

    assert ended == {
        "ended": True, "kalillac_session_state": "removed", "active_requests": 1,
        "cancellation": "requested" if budgeted else "unavailable",
    }
    assert (status, body) == (409, {"error": "session_ended"})
    assert sid not in app.SESSION_STATE                    # the late write did not recreate it
    assert app._SESSION_WORK == {}                          # registration drained
    assert app._ENDED_SESSIONS[sid] is not None             # tombstone now expiring
    assert app._session_locks == {}


@pytest.mark.parametrize("budgeted", [True, False])
def test_running_work_cannot_refetch_and_recreate_the_session(monkeypatch, budgeted):
    if budgeted:
        enable_budget(monkeypatch)
    sid = seed("refetch")
    chat = GatedChat(refetch=True)
    monkeypatch.setattr(app, "chat", chat)

    async def scenario():
        request = asyncio.ensure_future(asgi("/api/chat", {"message": "m", "history": [], "session_id": sid}))
        await until(chat.started.is_set)
        await asgi(END, {"session_id": sid})
        chat.release.set()
        return await asyncio.wait_for(request, JOIN)

    assert asyncio.run(scenario()) == (409, {"error": "session_ended"})
    assert sid not in app.SESSION_STATE
    assert app._SESSION_WORK == {}


@pytest.mark.parametrize("budgeted", [True, False])
def test_queued_work_never_starts_and_cannot_recreate_the_session(monkeypatch, budgeted):
    if budgeted:
        enable_budget(monkeypatch)
    sid = seed("queued")
    chat = GatedChat()
    monkeypatch.setattr(app, "chat", chat)

    async def scenario():
        first = asyncio.ensure_future(asgi("/api/chat", {"message": "first", "history": [], "session_id": sid}))
        await until(chat.started.is_set)
        second = asyncio.ensure_future(asgi("/api/chat", {"message": "second", "history": [], "session_id": sid}))
        # The second request is registered and waiting on the session lock.
        await until(lambda: len(app._SESSION_WORK.get(sid, ())) == 2
                    and sid in app._session_locks and app._session_locks[sid].refs == 2)

        status, ended = await asgi(END, {"session_id": sid})
        assert ended["active_requests"] == 2
        chat.release.set()
        return await asyncio.wait_for(first, JOIN), await asyncio.wait_for(second, JOIN)

    first, second = asyncio.run(scenario())

    assert first == (409, {"error": "session_ended"})
    assert second == (409, {"error": "session_ended"})
    assert chat.calls == ["first"]                         # the queued request never ran
    assert sid not in app.SESSION_STATE
    assert app._SESSION_WORK == {}
    assert app._session_locks == {}


def test_a_worker_that_outlives_its_handler_keeps_the_tombstone(monkeypatch, isolated):
    """The handler returns (client disconnect) while the worker still runs: the
    registration -- and so the tombstone -- lasts until the worker exits."""
    enable_budget(monkeypatch)
    sid = seed("outlives")
    chat = GatedChat(refetch=True)
    monkeypatch.setattr(app, "chat", chat)

    async def scenario():
        client = Client("/api/chat", {"message": "m", "history": [], "session_id": sid})
        request = asyncio.ensure_future(client.run())
        await until(chat.started.is_set)
        await asgi(END, {"session_id": sid})
        client.disconnect.set()
        status, body = await asyncio.wait_for(request, JOIN)

        # Handler gone, worker still running: tombstone pinned despite expiry time.
        assert app._ENDED_SESSIONS[sid] is None
        isolated.now += 10 * app.ENDED_SESSION_TTL_SECONDS
        app.end_chat_session("prune-trigger")
        assert sid in app._ENDED_SESSIONS

        chat.release.set()
        await until(lambda: chat.finished.is_set() and not app._SESSION_WORK)
        return status, body

    assert asyncio.run(scenario()) == (409, {"error": "session_ended"})
    assert sid not in app.SESSION_STATE
    assert app._ENDED_SESSIONS[sid] is not None


def test_ending_one_session_leaves_another_in_flight_session_alone(monkeypatch):
    enable_budget(monkeypatch)
    ended_sid = seed("ended-one")
    kept_sid = seed("kept-one")
    gates = {ended_sid: GatedChat(), kept_sid: GatedChat()}

    def chat(message, history, request=None, session_id=None):
        return gates[session_id](message, history, request, session_id)

    monkeypatch.setattr(app, "chat", chat)

    async def scenario():
        a = asyncio.ensure_future(asgi("/api/chat", {"message": "a", "history": [], "session_id": ended_sid}))
        b = asyncio.ensure_future(asgi("/api/chat", {"message": "b", "history": [], "session_id": kept_sid}))
        await until(lambda: all(g.started.is_set() for g in gates.values()))
        status, ended = await asgi(END, {"session_id": ended_sid})
        assert ended["active_requests"] == 1
        assert gates[kept_sid].budgets[0].cancelled is False
        for gate in gates.values():
            gate.release.set()
        return await asyncio.wait_for(a, JOIN), await asyncio.wait_for(b, JOIN)

    a, b = asyncio.run(scenario())

    assert a == (409, {"error": "session_ended"})
    assert b[0] == 200 and b[1]["session_id"] == kept_sid
    assert kept_sid in app.SESSION_STATE and kept_sid not in app._ENDED_SESSIONS
    assert app.SESSION_STATE[kept_sid]["memory"][-1]["fact"] == "late write"


def test_a_brand_new_session_is_registered_before_its_state_exists(monkeypatch):
    """No session id: the minted id is registered at resolution, so even a
    request that has not created its state yet is covered."""
    monkeypatch.setattr(app, "chat", lambda *a, **k: "ok")

    with TestClient(app.api) as client:
        response = client.post("/api/chat", json={"message": "hi", "history": []})

    assert response.status_code == 200
    assert app._SESSION_WORK == {}                          # registered, then drained


# --- privacy-safe logging ------------------------------------------------------------------------


PROMPT_SECRET = "PROMPT-SECRET-7781-ZEBRA"
REPLY_SECRET = "REPLY-SECRET-5512-OTTER"


def openai_reply(text):
    return {"status": "completed", "output": [{"type": "message", "role": "assistant",
                                               "content": [{"type": "output_text", "text": text}]}]}


def test_raw_prompt_and_reply_text_never_reach_the_logs_even_with_debug(monkeypatch, capsys):
    monkeypatch.setattr(app, "DEBUG_MODE", True)
    monkeypatch.setattr(app, "OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", False)
    monkeypatch.setattr(app, "_post_openai_responses", lambda payload, **kw: openai_reply(
        f"Here you go. {REPLY_SECRET}"))

    with TestClient(app.api) as client:
        saved = client.post("/api/chat", json={
            "message": f"Remember that my code word is {PROMPT_SECRET}", "history": []})
        general = client.post("/api/chat", json={
            "message": f"Explain why the sky looks blue, mentioning {PROMPT_SECRET}",
            "history": [{"role": "user", "content": f"earlier {PROMPT_SECRET}"},
                        {"role": "assistant", "content": f"earlier answer {REPLY_SECRET}"}]})
        calculated = client.post("/api/chat", json={"message": "What is 48151 * 62342?", "history": []})

    assert saved.status_code == general.status_code == calculated.status_code == 200
    out = capsys.readouterr()
    logged = out.out + out.err
    assert "ROUTE SELECTED" in logged                       # debug logging really ran
    for secret in (PROMPT_SECRET, REPLY_SECRET, str(48151 * 62342)):
        assert secret not in logged, secret


def test_search_query_text_never_reaches_the_logs(monkeypatch, capsys, providers):  # noqa: F811
    monkeypatch.setattr(app, "DEBUG_MODE", True)
    providers["brave"] = [brave_result()]

    app.run_web_search(f"latest {PROMPT_SECRET} release notes")

    logged = "".join(capsys.readouterr())
    assert "SEARCH QUERY: chars=" in logged
    assert PROMPT_SECRET not in logged


def test_no_log_call_interpolates_conversation_content():
    source = (CANDIDATE_DIR / "app_fastapi_candidate.py").read_text(encoding="utf-8")
    assert "RAW INPUT" not in source and "NORMALIZED INPUT" not in source
    content_names = r"(message|normalized|search_query|query|reply|result|fact|history|content|text|error|e)"
    offenders = re.findall(r'\blog\(f"[^"\n]*\{' + content_names + r'(?:\[[^\]]*\])?(?:!r)?\}', source)
    assert offenders == [], offenders


# --- truthful wording ----------------------------------------------------------------------------


OVERCLAIMS = ("securely wiped", "secure wipe of", "permanently deleted", "deleted from openai",
              "deleted from providers", "erases everything", "all data is deleted", "guaranteed",
              "deleted immediately", "immediately deleted", "logs are deleted", "zeroed")


def privacy_text():
    app_tsx = APP_TSX.read_text(encoding="utf-8")
    page = app_tsx[app_tsx.index("function PrivacyPage()"):app_tsx.index("function TermsPage()")]
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", page)).replace("&amp;", "&")


def test_privacy_page_describes_end_and_erase_precisely():
    text = privacy_text()
    lower = text.lower()

    for required in (
        "The chat’s End & erase control ends a conversation on demand.",
        "It clears the conversation, the session identifier and any unsent draft from the page, "
        "and asks Kalillac to remove that session’s temporary server-memory entry.",
        "Stopping is best effort: an AI or search provider may keep processing what it already received.",
        "End & erase does not retract information already sent to OpenAI, Brave or Tavily.",
        "It does not erase daily allowance records, account or billing records, or operational and "
        "security logs kept by Kalillac, Cloudflare or DigitalOcean, and it is not a secure wipe of server memory.",
        "End & erase, eviction or service restart.",
        "You can use End & erase in the chat to end a conversation",
        # The pre-existing refresh statements are kept: refresh still does not delete.
        "That does not promise immediate deletion of the server-memory entry.",
        "Active-session context may remain in server memory until eviction or service restart.",
    ):
        assert required in text, required
    for claim in OVERCLAIMS:
        if claim == "secure wipe of":
            assert lower.count(claim) == 1 and "not a secure wipe of server memory" in lower
        else:
            assert claim not in lower, claim


def test_self_knowledge_states_end_and_erase_limits():
    facts = " ".join(app.KALILLAC_SELF_KNOWLEDGE["session_state"])
    assert "End & erase control removes Kalillac's temporary session state" in facts
    assert "best effort, and a provider may keep processing" in facts
    assert "does not retract information already sent to AI or search providers" in facts
    assert "not secure memory wiping" in facts

    for text in (app.SYSTEM_PROMPT, facts):
        lower = text.lower()
        for claim in ("securely wiped", "deleted from openai", "deleted from providers",
                      "logs are deleted", "erases everything"):
            assert claim not in lower, claim
    # The refresh/closing rule still holds, with the explicit control as the only exception.
    assert "Never claim RAM is erased when a request ends, a tab or browser closes" in app.SYSTEM_PROMPT
    assert "End & erase control is the one on-demand exception" in app.SYSTEM_PROMPT
