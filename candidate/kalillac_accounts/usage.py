"""Aggregate usage metering for signed-in accounts.

Enabled only by KALILLAC_USAGE_METERING_ENABLED. This is the one deliberate
place where /api/chat looks at the account cookie while quota enforcement is
off, and it does so only to
add numbers to the signed-in account's daily totals:

- recorded: successful chat count, request characters, response characters;
- never recorded: message, reply, or history text, prompts, the temporary
  chat session id, search queries or results, or any conversation id.

The account is resolved once, when a chat request is accepted and before
any chat work starts, with the same rules as every account endpoint
(get_signed_in_user; with V1 account security on, V1-01's authorization of
the original session instead, never the legacy lookup). A missing, forged,
expired, or revoked cookie, an account V1 refuses, an
inactive user, or a failed lookup means "anonymous" for that request and
nothing is recorded; signing out or in while it runs does not change it.
After that, only the account id and aggregate numbers travel to the write.
Neither the lookup nor the write ever raises into chat: failures are logged
by exception class only.

Character counts are Unicode code points. They are usage metrics, not
provider tokens or inference cost. No quota is enforced here. With daily
enforcement on, the separate quota path resolves identity and commits the
successful count; this meter adds only after-response character totals.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
import os
from typing import Any, Callable
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session, sessionmaker
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from kalillac_db.engine import get_session_factory
from kalillac_db.repositories.accounts import get_signed_in_user
from kalillac_db.repositories.usage import get_daily_usage, increment_daily_usage

from .router import AccountSettings, load_account_settings
from .tokens import hash_session_token


_TRUE_VALUES = {"1", "true", "yes", "on"}

SessionFactoryProvider = Callable[[], sessionmaker[Session] | None]


async def _v1_account_id(v1_security: Any, token_hash: str) -> uuid.UUID | None:
    """The V1-authorized account id, or None for any refusal (anonymous for the meter). An unavailable
    authorization raises, and the caller logs its class."""
    from .auth import RequestRejected

    try:
        account_id = await v1_security.read_async(token_hash, lambda session, account: account.id)
    except RequestRejected:
        return None

    return account_id if type(account_id) is uuid.UUID else None


def usage_metering_enabled() -> bool:
    value = os.getenv("KALILLAC_USAGE_METERING_ENABLED", "")
    return value.strip().lower() in _TRUE_VALUES


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def request_chars_for(message: str, history: list[dict[str, Any]]) -> int:
    """Characters of the validated request context: the current message
    plus the normalized user/assistant history sent with it."""

    return len(message) + sum(len(turn["content"]) for turn in history)


class UsageMeter:
    def __init__(
        self,
        session_factory: SessionFactoryProvider = get_session_factory,
        settings: AccountSettings | None = None,
        clock: Callable[[], datetime] = _utc_now,
        v1_security: Any = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings or load_account_settings()
        self._clock = clock
        # V1 account security on: the account is the one V1-01 authorizes, never the legacy lookup.
        self._v1_security = v1_security

    async def resolve_request_account(self, request: Request) -> uuid.UUID | None:
        """The signed-in account a chat request belongs to, decided once when
        the request is accepted, before any chat work starts.

        Uses the same rule as every account endpoint (get_signed_in_user).
        A missing, forged, expired, revoked, or inactive session means
        anonymous (None). That answer is fixed for the request: signing out,
        expiring, or signing in while it runs does not change it.

        Never raises (cancellation aside): a lookup failure is logged by
        exception class only and the request is treated as anonymous, so
        Private Session keeps working.
        """

        try:
            token = request.cookies.get(self._settings.cookie_name)

            if not token:
                return None

            # Only the digest is used; the raw token goes no further.
            token_hash = hash_session_token(token)
            del token

            if self._v1_security is not None:
                return await _v1_account_id(self._v1_security, token_hash)

            return await run_in_threadpool(self._lookup_account_id, token_hash)
        except Exception as exc:
            print(f"WARN: USAGE_METER_IDENTITY_FAILED {type(exc).__name__}")
            return None

    def _lookup_account_id(self, token_hash: str) -> uuid.UUID | None:
        factory = self._session_factory()

        if factory is None:
            raise RuntimeError("database support is disabled")

        with factory() as session, session.begin():
            user = get_signed_in_user(session, token_hash, self._clock())
            return user.id if user is not None else None

    def background_for_chat(
        self,
        user_id: uuid.UUID | None,
        message: str,
        history: list[dict[str, Any]],
        reply: str,
    ) -> BackgroundTask | None:
        """Background task recording one successful chat, or None when the
        request was anonymous at acceptance.

        Called only on the normal 200 path. The counts and the UTC date are
        computed here, so the task carries only (user_id, usage_date,
        request_chars, response_chars): no request object, cookie, token
        digest, message, history, reply, or chat session id. Starlette runs
        it after the response is sent, when every chat slot and session lock
        is already released; a cancelled handler never gets here.
        """

        if user_id is None:
            return None

        try:
            request_chars = request_chars_for(message, history)
            response_chars = len(reply)
            usage_date = self._clock().date()
        except Exception as exc:
            print(f"WARN: USAGE_METER_SETUP_FAILED {type(exc).__name__}")
            return None

        return BackgroundTask(
            self.record_usage,
            user_id,
            usage_date,
            request_chars,
            response_chars,
        )

    def record_usage(
        self,
        user_id: uuid.UUID,
        usage_date: date,
        request_chars: int,
        response_chars: int,
        *,
        successful_chats: int = 1,
    ) -> None:
        """Add one successful chat to an account's daily aggregate.

        successful_chats defaults to one for the original meter. The quota
        path passes zero after settling the count, preventing double counting.
        The account was resolved at request acceptance and is not checked
        again. Never raises: the chat has already succeeded, and a missed
        meter during a database incident is preferred over failing it.
        """

        try:
            factory = self._session_factory()

            if factory is None:
                raise RuntimeError("database support is disabled")

            with factory() as session, session.begin():
                increment_daily_usage(
                    session,
                    user_id,
                    usage_date,
                    request_chars=request_chars,
                    response_chars=response_chars,
                    **({} if successful_chats == 1 else {"successful_chats": successful_chats}),
                )
        except Exception as exc:
            # Class name only: no SQL, parameters, or account data.
            print(f"WARN: USAGE_METER_WRITE_FAILED {type(exc).__name__}")


def build_usage_router(
    session_factory: SessionFactoryProvider = get_session_factory,
    settings: AccountSettings | None = None,
    clock: Callable[[], datetime] = _utc_now,
    v1_security: Any = None,
) -> APIRouter:
    """GET /api/account/usage: the signed-in account's totals for today
    (UTC). Read-only; no HTTP route modifies counters. With V1 account
    security on, the account is the one V1-01 authorizes and its refusals
    are returned as they are (401, 403), 503 account_unavailable when the
    authorization cannot be established."""

    settings = settings or load_account_settings()
    router = APIRouter(prefix="/api/account")

    def error(status: int, code: str) -> JSONResponse:
        return JSONResponse(
            status_code=status,
            content={"error": code},
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/usage")
    async def usage(request: Request):
        token = request.cookies.get(settings.cookie_name)

        if not token:
            return error(401, "not_authenticated")

        token_hash = hash_session_token(token)
        now = clock()
        today = now.date()

        def describe(session: Session, user_id: uuid.UUID) -> dict[str, Any]:
            row = get_daily_usage(session, user_id, today)

            return {
                "usage": {
                    "date": today.isoformat(),
                    "successful_chats": row.successful_chats if row else 0,
                    "request_chars": row.request_chars if row else 0,
                    "response_chars": row.response_chars if row else 0,
                }
            }

        if v1_security is not None:
            from .auth import RequestRejected
            from .v1_security import V1SecurityUnavailable

            try:
                payload = await v1_security.read_async(
                    token_hash, lambda session, account: describe(session, account.id),
                )
            except RequestRejected as rejected:
                return error(rejected.status, rejected.code)
            except V1SecurityUnavailable:
                return error(503, "account_unavailable")

            return JSONResponse(content=payload, headers={"Cache-Control": "no-store"})

        def work() -> dict[str, Any] | None:
            factory = session_factory()

            if factory is None:
                raise RuntimeError("Kalillac database support is disabled.")

            with factory() as session, session.begin():
                user = get_signed_in_user(session, token_hash, now)

                if user is None:
                    return None

                return describe(session, user.id)

        payload = await run_in_threadpool(work)

        if payload is None:
            return error(401, "not_authenticated")

        return JSONResponse(
            content=payload,
            headers={"Cache-Control": "no-store"},
        )

    return router
