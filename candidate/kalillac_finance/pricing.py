"""Explicit price configuration. There are no default or built-in prices.

load_pricing() reads nothing but KALILLAC_FINANCIAL_SAFEGUARDS_ENABLED
unless that flag is true. When it is true, every variable in REQUIRED must
be explicitly set or loading refuses; batch 1 does not call it from the
application, so the running service is unaffected either way.

Dollar amounts are plain decimal strings with at most six decimal places
(see money.parse_usd) and are held as exact µUSD integers.

OpenAI rates are dollars per 1,000,000 tokens of the configured model at
the service tier the application uses (it requests none, so the account
default). Reasoning tokens have no rate of their own: the Responses API
reports them inside output_tokens, which are billed at the output rate, so
a separate reasoning rate would double count. Cached-input and cache-write
rates are separate because the API reports those tokens separately inside
input_tokens; set a rate equal to the input rate where no discount or
surcharge applies. If the model bills a whole request at different rates
above an input-token threshold, set that threshold and its four rates;
otherwise set the threshold to "none".

Brave is priced per dispatched request: its responses carry no usage, so
the request is the only unit the application can authoritatively count.
Tavily is priced per credit. The per-operation credit maxima are the most
one dispatched operation can bill; they are the reservation and, until a
response reports usage.credits, also the charge.
"""
from dataclasses import dataclass
import os
import re

from kalillac_routing.provider_usage import MAX_TOKENS as MAX_OUTPUT_TOKENS

from .money import MICROUSD_PER_USD, MoneyError, is_count, parse_usd

# µUSD per 1,000,000 tokens: at most $100,000 per million tokens.
MAX_TOKEN_RATE = 100_000 * MICROUSD_PER_USD
# µUSD per Brave request or Tavily credit: at most $1,000.
MAX_OPERATION_PRICE = 1_000 * MICROUSD_PER_USD
MAX_CREDITS_PER_OPERATION = 1_000
MAX_OVERHEAD_TOKENS = 1_000_000
# The largest input-token bound a reservation accepts (2**24); a payload
# needing more is refused before dispatch rather than reserved.
MAX_INPUT_BOUND_TOKENS = 16_777_216

TAVILY_OPERATIONS = (("search", "basic"), ("search", "advanced"), ("extract", "basic"), ("extract", "advanced"))
_MODEL_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}", re.ASCII)
_COUNT = re.compile(r"(0|[1-9][0-9]{0,8})", re.ASCII)

ENABLE = "KALILLAC_FINANCIAL_SAFEGUARDS_ENABLED"
TRUE = frozenset({"1", "true", "yes", "on"})
FALSE = frozenset({"", "0", "false", "no", "off"})

OPENAI_MODEL = "KALILLAC_PRICE_OPENAI_MODEL"
OPENAI_RATES = {
    "input": "KALILLAC_PRICE_OPENAI_INPUT_USD_PER_MTOK",
    "cached_input": "KALILLAC_PRICE_OPENAI_CACHED_INPUT_USD_PER_MTOK",
    "cache_write": "KALILLAC_PRICE_OPENAI_CACHE_WRITE_USD_PER_MTOK",
    "output": "KALILLAC_PRICE_OPENAI_OUTPUT_USD_PER_MTOK",
}
LONG_CONTEXT_ABOVE = "KALILLAC_PRICE_OPENAI_LONG_CONTEXT_ABOVE_INPUT_TOKENS"
LONG_CONTEXT_RATES = {
    "input": "KALILLAC_PRICE_OPENAI_LONG_CONTEXT_INPUT_USD_PER_MTOK",
    "cached_input": "KALILLAC_PRICE_OPENAI_LONG_CONTEXT_CACHED_INPUT_USD_PER_MTOK",
    "cache_write": "KALILLAC_PRICE_OPENAI_LONG_CONTEXT_CACHE_WRITE_USD_PER_MTOK",
    "output": "KALILLAC_PRICE_OPENAI_LONG_CONTEXT_OUTPUT_USD_PER_MTOK",
}
OPENAI_OVERHEAD = "KALILLAC_FINANCE_OPENAI_INPUT_OVERHEAD_TOKENS"
BRAVE_REQUEST = "KALILLAC_PRICE_BRAVE_USD_PER_REQUEST"
TAVILY_CREDIT = "KALILLAC_PRICE_TAVILY_USD_PER_CREDIT"
TAVILY_MAX_CREDITS = {op: "KALILLAC_FINANCE_TAVILY_MAX_CREDITS_" + op[0].upper() + "_" + op[1].upper()
                      for op in TAVILY_OPERATIONS}

