"""Slice 1: the pre-authentication guard for unsafe account, billing and
Spaces requests (kalillac_accounts/request_guard.py).

Every inventoried route is driven through raw ASGI scopes, so the header
list and query string reach the guard exactly as written here: no client
library merges duplicate headers or normalises their names. The stack mounts
the real account, recovery, verification, billing and Spaces routers on an
isolated SQLite file database, with in-memory email and Stripe fakes and a
recording Spaces generator, so every side effect a rejected request could
cause is observable.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import os
import socket
import subprocess
import sys
from typing import Any
import uuid

from argon2 import PasswordHasher
from fastapi import FastAPI
import pytest
from sqlalchemy import event, func, select
import uvicorn

from kalillac_accounts.email_verification_router import build_email_verification_router
from kalillac_accounts.recovery_router import build_recovery_router
from kalillac_accounts.request_guard import (
    EXEMPT,
    INVENTORY,
    GuardConfigError,
    UnsafeRequestGuard,
    parse_guard_origin,
    load_guard_origin,
)
from kalillac_accounts.rate_limit import LoginRateLimiter
from kalillac_accounts.recovery_rate_limit import RecoveryRateLimiter
from kalillac_accounts.verification_rate_limit import VerificationRateLimiter
from kalillac_accounts.router import AccountSettings, build_account_router
from kalillac_billing.config import BillingConfig
from kalillac_billing.router import build_billing_router
from kalillac_db.models import Message, Space, User
from kalillac_spaces.router import build_spaces_router

from spaces_test_support import ORIGIN, make_world, token_for


CANDIDATE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CANONICAL = ORIGIN.encode("ascii")
SETTINGS = AccountSettings(cookie_secure=True, email_verification_enabled=True)
COOKIE = SETTINGS.cookie_name
EMAIL_ENV = {
    "KALILLAC_ACCOUNTS_ENABLED": "true",
    "KALILLAC_DB_ENABLED": "true",
    "KALILLAC_ACCOUNT_RECOVERY_ENABLED": "true",
    "KALILLAC_ACCOUNT_RECOVERY_URL": "https://kalillac.ai/reset",
    "KALILLAC_ACCOUNT_EMAIL_VERIFICATION_ENABLED": "true",
    "KALILLAC_ACCOUNT_EMAIL_VERIFICATION_URL": "https://kalillac.ai/verify-email",
    "KALILLAC_EMAIL_FROM": "help@kalillac.ai",
}
BILLING = BillingConfig(
    secret_key="sk_test_guardsecret123",
    webhook_secret="whsec_guardsecret456",
    price_id="price_guardpaid",
    success_url="https://kalillac.ai/billing/success",
    cancel_url="https://kalillac.ai/billing/cancel",
    portal_return_url="https://kalillac.ai/account",
    mode="test",
    allowed_price_ids=frozenset({"price_guardpaid"}),
)
# 256 code points, each four UTF-8 bytes (and a 12-byte \u escape pair).
UNICODE_PASSWORD = "\U0001F600" * 256


# --- the stack --------------------------------------------------------------------------


@dataclass
class Effects:
    """Everything a guarded request could do besides answering."""

    db_provider: int = 0
    db_checkouts: int = 0
    hashes: int = 0
    verifies: int = 0
    stripe: list = field(default_factory=list)
    emails: list = field(default_factory=list)
    generations: int = 0
    cookies: int = 0
    budgets: int = 0
    limiter_calls: int = 0

    def none(self) -> bool:
        return (self.db_provider, self.db_checkouts, self.hashes, self.verifies,
                self.stripe, self.emails, self.generations, self.cookies, self.budgets,
                self.limiter_calls) == (0, 0, 0, 0, [], [], 0, 0, 0, 0)


class RecordingLimiter:
    def __init__(self, limiter, effects):
        self.limiter, self.effects = limiter, effects

    def __getattr__(self, name):
        method = getattr(self.limiter, name)

        def counted(*args, **kwargs):
            self.effects.limiter_calls += 1
            return method(*args, **kwargs)
        return counted


class RecordingStripe:
    """Any gateway use is recorded and refused: no test here needs Stripe."""

    def __init__(self, effects: Effects):
        self._effects = effects

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self._effects.stripe.append(name)
            raise AssertionError(f"unexpected Stripe call {name}")
        return call


class RecordingEmail:
    def __init__(self, effects: Effects):
        self.effects = effects
        self.links: list[str] = []

    def send_password_reset(self, to, link):
        self.effects.emails.append("reset")
        self.links.append(link)

    def send_password_changed(self, to):
        self.effects.emails.append("changed")

    def send_email_verification(self, to, link):
        self.effects.emails.append("verification")


@dataclass
class Stack:
    world: Any
    effects: Effects
    email: RecordingEmail
    plain: FastAPI
    guarded: UnsafeRequestGuard


def build_stack(tmp_path, monkeypatch, origin: str | None = ORIGIN) -> Stack:
    world = make_world(tmp_path)
    effects = Effects()
    email = RecordingEmail(effects)

    def provider():
        effects.db_provider += 1
        return world.factory

    event.listen(world.engine, "checkout", lambda *a: setattr(effects, "db_checkouts", effects.db_checkouts + 1))

    original_hash, original_verify = PasswordHasher.hash, PasswordHasher.verify

    def counted_hash(self, *a, **k):
        effects.hashes += 1
        return original_hash(self, *a, **k)

    def counted_verify(self, *a, **k):
        effects.verifies += 1
        return original_verify(self, *a, **k)

    monkeypatch.setattr(PasswordHasher, "hash", counted_hash)
    monkeypatch.setattr(PasswordHasher, "verify", counted_verify)

    async def generate(request, *, user_id, lock_key, message, history, workspace, budget, finalize):
        effects.generations += 1
        return await finalize("Echo: " + message)

    from starlette.requests import HTTPConnection
    original_cookies = HTTPConnection.cookies

    def cookies(request):
        effects.cookies += 1
        return original_cookies.fget(request)

    monkeypatch.setattr(HTTPConnection, "cookies", property(cookies))

    def budget():
        effects.budgets += 1
        return world.budget()

    plain = FastAPI()
    plain.include_router(build_account_router(session_factory=provider, settings=SETTINGS,
        limiter=RecordingLimiter(LoginRateLimiter(), effects)))
    plain.include_router(build_recovery_router(
        session_factory=provider, gateway=email, settings=SETTINGS, environ=EMAIL_ENV, clock=world.clock,
        limiter=RecordingLimiter(RecoveryRateLimiter(), effects)))
    plain.include_router(build_email_verification_router(
        session_factory=provider, gateway=email, settings=SETTINGS, environ=EMAIL_ENV, clock=world.clock,
        limiter=RecordingLimiter(VerificationRateLimiter(), effects)))
    plain.include_router(build_billing_router(
        config=BILLING, settings=SETTINGS, gateway=RecordingStripe(effects), session_factory=provider))
    plain.include_router(build_spaces_router(
        config=world.config(), cookie_name=COOKIE, generate=generate, lease_seconds=60,
        session_factory=provider, clock=world.clock, new_budget=budget))

    # Setup work (seeding) is not a request's side effect.
    effects.db_provider = effects.db_checkouts = 0
    return Stack(world, effects, email, plain, UnsafeRequestGuard(plain, origin=origin))


@pytest.fixture
def stack(tmp_path, monkeypatch):
    s = build_stack(tmp_path, monkeypatch)
    yield s
    s.world.engine.dispose()


@dataclass
class Reply:
    status: int
    headers: dict
    body: bytes
    body_chunks_read: int

    def json(self):
        return json.loads(self.body)


def call(app, method: str, path: str, *, headers=(), query: bytes = b"", chunks=(b"",)) -> Reply:
    """One ASGI request. headers are raw (name, value) byte pairs, sent as is."""
    pending = [{"type": "http.request", "body": c, "more_body": i < len(chunks) - 1}
               for i, c in enumerate(chunks)]
    read = []
    sent = []

    async def receive():
        if pending:
            read.append(1)
            return pending.pop(0)
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "https", "path": path, "raw_path": path.encode(),
        "query_string": query, "root_path": "", "headers": [tuple(h) for h in headers],
        "client": ("192.0.2.10", 50000), "server": ("testserver", 443),
    }
    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return Reply(start["status"], {k.decode().lower(): v.decode() for k, v in start["headers"]}, body, len(read))


def jbody(payload: Any, *, ascii_only: bool = True) -> bytes:
    return json.dumps(payload, ensure_ascii=ascii_only).encode("utf-8")


def json_headers(origin_fields=((b"origin", CANONICAL),), cookie: str | None = None, length: int | None = None):
    out = [(b"content-type", b"application/json"), *origin_fields]
    if cookie is not None:
        out.append((b"cookie", f"{COOKIE}={token_for(cookie)}".encode()))
    if length is not None:
        out.append((b"content-length", str(length).encode()))
    return out


def concrete(path: str) -> str:
    return (path.replace("{space_id}", str(uuid.uuid4()))
                .replace("{conversation_id}", str(uuid.uuid4())))


ROUTES = [pytest.param(r, id=f"{r.method} {r.path}") for r in INVENTORY]

GUARD_BODIES = {
    b'{"error":"origin_rejected"}', b'{"error":"query_not_allowed"}', b'{"error":"invalid_request"}',
}


# --- inventory coverage -----------------------------------------------------------------


def test_inventory_is_exactly_the_unsafe_account_billing_and_spaces_routes(stack):
    # Included routers aren't flattened into app.routes; the generated schema
    # lists every route (none of these routers opts out of it).
    registered = {
        (method.upper(), path)
        for path, operations in stack.plain.openapi()["paths"].items()
        for method in operations
        if method.upper() in {"POST", "PUT", "PATCH", "DELETE"}
        and path.startswith(("/api/account", "/api/billing", "/api/spaces"))
    }
    assert ("GET", "/api/spaces") not in registered and len(registered) > 10
    guarded = {(r.method, r.path) for r in INVENTORY}
    assert registered == guarded | set(EXEMPT)
    assert not guarded & set(EXEMPT)
    assert len(INVENTORY) == 17                 # 7 account, 2 billing, 8 Spaces mutations
    assert sum(r.family == "spaces" for r in INVENTORY) == 8


# --- Origin -----------------------------------------------------------------------------


REJECTED_ORIGINS = {
    "missing": [],
    "wrong": [(b"origin", b"https://evil.example")],
    "duplicate lowercase": [(b"origin", CANONICAL), (b"origin", CANONICAL)],
    "mixed-case duplicate": [(b"Origin", CANONICAL), (b"origin", CANONICAL)],
    "mixed-case duplicate, upper": [(b"origin", CANONICAL), (b"ORIGIN", CANONICAL)],
    "foreign then canonical": [(b"Origin", b"https://evil.example"), (b"origin", CANONICAL)],
    "canonical then foreign": [(b"origin", CANONICAL), (b"Origin", b"https://evil.example")],
    "combined value": [(b"origin", CANONICAL + b", https://evil.example")],
    "null": [(b"origin", b"null")],
    "uppercase host": [(b"origin", b"https://KALILLAC.test")],
    "default port": [(b"origin", CANONICAL + b":443")],
    "trailing slash": [(b"origin", CANONICAL + b"/")],
    "path": [(b"origin", CANONICAL + b"/account")],
    "userinfo": [(b"origin", b"https://user@kalillac.test")],
    "control byte": [(b"origin", CANONICAL + b"\x00")],
    "fetch metadata only": [(b"sec-fetch-site", b"same-origin")],
    "referer only": [(b"referer", CANONICAL + b"/account")],
    "leading whitespace": [(b"origin", b" " + CANONICAL)],
    "trailing whitespace": [(b"origin", CANONICAL + b" ")],
    "non-ASCII": [(b"origin", CANONICAL + b"\xff")],
    "Host and forwarded only": [(b"host", CANONICAL[8:]), (b"x-forwarded-host", CANONICAL[8:]),
                                (b"x-forwarded-proto", b"https"), (b"forwarded", b"proto=https;host=kalillac.test")],
}


@pytest.mark.parametrize("case", list(REJECTED_ORIGINS))
@pytest.mark.parametrize("route", ROUTES)
def test_every_route_rejects_a_bad_origin_before_any_work(stack, route, case):
    fields = REJECTED_ORIGINS[case]
    for cookie in (None, "c", "a"):
        reply = call(stack.guarded, route.method, concrete(route.path),
                     headers=json_headers(fields, cookie=cookie), chunks=(b"{}",))
        assert (reply.status, reply.json()) == (403, {"error": "origin_rejected"})
        assert reply.headers["cache-control"] == "no-store"
        if route.family == "spaces":
            assert reply.headers["vary"] == "Cookie"
        assert reply.body_chunks_read == 0
        assert stack.effects.none(), stack.effects


@pytest.mark.parametrize("name", [b"origin", b"Origin", b"ORIGIN"])
@pytest.mark.parametrize("route", ROUTES)
def test_every_route_passes_exactly_one_canonical_origin_in_any_name_case(stack, route, name):
    # A valid Spaces cookie exercises its downstream Origin check too.
    # Billing stays signed out here; its bodyless authenticated path has its own test.
    reply = call(stack.guarded, route.method, concrete(route.path),
                 headers=json_headers(((name, CANONICAL),), cookie="a" if route.family == "spaces" else None),
                 chunks=(b"{}",))
    assert reply.status != 403 or reply.body not in GUARD_BODIES
    assert reply.body not in {b'{"error":"query_not_allowed"}'}
    unguarded = call(stack.plain, route.method, concrete(route.path),
                     headers=json_headers(((name, CANONICAL),), cookie="a" if route.family == "spaces" else None),
                 chunks=(b"{}",))
    assert (reply.status, reply.json()) == (unguarded.status, unguarded.json())


def test_an_unset_origin_rejects_every_guarded_request(tmp_path, monkeypatch):
    s = build_stack(tmp_path, monkeypatch, origin=None)
    for route in INVENTORY:
        reply = call(s.guarded, route.method, concrete(route.path), headers=json_headers(), chunks=(b"{}",))
        assert (reply.status, reply.json()) == (403, {"error": "origin_rejected"})
    assert s.effects.none()
    s.world.engine.dispose()


def test_spaces_no_longer_accept_fetch_metadata_without_origin(stack):
    # Both the pre-authentication guard and the Spaces helper reject this.
    headers = [(b"content-type", b"application/json"), (b"sec-fetch-site", b"same-origin"),
               (b"cookie", f"{COOKIE}={token_for('a')}".encode())]
    reply = call(stack.guarded, "POST", "/api/spaces", headers=headers, chunks=(b'{"name":"x"}',))
    assert (reply.status, reply.json()) == (403, {"error": "origin_rejected"})
    assert stack.world.count(Space) == 0


# --- query ------------------------------------------------------------------------------


QUERIES = {
    "normal": b"x=1",
    "ownership": b"user_id=" + str(uuid.uuid4()).encode(),
    "repeated": b"user_id=a&user_id=b",
    "?&": b"&",
    "?&&": b"&&",
    "?=": b"=",
    "encoded nul": b"%00",
}


@pytest.mark.parametrize("case", list(QUERIES))
@pytest.mark.parametrize("route", ROUTES)
def test_every_route_rejects_a_query_before_any_work(stack, route, case):
    # A valid session cookie and a canonical Origin: only the query is wrong.
    reply = call(stack.guarded, route.method, concrete(route.path), query=QUERIES[case],
                 headers=json_headers(cookie="a"), chunks=(b"{}",))
    if route.family == "spaces":
        assert (reply.status, reply.json()) == (422, {"error": "invalid_request"})
        assert reply.headers["vary"] == "Cookie"
    else:
        assert (reply.status, reply.json()) == (400, {"error": "query_not_allowed"})
    assert reply.headers["cache-control"] == "no-store"
    assert reply.body_chunks_read == 0
    assert stack.effects.none(), stack.effects


@pytest.mark.parametrize("route", ROUTES)
def test_the_query_is_checked_before_the_origin(stack, route):
    reply = call(stack.guarded, route.method, concrete(route.path), query=b"x=1",
                 headers=json_headers(((b"origin", b"https://evil.example"),)), chunks=(b"{}",))
    expected = (422, {"error": "invalid_request"}) if route.family == "spaces" else \
        (400, {"error": "query_not_allowed"})
    assert (reply.status, reply.json()) == expected
    assert stack.effects.none()


def test_the_guard_precedes_spaces_authentication_and_allowlisting(stack):
    path = "/api/spaces"
    # Cookieless and cross-origin: the guard's 403, not the router's 401.
    assert call(stack.guarded, "POST", path, headers=json_headers(()),
                chunks=(b'{"name":"x"}',)).status == 403
    # A verified account that is not allowlisted (c): 403, not the router's 404.
    reply = call(stack.guarded, "POST", path, headers=json_headers((), cookie="c"), chunks=(b'{"name":"x"}',))
    assert (reply.status, reply.json()) == (403, {"error": "origin_rejected"})
    # Cookieless with a query: the guard's 422, not the router's 401.
    reply = call(stack.guarded, "POST", path, query=b"x=1", headers=json_headers(), chunks=(b'{"name":"x"}',))
    assert (reply.status, reply.json()) == (422, {"error": "invalid_request"})
    assert stack.effects.none()


def test_a_bare_question_mark_is_invisible_to_asgi_and_is_not_claimed_rejected():
    """The ASGI server splits the request target at '?', so '/path?' arrives
    exactly like '/path'. This pins that limitation (D23); it does not claim
    a rejection. A real Uvicorn server on loopback is used."""
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["query_string"])
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    guarded = UnsafeRequestGuard(inner, origin=ORIGIN)

    async def exchange(port, target):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            f"POST {target} HTTP/1.1\r\nHost: kalillac.test\r\nOrigin: {ORIGIN}\r\n"
            "Content-Length: 0\r\nConnection: close\r\n\r\n".encode()
        )
        await writer.drain()
        response = await reader.read()
        writer.close()
        return int(response.split(b" ", 2)[1])

    async def main():
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        config = uvicorn.Config(guarded, lifespan="off", log_level="critical")
        config.load()
        server = uvicorn.Server(config)
        server.lifespan = config.lifespan_class(config)   # as Server.serve() sets it up
        await server.startup(sockets=[sock])
        try:
            port = sock.getsockname()[1]
            return [await exchange(port, t) for t in
                    ("/api/account/logout?", "/api/account/logout", "/api/account/logout?x=1",
                     "/api/spaces?", "/api/spaces", "/api/spaces?&")]
        finally:
            await server.shutdown(sockets=[sock])

    statuses = asyncio.run(main())
    assert statuses == [204, 204, 400, 204, 204, 422]
    assert seen == [b"", b"", b"", b""]          # the bare '?' reached the app as no query at all


# --- the webhook exemption --------------------------------------------------------------


@pytest.mark.parametrize("fields,query", [
    ((), b""),
    (((b"origin", b"https://evil.example"),), b""),
    (((b"origin", CANONICAL), (b"origin", CANONICAL)), b""),
    ((), b"x=1"),
])
def test_the_stripe_webhook_is_exempt(stack, fields, query):
    reply = call(stack.guarded, "POST", "/api/billing/stripe/webhook", query=query,
                 headers=list(fields), chunks=(b"{}",))
    unguarded = call(stack.plain, "POST", "/api/billing/stripe/webhook", query=query,
                     headers=list(fields), chunks=(b"{}",))
    # Its own signature contract answers, identically with and without the guard.
    assert (reply.status, reply.json()) == (unguarded.status, unguarded.json()) == (
        400, {"error": "invalid_signature"})
    assert stack.effects.stripe == []


# --- password-bearing bodies: 4,096 bytes, streamed ---------------------------------------


def credentials(email: str, password: str, *, size: int | None = None, ascii_only: bool = True) -> bytes:
    raw = jbody({"email": email, "password": password}, ascii_only=ascii_only)
    if size is not None:
        raw = b"{" + b" " * (size - len(raw)) + raw[1:]
        assert len(raw) == size
    return raw


def chunked(raw: bytes, size: int = 1000) -> tuple[bytes, ...]:
    return tuple(raw[i:i + size] for i in range(0, len(raw), size))


@pytest.mark.parametrize("path", ["/api/account/register", "/api/account/login"])
def test_credential_bodies_at_the_limit_are_read_and_one_byte_more_is_refused(stack, path):
    at_limit = credentials("limit@example.test", "a valid long password", size=4096)
    over = credentials("limit@example.test", "a valid long password", size=4097)

    reply = call(stack.guarded, "POST", path, headers=json_headers(length=4096), chunks=chunked(at_limit))
    assert reply.status != 413

    # Honest Content-Length over the limit: refused before any body is read.
    reply = call(stack.guarded, "POST", path, headers=json_headers(length=4097), chunks=chunked(over))
    assert (reply.status, reply.json(), reply.body_chunks_read) == (413, {"error": "body_too_large"}, 0)

    # No Content-Length: refused while streaming.
    reply = call(stack.guarded, "POST", path, headers=json_headers(), chunks=chunked(over))
    assert (reply.status, reply.json()) == (413, {"error": "body_too_large"})


@pytest.mark.parametrize("path", ["/api/account/register", "/api/account/login", "/api/account/recovery/confirm"])
@pytest.mark.parametrize("declared", [None, 10, 4096])
def test_password_bodies_stop_at_the_chunk_that_would_pass_the_limit(stack, path, declared):
    # 10 chunks of 1,000 bytes, with a missing or dishonest Content-Length:
    # four chunks fit (4,000 bytes); the fifth would pass 4,096, so it is
    # refused before it is kept and no sixth chunk is ever requested.
    reply = call(stack.guarded, "POST", path, headers=json_headers(length=declared),
                 chunks=tuple(b" " * 1000 for _ in range(10)))
    assert (reply.status, reply.json()) == (413, {"error": "body_too_large"})
    assert reply.body_chunks_read == 5
    assert stack.effects.hashes == stack.effects.verifies == 0


def test_a_malformed_content_length_on_login_is_still_ignored(stack):
    raw = credentials("nobody@example.test", "a valid long password")
    headers = [(b"content-type", b"application/json"), (b"origin", CANONICAL), (b"content-length", b"abc")]
    assert call(stack.guarded, "POST", "/api/account/login", headers=headers, chunks=(raw,)).status == 401


@pytest.mark.parametrize("ascii_only", [False, True], ids=["raw UTF-8", "JSON-escaped"])
def test_a_256_character_four_byte_password_registers_and_signs_in(stack, ascii_only):
    raw = credentials("unicode@example.test", UNICODE_PASSWORD, ascii_only=ascii_only)
    assert len(raw) == (1024 if not ascii_only else 3072) + len(b'{"email": "unicode@example.test", "password": ""}')
    assert len(raw) < 4096
    reply = call(stack.guarded, "POST", "/api/account/register", headers=json_headers(), chunks=chunked(raw))
    assert reply.status == 201, reply.body
    reply = call(stack.guarded, "POST", "/api/account/login", headers=json_headers(), chunks=chunked(raw))
    assert reply.status == 200, reply.body


@pytest.mark.parametrize("ascii_only", [False, True], ids=["raw UTF-8", "JSON-escaped"])
def test_a_256_character_four_byte_password_is_set_by_recovery(stack, ascii_only):
    email = "spaces-a@example.test"
    reply = call(stack.guarded, "POST", "/api/account/recovery/request", headers=json_headers(),
                 chunks=(jbody({"email": email}),))
    assert reply.status == 202, reply.body
    token = stack.email.links[-1].split("#token=", 1)[1]

    raw = jbody({"token": token, "password": UNICODE_PASSWORD}, ascii_only=ascii_only)
    assert len(raw) < 4096
    reply = call(stack.guarded, "POST", "/api/account/recovery/confirm", headers=json_headers(), chunks=chunked(raw))
    assert reply.status == 200, reply.body

    login = credentials(email, UNICODE_PASSWORD, ascii_only=ascii_only)
    assert call(stack.guarded, "POST", "/api/account/login", headers=json_headers(), chunks=(login,)).status == 200


def test_257_code_points_is_a_length_error_not_a_size_error(stack):
    raw = credentials("long@example.test", "\U0001F600" * 257, ascii_only=False)
    reply = call(stack.guarded, "POST", "/api/account/register", headers=json_headers(), chunks=(raw,))
    assert (reply.status, reply.json()) == (422, {"error": "password_length"})


# --- preserved contracts ------------------------------------------------------------------


def both(stack_pair, method, path, **kwargs):
    return [call(s.guarded, method, path, **kwargs) for s in stack_pair]


@pytest.fixture
def pair(tmp_path, monkeypatch):
    """The same requests against a guarded and an unguarded stack."""
    (tmp_path / "guarded").mkdir()
    (tmp_path / "plain").mkdir()
    first = build_stack(tmp_path / "guarded", monkeypatch)
    second = build_stack(tmp_path / "plain", monkeypatch)
    second.guarded = second.plain                    # the second answers without the guard
    yield first, second
    first.world.engine.dispose()
    second.world.engine.dispose()


def same(replies):
    guarded, plain = replies
    assert guarded.status == plain.status, (guarded.body, plain.body)
    return guarded


def test_bodyless_logout_checkout_and_portal_are_unchanged(pair):
    # No body and no Content-Type, exactly as account-api.ts sends them.
    origin = [(b"origin", CANONICAL)]
    signed_in = origin + [(b"cookie", f"{COOKIE}={token_for('a')}".encode())]
    for path, headers in (("/api/account/logout", origin), ("/api/account/logout", signed_in),
                          ("/api/account/billing/checkout", origin), ("/api/account/billing/portal", origin)):
        replies = both(pair, "POST", path, headers=headers)
        reply = same(replies)
        assert replies[0].json() == replies[1].json()
        assert reply.status in {200, 401}, (path, reply.body)
    for s in pair:
        assert s.effects.stripe == [] and s.effects.hashes == 0


@pytest.mark.parametrize("path,raw,expected", [
    ("/api/account/login", b"{", (400, {"error": "invalid_json"})),
    ("/api/account/login", b"[]", (400, {"error": "invalid_body"})),
    ("/api/account/register", b"\xff", (400, {"error": "invalid_json"})),
    ("/api/account/recovery/confirm", b"{", (400, {"error": "invalid_json"})),
    ("/api/account/recovery/confirm", b'{"token":"x"}', (400, {"error": "invalid_body"})),
    ("/api/account/email-verification/confirm", b"{", (400, {"error": "invalid_json"})),
    ("/api/spaces", b"{", (422, {"error": "invalid_request"})),
    ("/api/spaces", b"[]", (422, {"error": "invalid_request"})),
])
def test_malformed_json_answers_are_unchanged(pair, path, raw, expected):
    reply = same(both(pair, "POST", path, headers=json_headers(cookie="a"), chunks=(raw,)))
    assert (reply.status, reply.json()) == expected


def test_duplicate_json_keys_still_take_the_last_value(pair):
    for s in pair:
        reply = call(s.guarded, "POST", "/api/account/register", headers=json_headers(),
                     chunks=(b'{"email":"first@example.test","email":"last@example.test",'
                             b'"password":"a valid long password"}',))
        assert reply.status == 201
        with s.world.factory() as session:
            assert session.scalar(select(func.count()).select_from(User).where(User.email == "last@example.test")) == 1
        reply = call(s.guarded, "POST", "/api/spaces", headers=json_headers(cookie="a"),
                     chunks=(b'{"name":"First","name":"Second"}',))
        assert (reply.status, reply.json()["space"]["name"]) == (201, "Second")


def test_all_eight_spaces_mutations_keep_their_schemas_and_bodyless_deletes(pair):
    """M1-M8 with required and optional fields, through both stacks."""
    for s in pair:
        g = s.guarded
        h = json_headers(cookie="a")

        def post(path, payload, method="POST"):
            return call(g, method, path, headers=h, chunks=(jbody(payload),))

        # M1, with and without the optional instructions.
        bare = post("/api/spaces", {"name": "Bare"})
        assert bare.status == 201 and bare.json()["space"]["instructions"] == ""
        space = post("/api/spaces", {"name": "Research", "instructions": "Be brief."}).json()["space"]
        sid = space["id"]
        # Unknown and wrongly typed fields stay 422.
        assert post("/api/spaces", {"name": "x", "user_id": "y"}).status == 422
        assert post("/api/spaces", {"name": 7}).status == 422
        # M2, M3.
        assert post(f"/api/spaces/{sid}", {"name": "Renamed", "expected_version": 1}, "PATCH").status == 200
        assert post(f"/api/spaces/{sid}/instructions", {"instructions": "x", "expected_version": 2},
                    "PUT").status == 200
        # M5: {} and title null and a title are all valid.
        for payload in ({}, {"title": None}, {"title": "Notes"}):
            assert post(f"/api/spaces/{sid}/conversations", payload).status == 201
        conversation = post(f"/api/spaces/{sid}/conversations", {"title": "Main"}).json()["conversation"]
        cid = conversation["id"]
        # M6.
        assert post(f"/api/spaces/{sid}/conversations/{cid}", {"title": "Renamed", "expected_version": 1},
                    "PATCH").status == 200
        # M8 with uppercase hexadecimal: accepted, and normalised to lowercase,
        # so the lowercase form replays the same exchange.
        request_id = str(uuid.uuid4())
        first = post(f"/api/spaces/{sid}/conversations/{cid}/messages",
                     {"message": "hello", "client_request_id": request_id.upper()})
        assert first.status == 200, first.body
        replay = post(f"/api/spaces/{sid}/conversations/{cid}/messages",
                      {"message": "hello", "client_request_id": request_id})
        assert replay.status == 200
        assert replay.json()["assistant_message"]["id"] == first.json()["assistant_message"]["id"]
        with s.world.factory() as session:
            stored = session.scalars(select(Message.client_request_id).where(Message.client_request_id.is_not(None))).all()
        assert [str(v) for v in stored] == [request_id]
        assert s.effects.generations == 1
        # M7 and M4: bodyless, with no Content-Type at all, and with a stray body.
        bodyless = [(b"origin", CANONICAL), (b"cookie", f"{COOKIE}={token_for('a')}".encode())]
        other = post(f"/api/spaces/{sid}/conversations", {}).json()["conversation"]["id"]
        assert call(g, "DELETE", f"/api/spaces/{sid}/conversations/{other}", headers=bodyless).status == 204
        assert call(g, "DELETE", f"/api/spaces/{sid}/conversations/{cid}", headers=bodyless,
                    chunks=(b"not json at all",)).status == 204
        assert call(g, "DELETE", f"/api/spaces/{bare.json()['space']['id']}", headers=bodyless).status == 204
        assert call(g, "DELETE", f"/api/spaces/{sid}", headers=bodyless,
                    chunks=(b"[1,2,3]",)).status == 204
        assert s.world.count(Space) == 0


def test_spaces_body_cap_and_media_type_are_unchanged(pair):
    big = jbody({"name": "x", "instructions": "y" * 16400})
    for raw, ctype, expected in ((big, b"application/json", 413), (b'{"name":"x"}', b"text/plain", 415)):
        headers = [(b"content-type", ctype), (b"origin", CANONICAL), (b"cookie", f"{COOKIE}={token_for('a')}".encode())]
        reply = same(both(pair, "POST", "/api/spaces", headers=headers, chunks=(raw,)))
        assert reply.status == expected


# --- email-verification/request keeps its body-before-cookie order ------------------------


def test_verification_request_reads_its_body_before_authenticating(stack):
    # No cookie, a bad body: the body error answers first, not 401.
    reply = call(stack.guarded, "POST", "/api/account/email-verification/request",
                 headers=[(b"content-type", b"text/plain"), (b"origin", CANONICAL)], chunks=(b"{}",))
    assert (reply.status, reply.json()) == (415, {"error": "json_required"})
    reply = call(stack.guarded, "POST", "/api/account/email-verification/request",
                 headers=json_headers(), chunks=(b'{"x":1}',))
    assert (reply.status, reply.json()) == (400, {"error": "invalid_body"})
    assert stack.effects.db_provider == 0
    # A good body and no cookie: then the 401.
    reply = call(stack.guarded, "POST", "/api/account/email-verification/request",
                 headers=json_headers(), chunks=(b"{}",))
    assert (reply.status, reply.json()) == (401, {"error": "not_authenticated"})
    # The guard still comes first: query, then Origin, before that body read.
    reply = call(stack.guarded, "POST", "/api/account/email-verification/request", query=b"x=1",
                 headers=[(b"content-type", b"text/plain")], chunks=(b"{}",))
    assert (reply.status, reply.json(), reply.body_chunks_read) == (400, {"error": "query_not_allowed"}, 0)
    reply = call(stack.guarded, "POST", "/api/account/email-verification/request",
                 headers=[(b"content-type", b"text/plain")], chunks=(b"{}",))
    assert (reply.status, reply.json(), reply.body_chunks_read) == (403, {"error": "origin_rejected"}, 0)


# --- configuration ------------------------------------------------------------------------


@pytest.mark.parametrize("value", [
    "https://kalillac.ai", "https://kalillac.ai:8443", "http://127.0.0.1:5173", "http://localhost:8001",
])
def test_exact_origins_are_accepted(value):
    assert parse_guard_origin(value) == value


@pytest.mark.parametrize("value", [None, "", "   "])
def test_an_unset_origin_is_none(value):
    assert parse_guard_origin(value) is None


@pytest.mark.parametrize("value", [
    "https://KALILLAC.ai", "https://kalillac.ai/", "https://kalillac.ai/path", "https://kalillac.ai:443",
    "http://kalillac.ai", "http://localhost:80", "https://user@kalillac.ai", "https://kalillac.ai?x=1",
    "https://kalillac.ai#x", "kalillac.ai", "null", "https://kalillac.ai, https://evil.test",
    "https://kalillac.ai:99999",
])
def test_inexact_origins_are_a_configuration_error_that_hides_the_value(value):
    with pytest.raises(GuardConfigError) as raised:
        parse_guard_origin(value)
    assert "KALILLAC_SITE_ORIGIN" in str(raised.value)
    assert value not in str(raised.value)


# --- the real application -----------------------------------------------------------------


def _run_app(env_overrides: dict, script: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("KALILLAC_", "CREDENTIALS_DIRECTORY"))}
    env.update({
        "GROQ_API_KEY": "test-not-real",
        "KALILLAC_ACCOUNTS_ENABLED": "true",
        "KALILLAC_DB_ENABLED": "true",
        "KALILLAC_DATABASE_URL": "postgresql+psycopg://user:pw@unreachable.invalid/kalillac",
        **env_overrides,
    })
    # The child counts (and refuses) any non-loopback connection or lookup.
    guard = (
        "import socket\nattempts=[]\n"
        "_gai=socket.getaddrinfo\n"
        "def _g(host,*a,**k):\n"
        " if host not in (None,'localhost','127.0.0.1','::1'): attempts.append(host); raise OSError('blocked')\n"
        " return _gai(host,*a,**k)\n"
        "socket.getaddrinfo=_g\n"
    )
    return subprocess.run([sys.executable, "-c", guard + script], cwd=CANDIDATE_DIR, env=env,
                          capture_output=True, text=True, timeout=120)


REAL_APP_SCRIPT = (
    "import app_fastapi_candidate as a\n"
    "from fastapi.testclient import TestClient\n"
    "from kalillac_accounts.request_guard import UnsafeRequestGuard\n"
    "c=TestClient(a.api, base_url='https://testserver')\n"
    "print(sum(m.cls is UnsafeRequestGuard for m in a.api.user_middleware))\n"
    "for kw in ({}, {'headers':{'Origin':'https://evil.example'}}, {'params':{'x':'1'},'headers':{'Origin':'https://kalillac.ai'}}):\n"
    "    r=c.post('/api/account/logout', **kw); print(r.status_code, r.text)\n"
    "print('attempts', len(attempts))\n"
)


def test_the_real_application_installs_the_guard_with_accounts():
    result = _run_app({"KALILLAC_SITE_ORIGIN": "https://kalillac.ai"}, REAL_APP_SCRIPT)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().splitlines()[-5:]
    assert lines == [
        "1",
        '403 {"error":"origin_rejected"}',
        '403 {"error":"origin_rejected"}',
        '400 {"error":"query_not_allowed"}',
        "attempts 0",
    ]


@pytest.mark.parametrize("value", [None, "", "https://Secret-Host.example/", "https://kalillac.ai:443"])
def test_enabled_accounts_refuse_startup_without_a_canonical_origin(value):
    result = _run_app({} if value is None else {"KALILLAC_SITE_ORIGIN": value}, "import app_fastapi_candidate")
    assert result.returncode != 0
    assert "GuardConfigError: KALILLAC_SITE_ORIGIN must be an exact origin." in result.stderr
    assert "Secret-Host" not in result.stdout + result.stderr


def test_without_accounts_the_application_has_no_guard():
    result = _run_app({"KALILLAC_ACCOUNTS_ENABLED": "", "KALILLAC_SITE_ORIGIN": "https://kalillac.ai"}, (
        "import app_fastapi_candidate as a\n"
        "from kalillac_accounts.request_guard import UnsafeRequestGuard\n"
        "print(sum(m.cls is UnsafeRequestGuard for m in a.api.user_middleware))\n"
    ))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "0"


# --- Anonymous Free Chat --------------------------------------------------------------------


def test_anonymous_free_chat_is_unchanged_behind_the_guard(monkeypatch):
    import app_fastapi_candidate as app
    from fastapi.testclient import TestClient

    calls = []

    def fake_chat(message, history, request=None, session_id=None):
        calls.append((message, history))
        return f"echo: {message}"

    monkeypatch.setattr(app, "chat", fake_chat)
    guarded = UnsafeRequestGuard(app.api, origin=ORIGIN)
    requests = [
        {},
        {"headers": {"Origin": "https://evil.example"}},
        {"params": {"x": "1"}},
        {"headers": {"Origin": ORIGIN}},
    ]
    answers = {}
    for name, application in (("plain", app.api), ("guarded", guarded)):
        with TestClient(application, base_url="https://testserver") as c:
            answers[name] = [c.post("/api/chat", json={"message": "hello", "history": []}, **kwargs)
                             for kwargs in requests]
    for a, b in zip(answers["plain"], answers["guarded"]):
        assert a.status_code == b.status_code == 200
        assert set(a.json()) == set(b.json()) == {"reply", "session_id"}
        assert a.json()["reply"] == b.json()["reply"] == "echo: hello"
    assert calls == [("hello", [])] * (2 * len(requests))


# Additional Slice-1 boundaries and preserved contracts.

@pytest.mark.parametrize("value", [None, "", " ", "https://exa mple.test", "https://host\\evil", "https://host:0"])
def test_loading_an_unset_or_invalid_origin_is_fatal_without_echo(value):
    with pytest.raises(GuardConfigError, match="KALILLAC_SITE_ORIGIN"):
        load_guard_origin({} if value is None else {"KALILLAC_SITE_ORIGIN": value})


@pytest.mark.parametrize("route", ROUTES)
def test_host_and_forwarding_headers_cannot_override_the_configured_origin(stack, route):
    headers = json_headers() + [(b"host", b"evil.test"), (b"x-forwarded-host", b"evil.test"),
                                (b"x-forwarded-proto", b"http"), (b"forwarded", b"host=evil.test;proto=http")]
    reply = call(stack.guarded, route.method, concrete(route.path), headers=headers, chunks=(b"{}",))
    assert reply.body != b'{"error":"origin_rejected"}'


@pytest.mark.parametrize("path,payload,expected", [
    ("/api/account/register", {"email": "boundary@example.test", "password": "valid password 123"}, 201),
    ("/api/account/login", {"email": "boundary@example.test", "password": "valid password 123"}, 401),
    ("/api/account/recovery/confirm", {"token": "x", "password": "valid password 123"}, 400),
])
@pytest.mark.parametrize("declared", [None, 0, 4096])
def test_exact_credential_boundary_with_missing_or_dishonest_length(stack, path, payload, expected, declared):
    raw = jbody(payload)
    raw += b" " * (4096 - len(raw))
    reply = call(stack.guarded, "POST", path, headers=json_headers(length=declared), chunks=chunked(raw))
    assert reply.status == expected, reply.body
    reply = call(stack.guarded, "POST", path, headers=json_headers(length=declared), chunks=chunked(raw + b" "))
    assert (reply.status, reply.json()) == (413, {"error": "body_too_large"})


@pytest.mark.parametrize("path", ["/api/account/register", "/api/account/login"])
@pytest.mark.parametrize("declared", [None, "1", "00004096", "abc", "9" * 5000])
def test_credential_single_oversized_chunk_and_unread_tail(stack, path, declared):
    headers = json_headers()
    if declared is not None:
        headers.append((b"content-length", declared.encode()))
    reply = call(stack.guarded, "POST", path, headers=headers, chunks=(b" " * 1000000, b"unread"))
    assert (reply.status, reply.json()) == (413, {"error": "body_too_large"})
    assert reply.body_chunks_read == (0 if declared == "9" * 5000 else 1)
    assert stack.effects.hashes == stack.effects.verifies == stack.effects.db_provider == 0


def test_256_code_points_including_one_astral_character_are_accepted(stack):
    raw = credentials("mixed@example.test", "x" * 255 + "\U0001F600", ascii_only=False)
    assert call(stack.guarded, "POST", "/api/account/register", headers=json_headers(), chunks=(raw,)).status == 201
    assert call(stack.guarded, "POST", "/api/account/login", headers=json_headers(), chunks=(raw,)).status == 200


@pytest.mark.parametrize("path,expected", [
    ("/api/account/register", (415, {"error": "json_required"})),
    ("/api/account/login", (415, {"error": "json_required"})),
    ("/api/account/recovery/request", (415, {"error": "json_required"})),
    ("/api/account/recovery/confirm", (415, {"error": "json_required"})),
    ("/api/account/email-verification/request", (415, {"error": "json_required"})),
    ("/api/account/email-verification/confirm", (415, {"error": "json_required"})),
])
def test_all_account_json_routes_keep_the_media_type_contract(pair, path, expected):
    headers = [(b"origin", CANONICAL), (b"content-type", b"text/plain")]
    reply = same(both(pair, "POST", path, headers=headers, chunks=(b"{}",)))
    assert (reply.status, reply.json()) == expected


@pytest.mark.parametrize("path", [
    "/api/account/register", "/api/account/login", "/api/account/recovery/request",
    "/api/account/recovery/confirm", "/api/account/email-verification/request",
    "/api/account/email-verification/confirm",
])
def test_all_account_json_routes_keep_malformed_and_duplicate_key_behavior(pair, path):
    for s in pair:
        reply = call(s.guarded, "POST", path, headers=json_headers(), chunks=(b"{",))
        assert (reply.status, reply.json()) == (400, {"error": "invalid_json"})
    # Duplicates still take the last value; extra-field policy stays per reader.
    if path.endswith(("register", "login")):
        raw = b'{"email":"bad","email":"last@example.test","password":"a valid long password","extra":true}'
        expected = 201 if path.endswith("register") else 401
    elif path.endswith("recovery/request"):
        raw, expected = b'{"email":"bad","email":"unknown@example.test"}', 202
    elif path.endswith("/confirm"):
        raw, expected = b'{"token":"first","token":"x"}', 400
        if "recovery" in path:
            raw = raw[:-1] + b',"password":"a valid long password"}'
    else:
        raw, expected = b'{"extra":1,"extra":2}', 400
    for s in pair:
        reply = call(s.guarded, "POST", path, headers=json_headers(), chunks=(raw,))
        assert reply.status == expected, reply.body


@pytest.mark.parametrize("path,expected", [
    ("/api/account/logout", 200),
    ("/api/account/billing/checkout", 200),
    ("/api/account/billing/portal", 409),
])
@pytest.mark.parametrize("raw", [b"", b"arbitrary ignored body" * 1000])
def test_signed_in_bodyless_routes_never_read_a_body(stack, monkeypatch, path, expected, raw):
    from types import SimpleNamespace
    import kalillac_billing.router as billing_router
    calls = []
    def checkout(*args, **kwargs):
        calls.append("checkout")
        return SimpleNamespace(outcome="created", url="https://checkout.stripe.test/fake")
    monkeypatch.setattr(billing_router, "start_checkout", checkout)
    headers = [(b"origin", CANONICAL), (b"cookie", f"{COOKIE}={token_for('a')}".encode()),
               (b"content-length", str(len(raw)).encode())]
    reply = call(stack.guarded, "POST", path, headers=headers, chunks=(raw,))
    assert reply.status == expected, reply.body
    assert reply.body_chunks_read == 0
    assert calls == (["checkout"] if path.endswith("checkout") else [])


@pytest.mark.parametrize("route", [r for r in INVENTORY if r.family == "spaces" and r.method != "DELETE"])
def test_every_spaces_json_route_preserves_its_16384_byte_boundary(stack, route):
    raw = b"{}" + b" " * (16384 - 2)
    reply = call(stack.guarded, route.method, concrete(route.path), headers=json_headers(cookie="a"), chunks=chunked(raw))
    assert reply.status in (422, 404), reply.body
    reply = call(stack.guarded, route.method, concrete(route.path), headers=json_headers(cookie="a"), chunks=chunked(raw + b" "))
    assert (reply.status, reply.json()) == (413, {"error": "body_too_large"})


def test_real_application_inventory_with_every_feature_enabled():
    from test_spaces_config import FULL
    env = {
        **FULL, **EMAIL_ENV,
        "KALILLAC_SITE_ORIGIN": ORIGIN,
        "KALILLAC_EMAIL_PROVIDER": "resend", "KALILLAC_RESEND_API_KEY": "re_" + "a" * 32,
        "KALILLAC_BILLING_ENABLED": "true", "KALILLAC_STRIPE_SECRET_KEY": BILLING.secret_key,
        "KALILLAC_STRIPE_WEBHOOK_SECRET": BILLING.webhook_secret, "KALILLAC_STRIPE_PRICE_ID": BILLING.price_id,
        "KALILLAC_STRIPE_ALLOWED_PRICE_IDS": BILLING.price_id, "KALILLAC_STRIPE_MODE": "test",
        "KALILLAC_BILLING_SUCCESS_URL": BILLING.success_url, "KALILLAC_BILLING_CANCEL_URL": BILLING.cancel_url,
        "KALILLAC_BILLING_PORTAL_RETURN_URL": BILLING.portal_return_url,
    }
    result = _run_app(env, """
