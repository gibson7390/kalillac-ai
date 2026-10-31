"""Shared signed-in account resolution and unsafe-request protection.

The account id always comes from the authenticated cookie session
(get_signed_in_user). Nothing here accepts an account id from a request
body, query string or path.

Unsafe requests require exactly one raw Origin field equal to the configured
site origin. The application guard checks it before authentication; the
Spaces body helpers use the same predicate. JSON body contracts stay local.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from typing import Any
import uuid

from sqlalchemy.orm import Session

from kalillac_db.repositories.accounts import get_signed_in_user

from .request_guard import has_canonical_origin
from .tokens import hash_session_token


@dataclass(frozen=True)
class AuthContext:
    """The signed-in account for one request. token_hash identifies the
    session, so it can be rechecked later in the same request."""

    user_id: uuid.UUID
    email_verified: bool
    token_hash: str

    def __repr__(self) -> str:
        # Never print the session digest.
        return "AuthContext()"


class RequestRejected(Exception):
    """A request failed a pre-database check. status and code are public."""

    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status = status
        self.code = code


def session_token_hash(cookies: Any, cookie_name: str) -> str | None:
    token = cookies.get(cookie_name)
    return hash_session_token(token) if token else None


def resolve_account(session: Session, token_hash: str, now: datetime) -> AuthContext | None:
    """The active account for a live, unrevoked, unexpired session, or None."""
    user = get_signed_in_user(session, token_hash, now)
    if user is None:
        return None
    return AuthContext(
        user_id=user.id,
        email_verified=user.email_verified_at is not None,
        token_hash=token_hash,
    )


def require_same_origin(headers: Any, allowed_origin: str) -> None:
    """Use the guard's raw, case-insensitive field count and exact value rule."""
    raw = getattr(headers, "raw", None)
    if raw is None:
        # Mapping inputs are useful for callers without an ASGI Request.
        raw = [(name.encode("latin-1"), value.encode("latin-1"))
               for name, value in headers.items()]
    if not has_canonical_origin(raw, allowed_origin.encode("ascii")):
        raise RequestRejected(403, "origin_rejected")


async def read_json_object(request: Any, max_bytes: int) -> dict[str, Any]:
    """The request body as a JSON object. Raises RequestRejected with
    415 json_required, 413 body_too_large or 422 invalid_request."""
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise RequestRejected(415, "json_required")

    declared = request.headers.get("content-length")
    if declared is not None:
        if not declared.isdigit() or len(declared) > 9 or int(declared) > max_bytes:
            raise RequestRejected(413, "body_too_large")

    raw = b""
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > max_bytes:
            raise RequestRejected(413, "body_too_large")

    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise RequestRejected(422, "invalid_request") from None

    if not isinstance(body, dict):
        raise RequestRejected(422, "invalid_request")
    return body
