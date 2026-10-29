"""Default-off email-verification endpoints: bounded JSON bodies, privacy-safe failures.

POST /api/account/email-verification/request   (signed in; body exactly {})
POST /api/account/email-verification/confirm   (body exactly {"token": ...})

The destination is always the signed-in account's current email; callers
never supply an address. Confirmation never signs in, changes a password or
revokes sessions.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from kalillac_db.engine import get_session_factory
from kalillac_db.repositories.accounts import get_signed_in_user
from kalillac_db.repositories.email_verification import InvalidVerificationToken

from .email_verification import VerificationService, utc_now, valid_verification_token
from .rate_limit import client_identifier
from .recovery_router import _read_body
from .router import load_account_settings
from .tokens import hash_session_token
from .verification_config import load_verification_config
from .verification_rate_limit import VerificationRateLimiter


def _response(status=200, error=None, *, background=None):
    return JSONResponse(
        {"error": error} if error else {"ok": True},
        status_code=status, headers={"Cache-Control": "no-store"}, background=background,
    )


def _limited(decision):
    response = _response(429, "too_many_attempts")
    response.headers["Retry-After"] = str(decision.retry_after_seconds)
    return response


def build_email_verification_router(
    *, session_factory=get_session_factory, gateway=None, settings=None,
    limiter=None, clock=utc_now, environ=None,
):
    """No routes while off; when on, validate all prerequisites before mounting."""
    config = load_verification_config(gateway, environ)
    router = APIRouter(prefix="/api/account/email-verification")
    if config is None:
        return router
    settings = settings or load_account_settings()
    limiter = limiter if limiter is not None else VerificationRateLimiter()
    service = VerificationService(session_factory, gateway, config, clock)

    def client(request):
        return client_identifier(
            request.client.host if request.client else None, request.headers,
            settings.trust_cloudflare_client_ip,
        )

    def signed_in_account(token_hash):
        """(user id, normalized email) for a live session, else None."""
        factory = session_factory()
        if factory is None:
            raise RuntimeError()
        with factory() as session, session.begin():
            user = get_signed_in_user(session, token_hash, datetime.now(timezone.utc))
            return None if user is None else (user.id, user.email)

    @router.post("/request")
    async def request_verification(request: Request):
        try:
            body = await _read_body(request, set())
            if isinstance(body, JSONResponse):
                return body
            decision = limiter.request_client(client(request))
            if not decision.allowed:
                return _limited(decision)
            cookie = request.cookies.get(settings.cookie_name)
            account = None
            if cookie:
                account = await run_in_threadpool(signed_in_account, hash_session_token(cookie))
            if account is None:
                return _response(401, "not_authenticated")
            user_id, email = account
            # Eligibility (active, unverified) is decided only after the
            # response, under the account lock; every outcome here is 202.
            background = (
                BackgroundTask(service.request_verification, user_id)
                if limiter.request_account(str(user_id), email) else None
            )
            return _response(202, background=background)
        except Exception:
            print("ACCOUNT_EMAIL_VERIFICATION_REQUEST_FAILED")
            return _response(503, "verification_unavailable")

    @router.post("/confirm")
    async def confirm_verification(request: Request):
        attempt = None
        invalid = False
        try:
            body = await _read_body(request, {"token"})
            if isinstance(body, JSONResponse):
                return body
            decision, keys = limiter.begin_confirm(client(request))
            if not decision.allowed:
                return _limited(decision)
            attempt = keys
            token = body["token"]
            # Canonical encoding is checked before any database access.
            if not valid_verification_token(token):
                invalid = True
                return _response(400, "invalid_or_expired_token")
            try:
                await run_in_threadpool(service.confirm_verification, token)
            except InvalidVerificationToken:
                invalid = True
                return _response(400, "invalid_or_expired_token")
            # No cookie is set or cleared: verification never signs in or out.
            return _response()
        except Exception:
            print("ACCOUNT_EMAIL_VERIFICATION_CONFIRM_FAILED")
            return _response(503, "verification_unavailable")
        finally:
            if attempt is not None:
                try:
                    limiter.finish_confirm(attempt, invalid=invalid)
                except Exception:
                    print("ACCOUNT_EMAIL_VERIFICATION_LIMITER_FAILED")

    return router
