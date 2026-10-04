"""Stripe implementation of BillingGateway (stripe==16.0.0).

The only module that imports stripe. It verifies webhook signatures with
Stripe's library, and reduces every Stripe object to a Kalillac snapshot
before returning. Stripe errors are converted to BillingProviderError
without their messages, so nothing Stripe-internal reaches callers or logs.

API note: on the API version pinned by this SDK, current_period_end and the
price live on the subscription items, not the Subscription itself.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import stripe

from .config import BillingConfig
from .gateway import (
    BillingProviderError,
    CheckoutSessionState,
    InvalidWebhookSignature,
    SubscriptionState,
    WebhookEvent,
)


KALILLAC_USER_METADATA_KEY = "kalillac_user_id"


def _field(obj: Any, key: str) -> Any:
    if obj is None:
        return None

    if isinstance(obj, dict):
        return obj.get(key)

    return getattr(obj, key, None)


def _id_of(value: Any) -> str | None:
    """A Stripe reference may be an id string or an expanded object."""

    if value is None or isinstance(value, str):
        return value

    return _field(value, "id")


def _metadata_user_id(obj: Any) -> str | None:
    value = _field(_field(obj, "metadata"), KALILLAC_USER_METADATA_KEY)
    return value if isinstance(value, str) else None


def _timestamp(value: Any) -> datetime | None:
    if value is None:
        return None

    return datetime.fromtimestamp(int(value), tz=timezone.utc)


def subscription_state_from_stripe(subscription: Any) -> SubscriptionState:
    items = _field(_field(subscription, "items"), "data") or []
    first_item = items[0] if items else None

    return SubscriptionState(
        subscription_id=_field(subscription, "id"),
        customer_id=_id_of(_field(subscription, "customer")),
        status=_field(subscription, "status"),
        price_id=_id_of(_field(first_item, "price")),
        cancel_at_period_end=bool(_field(subscription, "cancel_at_period_end")),
        current_period_end=_timestamp(_field(first_item, "current_period_end")),
        kalillac_user_id=_metadata_user_id(subscription),
    )


def checkout_session_state_from_stripe(session: Any) -> CheckoutSessionState:
    return CheckoutSessionState(
        session_id=_field(session, "id"),
        status=_field(session, "status"),
        url=_field(session, "url"),
        expires_at=_timestamp(_field(session, "expires_at")),
        subscription_id=_id_of(_field(session, "subscription")),
    )


def _provider_error(exc: Exception) -> BillingProviderError:
    """An opaque provider error.

    No Stripe error class or status is treated as proof that an earlier
    request under the same idempotency key produced no Checkout Session.
    One request's 400 says nothing certain about a previous request under
    that key whose response was lost; an IdempotencyError (409 concurrent
    same-key request, or a parameter mismatch) means another request under
    the key exists. So every failure keeps the attempt and its key, and
    avoiding a double charge outranks automatic recovery.
    """

    return BillingProviderError()


def webhook_event_from_stripe(event: Any) -> WebhookEvent:
    event_type = _field(event, "type")
    obj = _field(_field(event, "data"), "object")

    if event_type == "checkout.session.completed":
        subscription_id = _id_of(_field(obj, "subscription"))
        kalillac_user_id = (
            _field(obj, "client_reference_id") or _metadata_user_id(obj)
        )
    elif isinstance(event_type, str) and event_type.startswith(
        "customer.subscription."
    ):
        subscription_id = _field(obj, "id")
        kalillac_user_id = _metadata_user_id(obj)
    else:
        subscription_id = None
        kalillac_user_id = None

    return WebhookEvent(
        event_id=_field(event, "id"),
        event_type=event_type,
        subscription_id=subscription_id,
        customer_id=_id_of(_field(obj, "customer")),
        kalillac_user_id=kalillac_user_id,
    )


class StripeGateway:
    def __init__(self, config: BillingConfig) -> None:
        self._config = config
        self._client = stripe.StripeClient(
            config.secret_key,
            max_network_retries=2,
        )

    def create_checkout_session(
        self,
        *,
        price_id: str,
        customer_id: str | None,
        kalillac_user_id: str,
        success_url: str,
        cancel_url: str,
        idempotency_key: str,
    ) -> CheckoutSessionState:
        params: dict[str, Any] = {
            "mode": "subscription",
            "line_items": [{"price": price_id, "quantity": 1}],
            "success_url": success_url,
            "cancel_url": cancel_url,
            "client_reference_id": kalillac_user_id,
            "metadata": {KALILLAC_USER_METADATA_KEY: kalillac_user_id},
            "subscription_data": {
                "metadata": {KALILLAC_USER_METADATA_KEY: kalillac_user_id},
            },
        }

        if customer_id:
            params["customer"] = customer_id

        try:
            # Stripe returns the original session for a repeated key, so
            # concurrent requests and crash retries cannot create a second.
            session = self._client.v1.checkout.sessions.create(
                params=params,
                options={"idempotency_key": idempotency_key},
            )
        except stripe.StripeError as exc:
            raise _provider_error(exc) from exc

        state = checkout_session_state_from_stripe(session)

        if not state.url:
            raise BillingProviderError()

        return state

    def retrieve_checkout_session(self, session_id: str) -> CheckoutSessionState:
        try:
            session = self._client.v1.checkout.sessions.retrieve(session_id)
        except stripe.StripeError as exc:
            raise _provider_error(exc) from exc

        return checkout_session_state_from_stripe(session)

    def create_portal_session(self, *, customer_id: str, return_url: str) -> str:
        try:
            session = self._client.v1.billing_portal.sessions.create(
                params={"customer": customer_id, "return_url": return_url}
            )
        except stripe.StripeError as exc:
            raise _provider_error(exc) from exc

        url = _field(session, "url")

        if not url:
            raise BillingProviderError()

        return url

    def retrieve_subscription(self, subscription_id: str) -> SubscriptionState:
        try:
            subscription = self._client.v1.subscriptions.retrieve(
                subscription_id
            )
        except stripe.StripeError as exc:
            raise _provider_error(exc) from exc

        return subscription_state_from_stripe(subscription)

    def parse_webhook(self, payload: bytes, signature: str) -> WebhookEvent:
        try:
            event = self._client.construct_event(
                payload,
                signature,
                self._config.webhook_secret,
            )
        except (stripe.SignatureVerificationError, ValueError) as exc:
            raise InvalidWebhookSignature() from exc

        return webhook_event_from_stripe(event)
