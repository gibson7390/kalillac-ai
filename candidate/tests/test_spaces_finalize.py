"""Finalization: cancellation and expiry at every phase, the commit
boundary, the global lock order, and fail-closed lease tokens.

The save (Transaction B) runs in phases, and the send's one budget is
checked -- and, on PostgreSQL, its remaining time re-applied as the lock and
statement timeout -- before and after each blocking phase and immediately
before commit:

    account lock -> Space lock -> conversation lock (lease) -> authorization
    -> settlement -> message flush -> Space update -> final authorization
    -> commit

Before the commit: a cancelled or expired send rolls back -- no exchange,
no charge. After the commit: the exchange and its charge remain, and the
same idempotency key recovers it. Between the two lies one unavoidable
uncertain moment (the database may commit while the acknowledgement is
lost); the same key reconciles it.

Real /api/chat lifecycle with the account quota; model and search mocked;
SQLite only (PostgreSQL lock behaviour is CI-only)."""

from __future__ import annotations

import ast
import asyncio
import inspect
import threading
import uuid

import httpx
import pytest
from sqlalchemy.dialects import postgresql

import app_fastapi_candidate as app
from kalillac_db.models import Conversation, Message
from kalillac_spaces import repository as repo
from kalillac_spaces import router as spaces_router
from spaces_test_support import build_app, client, headers, make_world, new_request_id
from test_spaces_chat import LEASE_SECONDS, REPLY, lease_of, new_conversation, new_space  # noqa: F401
from test_spaces_chat import provider, world  # noqa: F401  (pytest fixtures)
from test_spaces_quota import api, budgets, charged, post, setup, slots  # noqa: F401  (fixtures and helpers)


def stop(budgets, how):  # noqa: F811  (pytest fixtures)
    if how == "cancel":
        budgets[-1].cancel()
    else:
        budgets[-1]._deadline = 0.0


def assert_nothing_saved_or_charged(world, conversation_id):  # noqa: F811
    assert world.count(Message) == 0
    assert charged(world) == 0 and set(slots(world)) <= {"released"}
    assert lease_of(world, conversation_id) == (None, None)       # cleanup ran on its own allowance


def assert_saved_and_charged_once(world, api, space_id, conversation_id, key, provider):  # noqa: F811
    assert world.count(Message) == 2 and charged(world) == 1 and slots(world) == ["completed"]
    calls = len(provider.payloads)
    replay = post(api, space_id, conversation_id, key)
    assert replay.status_code == 200 and replay.json()["assistant_message"]["content"] == REPLY
    assert len(provider.payloads) == calls and charged(world) == 1


EXPECTED = {"cancel": (499, "request_cancelled"), "expire": (504, "request_timeout")}


# --- before the commit: nothing saved, nothing charged ---------------------------------------


def _during_settlement(monkeypatch, budgets, how):  # noqa: F811  (pytest fixtures)
    from kalillac_accounts.quota import UsageQuota
    original = UsageQuota.settle_in_transaction

    def settle(self, session, admission):
        day = original(self, session, admission)
        stop(budgets, how)
        return day
    monkeypatch.setattr(UsageQuota, "settle_in_transaction", settle)


def _between_settlement_and_flush(monkeypatch, budgets, how):  # noqa: F811  (pytest fixtures)
    original = repo.insert_exchange

    def insert(*args, **kwargs):
        stop(budgets, how)
        return original(*args, **kwargs)
    monkeypatch.setattr(repo, "insert_exchange", insert)


def _during_message_flush(monkeypatch, budgets, how):  # noqa: F811  (pytest fixtures)
    original = repo.insert_exchange

    def insert(*args, **kwargs):
        rows = original(*args, **kwargs)
        stop(budgets, how)
        return rows
    monkeypatch.setattr(repo, "insert_exchange", insert)


def _during_space_update(monkeypatch, budgets, how):  # noqa: F811  (pytest fixtures)
    original = repo.complete_exchange

    def complete(*args, **kwargs):
        result = original(*args, **kwargs)
        stop(budgets, how)
        return result
    monkeypatch.setattr(repo, "complete_exchange", complete)


