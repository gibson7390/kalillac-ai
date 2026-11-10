"""Spaces API: access gates, Space and conversation CRUD, instructions,
limits, ownership isolation, CSRF protection, strict bodies, pagination and
response headers. Provider-free (a deterministic local generator); the
persistent send through the real chat lifecycle is in test_spaces_chat.py."""

from __future__ import annotations

from datetime import timedelta
import uuid

import pytest
from sqlalchemy import select, update

from kalillac_db.models import AccountSession, Conversation, Message, Space
from kalillac_spaces import config as spaces_config
from kalillac_spaces import repository as repo
from spaces_test_support import (
    ORIGIN,
    build_app,
    client,
    headers,
    make_world,
    new_request_id,
)


@pytest.fixture
def world(tmp_path):
    w = make_world(tmp_path)
    yield w
    w.engine.dispose()


@pytest.fixture
def api(world):
    with client(build_app(world)) as c:
        yield c


def create_space(api, key="a", name="Research", instructions=""):
    response = api.post("/api/spaces", json={"name": name, "instructions": instructions}, headers=headers(key))
    assert response.status_code == 201, response.text
    return response.json()["space"]


def create_conversation(api, space_id, key="a", **body):
    response = api.post(f"/api/spaces/{space_id}/conversations", json=body, headers=headers(key))
    assert response.status_code == 201, response.text
    return response.json()["conversation"]


def send(api, space_id, conversation_id, message, key="a", request_id=None):
    return api.post(
        f"/api/spaces/{space_id}/conversations/{conversation_id}/messages",
        json={"message": message, "client_request_id": request_id or new_request_id()},
        headers=headers(key),
    )


# --- access gates ---------------------------------------------------------------------


def test_signed_out_and_forged_sessions_are_401(api):
    for cookie_headers in ({}, {"Cookie": "__Host-kalillac_account=not-a-real-session"}):
        response = api.get("/api/spaces", headers=cookie_headers)
        assert response.status_code == 401 and response.json() == {"error": "not_authenticated"}
        assert response.headers["cache-control"] == "no-store"


def test_expired_and_revoked_sessions_are_401(api, world):
    with world.factory() as session, session.begin():
        session.execute(update(AccountSession).where(AccountSession.user_id == world.users["a"])
                        .values(revoked_at=world.clock()))
    assert api.get("/api/spaces", headers=headers("a")).status_code == 401
    world.advance(15 * 86400)                                # b's session has expired
    assert api.get("/api/spaces", headers=headers("b")).status_code == 401


def test_non_allowlisted_account_cannot_discover_spaces(api, world):
    # Indistinguishable from an unknown route or object: 404 everywhere,
    # including on the mutating routes, with no write.
    space = create_space(api, "a")
    for method, path in (
        ("GET", "/api/spaces"),
        ("POST", "/api/spaces"),
        ("GET", f"/api/spaces/{space['id']}"),
        ("DELETE", f"/api/spaces/{space['id']}"),
        ("GET", f"/api/spaces/{space['id']}/conversations"),
    ):
        response = api.request(method, path, json={"name": "x"} if method == "POST" else None, headers=headers("c"))
        assert response.status_code == 404 and response.json() == {"error": "not_found"}, (method, path)
    assert world.count(Space) == 1


def test_unverified_allowlisted_account_must_verify(api, world):
    response = api.get("/api/spaces", headers=headers("d"))
    assert response.status_code == 403 and response.json() == {"error": "email_verification_required"}
    response = api.post("/api/spaces", json={"name": "x"}, headers=headers("d"))
    assert response.status_code == 403
    assert world.count(Space) == 0


def test_allowlisted_verified_account_has_access(api):
    response = api.get("/api/spaces", headers=headers("a"))
    assert response.status_code == 200 and response.json() == {"spaces": [], "next_cursor": None}
    assert response.headers["cache-control"] == "no-store" and response.headers["vary"] == "Cookie"


