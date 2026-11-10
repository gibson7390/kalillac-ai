"""Durable answer availability is the Space quota boundary.

A Space send's account-quota reservation is completed and counted inside the
same database transaction that saves the exchange (UsageQuota
.settle_in_transaction, called from the router's finalize). So:

| # | What happens                                         | Answer durable | Charge              |
|---|------------------------------------------------------|----------------|---------------------|
| 1 | persistence and settlement commit                    | yes            | counted once        |
| 2 | persistence fails (database error in the save)       | no             | released, not counted |
| 3 | quota completion fails inside the save transaction   | no (rollback)  | released, not counted |
| 4 | budget expires immediately before persistence        | no             | released, not counted |
| 5 | budget expires or is cancelled after the commit      | yes            | counted once        |
| 6 | the HTTP response cannot be delivered after commit   | yes            | counted once        |
| 7 | idempotent replay after any durable state            | yes (stored)   | nothing more        |
| 8 | quota cleanup itself fails after a failed save       | no             | pending, never counted; lazily released |
| 9 | cleanup run against a settled exchange               | yes            | still counted once  |

A process crash before the commit leaves the reservation pending (released
by lazy expiry, never counted); after the commit the exchange and its count
are both durable. Free Chat's delivery-based settlement is untouched
(test_spaces_anonymous_unchanged.py). No real model, search or network."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import json

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

import app_fastapi_candidate as app
from kalillac_db.models import AccountUsageDaily, AccountUsageSlot, Message
from kalillac_db.repositories.quota import QuotaUnavailable, Reservation, release_slot
from kalillac_routing.request_budget import RequestBudget
from kalillac_spaces import repository as repo
from kalillac_spaces import router as spaces_router
from spaces_test_support import COOKIE, ORIGIN, build_app, client, headers, new_request_id, token_for
from test_anonymous_chat import settle
from test_spaces_chat import (  # noqa: F401  (pytest fixtures)
    LEASE_SECONDS,
    REPLY,
    lease_of,
    new_conversation,
    new_space,
    provider,
    world,
)


@pytest.fixture
def budgets():
    return []


@pytest.fixture
def api(world, budgets):  # noqa: F811
    def new_budget():
        budget = RequestBudget(duration_seconds=30, max_model_attempts=6, max_search_attempts=6)
        budgets.append(budget)
        return budget
    with client(build_app(world, generate=app._space_generate, lease_seconds=LEASE_SECONDS,
                          new_budget=new_budget)) as c:
        yield c


def charged(world):  # noqa: F811
    with world.factory() as session:
        return sum(row.successful_chats for row in session.scalars(select(AccountUsageDaily)))


def slots(world):  # noqa: F811
    settle()
    with world.factory() as session:
        return sorted(row.state for row in session.scalars(select(AccountUsageSlot)))


def post(api, space_id, conversation_id, key, message="Explain how encryption uses keys."):  # noqa: F811
    return api.post(f"/api/spaces/{space_id}/conversations/{conversation_id}/messages",
                    json={"message": message, "client_request_id": key}, headers=headers("a"))


def setup(api):  # noqa: F811
    space_id = new_space(api)
    return space_id, new_conversation(api, space_id)


def assert_durable_once(world, api, space_id, conversation_id, key, provider):  # noqa: F811
    """The exchange is stored, counted once, and a replay neither
    regenerates nor charges again."""
    assert world.count(Message) == 2
    assert charged(world) == 1 and slots(world) == ["completed"]
    calls = len(provider.payloads)
    replay = post(api, space_id, conversation_id, key)
    assert replay.status_code == 200 and replay.json()["assistant_message"]["content"] == REPLY
    assert len(provider.payloads) == calls
    assert charged(world) == 1 and slots(world) == ["completed"]


def assert_nothing_consumed(world, conversation_id):  # noqa: F811
    assert world.count(Message) == 0
    assert charged(world) == 0
    assert set(slots(world)) <= {"released"}
    assert lease_of(world, conversation_id) == (None, None)


# 1 ---------------------------------------------------------------------------------------


def test_1_success_is_saved_and_counted_exactly_once(world, api, provider):  # noqa: F811  (pytest fixtures)
    space_id, conversation_id = setup(api)
    key = new_request_id()
    response = post(api, space_id, conversation_id, key)
    assert response.status_code == 200
    assert_durable_once(world, api, space_id, conversation_id, key, provider)


# 2, 3, 4 ---------------------------------------------------------------------------------


def test_2_persistence_failure_consumes_nothing(world, api, provider, monkeypatch):  # noqa: F811  (pytest fixtures)
    def broken(*args, **kwargs):
        raise OperationalError("INSERT", {}, Exception("synthetic database failure"))
    monkeypatch.setattr(repo, "finish_send", broken)
    space_id, conversation_id = setup(api)
    response = post(api, space_id, conversation_id, new_request_id())
    assert (response.status_code, response.json()) == (503, {"error": "database_unavailable"})
    assert_nothing_consumed(world, conversation_id)


def test_3_quota_completion_failure_rolls_the_save_back(world, api, provider, monkeypatch):  # noqa: F811  (pytest fixtures)
    from kalillac_accounts.quota import UsageQuota

    def refuse(self, session, admission):
        raise QuotaUnavailable()
    monkeypatch.setattr(UsageQuota, "settle_in_transaction", refuse)
    space_id, conversation_id = setup(api)
    response = post(api, space_id, conversation_id, new_request_id())
    assert (response.status_code, response.json()) == (503, {"error": "service_unavailable"})
    assert_nothing_consumed(world, conversation_id)


def test_4_timeout_immediately_before_persistence_saves_and_charges_nothing(world, api, provider, budgets,  # noqa: F811  (pytest fixtures)
                                                                            monkeypatch):
    original = repo.finish_send

    def expire_then_finish(*args, **kwargs):
        budgets[-1]._deadline = 0.0          # the deadline passes while the save is waiting
        return original(*args, **kwargs)
    monkeypatch.setattr(repo, "finish_send", expire_then_finish)
    space_id, conversation_id = setup(api)
    response = post(api, space_id, conversation_id, new_request_id())
    assert (response.status_code, response.json()) == (504, {"error": "request_timeout"})
    assert_nothing_consumed(world, conversation_id)


# 5, 6 ------------------------------------------------------------------------------------


@pytest.mark.parametrize("after_commit", ["timeout", "cancel"])
def test_5_timeout_or_cancellation_after_the_commit_keeps_one_charge(world, api, provider, budgets, monkeypatch,  # noqa: F811  (pytest fixtures)
                                                                     after_commit):
    original = spaces_router._replay

    def committed_then_stopped(result):
        # finalize's transaction has committed; the send's budget now ends.
        if after_commit == "timeout":
            budgets[-1]._deadline = 0.0
        else:
            budgets[-1].cancel()
        return original(result)
    monkeypatch.setattr(spaces_router, "_replay", committed_then_stopped)
    space_id, conversation_id = setup(api)
    key = new_request_id()
    response = post(api, space_id, conversation_id, key)
    monkeypatch.setattr(spaces_router, "_replay", original)
    # The durable answer is delivered and charged once; nothing refunds it.
    assert response.status_code == 200 and response.json()["assistant_message"]["content"] == REPLY
    assert_durable_once(world, api, space_id, conversation_id, key, provider)


def test_6_response_delivery_failure_after_the_commit_keeps_one_charge(world, api, provider):  # noqa: F811  (pytest fixtures)
    space_id, conversation_id = setup(api)
    key = new_request_id()
    body = json.dumps({"message": "Explain how encryption uses keys.", "client_request_id": key}).encode()
    application = api.app
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "https",
        "path": f"/api/spaces/{space_id}/conversations/{conversation_id}/messages", "raw_path": b"",
        "query_string": b"", "root_path": "", "server": ("testserver", 443), "client": ("127.0.0.1", 1),
        "headers": [(b"host", b"testserver"), (b"origin", ORIGIN.encode()), (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"cookie", f"{COOKIE}={token_for('a')}".encode())],
    }
    delivered = []

    async def receive():
        if not delivered:
            delivered.append("body")
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Event().wait()                 # the client never disconnects on its own

    async def send(message):
        if message["type"] == "http.response.body":
            raise ConnectionResetError("synthetic delivery failure")

    async def call():
        with pytest.raises(ConnectionResetError):
            await application(scope, receive, send)

    asyncio.run(call())
    assert_durable_once(world, api, space_id, conversation_id, key, provider)


# 8, 9 ------------------------------------------------------------------------------------


def test_8_quota_cleanup_failure_never_counts_and_expires(world, api, provider, monkeypatch):  # noqa: F811  (pytest fixtures)
    from kalillac_accounts.quota import UsageQuota

    def broken_save(*args, **kwargs):
        raise OperationalError("INSERT", {}, Exception("synthetic database failure"))

    def broken_release(self, admission):
        raise QuotaUnavailable()
    original_save, original_release = repo.finish_send, UsageQuota.release
    monkeypatch.setattr(repo, "finish_send", broken_save)
    monkeypatch.setattr(UsageQuota, "release", broken_release)
    space_id, conversation_id = setup(api)
    assert post(api, space_id, conversation_id, new_request_id()).status_code == 503
    # Cleanup failed: the reservation is still pending -- holding capacity,
    # never counted as a completed chat.
    assert world.count(Message) == 0 and charged(world) == 0 and slots(world) == ["pending"]
    monkeypatch.setattr(repo, "finish_send", original_save)
    monkeypatch.setattr(UsageQuota, "release", original_release)

    # After its lease lifetime the stale reservation is released lazily by
    # the next admission (which reuses the released slot), and only the new
    # exchange is counted.
    world.advance(timedelta(hours=1).total_seconds())
    key = new_request_id()
    assert post(api, space_id, conversation_id, key).status_code == 200
    assert charged(world) == 1 and slots(world) == ["completed"]


def test_9_cleanup_after_a_settled_exchange_never_refunds(world, api, provider):  # noqa: F811  (pytest fixtures)
    space_id, conversation_id = setup(api)
    key = new_request_id()
    assert post(api, space_id, conversation_id, key).status_code == 200
    with world.factory() as session, session.begin():
        row = session.scalar(select(AccountUsageSlot))
        reservation = Reservation(row.user_id, row.usage_date, row.slot, row.expires_at, 20)
        release_slot(session, reservation)                  # a late or duplicate cleanup
    assert_durable_once(world, api, space_id, conversation_id, key, provider)


def test_concurrent_same_key_sends_charge_once(world, provider):  # noqa: F811
    import threading
    from test_spaces_chat import concurrently, make_app, post_send
    application = make_app(world)
    with client(application) as c:
        space_id = new_space(c)
        conversation_id = new_conversation(c, space_id)
    provider.hold = threading.Event()
    key = new_request_id()

    async def same_key(http):
        return await post_send(space_id, conversation_id, "Explain how encryption uses keys.", key)(http)

    first, second = asyncio.run(concurrently(
        application, post_send(space_id, conversation_id, "Explain how encryption uses keys.", key), same_key,
        provider))
    assert first.status_code == 200
    assert (second.status_code, second.json()) == (409, {"error": "conversation_busy"})
    assert world.count(Message) == 2 and charged(world) == 1 and slots(world) == ["completed"]
    assert len(provider.payloads) == 1
