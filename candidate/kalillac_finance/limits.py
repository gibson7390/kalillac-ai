"""Explicit µUSD ceilings per UTC calendar day and month, per ledger scope
and per provider, plus the most one chat request may be authorized to cost.

There are no defaults and no production values in source. Every period
needs a GLOBAL, FREE and PAID ceiling, with

    FREE + PAID < GLOBAL   and   GLOBAL - FREE - PAID >= the minimum headroom,

where the minimum headroom is ceil(GLOBAL x KALILLAC_FINANCE_MIN_HEADROOM_PERCENT
/ 100) and at least 1 µUSD. The headroom is never admitted to any scope; it
absorbs a settlement overrun, the case where a reservation's conservative
assumption failed (see ledger semantics in
kalillac_db/repositories/finance_ledger.py).

Provider ceilings (OPENAI, BRAVE, TAVILY) bound each provider's own exposure
in the same periods. They are independent of the scope ceilings: an attempt
is admitted only when its scope, GLOBAL and its provider all have room. A
provider ceiling may not exceed its period's GLOBAL ceiling (it could never
bind) and a day ceiling may not exceed its month ceiling.

The request ceilings (KALILLAC_FINANCE_MAX_REQUEST_{FREE,PAID}_USD) bound the
total a single chat request may have authorized at any moment across all of
its provider attempts (model calls, tool-loop turns, searches, retries). A
request ceiling may not exceed its scope's day ceiling.
"""
from dataclasses import dataclass

from .money import MAX_MICROUSD, MoneyError, ceil_div, is_count, parse_usd
from .scopes import FinancialScope, ScopeCeilings

PERIODS = ("day", "month")
PROVIDERS = ("openai", "brave", "tavily")
REQUEST_SCOPES = (FinancialScope.FREE, FinancialScope.PAID)
MIN_HEADROOM_PERCENT = "KALILLAC_FINANCE_MIN_HEADROOM_PERCENT"
LIMIT_NAMES = {(period, scope): f"KALILLAC_FINANCE_{period.upper()}_{scope.value.upper()}_LIMIT_USD"
               for period in PERIODS for scope in FinancialScope}
PROVIDER_LIMIT_NAMES = {(period, provider): f"KALILLAC_FINANCE_{period.upper()}_{provider.upper()}_LIMIT_USD"
                        for period in PERIODS for provider in PROVIDERS}
MAX_REQUEST_NAMES = {scope: f"KALILLAC_FINANCE_MAX_REQUEST_{scope.value.upper()}_USD" for scope in REQUEST_SCOPES}
REQUIRED = (*LIMIT_NAMES.values(), MIN_HEADROOM_PERCENT, *PROVIDER_LIMIT_NAMES.values(),
            *MAX_REQUEST_NAMES.values())


@dataclass(frozen=True)
class ProviderCeilings:
    """µUSD ceilings per provider for one ledger period."""

    openai: int
    brave: int
    tavily: int

    def __post_init__(self):
        if not all(is_count(v, MAX_MICROUSD) for v in (self.openai, self.brave, self.tavily)):
            raise MoneyError("Invalid provider ceiling.")

    def limit(self, provider):
        if type(provider) is not str or provider not in PROVIDERS:
            raise MoneyError("Unknown provider.")
        return getattr(self, provider)


@dataclass(frozen=True)
class FinancialLimits:
    day: ScopeCeilings
    month: ScopeCeilings
    min_headroom_percent: int
    provider_day: ProviderCeilings
    provider_month: ProviderCeilings
    max_request_free: int
    max_request_paid: int

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
        for providers, ceilings in ((self.provider_day, self.day), (self.provider_month, self.month)):
            if type(providers) is not ProviderCeilings:
                raise MoneyError("Invalid provider ceilings.")
            if any(providers.limit(p) > ceilings.global_limit for p in PROVIDERS):
                raise MoneyError("A provider ceiling exceeds its period's global ceiling.")
        if any(self.provider_day.limit(p) > self.provider_month.limit(p) for p in PROVIDERS):
            raise MoneyError("A provider day ceiling exceeds its month ceiling.")
        for value, ceiling in ((self.max_request_free, self.day.free_limit),
                               (self.max_request_paid, self.day.paid_limit)):
            if not is_count(value, MAX_MICROUSD):
                raise MoneyError("Invalid request ceiling.")
            if value > ceiling:
                raise MoneyError("A request ceiling exceeds its scope's day ceiling.")

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

    def provider_limit(self, period, provider):
        if period == "day":
            return self.provider_day.limit(provider)
        if period == "month":
            return self.provider_month.limit(provider)
        raise MoneyError("Unknown ledger period.")

    def max_request(self, scope):
        """The most one chat request charged to `scope` may have authorized."""
        scope = FinancialScope(scope)
        if scope is FinancialScope.FREE:
            return self.max_request_free
        if scope is FinancialScope.PAID:
            return self.max_request_paid
        raise MoneyError("Requests are charged to FREE or PAID.")


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
    periods, providers = {}, {}
    for period in PERIODS:
        values = {scope: usd(LIMIT_NAMES[(period, scope)]) for scope in FinancialScope}
        try:
            periods[period] = ScopeCeilings(values[FinancialScope.GLOBAL], values[FinancialScope.FREE],
                                            values[FinancialScope.PAID])
        except MoneyError:
            raise error(f"KALILLAC_FINANCE_{period.upper()}_* ceilings must fit within the global ceiling.") from None
        if values[FinancialScope.FREE] + values[FinancialScope.PAID] >= values[FinancialScope.GLOBAL]:
            raise error(f"KALILLAC_FINANCE_{period.upper()}_* free + paid must be below the global ceiling.")
        providers[period] = ProviderCeilings(**{p: usd(PROVIDER_LIMIT_NAMES[(period, p)]) for p in PROVIDERS})
        for provider in PROVIDERS:
            if providers[period].limit(provider) > values[FinancialScope.GLOBAL]:
                raise error(PROVIDER_LIMIT_NAMES[(period, provider)] + " must not exceed the period's global ceiling.")
    for provider in PROVIDERS:
        if providers["day"].limit(provider) > providers["month"].limit(provider):
            raise error(PROVIDER_LIMIT_NAMES[("day", provider)] + " must not exceed the provider's month ceiling.")
    requests = {scope: usd(name) for scope, name in MAX_REQUEST_NAMES.items()}
    for scope, name in MAX_REQUEST_NAMES.items():
        if requests[scope] > periods["day"].limit(scope):
            raise error(name + " must not exceed the scope's day ceiling.")
    try:
        return FinancialLimits(periods["day"], periods["month"], int(percent), providers["day"], providers["month"],
                               requests[FinancialScope.FREE], requests[FinancialScope.PAID])
    except MoneyError as refused:
        raise error("Financial ceilings refused: " + str(refused)) from None