def test_empty_allowlist_grants_nobody(world):
    config = spaces_config.SpacesConfig(beta_account_ids=frozenset(), site_origin=ORIGIN)
    with client(build_app(world, config=config)) as api:
        for key in ("a", "b", "c", "d"):
            assert api.get("/api/spaces", headers=headers(key)).status_code == 404, key


# --- Spaces CRUD and instructions -------------------------------------------------------


def test_space_create_read_list_rename_instructions_delete(api, world):
    space = create_space(api, name="  Thesis  ", instructions="Answer in British English.")
    assert space["name"] == "Thesis" and space["instructions"] == "Answer in British English."
    assert space["version"] == 1 and space["instructions_updated_at"] is not None
    assert set(space) == {"id", "name", "instructions", "instructions_updated_at", "version", "created_at", "updated_at"}

    read = api.get(f"/api/spaces/{space['id']}", headers=headers("a")).json()["space"]
    assert read == space
    listed = api.get("/api/spaces", headers=headers("a")).json()
    assert listed == {"spaces": [{"id": space["id"], "name": "Thesis", "conversation_count": 0,
                                  "updated_at": space["updated_at"]}], "next_cursor": None}

    world.advance(1)
    renamed = api.patch(f"/api/spaces/{space['id']}", json={"name": "Thesis 2", "expected_version": 1},
                        headers=headers("a"))
    assert renamed.status_code == 200 and renamed.json()["space"]["version"] == 2
    instructions = api.put(f"/api/spaces/{space['id']}/instructions",
                           json={"instructions": "", "expected_version": 2}, headers=headers("a"))
    assert instructions.status_code == 200
    assert instructions.json()["space"]["instructions"] == "" and instructions.json()["space"]["version"] == 3

    assert api.delete(f"/api/spaces/{space['id']}", headers=headers("a")).status_code == 204
    assert api.get(f"/api/spaces/{space['id']}", headers=headers("a")).status_code == 404
    assert world.count(Space) == 0


def test_version_conflicts(api):
    space = create_space(api)
    first = api.patch(f"/api/spaces/{space['id']}", json={"name": "One", "expected_version": 1}, headers=headers("a"))
    second = api.patch(f"/api/spaces/{space['id']}", json={"name": "Two", "expected_version": 1}, headers=headers("a"))
    assert first.status_code == 200 and second.status_code == 409
    assert second.json() == {"error": "version_conflict"}
    assert api.get(f"/api/spaces/{space['id']}", headers=headers("a")).json()["space"]["name"] == "One"
    stale = api.put(f"/api/spaces/{space['id']}/instructions",
                    json={"instructions": "x", "expected_version": 1}, headers=headers("a"))
    assert stale.status_code == 409


@pytest.mark.parametrize("body", [
    {"name": ""}, {"name": "   "}, {"name": "x" * 81}, {"name": 7},
    {"name": "ok", "instructions": "x" * 8001}, {"name": "ok", "instructions": None},
])
def test_space_field_limits(api, world, body):
    response = api.post("/api/spaces", json=body, headers=headers("a"))
    assert response.status_code == 422 and response.json() == {"error": "invalid_request"}
    assert world.count(Space) == 0


def test_instruction_limit_is_exactly_8000(api):
    space = create_space(api, instructions="x" * 8000)
    assert len(space["instructions"]) == 8000
    too_long = api.put(f"/api/spaces/{space['id']}/instructions",
                       json={"instructions": "y" * 8001, "expected_version": 1}, headers=headers("a"))
    assert too_long.status_code == 422


def test_space_limit_of_fifty_per_account(api, world):
    for index in range(50):
        create_space(api, name=f"Space {index}")
    over = api.post("/api/spaces", json={"name": "One too many"}, headers=headers("a"))
    assert over.status_code == 409 and over.json() == {"error": "space_limit_reached"}
    assert world.count(Space, user_id=world.users["a"]) == 50
    # Another account's ceiling is its own.
    assert api.post("/api/spaces", json={"name": "B's first"}, headers=headers("b")).status_code == 201