REQUIRED = (OPENAI_MODEL, *OPENAI_RATES.values(), LONG_CONTEXT_ABOVE, OPENAI_OVERHEAD,
            BRAVE_REQUEST, TAVILY_CREDIT, *TAVILY_MAX_CREDITS.values())
KNOWN = frozenset(REQUIRED) | frozenset(LONG_CONTEXT_RATES.values())
_FAMILIES = ("KALILLAC_PRICE_", "KALILLAC_FINANCE_")


class PricingConfigError(RuntimeError):
    """Refused price configuration. Messages name a variable, never a value."""


def _rate(value, positive):
    return is_count(value, MAX_TOKEN_RATE) and (value > 0 or not positive)


@dataclass(frozen=True)
class TokenRates:
    """µUSD per 1,000,000 tokens. Input and output must be non-zero."""

    input: int
    cached_input: int
    cache_write: int
    output: int

    def __post_init__(self):
        if not (_rate(self.input, True) and _rate(self.output, True)
                and _rate(self.cached_input, False) and _rate(self.cache_write, False)):
            raise MoneyError("Invalid token rate.")

    @property
    def max_input(self):
        """The highest rate any input token can be billed at."""
        return max(self.input, self.cached_input, self.cache_write)

    def ceiling(self, other):
        """Componentwise maximum of two rate sets."""
        return TokenRates(max(self.input, other.input), max(self.cached_input, other.cached_input),
                          max(self.cache_write, other.cache_write), max(self.output, other.output))


@dataclass(frozen=True)
class LongContext:
    """Rates billed for a whole request whose input_tokens exceed above."""

    above_input_tokens: int
    rates: TokenRates

    def __post_init__(self):
        if type(self.rates) is not TokenRates or type(self.above_input_tokens) is not int \
                or not 1 <= self.above_input_tokens <= MAX_INPUT_BOUND_TOKENS:
            raise MoneyError("Invalid long-context pricing.")


@dataclass(frozen=True)
class OpenAIPricing:
    model: str
    standard: TokenRates
    long_context: LongContext | None
    input_overhead_tokens: int

    def __post_init__(self):
        if type(self.model) is not str or _MODEL_ID.fullmatch(self.model) is None:
            raise MoneyError("Invalid priced model.")
        if type(self.standard) is not TokenRates or not (self.long_context is None or type(self.long_context) is LongContext):
            raise MoneyError("Invalid OpenAI pricing.")
        if not is_count(self.input_overhead_tokens, MAX_OVERHEAD_TOKENS):
            raise MoneyError("Invalid input overhead.")

    def rates_for(self, input_tokens):
        """The rates billed for a request with exactly input_tokens."""
        if self.long_context is not None and input_tokens > self.long_context.above_input_tokens:
            return self.long_context.rates
        return self.standard

    def reservation_rates(self, input_token_bound):
        """Rates covering every tier a request with at most input_token_bound
        input tokens could be billed at (componentwise maximum)."""
        if self.long_context is not None and input_token_bound > self.long_context.above_input_tokens:
            return self.standard.ceiling(self.long_context.rates)
        return self.standard


@dataclass(frozen=True)
class BravePricing:
    per_request: int

    def __post_init__(self):
        if not is_count(self.per_request, MAX_OPERATION_PRICE) or self.per_request == 0:
            raise MoneyError("Invalid Brave price.")


@dataclass(frozen=True)
class TavilyPricing:
    per_credit: int
    max_credits: tuple  # ((operation, depth, credits), ...) for every TAVILY_OPERATIONS entry

    def __post_init__(self):
        if not is_count(self.per_credit, MAX_OPERATION_PRICE) or self.per_credit == 0:
            raise MoneyError("Invalid Tavily price.")
        if type(self.max_credits) is not tuple or len(self.max_credits) != len(TAVILY_OPERATIONS) \
                or {entry[:2] for entry in self.max_credits if type(entry) is tuple and len(entry) == 3} != set(TAVILY_OPERATIONS) \
                or not all(type(e[2]) is int and 1 <= e[2] <= MAX_CREDITS_PER_OPERATION for e in self.max_credits):
            raise MoneyError("Invalid Tavily credit maxima.")

    def max_credits_for(self, operation, depth):
        for entry in self.max_credits:
            if entry[0] == operation and entry[1] == depth and type(operation) is str and type(depth) is str:
                return entry[2]
        raise MoneyError("Unpriced Tavily operation.")


