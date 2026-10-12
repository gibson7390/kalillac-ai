"""Reusable account/day capacity leases; no request or conversation IDs."""
from datetime import date, datetime
import uuid
from sqlalchemy import BigInteger, CheckConstraint, Date, DateTime, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column
from .base import Base


class AccountUsageSlot(Base):
    __tablename__ = "account_usage_slots"
    __table_args__ = (
        CheckConstraint("slot >= 0", name="slot_non_negative"),
        CheckConstraint("state IN ('pending', 'ready', 'uncertain', 'completed', 'released')", name="state_valid"),
        CheckConstraint("(state IN ('ready', 'uncertain', 'completed')) = (completed_date IS NOT NULL)", name="completion_date_valid"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("kalillac.users.id", ondelete="CASCADE"), primary_key=True)
    usage_date: Mapped[date] = mapped_column(Date, primary_key=True)
    slot: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    completed_date: Mapped[date | None] = mapped_column(Date, nullable=True)
