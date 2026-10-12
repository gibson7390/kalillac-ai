"""Explicit operator ceilings, never customer allowances or invoice caps."""
from dataclasses import dataclass
import os
import re

PROVIDERS = ("openai", "brave", "tavily")
TRUE = {"1", "true", "yes", "on"}

class ControlConfigError(RuntimeError):
    pass

@dataclass(frozen=True)
class ProviderLimit:
    total: int
    anonymous: int
    period_seconds: int

@dataclass(frozen=True)
class AbuseLimit:
    burst: int
    window_seconds: int
    concurrent: int

@dataclass(frozen=True)
class ControlConfig:
    providers: dict
    abuse: AbuseLimit | None = None


def enabled(name):
    return os.getenv(name, "").strip().lower() in TRUE


def integer(name, minimum=0, maximum=2**31-1):
    value = os.getenv(name, "").strip()
    if not re.fullmatch(r"[0-9]{1,10}", value):
        raise ControlConfigError(name + " requires an explicit integer.")
    number = int(value)
    if not minimum <= number <= maximum:
        raise ControlConfigError(name + " is outside the supported range.")
    return number


def load_controls():
    ceilings = enabled("KALILLAC_PROVIDER_CEILINGS_ENABLED")
    abuse = enabled("KALILLAC_ANONYMOUS_ABUSE_ENABLED")
    if not ceilings and not abuse:
        return None  # No limit parsing, database, auth or session work.
    if not ceilings:
        raise ControlConfigError("KALILLAC_ANONYMOUS_ABUSE_ENABLED requires KALILLAC_PROVIDER_CEILINGS_ENABLED.")
    for prerequisite in ("KALILLAC_DB_ENABLED", "KALILLAC_REQUEST_BUDGET_ENABLED"):
        if not enabled(prerequisite):
            raise ControlConfigError("KALILLAC_PROVIDER_CEILINGS_ENABLED requires " + prerequisite + ".")
    if enabled("KALILLAC_ACCOUNTS_ENABLED") and not enabled("KALILLAC_USAGE_METERING_ENABLED"):
        raise ControlConfigError("Account classification requires KALILLAC_USAGE_METERING_ENABLED.")
    providers = {}
    for provider in PROVIDERS:
        prefix = "KALILLAC_" + provider.upper()
        limit = ProviderLimit(integer(prefix + "_TOTAL_ATTEMPTS"), integer(prefix + "_ANONYMOUS_ATTEMPTS"), integer(prefix + "_PERIOD_SECONDS", 1))
        if limit.anonymous > limit.total:
            raise ControlConfigError(prefix + "_ANONYMOUS_ATTEMPTS must not exceed the total.")
        providers[provider] = limit
    limits = None
    if abuse:
        limits = AbuseLimit(integer("KALILLAC_ANONYMOUS_SESSION_BURST", 1, 10000), integer("KALILLAC_ANONYMOUS_SESSION_WINDOW_SECONDS", 1), integer("KALILLAC_ANONYMOUS_SESSION_CONCURRENT", 1, 10000))
    return ControlConfig(providers, limits)
