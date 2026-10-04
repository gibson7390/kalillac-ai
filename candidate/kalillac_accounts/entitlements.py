"""Effective entitlement: what an account's tier grants access to.

The access flags describe entitlement only. They do not implement Saved
Mode or usage metering, and Private Session never depends on them:
anonymous and signed-in users of every tier keep it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from kalillac_db.models import AccountEntitlement
from kalillac_db.models.account_entitlement import TIER_FREE, TIER_PAID


@dataclass(frozen=True)
class EffectiveEntitlement:
    tier: str
    saved_mode_access: bool
    higher_usage_access: bool
    # Persistent memory is outside this commercial checkpoint for every tier.
    persistent_memory_access: bool = False

    def as_json(self) -> dict[str, Any]:
        return {
            "entitlements": {
                "tier": self.tier,
                "saved_mode_access": self.saved_mode_access,
                "higher_usage_access": self.higher_usage_access,
                "persistent_memory_access": self.persistent_memory_access,
            }
        }


FREE = EffectiveEntitlement(
    tier=TIER_FREE,
    saved_mode_access=False,
    higher_usage_access=False,
)

PAID = EffectiveEntitlement(
    tier=TIER_PAID,
    saved_mode_access=True,
    higher_usage_access=True,
)


def _as_utc(value: datetime) -> datetime:
    # SQLite returns naive datetimes; PostgreSQL returns aware ones.
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def effective_entitlement(
    entitlement: AccountEntitlement | None,
    now: datetime,
) -> EffectiveEntitlement:
    """Compute access from the stored tier.

    Every account is expected to have exactly one row: registration creates
    it, and migration 0002 backfilled accounts that predate entitlements. A
    missing row is therefore not a normal state; if it ever occurs it fails
    closed. Anything other than an unexpired paid row is free: a missing
    row, an expired paid row, or an unrecognized tier all resolve to the
    least access.
    """

    if entitlement is None or entitlement.tier != TIER_PAID:
        return FREE

    if (
        entitlement.expires_at is not None
        and _as_utc(entitlement.expires_at) <= now
    ):
        return FREE

    return PAID
