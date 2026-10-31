"""One send deadline, lease fencing and numeric query parsing.

Deadline: one RequestBudget bounds authorization, preparation, generation,
the recheck and persistence; it is checked again after every lock is
acquired, so a send that waited past its deadline, was cancelled, lost its
session, verification or ownership, or whose conversation was deleted while
it waited, commits nothing. Cleanup has its own small, separate allowance.

The finalization lock waits here are real: the file-backed SQLite test
database begins every transaction with BEGIN IMMEDIATE, so a helper thread
holding a write transaction genuinely blocks the send's save until it
commits. PostgreSQL row-lock ordering, lock_timeout and statement_timeout
are exercised only by the PostgreSQL CI job (see the review's PostgreSQL
section); SQLite results are not evidence of them.

Lease fencing: every lease carries a fresh server attempt token, never the
client's idempotency key, so an expired attempt can neither complete nor
release a retry's lease even when the retry reuses the same key.

The send budget runs on the world's controlled clock; no sleeps."""

from __future__ import annotations

import asyncio
import threading
import time
import uuid

import httpx
import pytest
from fastapi.responses import JSONResponse
from sqlalchemy import delete, event, update

from kalillac_db.models import AccountSession, Conversation, Message, User
from kalillac_spaces import repository as repo
from spaces_test_support import NOW, build_app, client, headers, make_world, new_request_id


@pytest.fixture
def world(tmp_path):
    w = make_world(tmp_path)
    yield w
    w.engine.dispose()


def make(world, generate, **kwargs):
    kwargs.setdefault("new_budget", lambda: world.budget(30))
    return build_app(world, generate=generate, **kwargs)


def new_space_and_conversation(api, key="a"):
    space = api.post("/api/spaces", json={"name": "S"}, headers=headers(key)).json()["space"]
    conversation = api.post(f"/api/spaces/{space['id']}/conversations", json={}, headers=headers(key)).json()
    return space["id"], conversation["conversation"]["id"]


def send(api, space_id, conversation_id, key=None, who="a"):
    return api.post(f"/api/spaces/{space_id}/conversations/{conversation_id}/messages",
                    json={"message": "hello", "client_request_id": key or new_request_id()}, headers=headers(who))


def lease(world, conversation_id):
    with world.factory() as session:
        row = session.get(Conversation, uuid.UUID(conversation_id))
        return None if row is None else (row.active_request_id, row.active_until)


# --- preparation past the deadline -------------------------------------------------------


def test_preparation_that_waits_past_the_deadline_takes_no_lease(world, monkeypatch):
    generated = []

    async def generate(request, *, finalize, **kwargs):
        generated.append(1)
        return await finalize("never")

    original = repo.begin_send

    def slow_begin(*args, **kwargs):
        world.advance(31)                     # the wait for the conversation lock outlasts the budget
        return original(*args, **kwargs)
    monkeypatch.setattr(repo, "begin_send", slow_begin)
    with client(make(world, generate)) as api:
        space_id, conversation_id = new_space_and_conversation(api)
        response = send(api, space_id, conversation_id)
    assert (response.status_code, response.json()) == (504, {"error": "request_timeout"})
    assert generated == [] and world.count(Message) == 0
    assert lease(world, conversation_id) == (None, None)


def test_a_spent_budget_starts_no_database_work(world):
    async def generate(request, *, finalize, **kwargs):
        return await finalize("never")
    spent = world.budget(30)
    world.advance(31)
    with client(make(world, generate, new_budget=lambda: spent)) as api:
        space_id, conversation_id = new_space_and_conversation(api)
        response = send(api, space_id, conversation_id)
    assert (response.status_code, response.json()) == (504, {"error": "request_timeout"})
    assert world.count(Message) == 0 and lease(world, conversation_id) == (None, None)


# --- finalization blocked on a real lock ----------------------------------------------------


class LockHolder(threading.Thread):
    """Holds a write transaction until the send's save is waiting on it, then
    applies `change` in that transaction and commits."""

    def __init__(self, world, change):
        super().__init__(daemon=True)
        self.world, self.change = world, change
        self.locked, self.save_waiting, self.done = threading.Event(), threading.Event(), threading.Event()
        self.error = None

    def run(self):
        try:
            with self.world.engine.connect() as connection:
                transaction = connection.begin()          # BEGIN IMMEDIATE: the write lock
                self.locked.set()
                assert self.save_waiting.wait(10), "the save never started waiting"
                self.change(connection)
                transaction.commit()
        except BaseException as error:                    # surfaced by the test
            self.error = error
        finally:
            self.done.set()


