"""Per-account Stripe billing linkage.

Separate from identity, entitlement, and usage. It holds only the Stripe
references and subscription state needed to reconcile access: no card
numbers, payment methods, invoices, amounts, secrets, or conversation data.
Stripe is billing truth; account_entitlements remains Kalillac access truth.
"""

from __future__ import annotations

from datetime import datetime
import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Uuid, false, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .user import utc_now


class AccountBilling(Base):
    __tablename__ = "account_billing"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("kalillac.users.id", ondelete="CASCADE"),
        primary_key=True,
    )

    stripe_customer_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        unique=True,
    )

    stripe_subscription_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        unique=True,
    )

    stripe_price_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    # Stripe's subscription status string (active, past_due, canceled, ...).
    subscription_status: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
    )

    cancel_at_period_end: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=false(),
    )

    current_period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Pending Checkout, server-generated only. checkout_attempt_id (with the
    # user id) is the Stripe idempotency key, so concurrent requests and
    # crash retries for one attempt resolve to the same Checkout Session.
    checkout_attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        nullable=True,
    )

    # The Stripe customer snapshotted when the attempt began (None when it
    # began without one). Every retry of the attempt sends exactly this, so
    # one idempotency key always carries identical Checkout parameters even
    # if stripe_customer_id changes meanwhile.
    checkout_customer_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    # When the attempt was persisted (UTC, never updated). Automatic create
    # retries are allowed only within a window shorter than Stripe's minimum
    # idempotency-key retention measured from this instant.
    checkout_attempt_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # The remaining configuration-derived Checkout parameters, pinned per
    # attempt like the customer, so a retry sends exactly the original
    # request even if configuration changed in between.
    checkout_price_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    checkout_success_url: Mapped[str | None] = mapped_column(
        String(2048),
        nullable=True,
    )

    checkout_cancel_url: Mapped[str | None] = mapped_column(
        String(2048),
        nullable=True,
    )

    stripe_checkout_session_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        unique=True,
    )

    checkout_session_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
    )