def _after_flush_before_commit(monkeypatch, budgets, how):  # noqa: F811  (pytest fixtures)
    original = repo.finish_send

    def finish(*args, **kwargs):
        result = original(*args, **kwargs)
        stop(budgets, how)                 # every flush done; only the commit is left
        return result
    monkeypatch.setattr(repo, "finish_send", finish)


def _during_final_authorization(monkeypatch, budgets, how):  # noqa: F811  (pytest fixtures)
    original = spaces_router.resolve_account
    calls = []

    def resolve(*args, **kwargs):
        calls.append(1)
        account = original(*args, **kwargs)
        if len(calls) == 3:                # authorize, first recheck, final recheck
            stop(budgets, how)
        return account
    monkeypatch.setattr(spaces_router, "resolve_account", resolve)


PHASES = {
    "during quota settlement": _during_settlement,
    "between settlement and message flush": _between_settlement_and_flush,
    "during message flush": _during_message_flush,
    "during the Space update": _during_space_update,
    "after flush, before commit": _after_flush_before_commit,
    "during the final authorization recheck": _during_final_authorization,
}


@pytest.mark.parametrize("how", ["cancel", "expire"])
@pytest.mark.parametrize("phase", PHASES)
def test_stopping_before_the_commit_saves_and_charges_nothing(world, api, provider, budgets, monkeypatch, phase, how):  # noqa: F811  (pytest fixtures)
    PHASES[phase](monkeypatch, budgets, how)
    space_id, conversation_id = setup(api)
    response = post(api, space_id, conversation_id, new_request_id())
    assert (response.status_code, response.json()) == (EXPECTED[how][0], {"error": EXPECTED[how][1]})
    assert_nothing_saved_or_charged(world, conversation_id)


# --- after the commit: saved and charged, recoverable by the same key ------------------------


@pytest.mark.parametrize("how", ["cancel", "expire"])
def test_stopping_after_the_commit_keeps_the_exchange_and_its_charge(world, api, provider, budgets, monkeypatch, how):  # noqa: F811  (pytest fixtures)
    original = spaces_router._replay

    def committed_then_stopped(result):
        stop(budgets, how)
        return original(result)
    monkeypatch.setattr(spaces_router, "_replay", committed_then_stopped)
    space_id, conversation_id = setup(api)
    key = new_request_id()
    assert post(api, space_id, conversation_id, key).status_code == 200
    monkeypatch.setattr(spaces_router, "_replay", original)
    assert_saved_and_charged_once(world, api, space_id, conversation_id, key, provider)


def test_an_uncertain_commit_acknowledgement_is_reconciled_by_the_same_key(world, provider, budgets, monkeypatch):  # noqa: F811  (pytest fixtures)
    """The database commits, but the acknowledgement never reaches the client
    (here: the handler fails right after the commit). A retry with the same
    key returns the saved exchange: no second generation, no second charge."""
    from test_spaces_chat import make_app  # noqa: F401
    original = spaces_router._replay
    failed = []

    def lost_acknowledgement(result):
        if not failed:
            failed.append(1)
            raise ConnectionResetError("acknowledgement lost after commit")
        return original(result)
    monkeypatch.setattr(spaces_router, "_replay", lost_acknowledgement)

    def new_budget():
        from kalillac_routing.request_budget import RequestBudget
        budget = RequestBudget(duration_seconds=30, max_model_attempts=6, max_search_attempts=6)
        budgets.append(budget)
        return budget
    application = build_app(world, generate=app._space_generate, lease_seconds=LEASE_SECONDS, new_budget=new_budget)
    with client(application) as api_:
        space_id, conversation_id = setup(api_)
        key = new_request_id()
    from fastapi.testclient import TestClient
    with TestClient(application, base_url="https://testserver", raise_server_exceptions=False) as raw:
        lost = post(raw, space_id, conversation_id, key)
        assert lost.status_code == 500                       # the client cannot tell whether it saved
        assert world.count(Message) == 2 and charged(world) == 1     # ... but it did
        retry = post(raw, space_id, conversation_id, key)
    assert retry.status_code == 200 and retry.json()["assistant_message"]["content"] == REPLY
    assert len(provider.payloads) == 1 and charged(world) == 1 and slots(world) == ["completed"]


