"""Refunds and disputes Stripe reported, kept for an operator to review.

One row per refunded charge or per dispute, created and updated only by verified webhooks with the
facts fetched from Stripe (never taken from a webhook payload). Kalillac issues no refund and changes
no entitlement because of a case: an operator reviews it (tools/billing_cases.py), and access keeps
following the subscription's own status. Like every Kalillac table, it holds no amount, currency,
card or payment data: Stripe is billing truth, and the operator reads those in Stripe. The account
link is cleared, not the case, if the account is deleted.
"""

from __future__ import annotations

from datetime import datetime
import uuid

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .user import utc_now

CASE_KINDS = ("refund", "dispute")
REVIEW_STATUSES = ("open", "acknowledged", "closed")


class BillingCase(Base):
    __tablename__ = "billing_cases"
    __table_args__ = (
        UniqueConstraint("kind", "provider_object_id", name="uq_billing_cases_kind_object"),
        CheckConstraint("kind IN ('refund', 'dispute')", name="ck_billing_cases_kind"),
        CheckConstraint("review_status IN ('open', 'acknowledged', 'closed')", name="ck_billing_cases_review"),
        Index("ix_kalillac_billing_cases_user_id", "user_id"),
        {"schema": "kalillac"},
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("kalillac.users.id", ondelete="SET NULL"), nullable=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    provider_object_id: Mapped[str] = mapped_column(String(255), nullable=False)
    charge_id: Mapped[str | None] = mapped_column(String(255))
    customer_id: Mapped[str | None] = mapped_column(String(255))
    provider_status: Mapped[str] = mapped_column(String(40), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(60))
    review_status: Mapped[str] = mapped_column(String(16), nullable=False, default="open", server_default="open")
    last_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now,
                                                server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now,
                                                 server_default=func.now())
