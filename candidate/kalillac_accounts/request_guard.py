"""Pre-authentication guard for unsafe account, billing and Spaces requests.

One order, for every route in INVENTORY:
  1. a nonempty raw query string     -> 400 query_not_allowed (account,
                                        billing) or 422 invalid_request
                                        (Spaces, its existing code)
  2. not exactly one Origin field, or
     its raw value is not byte-equal
     to the configured origin        -> 403 origin_rejected
  3. only then the route itself: authentication, then its own body policy
     (email-verification/request keeps its existing bounded body read
     before the cookie lookup).

The guard reads only scope["method"], scope["path"], scope["query_string"]
and scope["headers"]. It never reads the body, the cookie or the database,
so a rejected request causes no other work. Origin fields are counted from
the raw header list with case-insensitive names, so duplicates are seen
whatever their case. The allowed origin comes only from KALILLAC_SITE_ORIGIN,
never from Host or forwarding headers. An unset or malformed origin
prevents startup whenever accounts are enabled. With accounts disabled,
the setting is not read.

A generic ASGI application cannot see a bare "?": the server splits the
request target there and passes an empty query string, exactly as for no
"?" at all. Rejecting that needs the ingress.

The signed Stripe webhook is the only unsafe route under /api/account,
/api/billing or /api/spaces outside the guard (EXEMPT).
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import re
from typing import Any, Mapping
from urllib.parse import urlsplit

from starlette.responses import JSONResponse


SITE_ORIGIN_ENV = "KALILLAC_SITE_ORIGIN"

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost"}
_DEFAULT_PORTS = {"https": 443, "http": 80}

_ACCOUNT_HEADERS = {"Cache-Control": "no-store"}
_SPACES_HEADERS = {"Cache-Control": "no-store", "Vary": "Cookie"}


class GuardConfigError(RuntimeError):
    """KALILLAC_SITE_ORIGIN is missing or malformed. The message names the setting only."""


@dataclass(frozen=True)
class GuardedRoute:
    method: str
    path: str        # the route's path template, as FastAPI registers it
    family: str      # "account", "billing" or "spaces"
    pattern: re.Pattern[str]


def _route(method: str, path: str, family: str) -> GuardedRoute:
    pattern = re.compile(re.sub(r"\\\{[a-z_]+\\\}", "[^/]+", re.escape(path)))
    return GuardedRoute(method, path, family, pattern)


INVENTORY: tuple[GuardedRoute, ...] = (
    _route("POST", "/api/account/register", "account"),
    _route("POST", "/api/account/login", "account"),
    _route("POST", "/api/account/logout", "account"),
    _route("POST", "/api/account/recovery/request", "account"),
    _route("POST", "/api/account/recovery/confirm", "account"),
    _route("POST", "/api/account/email-verification/request", "account"),
    _route("POST", "/api/account/email-verification/confirm", "account"),
    _route("POST", "/api/account/billing/checkout", "billing"),
    _route("POST", "/api/account/billing/portal", "billing"),
    _route("POST", "/api/spaces", "spaces"),
    _route("PATCH", "/api/spaces/{space_id}", "spaces"),
    _route("PUT", "/api/spaces/{space_id}/instructions", "spaces"),
    _route("DELETE", "/api/spaces/{space_id}", "spaces"),
    _route("POST", "/api/spaces/{space_id}/conversations", "spaces"),
    _route("PATCH", "/api/spaces/{space_id}/conversations/{conversation_id}", "spaces"),
    _route("DELETE", "/api/spaces/{space_id}/conversations/{conversation_id}", "spaces"),
    _route("POST", "/api/spaces/{space_id}/conversations/{conversation_id}/messages", "spaces"),
)

# Server-to-server and authenticated by Stripe's signature over the raw body.
EXEMPT: tuple[tuple[str, str], ...] = (("POST", "/api/billing/stripe/webhook"),)


def guarded_route(method: str, path: str) -> GuardedRoute | None:
    for route in INVENTORY:
        if route.method == method and route.pattern.fullmatch(path):
            return route
    return None


def parse_guard_origin(value: str | None) -> str | None:
    """Parse the canonical Origin; load_guard_origin requires it when enabled.
    It must be exact: https://host[:port], or http:// on a
    loopback host for local development; lowercase; no default port, path,
    query, fragment, credentials or trailing slash."""
    origin = value or ""
    if not origin.strip():
        return None
    try:
        parts = urlsplit(origin)
        port = parts.port
    except ValueError:
        raise GuardConfigError(f"{SITE_ORIGIN_ENV} must be an exact origin.") from None
    host = parts.hostname or ""
    secure = parts.scheme == "https" and bool(host)
    local = parts.scheme == "http" and host in _LOOPBACK_HOSTS
    rebuilt = f"{parts.scheme}://{host}" + (f":{port}" if port is not None else "")
    if (
        not (secure or local)
        or parts.username
        or parts.password
        or port == _DEFAULT_PORTS.get(parts.scheme)
        or rebuilt != origin
        or not origin.isascii()
        or not origin.isprintable()
        or re.fullmatch(r"[a-z0-9.-]+", host) is None
        or any(char.isspace() for char in origin)
        or (port is not None and port < 1)
    ):
        raise GuardConfigError(f"{SITE_ORIGIN_ENV} must be an exact origin.")
    return origin


def load_guard_origin(environ: Mapping[str, str] = os.environ) -> str:
    origin = parse_guard_origin(environ.get(SITE_ORIGIN_ENV))
    if origin is None:
        raise GuardConfigError(f"{SITE_ORIGIN_ENV} must be an exact origin.")
    return origin


def has_canonical_origin(headers, origin: bytes | None) -> bool:
    """Count Origin fields from raw ASGI headers; compare the value unchanged."""
    fields = [value for name, value in headers if name.lower() == b"origin"]
    return origin is not None and len(fields) == 1 and fields[0] == origin


def rejection(route: GuardedRoute, scope: Mapping[str, Any], origin: bytes | None) -> JSONResponse | None:
    """The guard's response for this request, or None to let it through."""
    headers = _SPACES_HEADERS if route.family == "spaces" else _ACCOUNT_HEADERS
    if scope.get("query_string", b"") != b"":
        if route.family == "spaces":
            return JSONResponse(status_code=422, content={"error": "invalid_request"}, headers=dict(headers))
        return JSONResponse(status_code=400, content={"error": "query_not_allowed"}, headers=dict(headers))
    if not has_canonical_origin(scope.get("headers", []), origin):
        return JSONResponse(status_code=403, content={"error": "origin_rejected"}, headers=dict(headers))
    return None


class UnsafeRequestGuard:
    """Pure ASGI middleware applying rejection() to every INVENTORY route."""

    def __init__(self, app, *, origin: str | None) -> None:
        self.app = app
        self.origin = origin.encode("ascii") if origin is not None else None

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            route = guarded_route(scope["method"], scope["path"])
            if route is not None:
                response = rejection(route, scope, self.origin)
                if response is not None:
                    await response(scope, receive, send)
                    return
        await self.app(scope, receive, send)
