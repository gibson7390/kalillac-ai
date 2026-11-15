"""Billing provider contract and the minimal data Kalillac takes from it.

The service and routes depend only on this interface, so tests inject a
fake and make no network calls. Stripe objects are reduced to these small
snapshots at the boundary; the full objects (payment methods, invoices,
customer details) never travel further into Kalillac.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class BillingProviderError(Exception):
    """A provider API call failed. Carries no provider details.

    No failure is treated as proof that an earlier request under the same
    idempotency key did not succeed, so callers never discard a Checkout
    attempt because of one: the next request retries the same key.
    """


class InvalidWebhookSignature(Exception):
    """A webhook payload failed signature verification."""


@dataclass(frozen=True)
class SubscriptionState:
    """Current subscription state as reported by the provider."""

    subscription_id: str
    customer_id: str | None
    status: str
    price_id: str | None
    cancel_at_period_end: bool
    current_period_end: datetime | None
    kalillac_user_id: str | None


@dataclass(frozen=True)
class CheckoutSessionState:
    """A hosted Checkout Session: open, complete, or expired."""

    session_id: str
    status: str | None
    url: str | None
    expires_at: datetime | None
    subscription_id: str | None


@dataclass(frozen=True)
class ChargeState:
    """A charge's refund facts as the provider reports them now. Amounts are in
    the currency's minor unit (cents)."""

    charge_id: str
    customer_id: str | None
    amount: int
    amount_refunded: int
    currency: str
    refunded: bool


@dataclass(frozen=True)
class DisputeState:
    """A dispute (chargeback) as the provider reports it now."""

    dispute_id: str
    charge_id: str | None
    customer_id: str | None
    amount: int
    currency: str
    status: str
    reason: str | None


@dataclass(frozen=True)
class WebhookEvent:
    """Only the fields needed to check the event's mode and to locate the
    subscription and the account. livemode is None when the event did not
    carry a boolean, which never matches either mode. object_id is the
    charge or dispute an event is about (None for other events)."""

    event_id: str
    event_type: str
    subscription_id: str | None
    customer_id: str | None
    kalillac_user_id: str | None
    livemode: bool | None
    object_id: str | None = None


class BillingGateway(Protocol):
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
        """Create a hosted subscription Checkout Session. The same
        idempotency key always yields the same session."""

    def retrieve_checkout_session(self, session_id: str) -> CheckoutSessionState:
        """Fetch a server-stored Checkout Session's current state."""

    def create_portal_session(self, *, customer_id: str, return_url: str) -> str:
        """Create a Customer Portal session; return its URL."""

    def retrieve_subscription(self, subscription_id: str) -> SubscriptionState:
        """Fetch the subscription's current state from the provider."""

    def retrieve_charge(self, charge_id: str) -> ChargeState:
        """Fetch a charge's current refund state from the provider."""

    def retrieve_dispute(self, dispute_id: str) -> DisputeState:
        """Fetch a dispute's current state from the provider."""

    def parse_webhook(self, payload: bytes, signature: str) -> WebhookEvent:
        """Verify the signature, then return the event's minimal fields.
        Raises InvalidWebhookSignature on any verification failure."""
