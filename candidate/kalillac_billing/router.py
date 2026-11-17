"""Billing HTTP routes.

Account routes (cookie-authenticated, same rules as every account endpoint):
- POST /api/account/billing/checkout: hosted subscription Checkout URL
- POST /api/account/billing/portal:   Customer Portal URL
- GET  /api/account/billing:          read-only Kalillac-facing state

Stripe route (authenticated only by the Stripe signature):
- POST /api/billing/stripe/webhook

While email verification is enabled (AccountSettings), Checkout refuses an
unverified account with 403 email_verification_required before any Stripe
call or billing write.

Request bodies on the account routes are ignored entirely: the Price,
customer, URLs, and tier are all server-controlled. No route here writes
billing state or entitlement; only verified webhook reconciliation does.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from kalillac_accounts.auth import (
    RequestRejected, authorize_original_identity, resolve_original_identity,
)
from kalillac_accounts.router import AccountSettings, load_account_settings
from kalillac_accounts.tokens import hash_session_token
from kalillac_db.engine import get_session_factory
from kalillac_db.repositories.accounts import AccountAuthorizationUnavailable, get_signed_in_user
from kalillac_db.repositories.billing import get_billing

from .config import BillingConfig
from .gateway import BillingGateway, BillingProviderError, InvalidWebhookSignature
from .service import (
    ReconciliationConflict,
    SubscriptionStateInvalid,
    WebhookModeMismatch,
    process_webhook,
    start_checkout,
)


# Stripe events are small; this bounds memory for unauthenticated input.
MAX_WEBHOOK_BYTES = 512 * 1024

SessionFactoryProvider = Callable[[], sessionmaker[Session] | None]


def _json(content: dict[str, Any], status: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content=content,
        headers={"Cache-Control": "no-store"},
    )


def _error(status: int, code: str) -> JSONResponse:
    return _json({"error": code}, status)


async def _read_limited_body(request: Request) -> bytes | None:
    """The raw body, or None if it exceeds MAX_WEBHOOK_BYTES."""

    declared = request.headers.get("content-length")

    if declared is not None:
        try:
            if int(declared) > MAX_WEBHOOK_BYTES:
                return None
        except ValueError:
            return None

    body = bytearray()

    async for chunk in request.stream():
        body.extend(chunk)

        if len(body) > MAX_WEBHOOK_BYTES:
            return None

    return bytes(body)


def build_billing_router(
    *,
    config: BillingConfig,
    settings: AccountSettings | None = None,
    gateway: BillingGateway | None = None,
    session_factory: SessionFactoryProvider = get_session_factory,
    v1_security: Any = None,
) -> APIRouter:
    """v1_security (V1 account security on): every account route authorizes through V1-01's read of the
    cookie's original session, never the legacy lookup, and returns its refusals as they are (401, 403;
    503 account_unavailable when the authorization cannot be established). Checkout also repeats that
    authorization inside each planning transaction, under the account lock, before any Stripe call."""
    settings = settings or load_account_settings()

    if gateway is None:
        from .stripe_gateway import StripeGateway

        gateway = StripeGateway(config)

    router = APIRouter()

    def factory() -> sessionmaker[Session]:
        resolved = session_factory()

        if resolved is None:
            raise RuntimeError("Kalillac database support is disabled.")

        return resolved

    async def signed_in(request: Request, work: Callable[[Session, Any], Any]):
        """("ok", work(session, user)) for the signed-in account, else the
        refusal response. user has .id and .email_verified_at."""

        token = request.cookies.get(settings.cookie_name)

        if not token:
            return _error(401, "not_authenticated")

        token_hash = hash_session_token(token)

        if v1_security is not None:
            from kalillac_accounts.v1_security import V1SecurityUnavailable

            try:
                return await v1_security.read_async(
                    token_hash, lambda session, account: ("ok", work(session, account), token_hash),
                )
            except RequestRejected as rejected:
                return _error(rejected.status, rejected.code)
            except V1SecurityUnavailable:
                return _error(503, "account_unavailable")

        now = datetime.now(timezone.utc)

        def run():
            with factory()() as session, session.begin():
                user = get_signed_in_user(session, token_hash, now)
                return None if user is None else ("ok", work(session, user), token_hash)

        found = await run_in_threadpool(run)
        return _error(401, "not_authenticated") if found is None else found

    def v1_recheck(token_hash: str, user_id):
        """Inside a Checkout planning transaction, before its account lock: V1-01's authorization of the
        same original session for the same account. It locks the account row FOR UPDATE, the same lock
        billing then takes, so nothing waits to upgrade."""

        def authorize(session: Session) -> None:
            identity = resolve_original_identity(session, token_hash)
            if identity is None or identity.user_id != user_id:
                raise RequestRejected(401, "not_authenticated")
            authorize_original_identity(session, identity)

        return authorize

    @router.post("/api/account/billing/checkout")
    async def checkout(request: Request):
        found = await signed_in(
            request,
            lambda session, user: (user.id, user.email_verified_at is not None),
        )

        if isinstance(found, JSONResponse):
            return found

        (user_id, email_verified), token_hash = found[1], found[2]

        # Read-only so far: refused before start_checkout, so before any
        # Stripe call or billing write. Portal, webhooks and reconciliation
        # are unaffected, and a verified address never becomes unverified.
        if settings.email_verification_enabled and not email_verified:
            return _error(403, "email_verification_required")

        # Attempt ids, session ids, keys, and expiry are all server-side;
        # the request body is never read.
        try:
            result = await run_in_threadpool(
                lambda: start_checkout(
                    user_id,
                    gateway=gateway,
                    session_factory=factory(),
                    price_id=config.price_id,
                    success_url=config.success_url,
                    cancel_url=config.cancel_url,
                    **({} if v1_security is None
                       else {"authorize": v1_recheck(token_hash, user_id)}),
                )
            )
        except BillingProviderError:
            print("WARN: STRIPE_CHECKOUT_FAILED")
            return _error(502, "billing_unavailable")
        except LookupError:
            return _error(401, "not_authenticated")
        except RequestRejected as refused:
            # Only the V1 recheck raises this (authorize is passed only in V1 mode).
            return _error(refused.status, refused.code)
        except AccountAuthorizationUnavailable:
            return _error(503, "account_unavailable")

        if result.outcome == "already_subscribed":
            # One paid tier: manage the existing subscription in the Portal.
            return _error(409, "already_subscribed")

        if result.outcome == "subscription_unresolved":
            # An existing subscription is neither paying nor explicitly
            # ended; it must be resolved (e.g. in the Portal) first.
            return _error(409, "subscription_unresolved")

        if result.outcome == "billing_processing":
            # Checkout completed; waiting for the verified webhook.
            return _error(409, "billing_processing")

        # Creating or reusing a Checkout Session grants nothing; only a
        # verified webhook can change entitlement.
        return _json({"checkout_url": result.url})

    @router.post("/api/account/billing/portal")
    async def portal(request: Request):
        def customer(session: Session, user) -> str | None:
            billing = get_billing(session, user.id)
            return billing.stripe_customer_id if billing is not None else None

        found = await signed_in(request, customer)

        if isinstance(found, JSONResponse):
            return found

        customer_id = found[1]

        if not customer_id:
            return _error(409, "billing_profile_unavailable")

        try:
            url = await run_in_threadpool(
                lambda: gateway.create_portal_session(
                    customer_id=customer_id,
                    return_url=config.portal_return_url,
                )
            )
        except BillingProviderError:
            print("WARN: STRIPE_PORTAL_FAILED")
            return _error(502, "billing_unavailable")

        return _json({"portal_url": url})

    @router.get("/api/account/billing")
    async def billing_status(request: Request):
        def describe(session: Session, user) -> dict[str, Any]:
            billing = get_billing(session, user.id)
            period_end = billing.current_period_end if billing else None

            if period_end is not None and period_end.tzinfo is None:
                period_end = period_end.replace(tzinfo=timezone.utc)

            return {
                "billing": {
                    "subscription_status": (
                        billing.subscription_status if billing else None
                    ),
                    "cancel_at_period_end": (
                        billing.cancel_at_period_end if billing else False
                    ),
                    "current_period_end": (
                        period_end.isoformat() if period_end else None
                    ),
                    "portal_available": bool(
                        billing and billing.stripe_customer_id
                    ),
                }
            }

        found = await signed_in(request, describe)

        if isinstance(found, JSONResponse):
            return found

        return _json(found[1])

    @router.post("/api/billing/stripe/webhook")
    async def stripe_webhook(request: Request):
        signature = request.headers.get("stripe-signature")

        if not signature:
            return _error(400, "invalid_signature")

        payload = await _read_limited_body(request)

        if payload is None:
            return _error(413, "payload_too_large")

        try:
            outcome = await run_in_threadpool(
                lambda: process_webhook(
                    payload,
                    signature,
                    gateway=gateway,
                    session_factory=factory(),
                    allowed_price_ids=config.allowed_price_ids,
                    livemode=config.livemode,
                )
            )
        except InvalidWebhookSignature:
            return _error(400, "invalid_signature")
        except WebhookModeMismatch:
            # Signed, but for the other Stripe mode: nothing read, written,
            # or recorded.
            print("WARN: STRIPE_WEBHOOK_MODE_MISMATCH")
            return _error(400, "livemode_mismatch")
        except SubscriptionStateInvalid:
            # A paying subscription without a usable period end: rolled
            # back and unrecorded, so Stripe retries.
            print("WARN: STRIPE_WEBHOOK_INVALID_SUBSCRIPTION_STATE")
            return _error(500, "subscription_state_invalid")
        except ReconciliationConflict:
            # Two different nonterminal subscriptions for one account:
            # nothing written, event left unrecorded, so Stripe keeps
            # reporting it until an operator resolves it.
            print("WARN: STRIPE_WEBHOOK_CONFLICT")
            return _error(409, "reconciliation_conflict")
        except Exception as exc:
            # Not recorded as processed; Stripe will retry. Class name only.
            print(f"WARN: STRIPE_WEBHOOK_FAILED {type(exc).__name__}")
            return _error(500, "webhook_processing_failed")

        return _json({"received": True, "result": outcome.result})

    return router
