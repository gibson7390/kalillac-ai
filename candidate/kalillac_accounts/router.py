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
import uuid

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
from kalillac_db.repositories.billing import lock_account
from kalillac_db.repositories.entitlements import (
    create_default_entitlement,
    get_entitlement,
)

from .entitlements import effective_entitlement

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
from .password_work import PASSWORD_WORK_GATE, PasswordWorkBusy
from .registration_rate_limit import RegistrationRateLimiter


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
    # KALILLAC_ACCOUNT_EMAIL_VERIFICATION_ENABLED. While false, /me and
    # Checkout are exactly as before; while true, /me adds email_verified and
    # new Checkout sessions require a verified email.
    email_verification_enabled: bool = False

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
        email_verification_enabled=os.getenv(
            "KALILLAC_ACCOUNT_EMAIL_VERIFICATION_ENABLED", ""
        ).strip().lower() in _TRUE_VALUES,
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

    # An honest Content-Length over the limit is refused before any body is
    # read. A missing or dishonest one is caught while streaming: each chunk
    # is checked before it is kept, so no more than the limit is buffered.
    # A malformed Content-Length is ignored here, as it always was.
    declared = request.headers.get("content-length")
    if declared is not None and declared.isascii() and declared.isdigit():
        significant = declared.lstrip("0") or "0"
        if len(significant) > len(str(MAX_BODY_BYTES)) or int(significant) > MAX_BODY_BYTES:
            return _error(413, "body_too_large")

    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > MAX_BODY_BYTES:
            return _error(413, "body_too_large")
        raw.extend(chunk)

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


class _DuplicateKey(ValueError):
    pass


def _unique_keys(pairs):
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise _DuplicateKey()
    return dict(pairs)


async def _read_exact_body(request: Request, fields: set[str]) -> dict[str, Any] | JSONResponse:
    """A JSON object with exactly these keys (V1 account routes): bounded to MAX_BODY_BYTES while
    streaming, duplicate and extra keys refused."""
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        return _error(415, "json_required")
    declared = request.headers.get("content-length")
    if declared is not None and declared.isascii() and declared.isdigit():
        significant = declared.lstrip("0") or "0"
        if len(significant) > len(str(MAX_BODY_BYTES)) or int(significant) > MAX_BODY_BYTES:
            return _error(413, "body_too_large")
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > MAX_BODY_BYTES:
            return _error(413, "body_too_large")
        raw.extend(chunk)
    try:
        body = json.loads(raw, object_pairs_hook=_unique_keys)
    except _DuplicateKey:
        return _error(400, "invalid_body")
    except (ValueError, UnicodeDecodeError, RecursionError):
        return _error(400, "invalid_json")
    if type(body) is not dict or set(body) != fields:
        return _error(400, "invalid_body")
    return body


_UNAVAILABLE = object()       # The credential read could not answer (None means "no such account").


def _credential_shape(credential: Any) -> bool:
    """None (no account for the email), or exactly (account id, stored hash) from v1_credential."""
    return credential is None or (
        type(credential) is tuple and len(credential) == 2 and type(credential[0]) is uuid.UUID
        and type(credential[1]) is str and 0 < len(credential[1]) <= 255)


def _profile(session: Session, user_id: uuid.UUID) -> dict[str, Any] | None:
    """The sign-in response's identity, read as R from non-credential columns only (never the hash)."""
    from sqlalchemy import select
    row = session.execute(select(User.id, User.email, User.created_at).where(User.id == user_id)).one_or_none()
    if row is None:
        return None
    return {"user": {"id": str(row.id), "email": row.email, "created_at": row.created_at.isoformat()}}


def _contract_outcome(result: Any, kind: type, outcomes: frozenset[str]) -> str | None:
    """The outcome of the authority's own result type, only when it is exactly a str and one the
    contract defines; None (an unavailable authority) for anything else."""
    if type(result) is not kind:
        return None
    outcome = result.outcome
    if type(outcome) is not str or outcome not in outcomes:
        return None
    return outcome


def _authority_refusal(outcome: str | None, result: Any) -> JSONResponse:
    """The response for every outcome of a proven V1 operation other than its success."""
    if outcome == "not_authenticated":
        return _error(401, "not_authenticated")
    if outcome == "account_unavailable":
        return _error(403, "account_unavailable")
    if outcome == "too_many_operations":
        retry = result.retry_after_seconds
        if type(retry) is int and retry >= 1:
            response = _error(429, "too_many_operations")
            response.headers["Retry-After"] = str(retry)
            return response
    return _error(503, "security_unavailable")


