"""Default-off recovery endpoints, bounded JSON bodies, and privacy-safe failures."""
from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from kalillac_db.engine import get_session_factory
from kalillac_db.repositories.recovery import InvalidRecoveryToken

from .passwords import hash_password, password_length_ok
from .rate_limit import client_identifier
from .recovery import RecoveryService, utc_now
from .recovery_config import load_recovery_config
from .recovery_rate_limit import RecoveryRateLimiter
from .recovery_tokens import valid_recovery_token
from .router import load_account_settings, normalize_email

MAX_BODY_BYTES = 4096


def _response(status=200, error=None, *, background=None):
    return JSONResponse(
        {"error": error} if error else {"ok": True},
        status_code=status, headers={"Cache-Control": "no-store"}, background=background,
    )


def _limited(decision):
    response = _response(429, "too_many_attempts")
    response.headers["Retry-After"] = str(decision.retry_after_seconds)
    return response


async def _read_body(request, fields):
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        return _response(415, "json_required")
    length = request.headers.get("content-length")
    if length is not None:
        if not length.isascii() or not length.isdigit():
            return _response(400, "invalid_body")
        # Avoid conversion of an attacker-sized integer.
        normalized_length = length.lstrip("0") or "0"
        if len(normalized_length) > 4 or int(normalized_length) > MAX_BODY_BYTES:
            return _response(413, "body_too_large")
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > MAX_BODY_BYTES:
            return _response(413, "body_too_large")
        raw.extend(chunk)
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return _response(400, "invalid_json")
    if not isinstance(body, dict) or set(body) != fields:
        return _response(400, "invalid_body")
    return body


def build_recovery_router(
    *, session_factory=get_session_factory, gateway=None, settings=None,
    limiter=None, clock=utc_now, environ=None,
):
    """No routes while off; when on, validate all prerequisites before mounting."""
    config = load_recovery_config(gateway, environ)
    router = APIRouter(prefix="/api/account/recovery")
    if config is None:
        return router
    settings = settings or load_account_settings()
    limiter = limiter if limiter is not None else RecoveryRateLimiter()
    service = RecoveryService(session_factory, gateway, config, clock)

    def client(request):
        return client_identifier(
            request.client.host if request.client else None, request.headers,
            settings.trust_cloudflare_client_ip,
        )

    @router.post("/request")
    async def request_reset(request: Request):
        try:
            body = await _read_body(request, {"email"})
            if isinstance(body, JSONResponse):
                return body
            email = normalize_email(body["email"])
            if email is None:
                return _response(422, "invalid_email")
            decision = limiter.request(client(request), email)
            if not decision.allowed:
                return _limited(decision)
            # Account existence is not read until after the public response.
            background = BackgroundTask(service.request_reset, email) if decision.send else None
            return _response(202, background=background)
        except Exception:
            print("ACCOUNT_RECOVERY_REQUEST_FAILED")
            return _response(202)

    @router.post("/confirm")
    async def confirm_reset(request: Request):
        attempt = None
        invalid = False
        try:
            body = await _read_body(request, {"token", "password"})
            if isinstance(body, JSONResponse):
                return body
            decision, keys = limiter.begin_confirm(client(request))
            if not decision.allowed:
                return _limited(decision)
            attempt = keys
            token, password = body["token"], body["password"]
            if not valid_recovery_token(token):
                invalid = True
                return _response(400, "invalid_or_expired_token")
            if not isinstance(password, str):
                return _response(422, "invalid_password")
            if not password_length_ok(password):
                return _response(422, "password_length")
            # Expensive Argon2 work is outside every database transaction.
            password_hash = await run_in_threadpool(hash_password, password)
            try:
                await run_in_threadpool(service.confirm_reset, token, password_hash)
            except InvalidRecoveryToken:
                invalid = True
                return _response(400, "invalid_or_expired_token")
            response = _response()
            # A reset never signs in, even if the browser presented an invalid cookie.
            response.delete_cookie(
                settings.cookie_name, path="/", secure=settings.cookie_secure,
                httponly=True, samesite="lax",
            )
            return response
        except Exception:
            print("ACCOUNT_RECOVERY_CONFIRM_FAILED")
            return _response(503, "recovery_unavailable")
        finally:
            if attempt is not None:
                try:
                    limiter.finish_confirm(attempt, invalid=invalid)
                except Exception:
                    print("ACCOUNT_RECOVERY_LIMITER_FAILED")

    return router
