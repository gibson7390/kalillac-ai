"""Financial scope identifiers for the future ledger. Nothing is persisted.

Every charge lands in GLOBAL and in exactly one of FREE or PAID:

    anonymous visitor       -> GLOBAL + FREE
    signed-in, not paying   -> GLOBAL + FREE
    paying subscriber       -> GLOBAL + PAID

PAID is a scope with its own ledger rows and ceiling. It is never derived
as GLOBAL minus FREE.

Subscriber capacity is protected structurally. ScopeCeilings requires
free + paid <= global. FREE exposure can never exceed the free ceiling, so
FREE can never take GLOBAL capacity that PAID needs. Formally: if paid
exposure + amount <= paid ceiling, then global exposure + amount
= free + paid + amount <= free ceiling + paid ceiling <= global ceiling.

This holds while every scope's exposure stays within its own ceiling,
which requires settled <= reserved. Batch 2 must treat a settlement overrun
(cost.Settlement.overrun) as an alert. It must not let one scope's overrun
silently consume another scope's protected capacity.
"""
from dataclasses import dataclass
from enum import Enum

from .money import MAX_MICROUSD, MoneyError, is_count


class FinancialScope(str, Enum):
    GLOBAL = "global"
    FREE = "free"
    PAID = "paid"


class TrafficClass(str, Enum):
    ANONYMOUS = "anonymous"
    FREE_ACCOUNT = "free_account"
    PAID_ACCOUNT = "paid_account"


_CHARGED = {
    TrafficClass.ANONYMOUS: (FinancialScope.GLOBAL, FinancialScope.FREE),
    TrafficClass.FREE_ACCOUNT: (FinancialScope.GLOBAL, FinancialScope.FREE),
    TrafficClass.PAID_ACCOUNT: (FinancialScope.GLOBAL, FinancialScope.PAID),
}


def charged_scopes(traffic_class):
    """The scopes one charge of this traffic class lands in."""
    if type(traffic_class) is not TrafficClass:
        raise MoneyError("Unknown traffic class.")
    return _CHARGED[traffic_class]


@dataclass(frozen=True)
class ScopeCeilings:
    """µUSD ceilings per scope for one ledger period."""

    global_limit: int
    free_limit: int
    paid_limit: int

    def __post_init__(self):
        if not all(is_count(v, MAX_MICROUSD) for v in (self.global_limit, self.free_limit, self.paid_limit)):
            raise MoneyError("Invalid scope ceiling.")
        if self.free_limit + self.paid_limit > self.global_limit:
            raise MoneyError("Free and paid ceilings must fit within the global ceiling.")

    def limit(self, scope):
        return {FinancialScope.GLOBAL: self.global_limit, FinancialScope.FREE: self.free_limit,
                FinancialScope.PAID: self.paid_limit}[scope]


def admits(ceilings, exposure, traffic_class, amount):
    """Whether reserving `amount` µUSD for this traffic class keeps every
    charged scope within its ceiling. `exposure` maps each FinancialScope
    to its settled + still-reserved µUSD (in-flight work included)."""
    if type(ceilings) is not ScopeCeilings or not is_count(amount, MAX_MICROUSD):
        raise MoneyError("Invalid admission check.")
    for scope in charged_scopes(traffic_class):
        current = exposure.get(scope) if isinstance(exposure, dict) else None
        if not is_count(current, MAX_MICROUSD):
            raise MoneyError("Invalid scope exposure.")
        if current + amount > ceilings.limit(scope):
            return False
    return True