def test_conversation_limit_of_five_hundred_per_space(api, world, monkeypatch):
    # The ceiling is read from config; prove it applies at the boundary
    # without creating 500 rows.
    monkeypatch.setattr(repo, "MAX_CONVERSATIONS_PER_SPACE", 3)
    space = create_space(api)
    for _ in range(3):
        create_conversation(api, space["id"])
    over = api.post(f"/api/spaces/{space['id']}/conversations", json={}, headers=headers("a"))
    assert over.status_code == 409 and over.json() == {"error": "conversation_limit_reached"}
    assert spaces_config.MAX_CONVERSATIONS_PER_SPACE == 500


def test_message_limit_of_four_hundred_per_conversation(api, world, monkeypatch):
    monkeypatch.setattr(repo, "MAX_MESSAGES_PER_CONVERSATION", 4)
    space = create_space(api)
    conversation = create_conversation(api, space["id"])
    for index in range(2):
        assert send(api, space["id"], conversation["id"], f"message {index}").status_code == 200
    full = send(api, space["id"], conversation["id"], "third")
    assert full.status_code == 409 and full.json() == {"error": "conversation_full"}
    assert world.count(Message) == 4
    assert spaces_config.MAX_MESSAGES_PER_CONVERSATION == 400


# --- strict bodies, mass assignment and client-supplied identity --------------------------


@pytest.mark.parametrize("extra", [
    {"user_id": "00000000-0000-4000-8000-000000000000"}, {"id": "x"}, {"version": 9},
    {"created_at": "2020-01-01T00:00:00+00:00"}, {"space_id": "x"},
])
def test_unknown_and_server_owned_fields_are_rejected(api, world, extra):
    response = api.post("/api/spaces", json={"name": "Mine", **extra}, headers=headers("a"))
    assert response.status_code == 422 and response.json() == {"error": "invalid_request"}
    assert world.count(Space) == 0


def test_account_identity_is_never_taken_from_client_input(api, world):
    a_space = create_space(api, "a")
    # Query string and body cannot select an account.
    assert api.get(f"/api/spaces?user_id={world.users['b']}", headers=headers("a")).status_code == 422
    response = api.post("/api/spaces", json={"name": "Hijack", "user_id": str(world.users["b"])}, headers=headers("a"))
    assert response.status_code == 422
    # B's session always acts as B: A's Space is not found for B.
    assert api.get(f"/api/spaces/{a_space['id']}", headers=headers("b")).status_code == 404
    with world.factory() as session:
        assert session.scalars(select(Space.user_id)).all() == [world.users["a"]]


@pytest.mark.parametrize("path,body", [
    ("/api/spaces/{sid}/conversations", {"title": "x", "message_count": 9}),
    ("/api/spaces/{sid}/conversations/{cid}/messages",
     {"message": "hi", "client_request_id": "00000000-0000-4000-8000-000000000001", "role": "system"}),
    ("/api/spaces/{sid}/conversations/{cid}/messages",
     {"message": "hi", "client_request_id": "00000000-0000-4000-8000-000000000001", "session_id": "anon"}),
    ("/api/spaces/{sid}/conversations/{cid}/messages",
     {"message": "hi", "client_request_id": "00000000-0000-4000-8000-000000000001", "history": []}),
])
def test_conversation_and_send_bodies_reject_extra_fields(api, world, path, body):
    space = create_space(api)
    conversation = create_conversation(api, space["id"])
    response = api.post(path.format(sid=space["id"], cid=conversation["id"]), json=body, headers=headers("a"))
    assert response.status_code == 422
    assert world.count(Message) == 0


@pytest.mark.parametrize("message,code", [("", "empty_message"), ("   ", "empty_message"), ("x" * 4001, "message_too_long")])
def test_send_message_validation(api, message, code):
    space = create_space(api)
    conversation = create_conversation(api, space["id"])
    response = send(api, space["id"], conversation["id"], message)
    assert response.status_code == 422 and response.json() == {"error": code}