@dataclass(frozen=True)
class PriceTable:
    openai: OpenAIPricing
    brave: BravePricing
    tavily: TavilyPricing

    def __post_init__(self):
        if (type(self.openai), type(self.brave), type(self.tavily)) != (OpenAIPricing, BravePricing, TavilyPricing):
            raise MoneyError("Invalid price table.")


def _flag(environ):
    value = environ.get(ENABLE, "")
    value = value.strip().lower() if type(value) is str else None
    if value in TRUE:
        return True
    if value in FALSE:
        return False
    raise PricingConfigError(ENABLE + " must be true or false.")


def _text(environ, name):
    value = environ.get(name)
    if type(value) is not str or not value.strip():
        raise PricingConfigError(name + " must be set explicitly.")
    return value.strip()


def _usd(environ, name, maximum, minimum):
    try:
        return parse_usd(_text(environ, name), maximum_microusd=maximum, minimum_microusd=minimum)
    except MoneyError:
        raise PricingConfigError(name + " requires a plain dollar amount within the supported range.") from None


def _count(environ, name, minimum, maximum):
    text = _text(environ, name)
    if _COUNT.fullmatch(text) is None or not minimum <= int(text) <= maximum:
        raise PricingConfigError(name + " requires a whole number within the supported range.")
    return int(text)


def _rates(environ, names):
    return TokenRates(**{field: _usd(environ, name, MAX_TOKEN_RATE, 1 if field in ("input", "output") else 0)
                         for field, name in names.items()})


def load_pricing(environ=None):
    """The PriceTable, or None when financial safeguards are not enabled.

    With the flag off only the flag itself is read. With it on, every
    REQUIRED variable must be explicit, long-context rates must be present
    exactly when a threshold is set, and an unrecognized KALILLAC_PRICE_* or
    KALILLAC_FINANCE_* variable is refused (a mistyped or unsupported price,
    such as a reasoning rate, must not be silently ignored).
    """
    environ = os.environ if environ is None else environ
    if not _flag(environ):
        return None
    unknown = sorted(name for name in environ if name.startswith(_FAMILIES) and name not in KNOWN)
    if unknown:
        raise PricingConfigError("Unsupported pricing variable: " + unknown[0] + ".")
    for name in REQUIRED:
        _text(environ, name)
    model = _text(environ, OPENAI_MODEL)
    if _MODEL_ID.fullmatch(model) is None:
        raise PricingConfigError(OPENAI_MODEL + " requires a model identifier.")
    above = _text(environ, LONG_CONTEXT_ABOVE)
    long_names = [name for name in LONG_CONTEXT_RATES.values() if name in environ]
    if above.lower() == "none":
        if long_names:
            raise PricingConfigError(long_names[0] + " requires " + LONG_CONTEXT_ABOVE + ".")
        long_context = None
    else:
        threshold = _count(environ, LONG_CONTEXT_ABOVE, 1, MAX_INPUT_BOUND_TOKENS)
        long_context = LongContext(threshold, _rates(environ, LONG_CONTEXT_RATES))
    openai = OpenAIPricing(model, _rates(environ, OPENAI_RATES), long_context,
                           _count(environ, OPENAI_OVERHEAD, 0, MAX_OVERHEAD_TOKENS))
    brave = BravePricing(_usd(environ, BRAVE_REQUEST, MAX_OPERATION_PRICE, 1))
    tavily = TavilyPricing(_usd(environ, TAVILY_CREDIT, MAX_OPERATION_PRICE, 1),
                           tuple((*op, _count(environ, name, 1, MAX_CREDITS_PER_OPERATION))
                                 for op, name in TAVILY_MAX_CREDITS.items()))
    return PriceTable(openai, brave, tavily)
