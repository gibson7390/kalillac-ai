"""Financial Safeguards ledger: µUSD aggregates, reservations and latches.

Financial and control metadata only: no account, visitor, cookie, address,
key, prompt or response is stored or referenced.
"""
from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, Date, DateTime, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

SCOPES = "('global', 'free', 'paid')"


class FinanceLedgerBucket(Base):
    """One UTC calendar day or month for one scope. Exposure = reserved + settled."""

    __tablename__ = "finance_ledger_buckets"
    __table_args__ = (
        CheckConstraint("period_kind IN ('day', 'month')", name="period_kind_valid"),
        CheckConstraint("scope IN " + SCOPES, name="scope_valid"),
        CheckConstraint("reserved_microusd >= 0 AND settled_microusd >= 0 AND overrun_microusd >= 0",
                        name="amounts_valid"),
    )
    period_kind: Mapped[str] = mapped_column(String(5), primary_key=True)
    period_start: Mapped[date] = mapped_column(Date, primary_key=True)
    scope: Mapped[str] = mapped_column(String(6), primary_key=True)
    reserved_microusd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    settled_microusd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    overrun_microusd: Mapped[int] = mapped_column(BigInteger, nullable=False)


class FinanceReservation(Base):
    """One provider attempt's financial reservation and, later, its settlement.

    'reserved' is outstanding (dispatched or not, settlement unknown) and stays
    in its buckets' reserved exposure until settled or released; a process
    crash therefore leaves it charged. Only an explicit operator reconciliation
    may ever resolve it.
    """

    __tablename__ = "finance_reservations"
    __table_args__ = (
        CheckConstraint("provider IN ('openai', 'brave', 'tavily')", name="provider_valid"),
        CheckConstraint("scope IN ('free', 'paid')", name="scope_valid"),
        CheckConstraint("state IN ('reserved', 'released', 'settled')", name="state_valid"),
        CheckConstraint("basis IS NULL OR basis IN ('usage', 'missing', 'invalid', 'partial', 'unknown')",
                        name="basis_valid"),
        CheckConstraint("reserved_microusd >= 0 AND (charged_microusd IS NULL OR charged_microusd >= 0) "
                        "AND overrun_microusd >= 0", name="amounts_valid"),
        CheckConstraint("(state = 'settled') = (charged_microusd IS NOT NULL AND basis IS NOT NULL)",
                        name="settlement_complete"),
    )
    id: Mapped[object] = mapped_column(Uuid, primary_key=True)
    provider: Mapped[str] = mapped_column(String(8), nullable=False)
    scope: Mapped[str] = mapped_column(String(4), nullable=False)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    month: Mapped[date] = mapped_column(Date, nullable=False)
    reserved_microusd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(8), nullable=False)
    charged_microusd: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    overrun_microusd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bound_exceeded: Mapped[bool] = mapped_column(Boolean, nullable=False)
    basis: Mapped[str | None] = mapped_column(String(8), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FinanceUnsafeLatch(Base):
    """A durable stop for one scope ('global' stops every scope).

    The application role may insert but never update or delete a latch, so
    neither a restart nor application code can clear it; clearing is an
    explicit operator action.
    """

    __tablename__ = "finance_unsafe_latches"
    __table_args__ = (
        CheckConstraint("scope IN " + SCOPES, name="scope_valid"),
        CheckConstraint("reason IN ('overrun', 'bound_exceeded', 'headroom_exhausted')", name="reason_valid"),
    )
    scope: Mapped[str] = mapped_column(String(6), primary_key=True)
    reason: Mapped[str] = mapped_column(String(18), nullable=False)
    latched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reservation_id: Mapped[object | None] = mapped_column(Uuid, nullable=True)