def build_account_router(
    session_factory: Callable[[], sessionmaker[Session] | None] = (
        get_session_factory
    ),
    settings: AccountSettings | None = None,
    limiter: LoginRateLimiter | None = None,
    registration_limiter: RegistrationRateLimiter | None = None,
    password_gate=None,
    registration_http=None,
    v1_security=None,
    v1_authority=None,
) -> APIRouter:
    """Build the /api/account router.

    session_factory returns the SQLAlchemy sessionmaker (tests inject one).
    limiter defaults to an in-process failed-login limiter, correct for the
    current single Uvicorn worker (see rate_limit.py).

    V1 mode is v1_security being set. In V1 mode login creates sessions only
    through v1_authority (the reviewed issuance authority, as S); without it,
    login answers 503 and never falls back to the legacy session path. The
    application's startup gate still refuses V1 mode, and the application
    passes no authority, so V1 login is reachable only where both are injected.
    """

    settings = settings or load_account_settings()
    from .v1_security import load_v1_security
    v1_security = v1_security if v1_security is not None else load_v1_security(session_factory)
    limiter = limiter or LoginRateLimiter()
    registration_limiter = registration_limiter if registration_limiter is not None else RegistrationRateLimiter()
    password_gate = password_gate if password_gate is not None else PASSWORD_WORK_GATE
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

    @router.get("/registration-contract")
    async def registration_contract(request: Request):
        # A fresh, non-secret challenge binds the response to this request.
        # Authority is the handler actually mounted by this router factory.
        nonces = [value for name, value in request.scope["headers"]
                  if name.lower() == b"x-kalillac-registration-nonce"]
        headers = {"Cache-Control": "no-store", "Pragma": "no-cache",
                   "Vary": "X-Kalillac-Registration-Nonce"}
        if (request.scope.get("query_string", b"") or len(nonces) != 1
                or len(nonces[0]) != 32
                or any(c not in b"0123456789abcdef" for c in nonces[0])):
            return JSONResponse({"error": "invalid_request"}, status_code=400, headers=headers)
        return JSONResponse({"contract": "email-first-v1" if registration_http is not None else "legacy-v1",
                             "nonce": nonces[0].decode("ascii")}, headers=headers)

    @router.post("/register")
    async def register(request: Request):
        if registration_http is not None:
            return await registration_http.request(request)
        credentials = await _read_credentials(request)

        if isinstance(credentials, JSONResponse):
            return credentials

        email, password = credentials

        if not password_length_ok(password):
            return _error(422, "password_length")

        decision, attempt = registration_limiter.begin(client_identifier(
            request.client.host if request.client else None, request.headers,
            settings.trust_cloudflare_client_ip,
        ))
        if not decision.allowed:
            response = _error(429, "too_many_attempts")
            response.headers["Retry-After"] = str(decision.retry_after_seconds)
            return response
        try:
            # No transaction while waiting/hashing. Charge at actual worker start.
            password_hash = await password_gate.run(hash_password, password, on_start=attempt.charge)
        except PasswordWorkBusy:
            return _error(503, "busy")
        finally:
            attempt.release()

        def work(session: Session) -> dict[str, Any]:
            # One transaction: the user and its free entitlement are created
            # together or not at all.
            user = create_user(session, email, password_hash)
            create_default_entitlement(session, user.id)
            return _user_json(user)

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

        if v1_security is not None:
            return await v1_login(email, password, limit_keys)

        token = new_session_token()
        issued_at = now()

        def work(session: Session) -> dict[str, Any] | None:
            user = get_user_by_email(session, email)

            # Unknown email still runs Argon2 (dummy hash) for equal timing.
            password_ok = verify_password(
                user.password_hash if user else None,
                password,
            )

            if user is None or not password_ok or not user.is_active or (registration_http is not None and (user.email_verified_at is None or user.quarantined_at is not None)):
                return None

            # A password reset may commit while Argon2 runs. Recheck under the
            # account lock a reset also takes: either the reset sees and
            # revokes this session, or this login sees the replaced password.
            verified_hash = user.password_hash
            lock_account(session, user.id)
            session.refresh(user)

            if user.password_hash != verified_hash or not user.is_active or (registration_http is not None and (user.email_verified_at is None or user.quarantined_at is not None)):
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
            # One admission covers verification and optional rehash, without
            # recursively acquiring the permit. Wait before opening the transaction.
            payload = await password_gate.run(run_in_transaction, work)
        except PasswordWorkBusy:
            limiter.release(limit_keys)
            return _error(503, "busy")
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

    async def v1_login(email: str, password: str, limit_keys) -> JSONResponse:
        """V1 sign-in (D3). The password is verified here, as before, but
        nothing is written as the runtime role (no session, no rehash). The
        session comes only from the issuance authority, which locks the account
        and re-checks the verified hash and every account state (inactive,
        unverified, quarantined, deletion requested, reset required) before it
        inserts one. Refused: 401, no cookie. Unavailable or any unexpected
        answer: 503, no cookie, and never the legacy path."""
        from .v1_authority import SessionIssued, V1AuthorityUnavailable

        # The stored hash comes only through the reviewed credential read path (v1_credential, as S);
        # the runtime role never reads a password hash in V1 mode.
        try:
            credential = await call_authority("credential", email=email)
        except BaseException:
            limiter.release(limit_keys)
            raise
        if credential is _UNAVAILABLE or not _credential_shape(credential):
            limiter.release(limit_keys)
            return _error(503, "account_unavailable")

        try:
            # Unknown email still runs Argon2 (dummy hash) for equal timing.
            password_ok = await password_gate.run(verify_password, credential[1] if credential else None, password)
        except PasswordWorkBusy:
            limiter.release(limit_keys)
            return _error(503, "busy")
        except BaseException:
            limiter.release(limit_keys)
            raise

        if credential is None or password_ok is not True:
            limiter.record_failure(limit_keys)
            return _error(401, "invalid_credentials")

        user_id, verified_hash = credential
        try:
            payload = await run_in_threadpool(run_in_transaction, lambda session: _profile(session, user_id))
        except BaseException:
            limiter.release(limit_keys)
            raise
        if payload is None:
            limiter.release(limit_keys)
            return _error(503, "account_unavailable")
        token = new_session_token()
        try:
            if v1_authority is None:
                raise V1AuthorityUnavailable()
            issued = await run_in_threadpool(
                lambda: v1_authority.issue_session(
                    session_id=uuid.uuid4(),
                    token_hash=hash_session_token(token),
                    user_id=user_id,
                    verified_password_hash=verified_hash,
                    lifetime_days=settings.session_days,
                )
            )
        except V1AuthorityUnavailable:
            issued = None
        except BaseException:
            # Cancellation or a server error is not a failed guess; free the slot.
            limiter.release(limit_keys)
            raise

        # Only the authority's own result type with a contract outcome counts.
        # Anything else (None, another type, an unknown outcome) is an
        # unavailable authority. Each path below ends the attempt exactly once.
        outcome = issued.outcome if type(issued) is SessionIssued else None
        if type(outcome) is not str:
            # Exactly str, checked before any comparison: a look-alike or a str
            # subclass could define equality to match a contract value.
            outcome = None
        if outcome == "not_authenticated":
            limiter.record_failure(limit_keys)
            return _error(401, "invalid_credentials")
        if outcome != "issued":
            limiter.release(limit_keys)
            return _error(503, "account_unavailable")

        limiter.record_success(limit_keys)
        response = JSONResponse(content=payload, headers={"Cache-Control": "no-store"})
        set_session_cookie(response, token)
        return response

    @router.post("/logout")
    async def logout(request: Request):
        if v1_security is not None:
            return await v1_logout(request)
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

    async def v1_read(request: Request, entitlement=False):
        from .auth import RequestRejected, session_token_hash
        from .v1_security import V1SecurityUnavailable
        token_hash = session_token_hash(request.cookies, settings.cookie_name)
        if token_hash is None:
            return _error(401, "not_authenticated")

        def payload(session, account):
            if entitlement:
                return effective_entitlement(
                    get_entitlement(session, account.id), account.authorized_at,
                ).as_json()
            result = _user_json(account)
            if settings.email_verification_enabled:
                result["user"]["email_verified"] = True
            return result

        try:
            result = await v1_security.read_async(token_hash, payload)
        except RequestRejected as rejected:
            return _error(rejected.status, rejected.code)
        except V1SecurityUnavailable:
            return _error(503, "account_unavailable")
        return JSONResponse(content=result, headers={"Cache-Control": "no-store"})

    @router.get("/me")
    async def me(request: Request):
        if v1_security is not None:
            return await v1_read(request)
        token = request.cookies.get(settings.cookie_name)

        if not token:
            return _error(401, "not_authenticated")

        token_hash = hash_session_token(token)
        checked_at = now()

        def work(session: Session) -> dict[str, Any] | None:
            user = get_signed_in_user(session, token_hash, checked_at)

            if user is None:
                return None

            payload = _user_json(user)

            if settings.email_verification_enabled:
                # A boolean only; the verification time is never exposed.
                payload["user"]["email_verified"] = user.email_verified_at is not None

            return payload

        payload = await run_in_threadpool(run_in_transaction, work)

        if payload is None:
            return _error(401, "not_authenticated")

        return JSONResponse(
            content=payload,
            headers={"Cache-Control": "no-store"},
        )

    # Read-only by design: no HTTP route can change a tier. Tier changes
    # come only from internal, verified callers (later, billing).
    @router.get("/entitlements")
    async def entitlements(request: Request):
        if v1_security is not None:
            return await v1_read(request, entitlement=True)
        token = request.cookies.get(settings.cookie_name)

        if not token:
            return _error(401, "not_authenticated")

        token_hash = hash_session_token(token)
        checked_at = now()

        def work(session: Session) -> dict[str, Any] | None:
            user = get_signed_in_user(session, token_hash, checked_at)

            if user is None:
                return None

            return effective_entitlement(
                get_entitlement(session, user.id),
                checked_at,
            ).as_json()

        payload = await run_in_threadpool(run_in_transaction, work)

        if payload is None:
            return _error(401, "not_authenticated")

        return JSONResponse(
            content=payload,
            headers={"Cache-Control": "no-store"},
        )

    # --- V1 mode (D3): every account-state change goes through a private authority as S ------------

    async def v1_identify(request: Request, *, with_password: bool = False):
        """(identity, email, stored password hash or None) for the presented cookie's exact original
        session, through the authorized read as R (which writes nothing and never reads a hash); the
        stored hash, when asked for, only through the credential read path as S. Or the error response."""
        from .auth import RequestRejected, session_token_hash
        from .v1_security import V1SecurityUnavailable
        token_hash = session_token_hash(request.cookies, settings.cookie_name)
        if token_hash is None:
            return _error(401, "not_authenticated")
        try:
            identity, email = await v1_security.read_async(
                token_hash, lambda session, account, identity: (identity, account.email), with_identity=True)
        except RequestRejected as rejected:
            return _error(rejected.status, rejected.code)
        except V1SecurityUnavailable:
            return _error(503, "account_unavailable")
        if not with_password:
            return identity, email, None
        credential = await call_authority("credential", email=email)
        # The account the session belongs to, and no other.
        if (credential is _UNAVAILABLE or credential is None or not _credential_shape(credential)
                or credential[0] != identity.user_id):
            return _error(503, "account_unavailable")
        return identity, email, credential[1]

    async def call_authority(name: str, **arguments):
        """The authority's answer, or None when there is none or it cannot answer (the credential read
        answers _UNAVAILABLE instead, since None is its "no such account"). Anything else raised
        (cancellation, a server error) propagates to the caller."""
        from .v1_authority import V1AuthorityUnavailable
        unavailable = _UNAVAILABLE if name == "credential" else None
        if v1_authority is None:
            return unavailable
        try:
            return await run_in_threadpool(lambda: getattr(v1_authority, name)(**arguments))
        except V1AuthorityUnavailable:
            return unavailable

    async def v1_logout(request: Request) -> JSONResponse:
        """End exactly the presented session through v1_end_session. No cookie, or a digest bound to no
        session: nothing to end (200). Any binding is ended whatever the account's state. If the end
        cannot be confirmed, 503 and the cookie is kept, so signing out can be retried."""
        from .auth import RequestRejected, session_token_hash
        from .v1_security import V1SecurityUnavailable
        token_hash = session_token_hash(request.cookies, settings.cookie_name)
        if token_hash is not None:
            try:
                identity = await v1_security.read_async(
                    token_hash, lambda session, account, identity: identity, with_identity=True, authorize=False)
            except RequestRejected:
                identity = None
            except V1SecurityUnavailable:
                return _error(503, "account_unavailable")
            if identity is not None:
                ended = await call_authority("end_session", session_id=identity.session_id,
                                             token_hash=identity.token_hash)
                if not (type(ended) is str and ended == "ended"):
                    return _error(503, "account_unavailable")
        response = JSONResponse(content={"ok": True}, headers={"Cache-Control": "no-store"})
        clear_session_cookie(response)
        return response

    async def v1_revoke(request: Request, scope: str) -> JSONResponse:
        from .v1_authority import REVOKE_OUTCOMES, SessionsRevoked
        body = await _read_exact_body(request, set())
        if isinstance(body, JSONResponse):
            return body
        found = await v1_identify(request)
        if isinstance(found, JSONResponse):
            return found
        identity, _email, _stored = found
        result = await call_authority("revoke_sessions", operation_id=uuid.uuid4(), session_id=identity.session_id,
                                      token_hash=identity.token_hash, scope=scope)
        outcome = _contract_outcome(result, SessionsRevoked, REVOKE_OUTCOMES)
        if outcome != "revoked":
            return _authority_refusal(outcome, result)
        response = JSONResponse(content={"ok": True}, headers={"Cache-Control": "no-store"})
        if scope == "all":
            clear_session_cookie(response)       # The presenting session ended too.
        return response

    async def v1_proven(request: Request, fields: set[str], new_password_field: str | None, call, kind, outcomes,
                        success: str, end_session_cookie: bool) -> JSONResponse:
        """A signed-in customer's own operation proven by the current password: the body, the exact
        original session, the login limiter (shared with sign-in, keyed by the account's email), one
        password-work admission for verifying the current password (and hashing a new one), then the
        authority. Every path after the limiter admits the attempt ends it exactly once."""
        body = await _read_exact_body(request, fields)
        if isinstance(body, JSONResponse):
            return body
        current = body["current_password"]
        new = body[new_password_field] if new_password_field else None
        if type(current) is not str or (new_password_field and type(new) is not str):
            return _error(422, "invalid_password")
        if new_password_field and not password_length_ok(new):
            return _error(422, "password_length")
        found = await v1_identify(request, with_password=True)
        if isinstance(found, JSONResponse):
            return found
        identity, email, stored = found
        if type(stored) is not str or not stored:
            return _error(503, "account_unavailable")

        limit_keys = limiter.keys_for(
            client_identifier(request.client.host if request.client else None, request.headers,
                              settings.trust_cloudflare_client_ip),
            email,
        )
        decision = limiter.begin(limit_keys)
        if not decision.allowed:
            response = _error(429, "too_many_attempts")
            response.headers["Retry-After"] = str(decision.retry_after_seconds)
            return response
        if len(current) > MAX_PASSWORD_LENGTH:
            limiter.record_failure(limit_keys)
            return _error(403, "invalid_current_password")

        def work():
            if not verify_password(stored, current):
                return None
            return hash_password(new) if new_password_field else stored

        try:
            proven = await password_gate.run(work)
        except PasswordWorkBusy:
            limiter.release(limit_keys)
            return _error(503, "busy")
        except BaseException:
            limiter.release(limit_keys)
            raise
        if proven is None:
            limiter.record_failure(limit_keys)
            return _error(403, "invalid_current_password")

        arguments = {"operation_id": uuid.uuid4(), "session_id": identity.session_id,
                     "token_hash": identity.token_hash, "verified_password_hash": stored}
        if new_password_field:
            arguments["new_password_hash"] = proven
        try:
            result = await call_authority(call, **arguments)
        except BaseException:
            limiter.release(limit_keys)
            raise
        outcome = _contract_outcome(result, kind, outcomes)
        if outcome != success:
            # Not a wrong password: the session ended, the password changed meanwhile, or the
            # authority refused or could not answer.
            limiter.release(limit_keys)
            return _authority_refusal(outcome, result)
        limiter.record_success(limit_keys)
        response = JSONResponse(content={"ok": True}, headers={"Cache-Control": "no-store"})
        if end_session_cookie:
            clear_session_cookie(response)
        return response

    if v1_security is not None:
        @router.post("/sessions/revoke-others")
        async def revoke_other_sessions(request: Request):
            return await v1_revoke(request, "others")

        @router.post("/sessions/revoke-all")
        async def revoke_all_sessions(request: Request):
            return await v1_revoke(request, "all")

        @router.post("/password")
        async def change_password(request: Request):
            from .v1_authority import CHANGE_OUTCOMES, PasswordChanged
            # The current session stays; every other session ends.
            return await v1_proven(request, {"current_password", "new_password"}, "new_password",
                                   "change_password", PasswordChanged, CHANGE_OUTCOMES, "changed",
                                   end_session_cookie=False)

        @router.post("/deletion-request")
        async def request_deletion(request: Request):
            from .v1_authority import DeletionRequested, OWN_DELETION_OUTCOMES
            # Marks the account and ends every session, this one too. Nothing is erased here.
            return await v1_proven(request, {"current_password"}, None, "request_own_deletion",
                                   DeletionRequested, OWN_DELETION_OUTCOMES, "requested", end_session_cookie=True)

    if registration_http is not None:
        from .registration_http import confirmation_router
        router.include_router(confirmation_router(registration_http))
    return router
