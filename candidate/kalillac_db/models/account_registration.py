"""Registration intent/control storage; keys and raw capabilities never enter rows."""
from __future__ import annotations
from datetime import datetime
import uuid
from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, FetchedValue, Index, LargeBinary, SmallInteger, String, UniqueConstraint, Uuid, false, text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import UserDefinedType
from .base import Base
from sqlalchemy.schema import conv
from sqlalchemy.dialects.postgresql.base import ischema_names

class Xid8(UserDefinedType):
    cache_ok = True
    def get_col_spec(self, **kw):
        return "xid8"
    def result_processor(self, dialect, coltype):
        return lambda value: None if value is None else str(value)

ischema_names.setdefault("xid8", Xid8)

class AccountRegistrationIntent(Base):
    __tablename__ = "account_registration_intents"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    address_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    format_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    address_lookup_key_version: Mapped[str] = mapped_column(String(32), nullable=False)
    address_lookup_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    token_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    email_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    email_nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    email_key_version: Mapped[str | None] = mapped_column(String(32))
    consumed_xid: Mapped[str | None] = mapped_column(Xid8().with_variant(String(20), "sqlite"), server_default=FetchedValue())
    provenance_claimed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("address_id", "generation"),
        CheckConstraint("generation > 0", name="generation"),
        CheckConstraint("purpose = 'finish_registration' AND format_version = 1", name="purpose_format"),
        CheckConstraint("expires_at > created_at", name="expiry"),
        CheckConstraint("substr(address_lookup_key_version,1,12) = 'reg-lookup-v' AND length(address_lookup_key_version) BETWEEN 13 AND 21 AND (email_key_version IS NULL OR (substr(email_key_version,1,9) = 'reg-enc-v' AND length(email_key_version) BETWEEN 10 AND 18))", name="key_versions"),
        CheckConstraint("length(token_digest) = 64 AND length(address_lookup_digest) = 64 AND length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(token_digest, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0 AND length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(address_lookup_digest, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0", name="digest_length"),
        CheckConstraint(
            "(state = 'pending' AND is_current AND email_ciphertext IS NOT NULL AND email_nonce IS NOT NULL AND email_key_version IS NOT NULL AND consumed_at IS NULL AND superseded_at IS NULL AND expired_at IS NULL) OR "
            "(state = 'consumed' AND NOT is_current AND email_ciphertext IS NULL AND email_nonce IS NULL AND email_key_version IS NULL AND consumed_at IS NOT NULL AND consumed_at >= created_at AND superseded_at IS NULL AND expired_at IS NULL) OR "
            "(state = 'superseded' AND NOT is_current AND email_ciphertext IS NULL AND email_nonce IS NULL AND email_key_version IS NULL AND consumed_at IS NULL AND superseded_at IS NOT NULL AND superseded_at >= created_at AND expired_at IS NULL) OR "
            "(state = 'expired' AND NOT is_current AND email_ciphertext IS NULL AND email_nonce IS NULL AND email_key_version IS NULL AND consumed_at IS NULL AND superseded_at IS NULL AND expired_at IS NOT NULL AND expired_at >= created_at)",
            name="lifecycle"),
        CheckConstraint("(state = 'consumed' AND consumed_xid IS NOT NULL) OR (state <> 'consumed' AND consumed_xid IS NULL AND NOT provenance_claimed)", name=conv("ck_intents_provenance_state")),
        CheckConstraint("email_nonce IS NULL OR length(email_nonce) = 12", name="nonce_length"),
        CheckConstraint("email_ciphertext IS NULL OR length(email_ciphertext) BETWEEN 17 AND 1296", name="ciphertext_length"),
        Index("uq_registration_current_address", "address_id", unique=True, postgresql_where=text("is_current"), sqlite_where=text("is_current")),
        Index("uq_registration_current_lookup", "address_lookup_key_version", "address_lookup_digest", unique=True, postgresql_where=text("is_current"), sqlite_where=text("is_current")),
        Index("ix_registration_lookup", "address_lookup_key_version", "address_lookup_digest", "generation"),
        Index("ix_registration_cleanup", "expires_at", "id"),
    )

class AccountRegistrationActivation(Base):
    __tablename__ = "account_registration_activation"
    singleton_id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    database_instance_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    policy_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    activation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, unique=True)
    cutover_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    deployment_id: Mapped[str | None] = mapped_column(String(64))
    evidence_sha256: Mapped[str | None] = mapped_column(String(64))
    inventory_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    legacy_unverified_count: Mapped[int | None] = mapped_column(BigInteger)
    __table_args__ = (
        CheckConstraint("singleton_id = 1 AND policy_version = 1", name="activation_singleton"),
        CheckConstraint(
            "(state = 'unactivated' AND activation_id IS NULL AND cutover_id IS NULL AND deployment_id IS NULL AND evidence_sha256 IS NULL AND inventory_at IS NULL AND activated_at IS NULL AND legacy_unverified_count IS NULL) OR "
            "(state = 'active_zero_legacy' AND activation_id IS NOT NULL AND cutover_id IS NOT NULL AND deployment_id IS NOT NULL AND evidence_sha256 IS NOT NULL AND length(evidence_sha256) = 64 AND length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(evidence_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0 AND inventory_at IS NOT NULL AND activated_at IS NOT NULL AND activated_at >= inventory_at AND legacy_unverified_count = 0)",
            name="activation_shape"),
    )
