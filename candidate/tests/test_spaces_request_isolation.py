"""Request-local isolation of the Space binding.

Several requests run at the same time through the real /api/chat lifecycle:
an anonymous chat beside a Space send, two Spaces of one account, and two
accounts. A barrier in the mocked model transport only opens when every
request is inside it, so the requests genuinely overlap. No request may see
another's instructions, history, ownership, lease or workspace block, and
nothing of a Space send -- finished or failed -- is left behind for the next
request. The binding is a per-request object and the workspace block a
ContextVar set only inside that request's worker; no process-global holds
either. No real model, search or network call is made."""

from __future__ import annotations

import asyncio
import json
import threading
import uuid

import httpx
import pytest
from sqlalchemy import select

import app_fastapi_candidate as app
from kalillac_db.models import Conversation, Message
from kalillac_spaces.context import WORKSPACE_PREAMBLE
from kalillac_spaces.facts import SPACE_PRODUCT_FACTS
from spaces_test_support import headers, new_request_id
from test_spaces_chat import (  # noqa: F401  (pytest fixtures)
    REPLY,
    api,
    lease_of,
    make_app,
    new_conversation,
    new_space,
    provider,
    send,
    world,
)

QUESTION = "Explain how encryption uses keys."


def completed(text):
    return {"status": "completed", "output": [{"type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": text}]}]}


@pytest.fixture
def overlapping(provider, monkeypatch):  # noqa: F811  (pytest fixtures)
    """Replace the model transport with one that holds every call until
    `parties` calls are inside it, then answers each by the marker its own
    payload carries."""
    monkeypatch.setattr(app, "_chat_semaphore", None)
    state = {}

    def arm(parties, replies, fail_marker=None):
        state["barrier"] = threading.Barrier(parties, timeout=5)
        state["replies"] = replies
        state["fail"] = fail_marker

    def openai(payload, budget, timeout, request_selected):
        text = json.dumps(payload)
        provider.payloads.append(json.loads(text))
        barrier = state.get("barrier")
        if barrier is not None:
            barrier.wait()
        if state.get("fail") and state["fail"] in text:
            raise RuntimeError("synthetic provider failure")
        matches = [reply for marker, reply in state.get("replies", {}).items() if marker in text]
        assert len(matches) == 1, "each payload must carry exactly one request's marker"
        return completed(matches[0])

    monkeypatch.setattr(app, "_post_openai_bounded", openai)
    return arm


def payload_with(provider, marker):  # noqa: F811  (pytest fixtures)
    found = [p for p in provider.payloads if marker in json.dumps(p)]
    assert len(found) == 1, marker
    return json.dumps(found[0])


def seed(api, overlapping, instructions, history_marker, key="a", name="Work"):  # noqa: F811  (pytest fixtures)
    """A Space with instructions and one completed exchange carrying
    history_marker, saved sequentially before the concurrent part."""
    space_id = new_space(api, instructions, key=key, name=name)
    conversation_id = new_conversation(api, space_id, key=key)
    overlapping(1, {history_marker: f"Earlier answer {history_marker}."})
    response = send(api, space_id, conversation_id, f"{QUESTION} ({history_marker})", key=key)
    assert response.status_code == 200, response.text
    return space_id, conversation_id


def space_post(space_id, conversation_id, key="a"):
    async def go(http):
        return await http.post(
            f"/api/spaces/{space_id}/conversations/{conversation_id}/messages",
            json={"message": QUESTION, "client_request_id": new_request_id()}, headers=headers(key))
    return go


def anonymous_post(history_marker):
    async def go(http):
        return await http.post("/api/chat", json={"message": QUESTION, "history": [
            {"role": "user", "content": f"Earlier question {history_marker}"},
            {"role": "assistant", "content": f"Earlier answer {history_marker}."},
        ]})
    return go


async def run_together(*calls):
    """Each call is (asgi_app, coroutine_factory); all run concurrently."""
    clients = {}
    try:
        for application, _ in calls:
            if id(application) not in clients:
                clients[id(application)] = httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=application), base_url="https://testserver")
        return await asyncio.gather(*(go(clients[id(application)]) for application, go in calls))
    finally:
        for c in clients.values():
            await c.aclose()


