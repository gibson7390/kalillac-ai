"""Anonymous daily allowance: per-UTC-day lease rows keyed by a digest.

Rows hold only a 64-character per-UTC-day visitor digest, the UTC usage date,
numeric lease slots, lease state and bounded timestamps. The digest is meant
to be derived from a fixed domain label, a random visitor token and the UTC
date, so a raw token is never stored and one browser's digests on different
days cannot be linked to each other. There is no raw visitor token, IP
address or IP-derived value, session id, user agent, account id or
conversation content, and no identifier that persists across days. Only
the current and previous UTC day are meant to be retained.

Phase 2 (not implemented here): a request that crosses UTC midnight must,
in one transaction, release or replace its pending slot under the old day's
digest and reserve and prepare a slot under the new day's digest. It must
not store any stable identifier linking the two days.
"""
from datetime import date, datetime
from sqlalchemy import BigInteger, CheckConstraint, Date, DateTime, ForeignKeyConstraint, Index, String
from sqlalchemy.orm import Mapped, mapped_column
from .base import Base

COMPLETION_DATE_CHECK = (
    "(state IN ('pending', 'released') AND completed_date IS NULL) OR "
    "(state IN ('ready', 'uncertain', 'completed') AND completed_date IS NOT NULL "
    "AND completed_date = usage_date)"
)


class AnonymousUsageDay(Base):
    """One visitor-day: the row lease operations for that day will lock."""
    __tablename__ = "anonymous_usage_days"
    __table_args__ = (
        CheckConstraint("length(visitor_digest) = 64", name="digest_length"),
        Index("ix_anonymous_usage_days_usage_date", "usage_date"),
    )
    visitor_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    usage_date: Mapped[date] = mapped_column(Date, primary_key=True)


class AnonymousUsageSlot(Base):
    """Reusable response leases with the same states as account leases."""
    __tablename__ = "anonymous_usage_slots"
    __table_args__ = (
        ForeignKeyConstraint(
            ["visitor_digest", "usage_date"],
            ["kalillac.anonymous_usage_days.visitor_digest", "kalillac.anonymous_usage_days.usage_date"],
            ondelete="CASCADE",
        ),
        CheckConstraint("slot >= 0", name="slot_non_negative"),
        CheckConstraint("state IN ('pending', 'ready', 'uncertain', 'completed', 'released')", name="state_valid"),
        # Each UTC day has its own digest, so a lease is completed only on its
        # own usage_date. IS NOT NULL keeps a NULL date from passing the check
        # as unknown.
        CheckConstraint(COMPLETION_DATE_CHECK, name="completion_date_valid"),
    )
    visitor_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    usage_date: Mapped[date] = mapped_column(Date, primary_key=True)
    slot: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    completed_date: Mapped[date | None] = mapped_column(Date, nullable=True)
