"""Processed Stripe webhook events, for idempotency.

Only the proof of processing is kept: Stripe's event id, its type, and when
Kalillac finished reconciling it. Raw webhook payloads are never stored.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .user import utc_now


class StripeWebhookEvent(Base):
    __tablename__ = "stripe_webhook_events"

    event_id: Mapped[str] = mapped_column(String(255), primary_key=True)

    event_type: Mapped[str] = mapped_column(String(100), nullable=False)

    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )
