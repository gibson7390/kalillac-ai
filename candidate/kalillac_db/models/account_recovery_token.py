"""Password-reset credentials: digest and lifecycle only, never raw links/content."""
from __future__ import annotations

from datetime import datetime
import uuid

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .user import utc_now

# Portable on PostgreSQL and SQLite, including rejection of non-hex characters.
_remainder = "token_hash"
for _digit in "0123456789abcdef":
    _remainder = f"replace({_remainder}, '{_digit}', '')"
TOKEN_HASH_CHECK = f"length(token_hash) = 64 AND length({_remainder}) = 0"


class AccountRecoveryToken(Base):
    __tablename__ = "account_recovery_tokens"
    __table_args__ = (
        CheckConstraint("purpose = 'password_reset'", name="purpose_valid"),
        CheckConstraint(TOKEN_HASH_CHECK, name="token_hash_hex"),
        CheckConstraint("expires_at > created_at", name="expiry_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("kalillac.users.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now(),
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
