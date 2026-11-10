"""Account identity.

A user row is identity only. It carries no conversation, saved-chat,
memory, plan, price, or entitlement data: creating an account never makes
conversations persistent.
"""

from __future__ import annotations

from datetime import datetime, timezone
import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, FetchedValue, Index, String, Uuid, false, func, true
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    __mapper_args__ = {"eager_defaults": False}
    __table_args__ = (Index("uq_users_registration_intent", "registration_intent_id", unique=True),)

    registration_intent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("kalillac.account_registration_intents.id", name="fk_users_registration_intent", ondelete="SET NULL"), deferred=True, server_default=FetchedValue(),
    )
    quarantined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), deferred=True, server_default=FetchedValue())
    sessions_valid_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), deferred=True, server_default=FetchedValue())
    # V1 preparation (0012). Deferred and server-supplied: ORM inserts never name
    # them, so R/A column-level INSERT grants and pre-0012 databases are unaffected.
    deletion_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), deferred=True, server_default=FetchedValue())
    password_reset_required: Mapped[bool] = mapped_column(Boolean, nullable=False, deferred=True, server_default=false())

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
    )

    # Stored normalized (trimmed, lowercased) so uniqueness is
    # case-insensitive without a database extension.
    email: Mapped[str] = mapped_column(
        String(320),
        nullable=False,
        unique=True,
    )

    # Argon2id PHC string; never a plaintext password.
    password_hash: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
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

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )

    # Set only by email-verification confirmation; NULL means unverified.
    # Never exposed: account responses carry only an email_verified boolean.
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