def blocked_finalize(world, change):
    """A generator whose save is blocked by a LockHolder applying change."""
    holder_box = {}

    def on_checkout(*args):
        # The save checks out its connection immediately before its
        # BEGIN IMMEDIATE, which then waits for the holder's commit.
        holder = holder_box.get("holder")
        if holder is not None and holder.locked.is_set() and threading.current_thread() is not holder:
            holder.save_waiting.set()

    event.listen(world.engine.pool, "checkout", on_checkout)

    async def generate(request, *, user_id, budget, finalize, **kwargs):
        holder = LockHolder(world, lambda connection: change(connection, budget, user_id))
        holder_box["holder"] = holder
        holder.start()
        assert await asyncio.to_thread(holder.locked.wait, 10)
        response = await finalize("A reply that must not be saved late.")
        assert await asyncio.to_thread(holder.done.wait, 10)
        if holder.error is not None:
            raise holder.error
        return response

    return generate


CHANGES = {
    "deadline passes": (lambda world: lambda c, budget, user: world.advance(31), 504, "request_timeout"),
    "send cancelled": (lambda world: lambda c, budget, user: budget.cancel(), 499, "request_cancelled"),
    "session revoked": (lambda world: lambda c, budget, user: c.execute(
        update(AccountSession).where(AccountSession.user_id == user).values(revoked_at=NOW)), 401, "not_authenticated"),
    "verification lost": (lambda world: lambda c, budget, user: c.execute(
        update(User).where(User.id == user).values(email_verified_at=None)), 403, "email_verification_required"),
    "account deactivated": (lambda world: lambda c, budget, user: c.execute(
        update(User).where(User.id == user).values(is_active=False)), 401, "not_authenticated"),
    "conversation deleted": (lambda world: lambda c, budget, user: c.execute(
        delete(Conversation).where(Conversation.user_id == user)), 404, "not_found"),
}


@pytest.mark.parametrize("change", CHANGES)
def test_a_save_blocked_on_a_lock_rechecks_everything_after_acquiring_it(world, change):
    make_change, status, code = CHANGES[change]
    with client(make(world, blocked_finalize(world, make_change(world)))) as api:
        space_id, conversation_id = new_space_and_conversation(api)
        response = send(api, space_id, conversation_id)
    assert (response.status_code, response.json()) == (status, {"error": code})
    assert world.count(Message) == 0                     # no late persistence
    if change == "conversation deleted":
        assert lease(world, conversation_id) is None
    else:
        assert lease(world, conversation_id) == (None, None)


def test_the_same_blocked_save_commits_when_nothing_changed(world):
    with client(make(world, blocked_finalize(world, lambda c, budget, user: None))) as api:
        space_id, conversation_id = new_space_and_conversation(api)
        response = send(api, space_id, conversation_id)
    assert response.status_code == 200 and world.count(Message) == 2


# --- bounded cleanup --------------------------------------------------------------------------


def test_cleanup_is_bounded_by_its_own_allowance(world, monkeypatch):
    release_started, release_may_finish = threading.Event(), threading.Event()
    original = repo.release_lease

    def hanging_release(*args, **kwargs):
        release_started.set()
        release_may_finish.wait(30)
        return original(*args, **kwargs)
    monkeypatch.setattr(repo, "release_lease", hanging_release)

    async def failing(request, **kwargs):
        return JSONResponse(status_code=503, content={"error": "model_provider_unavailable"})

    try:
        with client(make(world, failing, cleanup_seconds=0.05)) as api:
            space_id, conversation_id = new_space_and_conversation(api)
            started = time.monotonic()
            response = send(api, space_id, conversation_id)
            elapsed = time.monotonic() - started
    finally:
        release_may_finish.set()
    assert release_started.is_set()
    assert (response.status_code, response.json()) == (503, {"error": "model_provider_unavailable"})
    assert elapsed < 5                                    # not the 30 s the hung release would take
    assert world.count(Message) == 0


# --- lease fencing -----------------------------------------------------------------------------


