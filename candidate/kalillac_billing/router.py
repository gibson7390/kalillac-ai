"""Billing HTTP routes.

Account routes (cookie-authenticated, same rules as every account endpoint):
- POST /api/account/billing/checkout: hosted subscription Checkout URL
- POST /api/account/billing/portal:   Customer Portal URL
- GET  /api/account/billing:          read-only Kalillac-facing state

Stripe route (authenticated only by the Stripe signature):
- POST /api/billing/stripe/webhook

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

from kalillac_accounts.router import AccountSettings, load_account_settings
from kalillac_accounts.tokens import hash_session_token
from kalillac_db.engine import get_session_factory
from kalillac_db.repositories.accounts import get_signed_in_user
from kalillac_db.repositories.billing import get_billing

from .config import BillingConfig
from .gateway import BillingGateway, BillingProviderError, InvalidWebhookSignature
from .service import process_webhook, start_checkout


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
) -> APIRouter:
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
        """Run work(session, user) for the signed-in account, or return
        None when the request is not authenticated."""

        token = request.cookies.get(settings.cookie_name)

        if not token:
            return None

        token_hash = hash_session_token(token)
        now = datetime.now(timezone.utc)

        def run():
            with factory()() as session, session.begin():
                user = get_signed_in_user(session, token_hash, now)
                return None if user is None else ("ok", work(session, user))

        return await run_in_threadpool(run)

    @router.post("/api/account/billing/checkout")
    async def checkout(request: Request):
        found = await signed_in(request, lambda session, user: user.id)

        if found is None:
            return _error(401, "not_authenticated")

        user_id = found[1]

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
                )
            )
        except BillingProviderError:
            print("WARN: STRIPE_CHECKOUT_FAILED")
            return _error(502, "billing_unavailable")
        except LookupError:
            return _error(401, "not_authenticated")

        if result.outcome == "already_subscribed":
            # One paid tier: manage the existing subscription in the Portal.
            return _error(409, "already_subscribed")

        if result.outcome == "billing_processing":
            # Checkout completed; waiting for the verified webhook.
            return _error(409, "billing_processing")

        # Creating or reusing a Checkout Session grants nothing; only a
        # verified webhook can change entitlement.
        return _json({"checkout_url": result.url})

    @router.post("/api/account/billing/portal")
    async def portal(request: Request):
        found = await signed_in(
            request,
            lambda session, user: get_billing(session, user.id),
        )

        if found is None:
            return _error(401, "not_authenticated")

        billing = found[1]

        if billing is None or not billing.stripe_customer_id:
            return _error(409, "billing_profile_unavailable")

        customer_id = billing.stripe_customer_id

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

        if found is None:
            return _error(401, "not_authenticated")

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
                    paid_price_id=config.price_id,
                )
            )
        except InvalidWebhookSignature:
            return _error(400, "invalid_signature")
        except Exception as exc:
            # Not recorded as processed; Stripe will retry. Class name only.
            print(f"WARN: STRIPE_WEBHOOK_FAILED {type(exc).__name__}")
            return _error(500, "webhook_processing_failed")

        return _json({"received": True, "result": outcome.result})

    return router
