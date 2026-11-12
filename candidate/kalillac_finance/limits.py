"""Explicit µUSD ceilings per UTC calendar day and month, per ledger scope.

There are no defaults and no production values in source. Every period
needs a GLOBAL, FREE and PAID ceiling, with

    FREE + PAID < GLOBAL   and   GLOBAL - FREE - PAID >= the minimum headroom,

where the minimum headroom is ceil(GLOBAL x KALILLAC_FINANCE_MIN_HEADROOM_PERCENT
/ 100) and at least 1 µUSD. The headroom is never admitted to any scope; it
absorbs a settlement overrun, the case where a reservation's conservative
assumption failed (see ledger semantics in
kalillac_db/repositories/finance_ledger.py).
"""
from dataclasses import dataclass

from .money import MAX_MICROUSD, MoneyError, ceil_div, parse_usd
from .scopes import FinancialScope, ScopeCeilings

PERIODS = ("day", "month")
MIN_HEADROOM_PERCENT = "KALILLAC_FINANCE_MIN_HEADROOM_PERCENT"
LIMIT_NAMES = {(period, scope): f"KALILLAC_FINANCE_{period.upper()}_{scope.value.upper()}_LIMIT_USD"
               for period in PERIODS for scope in FinancialScope}
REQUIRED = (*LIMIT_NAMES.values(), MIN_HEADROOM_PERCENT)


@dataclass(frozen=True)
class FinancialLimits:
    day: ScopeCeilings
    month: ScopeCeilings
    min_headroom_percent: int

    def __post_init__(self):
        if type(self.min_headroom_percent) is not int or not 1 <= self.min_headroom_percent <= 90:
            raise MoneyError("Invalid minimum headroom.")
        for ceilings in (self.day, self.month):
            if type(ceilings) is not ScopeCeilings:
                raise MoneyError("Invalid period ceilings.")
            if self.headroom_of(ceilings) < self.minimum_headroom_of(ceilings):
                raise MoneyError("Free and paid ceilings leave too little global headroom.")
        if not (self.day.global_limit <= self.month.global_limit and self.day.free_limit <= self.month.free_limit
                and self.day.paid_limit <= self.month.paid_limit):
            raise MoneyError("A day ceiling exceeds its month ceiling.")

    @staticmethod
    def headroom_of(ceilings):
        return ceilings.global_limit - ceilings.free_limit - ceilings.paid_limit

    def minimum_headroom_of(self, ceilings):
        return max(1, ceil_div(ceilings.global_limit * self.min_headroom_percent, 100))

    def period(self, period):
        if period == "day":
            return self.day
        if period == "month":
            return self.month
        raise MoneyError("Unknown ledger period.")

    def limit(self, period, scope):
        return self.period(period).limit(FinancialScope(scope))

    def headroom(self, period):
        return self.headroom_of(self.period(period))


def load_limits(environ, error):
    """FinancialLimits from explicit variables; `error` is the config error class."""
    def text(name):
        value = environ.get(name)
        if type(value) is not str or not value.strip():
            raise error(name + " must be set explicitly.")
        return value.strip()

    def usd(name):
        try:
            return parse_usd(text(name), maximum_microusd=MAX_MICROUSD, minimum_microusd=0)
        except MoneyError:
            raise error(name + " requires a plain dollar amount within the supported range.") from None

    percent = text(MIN_HEADROOM_PERCENT)
    if percent not in {str(n) for n in range(1, 91)}:
        raise error(MIN_HEADROOM_PERCENT + " requires a whole percentage from 1 to 90.")
    periods = {}
    for period in PERIODS:
        values = {scope: usd(LIMIT_NAMES[(period, scope)]) for scope in FinancialScope}
        try:
            periods[period] = ScopeCeilings(values[FinancialScope.GLOBAL], values[FinancialScope.FREE],
                                            values[FinancialScope.PAID])
        except MoneyError:
            raise error(f"KALILLAC_FINANCE_{period.upper()}_* ceilings must fit within the global ceiling.") from None
        if values[FinancialScope.FREE] + values[FinancialScope.PAID] >= values[FinancialScope.GLOBAL]:
            raise error(f"KALILLAC_FINANCE_{period.upper()}_* free + paid must be below the global ceiling.")
    try:
        return FinancialLimits(periods["day"], periods["month"], int(percent))
    except MoneyError as refused:
        raise error("Financial ceilings refused: " + str(refused)) from None