@pytest.mark.parametrize("request_id", ["not-a-uuid", "", "{00000000-0000-4000-8000-000000000001}", 7])
def test_send_requires_a_uuid_idempotency_key(api, request_id):
    space = create_space(api)
    conversation = create_conversation(api, space["id"])
    response = api.post(f"/api/spaces/{space['id']}/conversations/{conversation['id']}/messages",
                        json={"message": "hi", "client_request_id": request_id}, headers=headers("a"))
    assert response.status_code == 422 and response.json() == {"error": "invalid_request"}


# --- CSRF and request shape --------------------------------------------------------------


@pytest.mark.parametrize("extra_headers,status,code", [
    ({"Origin": "https://evil.example"}, 403, "origin_rejected"),
    ({"Origin": "null"}, 403, "origin_rejected"),
    ({"Origin": None}, 403, "origin_rejected"),
    ({"Origin": None, "Sec-Fetch-Site": "cross-site"}, 403, "origin_rejected"),
    ({"Origin": None, "Sec-Fetch-Site": "same-site"}, 403, "origin_rejected"),
])
def test_cross_origin_mutations_are_refused(api, world, extra_headers, status, code):
    space = create_space(api)
    origin = extra_headers.pop("Origin")
    request_headers = headers("a", origin=origin, **extra_headers)
    for method, path, body in (
        ("POST", "/api/spaces", {"name": "x"}),
        ("PATCH", f"/api/spaces/{space['id']}", {"name": "x", "expected_version": 1}),
        ("PUT", f"/api/spaces/{space['id']}/instructions", {"instructions": "x", "expected_version": 1}),
        ("DELETE", f"/api/spaces/{space['id']}", None),
        ("POST", f"/api/spaces/{space['id']}/conversations", {}),
    ):
        response = api.request(method, path, json=body, headers=request_headers)
        assert (response.status_code, response.json()) == (status, {"error": code}), (method, path)
    assert world.count(Space) == 1 and world.count(Conversation) == 0


def test_fetch_metadata_without_origin_header_is_rejected(api):
    response = api.post("/api/spaces", json={"name": "No Origin"},
                        headers=headers("a", origin=None, **{"Sec-Fetch-Site": "same-origin"}))
    assert (response.status_code, response.json()) == (403, {"error": "origin_rejected"})


@pytest.mark.parametrize("content_type,raw,status,code", [
    ("text/plain", b'{"name":"x"}', 415, "json_required"),
    ("application/x-www-form-urlencoded", b"name=x", 415, "json_required"),
    ("application/json", b"{not json", 422, "invalid_request"),
    ("application/json", b"[1,2]", 422, "invalid_request"),
    ("application/json", b'{"name":"' + b"x" * (16 * 1024) + b'"}', 413, "body_too_large"),
])
def test_body_must_be_a_small_json_object(api, world, content_type, raw, status, code):
    response = api.post("/api/spaces", content=raw, headers=headers("a", **{"Content-Type": content_type}))
    assert (response.status_code, response.json()) == (status, {"error": code})
    assert world.count(Space) == 0


def test_unknown_query_parameters_and_bad_limits_are_rejected(api):
    for query in ("?debug=1", "?limit=0", "?limit=51", "?limit=abc", "?cursor=@@@", "?limit=1&limit=2"):
        assert api.get("/api/spaces" + query, headers=headers("a")).status_code == 422, query


@pytest.mark.parametrize("path", ["/api/spaces/not-a-uuid", "/api/spaces/123", "/api/spaces/00000000-0000-0000-0000-00000000000Z"])
def test_malformed_ids_are_plain_404(api, path):
    response = api.get(path, headers=headers("a"))
    assert (response.status_code, response.json()) == (404, {"error": "not_found"})


# --- ownership isolation ------------------------------------------------------------------