def saved(world, conversation_id):  # noqa: F811  (pytest fixtures)
    with world.factory() as session:
        rows = session.scalars(select(Message).where(Message.conversation_id == uuid.UUID(conversation_id))
                               .order_by(Message.seq)).all()
        return [(m.role, m.content, m.user_id) for m in rows]


def assert_nothing_left_behind():
    assert app._SPACE_REQUEST.get() is None
    assert app._session_locks == {}
    for state in app._SPACE_SEARCH_STATE.values():
        assert set(state) == {"memory", "search_times"} and state["memory"] == []


def test_anonymous_chat_beside_a_space_send_shares_nothing(world, api, provider, overlapping):  # noqa: F811  (pytest fixtures)
    space_id, conversation_id = seed(api, overlapping, "INSTR-SPACE-A", "HIST-SPACE-A")
    overlapping(2, {"HIST-SPACE-A": "REPLY-SPACE", "HIST-ANON": "REPLY-ANON"})

    space_response, anonymous_response = asyncio.run(run_together(
        (make_app(world), space_post(space_id, conversation_id)),
        (app.api, anonymous_post("HIST-ANON")),
    ))

    assert space_response.status_code == 200, space_response.text
    assert anonymous_response.status_code == 200, anonymous_response.text
    assert space_response.json()["assistant_message"]["content"] == "REPLY-SPACE"
    assert anonymous_response.json()["reply"] == "REPLY-ANON"

    anonymous = payload_with(provider, "HIST-ANON")
    space = [json.dumps(p) for p in provider.payloads[1:] if "HIST-SPACE-A" in json.dumps(p)]
    assert len(space) == 1
    # The anonymous request carries no workspace block, instructions or
    # Space history; the Space request carries no anonymous history.
    for leaked in ("INSTR-SPACE-A", "HIST-SPACE-A", WORKSPACE_PREAMBLE.split("\n")[0]):
        assert leaked not in anonymous
    assert "INSTR-SPACE-A" in space[0] and "HIST-ANON" not in space[0]
    # Mode isolation: the Space facts reach the Space request only.
    assert json.dumps(SPACE_PRODUCT_FACTS)[1:-1] in space[0]
    assert "KALILLAC SPACES" not in anonymous

    # The anonymous chat got its own RAM session; the Space touched none.
    assert list(app.SESSION_STATE) == [anonymous_response.json()["session_id"]]
    assert lease_of(world, conversation_id) == (None, None)
    assert_nothing_left_behind()


def test_two_spaces_of_one_account_at_once(world, api, provider, overlapping):  # noqa: F811  (pytest fixtures)
    a = seed(api, overlapping, "INSTR-SPACE-ONE", "HIST-SPACE-ONE", name="One")
    b = seed(api, overlapping, "INSTR-SPACE-TWO", "HIST-SPACE-TWO", name="Two")
    overlapping(2, {"HIST-SPACE-ONE": "REPLY-ONE", "HIST-SPACE-TWO": "REPLY-TWO"})
    application = make_app(world)

    one, two = asyncio.run(run_together((application, space_post(*a)), (application, space_post(*b))))

    assert (one.status_code, two.status_code) == (200, 200), (one.text, two.text)
    assert one.json()["assistant_message"]["content"] == "REPLY-ONE"
    assert two.json()["assistant_message"]["content"] == "REPLY-TWO"
    concurrent = [json.dumps(p) for p in provider.payloads[2:]]
    one_payload = next(p for p in concurrent if "HIST-SPACE-ONE" in p)
    two_payload = next(p for p in concurrent if "HIST-SPACE-TWO" in p)
    assert "INSTR-SPACE-ONE" in one_payload and "INSTR-SPACE-TWO" not in one_payload
    assert "INSTR-SPACE-TWO" in two_payload and "INSTR-SPACE-ONE" not in two_payload
    assert "HIST-SPACE-TWO" not in one_payload and "HIST-SPACE-ONE" not in two_payload
    assert all(json.dumps(SPACE_PRODUCT_FACTS)[1:-1] in p for p in (one_payload, two_payload))
    assert [c for _, c, _ in saved(world, a[1])][-1] == "REPLY-ONE"
    assert [c for _, c, _ in saved(world, b[1])][-1] == "REPLY-TWO"
    assert lease_of(world, a[1]) == lease_of(world, b[1]) == (None, None)
    assert dict(app.SESSION_STATE) == {}
    assert_nothing_left_behind()


