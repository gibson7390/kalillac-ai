"""Data access for Stripe billing linkage and webhook idempotency.

Functions take a caller-owned Session and never commit. Billing state is
written only by verified webhook reconciliation (kalillac_billing.service);
no HTTP request handler writes it.
"""

from __future__ import annotations

from datetime import datetime
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AccountBilling, StripeWebhookEvent, User


def account_lock_statement(user_id: uuid.UUID):
    """SELECT ... FOR UPDATE on the account's user row.

    The user row always exists (unlike account_billing), so it is the one
    per-account lock every billing transaction takes: Checkout attempts and
    webhook reconciliation for the same account serialize on it, while
    different accounts never block each other. (SQLite ignores FOR UPDATE;
    its own write locking serializes test transactions instead.)
    """

    return select(User.id).where(User.id == user_id).with_for_update()


def lock_account(session: Session, user_id: uuid.UUID) -> bool:
    """Lock the account row for this transaction; False if it is gone."""

    return session.execute(account_lock_statement(user_id)).first() is not None


def get_billing(session: Session, user_id: uuid.UUID) -> AccountBilling | None:
    return session.get(AccountBilling, user_id)


def get_or_create_billing(session: Session, user_id: uuid.UUID) -> AccountBilling:
    billing = get_billing(session, user_id)

    if billing is None:
        billing = AccountBilling(user_id=user_id)
        session.add(billing)
        session.flush()

    return billing


def clear_pending_checkout(billing: AccountBilling) -> None:
    billing.checkout_attempt_id = None
    billing.checkout_customer_id = None
    billing.checkout_attempt_created_at = None
    billing.checkout_price_id = None
    billing.checkout_success_url = None
    billing.checkout_cancel_url = None
    billing.stripe_checkout_session_id = None
    billing.checkout_session_expires_at = None


def get_billing_by_customer(
    session: Session,
    customer_id: str,
) -> AccountBilling | None:
    return session.scalar(
        select(AccountBilling).where(
            AccountBilling.stripe_customer_id == customer_id
        )
    )


def get_billing_by_subscription(
    session: Session,
    subscription_id: str,
) -> AccountBilling | None:
    return session.scalar(
        select(AccountBilling).where(
            AccountBilling.stripe_subscription_id == subscription_id
        )
    )


def upsert_billing_state(
    session: Session,
    user_id: uuid.UUID,
    *,
    stripe_customer_id: str | None,
    stripe_subscription_id: str | None,
    stripe_price_id: str | None,
    subscription_status: str | None,
    cancel_at_period_end: bool,
    current_period_end: datetime | None,
) -> AccountBilling:
    billing = get_billing(session, user_id)

    if billing is None:
        billing = AccountBilling(user_id=user_id)
        session.add(billing)

    billing.stripe_customer_id = stripe_customer_id
    billing.stripe_subscription_id = stripe_subscription_id
    billing.stripe_price_id = stripe_price_id
    billing.subscription_status = subscription_status
    billing.cancel_at_period_end = cancel_at_period_end
    billing.current_period_end = current_period_end
    session.flush()
    return billing


def webhook_event_processed(session: Session, event_id: str) -> bool:
    return session.get(StripeWebhookEvent, event_id) is not None


def record_webhook_event(
    session: Session,
    event_id: str,
    event_type: str,
) -> None:
    """Mark an event processed. The primary key rejects a second insert,
    so concurrent duplicate deliveries cannot both commit."""

    session.add(StripeWebhookEvent(event_id=event_id, event_type=event_type))
    session.flush()