def test_another_accounts_objects_are_indistinguishable_from_missing_ones(api, world):
    space = create_space(api, "a", instructions="A's private instructions")
    conversation = create_conversation(api, space["id"], "a")
    assert send(api, space["id"], conversation["id"], "A's private message").status_code == 200
    missing_space, missing_conv = str(uuid.uuid4()), str(uuid.uuid4())

    def probe(sid, cid):
        return [
            ("GET", f"/api/spaces/{sid}", None),
            ("PATCH", f"/api/spaces/{sid}", {"name": "x", "expected_version": 1}),
            ("PUT", f"/api/spaces/{sid}/instructions", {"instructions": "x", "expected_version": 1}),
            ("GET", f"/api/spaces/{sid}/conversations", None),
            ("POST", f"/api/spaces/{sid}/conversations", {}),
            ("GET", f"/api/spaces/{sid}/conversations/{cid}", None),
            ("PATCH", f"/api/spaces/{sid}/conversations/{cid}", {"title": "x", "expected_version": 1}),
            ("POST", f"/api/spaces/{sid}/conversations/{cid}/messages",
             {"message": "x", "client_request_id": new_request_id()}),
            ("DELETE", f"/api/spaces/{sid}/conversations/{cid}", None),
            ("DELETE", f"/api/spaces/{sid}", None),
        ]

    for (method, path, body), (_m, missing_path, _b) in zip(probe(space["id"], conversation["id"]),
                                                            probe(missing_space, missing_conv)):
        foreign = api.request(method, path, json=body, headers=headers("b"))
        absent = api.request(method, missing_path, json=body, headers=headers("b"))
        assert (foreign.status_code, foreign.json()) == (absent.status_code, absent.json()) == (404, {"error": "not_found"}), path
        assert dict(foreign.headers) == {**dict(absent.headers), "content-length": foreign.headers["content-length"]}

    # A's data is untouched, by direct database read.
    with world.factory() as session:
        stored = session.scalar(select(Space))
        assert (stored.name, stored.instructions, stored.version) == ("Research", "A's private instructions", 1)
    assert world.count(Conversation) == 1 and world.count(Message) == 2


def test_conversations_cannot_be_reached_through_another_space(api, world):
    x = create_space(api, name="X")
    y = create_space(api, name="Y")
    in_x = create_conversation(api, x["id"], title="Only in X")
    assert send(api, x["id"], in_x["id"], "X secret").status_code == 200
    for method, body in (("GET", None), ("DELETE", None), ("PATCH", {"title": "t", "expected_version": 1})):
        response = api.request(method, f"/api/spaces/{y['id']}/conversations/{in_x['id']}", json=body, headers=headers("a"))
        assert response.status_code == 404, method
    assert send(api, y["id"], in_x["id"], "into the wrong Space").status_code == 404
    listed = api.get(f"/api/spaces/{y['id']}/conversations", headers=headers("a")).json()
    assert listed["conversations"] == []
    assert world.count(Message) == 2


def test_lists_show_only_the_callers_own_rows(api, world):
    create_space(api, "a", name="A one")
    create_space(api, "b", name="B one")
    names_a = [s["name"] for s in api.get("/api/spaces", headers=headers("a")).json()["spaces"]]
    names_b = [s["name"] for s in api.get("/api/spaces", headers=headers("b")).json()["spaces"]]
    assert names_a == ["A one"] and names_b == ["B one"]


# --- conversations, messages and pagination ------------------------------------------------


def test_conversation_crud_and_message_retrieval(api, world):
    space = create_space(api)
    conversation = create_conversation(api, space["id"], title="  Plan  ")
    assert conversation["title"] == "Plan" and conversation["message_count"] == 0
    default = create_conversation(api, space["id"])
    assert default["title"] == "New conversation"

    for index in range(3):
        world.advance(1)
        assert send(api, space["id"], conversation["id"], f"question {index}").status_code == 200

    page = api.get(f"/api/spaces/{space['id']}/conversations/{conversation['id']}?limit=4", headers=headers("a")).json()
    assert [(m["seq"], m["role"]) for m in page["messages"]] == [(3, "user"), (4, "assistant"), (5, "user"), (6, "assistant")]
    assert page["next_before_seq"] == 3 and page["conversation"]["message_count"] == 6
    older = api.get(f"/api/spaces/{space['id']}/conversations/{conversation['id']}?before_seq=3&limit=4",
                    headers=headers("a")).json()
    assert [m["content"] for m in older["messages"]] == ["question 0", "Echo: question 0"]
    assert older["next_before_seq"] is None

    renamed = api.patch(f"/api/spaces/{space['id']}/conversations/{conversation['id']}",
                        json={"title": "Renamed", "expected_version": 1}, headers=headers("a"))
    assert renamed.status_code == 200 and renamed.json()["conversation"]["title"] == "Renamed"

    assert api.delete(f"/api/spaces/{space['id']}/conversations/{conversation['id']}", headers=headers("a")).status_code == 204
    assert world.count(Message) == 0 and world.count(Conversation) == 1
    assert api.get(f"/api/spaces/{space['id']}/conversations/{conversation['id']}", headers=headers("a")).status_code == 404


