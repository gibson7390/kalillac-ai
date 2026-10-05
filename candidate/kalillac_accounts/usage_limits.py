"""Daily chat-limit configuration for signed-in accounts.

Configuration only: nothing here enforces a limit, and nothing imports this
module yet. The quota service will load it at startup.

KALILLAC_USAGE_ENFORCEMENT_ENABLED is off by default. While it is off the
limit settings are never read, so leftover or malformed values cannot change
application behavior. When it is on:

- accounts, database support and usage metering must all be enabled;
- KALILLAC_USAGE_LIMIT_FREE_DAILY and KALILLAC_USAGE_LIMIT_PAID_DAILY must
  be set explicitly to positive integers. There are no built-in defaults,
  so no allowance exists that the operator did not choose;
- the paid limit must be strictly greater than the free limit.

Validation errors name the setting, never the value supplied for it.
Importing this module performs no I/O beyond reading the environment when
a loader is called.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import re


_TRUE_VALUES = {"1", "true", "yes", "on"}

ENFORCEMENT_FLAG = "KALILLAC_USAGE_ENFORCEMENT_ENABLED"
FREE_DAILY_LIMIT = "KALILLAC_USAGE_LIMIT_FREE_DAILY"
PAID_DAILY_LIMIT = "KALILLAC_USAGE_LIMIT_PAID_DAILY"

# Checked in this order; the first one that is off is named in the error.
PREREQUISITE_FLAGS = (
    "KALILLAC_ACCOUNTS_ENABLED",
    "KALILLAC_DB_ENABLED",
    "KALILLAC_USAGE_METERING_ENABLED",
)

# ASCII digits only: int() alone would also accept signs, surrounding
# whitespace inside the value, underscores ("1_000") and non-ASCII digits.
_POSITIVE_INTEGER_RE = re.compile(r"[0-9]+")


class UsageLimitConfigError(RuntimeError):
    """Usage enforcement is enabled but its configuration is invalid."""


@dataclass(frozen=True)
class UsageLimits:
    """Successful chats allowed per account per UTC day, by tier."""

    free_daily_chats: int
    paid_daily_chats: int


def _flag_enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in _TRUE_VALUES


def usage_enforcement_enabled() -> bool:
    return _flag_enabled(ENFORCEMENT_FLAG)


def _require_positive_integer(name: str) -> int:
    value = os.getenv(name, "").strip()

    if not value:
        raise UsageLimitConfigError(
            f"{ENFORCEMENT_FLAG} is enabled but {name} is not set."
        )

    # The message names the setting, never the value.
    invalid = UsageLimitConfigError(f"{name} must be a positive integer.")

    if not _POSITIVE_INTEGER_RE.fullmatch(value):
        raise invalid

    try:
        number = int(value)
    except ValueError:
        # Python refuses digit strings beyond its int conversion limit.
        raise invalid from None

    if number <= 0:
        raise invalid

    return number


def load_usage_limits() -> UsageLimits | None:
    """Return None while enforcement is off; otherwise validated limits.

    Raises UsageLimitConfigError naming the first missing prerequisite or
    invalid setting.
    """

    if not usage_enforcement_enabled():
        return None

    for prerequisite in PREREQUISITE_FLAGS:
        if not _flag_enabled(prerequisite):
            raise UsageLimitConfigError(
                f"{ENFORCEMENT_FLAG} requires {prerequisite}."
            )

    free = _require_positive_integer(FREE_DAILY_LIMIT)
    paid = _require_positive_integer(PAID_DAILY_LIMIT)

    if paid <= free:
        raise UsageLimitConfigError(
            f"{PAID_DAILY_LIMIT} must be greater than {FREE_DAILY_LIMIT}."
        )

    return UsageLimits(free_daily_chats=free, paid_daily_chats=paid)