import app_fastapi_candidate as a
from kalillac_accounts.request_guard import INVENTORY, EXEMPT, UnsafeRequestGuard
registered = {(m.upper(), p) for p, ops in a.api.openapi()["paths"].items() for m in ops
              if m.upper() in {"POST", "PUT", "PATCH", "DELETE"}
              and p.startswith(("/api/account", "/api/billing", "/api/spaces"))}
assert registered == {(r.method, r.path) for r in INVENTORY} | set(EXEMPT)
assert sum(m.cls is UnsafeRequestGuard for m in a.api.user_middleware) == 1
print("inventory", len(registered), "attempts", len(attempts))
""")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "inventory 18 attempts 0"


@pytest.mark.parametrize("fields,query", [
    ((), b""),
    (((b"origin", b"https://evil.test"),), b"x=1"),
    (((b"Origin", CANONICAL), (b"origin", CANONICAL)), b"&"),
    ((), b"&&"),
])
def test_signed_stripe_webhook_keeps_local_signature_verification_and_raw_body(fields, query):
    import hashlib
    import hmac
    import time
    from kalillac_billing.stripe_gateway import StripeGateway

    payload = jbody({"id": "evt_local_guard", "object": "event", "type": "ping",
                     "livemode": False, "data": {"object": {}}})
    timestamp = str(int(time.time()))
    digest = hmac.new(BILLING.webhook_secret.encode(), timestamp.encode() + b"." + payload, hashlib.sha256).hexdigest()
    signature = f"t={timestamp},v1={digest}".encode()
    plain = FastAPI()
    plain.include_router(build_billing_router(
        config=BILLING, settings=SETTINGS, gateway=StripeGateway(BILLING), session_factory=lambda: object()))
    guarded = UnsafeRequestGuard(plain, origin=ORIGIN)
    headers = [*fields, (b"stripe-signature", signature)]
    chunks = chunked(payload, 17)
    reply = call(guarded, "POST", "/api/billing/stripe/webhook", headers=headers, query=query, chunks=chunks)
    assert (reply.status, reply.json(), reply.body_chunks_read) == (
        200, {"received": True, "result": "ignored"}, len(chunks))
    # Same headers/signature, changed raw bytes: the webhook still refuses it.
    reply = call(guarded, "POST", "/api/billing/stripe/webhook",
                 headers=headers, query=query, chunks=(payload + b" ",))
    assert (reply.status, reply.json()) == (400, {"error": "invalid_signature"})


def test_safe_and_anonymous_routes_pass_through_the_guard_unchanged():
    seen = []
    async def inner(scope, receive, send):
        seen.append((scope["method"], scope["path"], scope["query_string"]))
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})
    guarded = UnsafeRequestGuard(inner, origin=ORIGIN)
    routes = [
        ("GET", "/api/account/me"), ("GET", "/api/account/entitlements"), ("GET", "/api/account/usage"),
        ("GET", "/api/account/billing"), ("GET", "/api/spaces"), ("GET", "/api/spaces/a"),
        ("GET", "/api/spaces/a/conversations"), ("GET", "/api/spaces/a/conversations/b"),
        ("POST", "/api/chat"), ("POST", "/api/chat/session/end"),
    ]
    for method, path in routes:
        assert call(guarded, method, path, query=b"&").status == 204
    assert seen == [(method, path, b"&") for method, path in routes]