def test_space_deletion_cascades_to_conversations_and_messages(api, world):
    space = create_space(api)
    other = create_space(api, name="Other")
    for target in (space, other):
        conversation = create_conversation(api, target["id"])
        assert send(api, target["id"], conversation["id"], "hello").status_code == 200
    assert api.delete(f"/api/spaces/{space['id']}", headers=headers("a")).status_code == 204
    assert world.count(Space) == 1 and world.count(Conversation) == 1 and world.count(Message) == 2


def test_cursor_pagination_is_deterministic_and_complete(api, world):
    created = []
    for index in range(7):
        created.append(create_space(api, name=f"S{index}")["id"])
        if index % 3:
            world.advance(1)                               # some equal timestamps, ordered by id
    seen, cursor, pages = [], None, 0
    while True:
        query = "?limit=3" + (f"&cursor={cursor}" if cursor else "")
        page = api.get("/api/spaces" + query, headers=headers("a")).json()
        seen.extend(s["id"] for s in page["spaces"])
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert sorted(seen) == sorted(created) and len(seen) == 7 and pages == 3
    again = []
    cursor = None
    while True:
        page = api.get("/api/spaces?limit=3" + (f"&cursor={cursor}" if cursor else ""), headers=headers("a")).json()
        again.extend(s["id"] for s in page["spaces"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert again == seen                                   # same order every time
    with world.factory() as session:
        rows = session.execute(select(Space.updated_at, Space.id).order_by(Space.updated_at.desc(), Space.id.desc())).all()
    assert [str(r.id) for r in rows] == seen
    # B's cursor walk never sees A's rows.
    assert api.get(f"/api/spaces?cursor={page['next_cursor'] or repo.encode_cursor(world.clock() + timedelta(days=1), uuid.uuid4())}",
                   headers=headers("b")).json()["spaces"] == []


def test_conversation_list_pagination(api, world):
    space = create_space(api)
    ids = []
    for _ in range(5):
        world.advance(1)
        ids.append(create_conversation(api, space["id"])["id"])
    first = api.get(f"/api/spaces/{space['id']}/conversations?limit=2", headers=headers("a")).json()
    second = api.get(f"/api/spaces/{space['id']}/conversations?limit=2&cursor={first['next_cursor']}", headers=headers("a")).json()
    third = api.get(f"/api/spaces/{space['id']}/conversations?limit=2&cursor={second['next_cursor']}", headers=headers("a")).json()
    walked = [c["id"] for c in first["conversations"] + second["conversations"] + third["conversations"]]
    assert walked == list(reversed(ids)) and third["next_cursor"] is None


def test_every_response_is_no_store_and_varies_by_cookie(api):
    space = create_space(api)
    for response in (
        api.get("/api/spaces", headers=headers("a")),
        api.get(f"/api/spaces/{space['id']}", headers=headers("a")),
        api.get(f"/api/spaces/{uuid.uuid4()}", headers=headers("a")),
        api.post("/api/spaces", json={"name": ""}, headers=headers("a")),
        api.get("/api/spaces", headers=headers(None)),
        api.delete(f"/api/spaces/{space['id']}", headers=headers("a")),
    ):
        assert response.headers["cache-control"] == "no-store", response.status_code
        assert response.headers["vary"] == "Cookie", response.status_code