def test_the_lease_token_is_server_generated_and_fences_stale_attempts(world):
    with client(make(world, None)) as api:
        space_id, conversation_id = new_space_and_conversation(api)
    user, sid, cid = world.users["a"], uuid.UUID(space_id), uuid.UUID(conversation_id)
    key = uuid.uuid4()

    def tx(work):
        with world.factory() as session, session.begin():
            return work(session)

    a = tx(lambda s: repo.begin_send(s, user, sid, cid, key, "hello", world.clock(), 60))
    assert a.attempt != key and lease(world, conversation_id)[0] == a.attempt
    world.advance(61)                                     # A's lease expires
    b = tx(lambda s: repo.begin_send(s, user, sid, cid, key, "hello", world.clock(), 60))   # retry, same key
    assert b.attempt not in (a.attempt, key)
    # Stale A can neither complete nor release B's lease.
    with pytest.raises(repo.Busy):
        tx(lambda s: repo.finish_send(s, user, sid, cid, a.attempt, key, "hello", "stale A", world.clock()))
    assert tx(lambda s: repo.release_lease(s, user, sid, cid, a.attempt)) is True
    assert lease(world, conversation_id)[0] == b.attempt
    # B completes normally; a plain same-key retry then replays B.
    done = tx(lambda s: repo.finish_send(s, user, sid, cid, b.attempt, key, "hello", "reply B", world.clock()))
    assert done.assistant_message["content"] == "reply B" and lease(world, conversation_id) == (None, None)
    replay = tx(lambda s: repo.begin_send(s, user, sid, cid, key, "hello", world.clock(), 60))
    assert isinstance(replay, repo.Replay) and replay.assistant_message["content"] == "reply B"
    # Stale A after B completed: still nothing.
    with pytest.raises(repo.Busy):
        tx(lambda s: repo.finish_send(s, user, sid, cid, a.attempt, key, "hello", "stale A", world.clock()))
    assert world.count(Message) == 2


def test_a_stale_request_cannot_complete_over_a_retry_with_the_same_key(world):
    key = new_request_id()
    state = {"calls": 0}
    a_waiting, b_done = asyncio.Event(), asyncio.Event()

    async def generate(request, *, finalize, **kwargs):
        state["calls"] += 1
        if state["calls"] == 1:                           # request A
            world.advance(61)                              # its lease expires while it generates
            a_waiting.set()
            await b_done.wait()
            return await finalize("stale A")
        response = await finalize("reply B")               # request B, same key
        b_done.set()
        return response

    # Budgets far longer than the lease, so only the lease decides.
    application = make(world, generate, new_budget=lambda: world.budget(3600))
    with client(application) as api:
        space_id, conversation_id = new_space_and_conversation(api)

    async def scenario():
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as http:
            url = f"/api/spaces/{space_id}/conversations/{conversation_id}/messages"
            body = {"message": "hello", "client_request_id": key}
            first = asyncio.create_task(http.post(url, json=body, headers=headers("a")))
            await a_waiting.wait()
            second = await http.post(url, json=body, headers=headers("a"))
            return await first, second

    first, second = asyncio.run(scenario())
    assert second.status_code == 200 and second.json()["assistant_message"]["content"] == "reply B"
    assert (first.status_code, first.json()) == (409, {"error": "conversation_busy"})
    assert world.count(Message) == 2 and lease(world, conversation_id) == (None, None)
    with client(application) as api:
        replay = send(api, space_id, conversation_id, key)
    assert replay.status_code == 200 and replay.json()["assistant_message"]["content"] == "reply B"
    assert state["calls"] == 2


# --- ASCII-only numeric query parameters -----------------------------------------------------


INVALID = ["¹", "²", "٣", "３", "१", "+5", "-1", " 5", "5 ", "1.5", "", "0", "51",
           "9999999", "1e2", "0x10", "١٠"]


@pytest.mark.parametrize("value", INVALID)
def test_limit_accepts_ascii_digits_only(world, value):
    with client(make(world, None)) as api:
        response = api.get("/api/spaces", params={"limit": value}, headers=headers("a"))
    assert (response.status_code, response.json()) == (422, {"error": "invalid_request"})


@pytest.mark.parametrize("value", INVALID[:-6] + ["", "0", "1e2", "1000001"])
def test_before_seq_accepts_ascii_digits_only(world, value):
    with client(make(world, None)) as api:
        space_id, conversation_id = new_space_and_conversation(api)
        response = api.get(f"/api/spaces/{space_id}/conversations/{conversation_id}",
                           params={"before_seq": value}, headers=headers("a"))
    assert (response.status_code, response.json()) == (422, {"error": "invalid_request"})


def test_numeric_boundaries_and_repeats(world):
    with client(make(world, None)) as api:
        space_id, conversation_id = new_space_and_conversation(api)
        for value in ("1", "50", "007"):
            assert api.get("/api/spaces", params={"limit": value}, headers=headers("a")).status_code == 200
        assert api.get("/api/spaces?limit=1&limit=2", headers=headers("a")).status_code == 422
        conversation = f"/api/spaces/{space_id}/conversations/{conversation_id}"
        # At most six ASCII digits: 999999 is the largest accepted value.
        for value in ("1", "999999"):
            assert api.get(conversation, params={"before_seq": value}, headers=headers("a")).status_code == 200
        assert api.get(conversation, params={"before_seq": "1000000"}, headers=headers("a")).status_code == 422
        assert api.get(conversation, params={"limit": "100"}, headers=headers("a")).status_code == 200
        assert api.get(conversation, params={"limit": "101"}, headers=headers("a")).status_code == 422
        assert api.get(conversation + "?before_seq=1&before_seq=2", headers=headers("a")).status_code == 422