def test_two_accounts_at_once(world, api, provider, overlapping):  # noqa: F811  (pytest fixtures)
    a = seed(api, overlapping, "INSTR-ACCOUNT-A", "HIST-ACCOUNT-A", key="a")
    b = seed(api, overlapping, "INSTR-ACCOUNT-B", "HIST-ACCOUNT-B", key="b")
    overlapping(2, {"HIST-ACCOUNT-A": "REPLY-A", "HIST-ACCOUNT-B": "REPLY-B"})
    application = make_app(world)

    first, second = asyncio.run(run_together(
        (application, space_post(*a, key="a")), (application, space_post(*b, key="b"))))

    assert (first.status_code, second.status_code) == (200, 200), (first.text, second.text)
    concurrent = [json.dumps(p) for p in provider.payloads[2:]]
    a_payload = next(p for p in concurrent if "HIST-ACCOUNT-A" in p)
    b_payload = next(p for p in concurrent if "HIST-ACCOUNT-B" in p)
    assert "INSTR-ACCOUNT-B" not in a_payload and "HIST-ACCOUNT-B" not in a_payload
    assert "INSTR-ACCOUNT-A" not in b_payload and "HIST-ACCOUNT-A" not in b_payload
    assert all(json.dumps(SPACE_PRODUCT_FACTS)[1:-1] in p for p in (a_payload, b_payload))
    # Each exchange is saved under its own owner only.
    assert {owner for _, _, owner in saved(world, a[1])} == {world.users["a"]}
    assert {owner for _, _, owner in saved(world, b[1])} == {world.users["b"]}
    assert saved(world, a[1])[-1][1] == "REPLY-A" and saved(world, b[1])[-1][1] == "REPLY-B"
    with world.factory() as session:
        owners = {c.id: c.user_id for c in session.scalars(select(Conversation))}
    assert owners == {uuid.UUID(a[1]): world.users["a"], uuid.UUID(b[1]): world.users["b"]}
    # Each account has its own search window.
    assert set(app._SPACE_SEARCH_STATE) == {world.users["a"], world.users["b"]}
    assert app._SPACE_SEARCH_STATE[world.users["a"]] is not app._SPACE_SEARCH_STATE[world.users["b"]]
    assert_nothing_left_behind()


def test_a_failed_space_send_leaves_nothing_for_the_next_anonymous_chat(world, api, provider, overlapping):  # noqa: F811  (pytest fixtures)
    space_id, conversation_id = seed(api, overlapping, "INSTR-FAILING", "HIST-FAILING")
    overlapping(1, {"HIST-FAILING": "never used"}, fail_marker="HIST-FAILING")
    failed = send(api, space_id, conversation_id, QUESTION)
    assert (failed.status_code, failed.json()) == (503, {"error": "model_provider_unavailable"})
    assert len(saved(world, conversation_id)) == 2           # only the seeded exchange
    assert lease_of(world, conversation_id) == (None, None)
    assert_nothing_left_behind()

    overlapping(1, {"HIST-AFTER": "REPLY-AFTER"})
    response = asyncio.run(run_together((app.api, anonymous_post("HIST-AFTER"))))[0]

    assert response.status_code == 200 and response.json()["reply"] == "REPLY-AFTER"
    anonymous = payload_with(provider, "HIST-AFTER")
    assert "INSTR-FAILING" not in anonymous and "HIST-FAILING" not in anonymous
    assert "KALILLAC SPACES" not in anonymous
    assert WORKSPACE_PREAMBLE.split("\n")[0] not in anonymous
    systems = [item["content"] for item in json.loads(anonymous)["input"] if item["role"] == "system"]
    assert systems[0] == app.CURRENT_INTERFACE_CAPABILITIES
    assert not any(WORKSPACE_PREAMBLE in s for s in systems)
    assert_nothing_left_behind()
