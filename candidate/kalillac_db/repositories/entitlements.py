"""Data access for per-account entitlement tier.

Functions take a caller-owned SQLAlchemy Session and never commit. Nothing
here is reachable from a user-controlled HTTP write: tier changes are for
internal, verified callers (later, billing) only.
"""

from __future__ import annotations

from datetime import datetime
import re
import uuid

from sqlalchemy.orm import Session

from ..models import AccountEntitlement
from ..models.account_entitlement import (
    SOURCE_REGISTRATION,
    TIER_FREE,
    VALID_TIERS,
)


class InvalidEntitlement(ValueError):
    """An unknown tier or malformed source was requested."""


_SOURCE_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")


def get_entitlement(
    session: Session,
    user_id: uuid.UUID,
) -> AccountEntitlement | None:
    return session.get(AccountEntitlement, user_id)


def create_default_entitlement(
    session: Session,
    user_id: uuid.UUID,
) -> AccountEntitlement:
    """Create the free entitlement every new account starts with."""

    entitlement = AccountEntitlement(
        user_id=user_id,
        tier=TIER_FREE,
        source=SOURCE_REGISTRATION,
    )
    session.add(entitlement)
    session.flush()
    return entitlement


def set_entitlement_tier(
    session: Session,
    user_id: uuid.UUID,
    tier: str,
    *,
    source: str,
    expires_at: datetime | None = None,
) -> AccountEntitlement:
    """Internally set an account's tier (creating the row if missing).

    For verified internal callers only; never wired to a user request.
    """

    if tier not in VALID_TIERS:
        raise InvalidEntitlement(f"Unknown entitlement tier: {tier!r}.")

    if not _SOURCE_RE.fullmatch(source):
        raise InvalidEntitlement("Entitlement source must be a short identifier.")

    entitlement = get_entitlement(session, user_id)

    if entitlement is None:
        entitlement = AccountEntitlement(user_id=user_id)
        session.add(entitlement)

    entitlement.tier = tier
    entitlement.source = source
    entitlement.expires_at = expires_at
    session.flush()
    return entitlement
