"""Aggregate daily usage per account.

One row per account per UTC calendar date holding only numeric totals.
Kalillac stays private by default: no message, reply, history, prompt,
session id, search query, or result text is stored here or anywhere else
by metering. Counters are character counts (Unicode code points), not
provider tokens and not inference cost.
"""

from __future__ import annotations

from datetime import date, datetime
import uuid

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .user import utc_now


class AccountUsageDaily(Base):
    __tablename__ = "account_usage_daily"
    __table_args__ = (
        CheckConstraint(
            "successful_chats >= 0",
            name="successful_chats_non_negative",
        ),
        CheckConstraint(
            "request_chars >= 0",
            name="request_chars_non_negative",
        ),
        CheckConstraint(
            "response_chars >= 0",
            name="response_chars_non_negative",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("kalillac.users.id", ondelete="CASCADE"),
        primary_key=True,
    )

    # UTC calendar date.
    usage_date: Mapped[date] = mapped_column(Date, primary_key=True)

    # /api/chat requests that returned the normal 200 reply contract.
    successful_chats: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default=text("0"),
    )

    # Characters submitted: current message plus normalized history.
    request_chars: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default=text("0"),
    )

    # Characters of the final reply returned to the client.
    response_chars: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default=text("0"),
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
