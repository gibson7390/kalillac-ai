"""Per-account entitlement tier.

Kept separate from users so identity carries no plan or billing data. One
row per account: registration creates a free row, and later verified
billing logic may change the tier. There are deliberately no Stripe ids,
prices, payment state, or usage counters here.
"""

from __future__ import annotations

from datetime import datetime
import uuid

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .user import utc_now


TIER_FREE = "free"
TIER_PAID = "paid"
VALID_TIERS = (TIER_FREE, TIER_PAID)

# Who established the current tier. Registration uses this; billing will
# record its own source later.
SOURCE_REGISTRATION = "registration"


class AccountEntitlement(Base):
    __tablename__ = "account_entitlements"
    __table_args__ = (
        CheckConstraint(
            "tier IN ('free', 'paid')",
            name="tier_valid",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("kalillac.users.id", ondelete="CASCADE"),
        primary_key=True,
    )

    tier: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=TIER_FREE,
        server_default=TIER_FREE,
    )

    source: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=SOURCE_REGISTRATION,
        server_default=SOURCE_REGISTRATION,
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

    # Optional end of paid access; NULL means no scheduled expiry.
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