def test_a_cancelled_handler_stops_the_save_running_in_its_worker(world, provider, monkeypatch):  # noqa: F811  (pytest fixtures)
    """Cancelling the request while its save is in progress cancels the
    shared budget; the worker's transaction sees it and rolls back."""
    from test_spaces_chat import make_app
    in_flush, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = repo.insert_exchange

    def blocking_insert(*args, **kwargs):
        rows = original(*args, **kwargs)
        in_flush.set()
        release.wait(10)
        return rows

    original_finish = repo.finish_send

    def finish(*args, **kwargs):
        try:
            return original_finish(*args, **kwargs)
        finally:
            finished.set()
    monkeypatch.setattr(repo, "insert_exchange", blocking_insert)
    monkeypatch.setattr(repo, "finish_send", finish)
    application = make_app(world)
    with client(application) as api_:
        space_id, conversation_id = setup(api_)

    async def scenario():
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as http:
            task = asyncio.create_task(http.post(
                f"/api/spaces/{space_id}/conversations/{conversation_id}/messages",
                json={"message": "Explain how encryption uses keys.", "client_request_id": new_request_id()},
                headers=headers("a")))
            assert await asyncio.to_thread(in_flush.wait, 10)
            task.cancel()                                   # the handler is cancelled mid-save
            await asyncio.gather(task, return_exceptions=True)
            release.set()
            assert await asyncio.to_thread(finished.wait, 10)

    asyncio.run(scenario())
    assert world.count(Message) == 0 and charged(world) == 0


# --- global lock order --------------------------------------------------------------------------


def test_the_save_locks_account_then_space_then_conversation(world, api, provider, monkeypatch):  # noqa: F811  (pytest fixtures)
    from kalillac_accounts.quota import UsageQuota
    order = []
    for name, target, attribute in (("account", UsageQuota, "lock_for_settlement"),
                                    ("space", repo, "lock_space"), ("conversation", repo, "lock_conversation")):
        original = getattr(target, attribute)

        def recorded(*args, _name=name, _original=original, **kwargs):
            order.append(_name)
            return _original(*args, **kwargs)
        monkeypatch.setattr(target, attribute, recorded)
    space_id, conversation_id = setup(api)
    order.clear()
    assert post(api, space_id, conversation_id, new_request_id()).status_code == 200
    # Transaction A takes the conversation only; Transaction B takes all three, in order.
    assert order == ["conversation", "account", "space", "conversation"]


def test_finish_send_source_takes_locks_in_the_global_order():
    tree = ast.parse(inspect.getsource(repo.finish_send))
    calls = [node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
             for node in ast.walk(tree) if isinstance(node, ast.Call)]
    positions = [calls.index(name) for name in ("lock_account", "lock_space", "lock_lease")]
    assert positions == sorted(positions)


def test_lock_statements_compile_to_the_intended_postgresql_locks(world):  # noqa: F811  (pytest fixtures)
    captured = []

    class Recorder:
        def scalar(self, statement):
            captured.append(str(statement.compile(dialect=postgresql.dialect())))
            return object()

    repo.lock_space(Recorder(), uuid.uuid4(), uuid.uuid4())
    repo.lock_conversation(Recorder(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    assert captured[0].endswith("FOR NO KEY UPDATE") and "FROM kalillac.spaces" in captured[0]
    assert captured[1].endswith("FOR UPDATE") and "FROM kalillac.conversations" in captured[1]


def test_no_space_or_conversation_path_locks_in_the_reverse_order():
    """begin_send and release_lease lock only the conversation; delete paths
    are single DELETE statements whose cascade follows Space -> conversation
    -> messages."""
    for function in (repo.begin_send, repo.release_lease, repo.delete_space, repo.delete_conversation):
        source = inspect.getsource(function)
        assert "lock_space(" not in source and "lock_account(" not in source, function.__name__


# --- fail-closed lease tokens ---------------------------------------------------------------------


@pytest.fixture
def lease_world(tmp_path):
    w = make_world(tmp_path)
    with client(build_app(w)) as c:
        space = c.post("/api/spaces", json={"name": "S"}, headers=headers("a")).json()["space"]
        conversation = c.post(f"/api/spaces/{space['id']}/conversations", json={},
                              headers=headers("a")).json()["conversation"]
    w.ids = (w.users["a"], uuid.UUID(space["id"]), uuid.UUID(conversation["id"]))
    yield w
    w.engine.dispose()


def tx(world, work):  # noqa: F811  (pytest fixtures)
    with world.factory() as session, session.begin():
        return work(session)


def finish(world, attempt):  # noqa: F811  (pytest fixtures)
    user, sid, cid = world.ids
    return tx(world, lambda s: repo.finish_send(s, user, sid, cid, attempt, uuid.uuid4(), "hi", "reply", world.clock()))


def lease(world):  # noqa: F811  (pytest fixtures)
    user, sid, cid = world.ids
    return tx(world, lambda s: (lambda c: (c.active_request_id, c.active_until))(s.get(Conversation, cid)))


@pytest.mark.parametrize("attempt", [None, "not-a-uuid", "", 0, str(uuid.uuid4()), uuid.uuid4().bytes])
def test_completion_rejects_missing_and_malformed_tokens(lease_world, attempt):
    with pytest.raises(repo.InvalidAttempt):
        finish(lease_world, attempt)
    assert lease_world.count(Message) == 0


def test_completion_never_matches_none_to_an_absent_lease(lease_world, monkeypatch):
    user, sid, cid = lease_world.ids
    # Even if a caller slipped past the type check, None never matches no lease.
    with pytest.raises(repo.Busy):
        tx(lease_world, lambda s: repo.lock_lease(s, user, sid, cid, uuid.uuid4()))       # valid token, no lease
    conversation = tx(lease_world, lambda s: s.get(Conversation, cid))
    assert repo._holds_lease(conversation, None) is False
    assert lease_world.count(Message) == 0


def test_completion_requires_the_active_token(lease_world):
    user, sid, cid = lease_world.ids
    key = uuid.uuid4()
    prepared = tx(lease_world, lambda s: repo.begin_send(s, user, sid, cid, key, "hi", lease_world.clock(), 60))
    for wrong in (uuid.uuid4(), key):                     # a stale token, and the client's own key
        with pytest.raises(repo.Busy):
            finish(lease_world, wrong)
    assert lease(lease_world)[0] == prepared.attempt and lease_world.count(Message) == 0
    done = tx(lease_world, lambda s: repo.finish_send(s, user, sid, cid, prepared.attempt, key, "hi", "reply",
                                                       lease_world.clock()))
    assert done.assistant_message["content"] == "reply" and lease(lease_world) == (None, None)


@pytest.mark.parametrize("attempt", [None, "not-a-uuid", str(uuid.uuid4()), "stale", "client-key", "no-lease"])
def test_cleanup_with_any_invalid_token_changes_nothing(lease_world, attempt):
    user, sid, cid = lease_world.ids
    key = uuid.uuid4()
    if attempt == "no-lease":
        assert tx(lease_world, lambda s: repo.release_lease(s, user, sid, cid, uuid.uuid4())) is True
        assert lease(lease_world) == (None, None)
        return
    prepared = tx(lease_world, lambda s: repo.begin_send(s, user, sid, cid, key, "hi", lease_world.clock(), 60))
    token = {"stale": uuid.uuid4(), "client-key": key}.get(attempt, attempt)
    assert tx(lease_world, lambda s: repo.release_lease(s, user, sid, cid, token)) is True
    assert lease(lease_world)[0] == prepared.attempt                    # still held
    assert tx(lease_world, lambda s: repo.release_lease(s, user, sid, cid, prepared.attempt)) is True
    assert lease(lease_world) == (None, None)                           # the real token releases it
