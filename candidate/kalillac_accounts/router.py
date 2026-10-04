"""Account HTTP endpoints: register, login, logout, current user.

Authentication is a server-side sign-in session. The browser holds an opaque
token in an HttpOnly, Secure, SameSite=Lax cookie; the database stores only
its SHA-256 digest, so logout revokes it immediately.

This is account identity only. It never touches Kalillac's chat pipeline:
the account cookie is not read by /api/chat, and signing in does not create
saved chats, conversation history, or memory.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
import re
from typing import Any, Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from kalillac_db.engine import get_session_factory
from kalillac_db.models import User
from kalillac_db.repositories.accounts import (
    EmailAlreadyRegistered,
    create_account_session,
    create_user,
    get_signed_in_user,
    get_user_by_email,
    revoke_account_session,
    set_password_hash,
)

from .passwords import (
    MAX_PASSWORD_LENGTH,
    hash_password,
    needs_rehash,
    password_length_ok,
    verify_password,
)
from .rate_limit import (
    LoginRateLimiter,
    client_identifier,
    trust_cloudflare_client_ip,
)
from .tokens import hash_session_token, new_session_token


_TRUE_VALUES = {"1", "true", "yes", "on"}

MAX_EMAIL_LENGTH = 320

# Deliberately simple: one @, no whitespace, a dotted domain. Deliverability
# is a later email-verification concern, not a format check.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

MAX_BODY_BYTES = 4096


@dataclass(frozen=True)
class AccountSettings:
    cookie_secure: bool = True
    session_days: int = 14
    # See rate_limit.client_identifier before enabling.
    trust_cloudflare_client_ip: bool = False

    @property
    def cookie_name(self) -> str:
        # __Host- binds the cookie to this exact host, path /, and HTTPS.
        return (
            "__Host-kalillac_account"
            if self.cookie_secure
            else "kalillac_account"
        )


def load_account_settings() -> AccountSettings:
    secure = os.getenv("KALILLAC_ACCOUNT_COOKIE_SECURE", "true")

    try:
        days = int(os.getenv("KALILLAC_ACCOUNT_SESSION_DAYS", "14"))
    except ValueError:
        days = 14

    return AccountSettings(
        cookie_secure=secure.strip().lower() in _TRUE_VALUES,
        session_days=max(1, days),
        trust_cloudflare_client_ip=trust_cloudflare_client_ip(),
    )


def accounts_enabled() -> bool:
    value = os.getenv("KALILLAC_ACCOUNTS_ENABLED", "")
    return value.strip().lower() in _TRUE_VALUES


def normalize_email(value: Any) -> str | None:
    if not isinstance(value, str):
        return None

    email = value.strip().lower()

    if len(email) > MAX_EMAIL_LENGTH or not _EMAIL_RE.match(email):
        return None

    return email


def _error(status: int, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": code},
        headers={"Cache-Control": "no-store"},
    )


def _user_json(user: User) -> dict[str, Any]:
    # Identity only; never the password hash.
    return {
        "user": {
            "id": str(user.id),
            "email": user.email,
            "created_at": user.created_at.isoformat(),
        }
    }


async def _read_credentials(request: Request) -> tuple[str, str] | JSONResponse:
    content_type = request.headers.get("content-type", "")

    # JSON-only bodies: a cross-site HTML form cannot send application/json.
    if not content_type.lower().startswith("application/json"):
        return _error(415, "json_required")

    raw = await request.body()

    if len(raw) > MAX_BODY_BYTES:
        return _error(413, "body_too_large")

    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return _error(400, "invalid_json")

    if not isinstance(body, dict):
        return _error(400, "invalid_body")

    email = normalize_email(body.get("email"))
    password = body.get("password")

    if email is None:
        return _error(422, "invalid_email")

    if not isinstance(password, str):
        return _error(422, "invalid_password")

    return email, password


def build_account_router(
    session_factory: Callable[[], sessionmaker[Session] | None] = (
        get_session_factory
    ),
    settings: AccountSettings | None = None,
    limiter: LoginRateLimiter | None = None,
) -> APIRouter:
    """Build the /api/account router.

    session_factory returns the SQLAlchemy sessionmaker (tests inject one).
    limiter defaults to an in-process failed-login limiter, correct for the
    current single Uvicorn worker (see rate_limit.py).
    """

    settings = settings or load_account_settings()
    limiter = limiter or LoginRateLimiter()
    router = APIRouter(prefix="/api/account")

    def run_in_transaction(work: Callable[[Session], Any]) -> Any:
        factory = session_factory()

        if factory is None:
            raise RuntimeError("Kalillac database support is disabled.")

        with factory() as session:
            with session.begin():
                return work(session)

    def now() -> datetime:
        return datetime.now(timezone.utc)

    def set_session_cookie(response: Response, token: str) -> None:
        response.set_cookie(
            key=settings.cookie_name,
            value=token,
            max_age=settings.session_days * 86400,
            path="/",
            secure=settings.cookie_secure,
            httponly=True,
            samesite="lax",
        )

    def clear_session_cookie(response: Response) -> None:
        response.delete_cookie(
            key=settings.cookie_name,
            path="/",
            secure=settings.cookie_secure,
            httponly=True,
            samesite="lax",
        )

    @router.post("/register")
    async def register(request: Request):
        credentials = await _read_credentials(request)

        if isinstance(credentials, JSONResponse):
            return credentials

        email, password = credentials

        if not password_length_ok(password):
            return _error(422, "password_length")

        # Hash outside the transaction; Argon2 is deliberately slow.
        password_hash = await run_in_threadpool(hash_password, password)

        def work(session: Session) -> dict[str, Any]:
            return _user_json(create_user(session, email, password_hash))

        try:
            payload = await run_in_threadpool(run_in_transaction, work)
        except EmailAlreadyRegistered:
            return _error(409, "email_already_registered")

        # Registration does not sign in or create any conversation state.
        return JSONResponse(
            status_code=201,
            content=payload,
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/login")
    async def login(request: Request):
        credentials = await _read_credentials(request)

        if isinstance(credentials, JSONResponse):
            return credentials

        email, password = credentials

        limit_keys = limiter.keys_for(
            client_identifier(
                request.client.host if request.client else None,
                request.headers,
                settings.trust_cloudflare_client_ip,
            ),
            email,
        )

        # Checked before any password work and identical for registered and
        # unregistered emails, so a 429 reveals nothing about the account.
        decision = limiter.begin(limit_keys)

        if not decision.allowed:
            response = _error(429, "too_many_attempts")
            response.headers["Retry-After"] = str(decision.retry_after_seconds)
            return response

        # Bounds Argon2 work; such a password can never have been registered.
        if len(password) > MAX_PASSWORD_LENGTH:
            limiter.record_failure(limit_keys)
            return _error(401, "invalid_credentials")

        token = new_session_token()
        issued_at = now()

        def work(session: Session) -> dict[str, Any] | None:
            user = get_user_by_email(session, email)

            # Unknown email still runs Argon2 (dummy hash) for equal timing.
            password_ok = verify_password(
                user.password_hash if user else None,
                password,
            )

            if user is None or not password_ok or not user.is_active:
                return None

            if needs_rehash(user.password_hash):
                set_password_hash(session, user, hash_password(password))

            create_account_session(
                session,
                user.id,
                hash_session_token(token),
                issued_at + timedelta(days=settings.session_days),
            )

            return _user_json(user)

        try:
            payload = await run_in_threadpool(run_in_transaction, work)
        except BaseException:
            # A server-side error is not a failed guess; free the slot.
            limiter.release(limit_keys)
            raise

        if payload is None:
            limiter.record_failure(limit_keys)
            return _error(401, "invalid_credentials")

        limiter.record_success(limit_keys)

        response = JSONResponse(
            content=payload,
            headers={"Cache-Control": "no-store"},
        )
        set_session_cookie(response, token)
        return response

    @router.post("/logout")
    async def logout(request: Request):
        token = request.cookies.get(settings.cookie_name)

        if token:
            token_hash = hash_session_token(token)
            revoked_at = now()

            await run_in_threadpool(
                run_in_transaction,
                lambda session: revoke_account_session(
                    session,
                    token_hash,
                    revoked_at,
                ),
            )

        # Idempotent: logging out without a session still succeeds.
        response = JSONResponse(
            content={"ok": True},
            headers={"Cache-Control": "no-store"},
        )
        clear_session_cookie(response)
        return response

    @router.get("/me")
    async def me(request: Request):
        token = request.cookies.get(settings.cookie_name)

        if not token:
            return _error(401, "not_authenticated")

        token_hash = hash_session_token(token)
        checked_at = now()

        def work(session: Session) -> dict[str, Any] | None:
            user = get_signed_in_user(session, token_hash, checked_at)
            return _user_json(user) if user else None

        payload = await run_in_threadpool(run_in_transaction, work)

        if payload is None:
            return _error(401, "not_authenticated")

        return JSONResponse(
            content=payload,
            headers={"Cache-Control": "no-store"},
        )

    return router
