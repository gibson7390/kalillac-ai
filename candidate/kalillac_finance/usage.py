"""Normalized integer provider usage for cost settlement.

OpenAI usage is read by kalillac_routing.provider_usage._numbers, the
evaluation parser, reused unchanged so evaluation and accounting can never
read the same response differently. It accepts only exact builtin ints from
0 to MAX_TOKENS and rejects inconsistent partitions (cached or cache-write
tokens exceeding input, reasoning exceeding output, a total that does not
add up).

Nothing here substitutes zero for an unknown count. A response whose usage
is missing, malformed or lacks input or output tokens yields no usage, and
settlement then keeps the worst-case reservation. A missing cached-input or
cache-write split is kept as None; cost.openai_cost then bills those tokens
at the highest rate they could have been billed at.
"""
from dataclasses import dataclass

from kalillac_routing.provider_usage import MAX_TOKENS, _counter, _numbers

from .money import MoneyError, is_count
from .pricing import MAX_CREDITS_PER_OPERATION

USABLE = "usable"
MISSING = "missing"
INVALID = "invalid"
PARTIAL = "partial"  # usage present but without input or output tokens


@dataclass(frozen=True)
class OpenAIUsage:
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int | None = None  # within input_tokens; None = not reported
    cache_write_tokens: int | None = None  # within input_tokens; None = not reported
    reasoning_tokens: int | None = None  # within output_tokens; informational only

    def __post_init__(self):
        optional = (self.cached_input_tokens, self.cache_write_tokens, self.reasoning_tokens)
        if not (is_count(self.input_tokens, MAX_TOKENS) and is_count(self.output_tokens, MAX_TOKENS)
                and all(v is None or is_count(v, MAX_TOKENS) for v in optional)):
            raise MoneyError("Invalid usage count.")
        if (self.cached_input_tokens or 0) + (self.cache_write_tokens or 0) > self.input_tokens:
            raise MoneyError("Invalid input partition.")
        if (self.reasoning_tokens or 0) > self.output_tokens:
            raise MoneyError("Invalid output partition.")


def normalize_openai_usage(response):
    """(status, OpenAIUsage | None) for a Responses API response object."""
    numbers = _numbers(response)
    status = numbers["usage_status"]
    if status in (MISSING, INVALID):
        return status, None
    if numbers["input_tokens"] is None or numbers["output_tokens"] is None:
        return PARTIAL, None
    try:
        usage = OpenAIUsage(numbers["input_tokens"], numbers["output_tokens"], numbers["cached_tokens"],
                            numbers["cache_write_tokens"], numbers["reasoning_tokens"])
    except MoneyError:
        return INVALID, None
    return USABLE, usage


def normalize_tavily_credits(response):
    """(status, credits | None) from a Tavily response's usage.credits.

    Tavily reports usage only when asked to; the application does not ask
    yet, so today every response is MISSING and is charged its reservation.
    A count above MAX_CREDITS_PER_OPERATION is treated as malformed.
    """
    usage = response.get("usage") if type(response) is dict else None
    if usage is not None and type(usage) is not dict:
        return INVALID, None
    try:
        credits = _counter(usage, "credits")
    except ValueError:
        return INVALID, None
    if credits is None:
        return MISSING, None
    if credits > MAX_CREDITS_PER_OPERATION:
        return INVALID, None
    return USABLE, credits
