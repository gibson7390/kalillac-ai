"""Actual cost, worst-case reservation and settlement, in exact µUSD.

Pure functions: every price and limit is an explicit argument. No
environment, database, network, clock or provider access.

Token arithmetic is done in "rate units" (tokens x µUSD per 1,000,000
tokens) and divided by 1,000,000 once per call, rounding up, so a call is
never under-counted and is over-counted by less than 1 µUSD.

OpenAI input-token bound (reserve_openai)
-----------------------------------------
Exact tokenization is not available before dispatch, so the reservation
uses an upper bound on input tokens. UTF-8 bytes are NOT treated as equal
to tokens; the bound is

    input_token_bound = len(request body bytes) + configured overhead

where the request body is the exact bytes the bounded transport sends
(bounded_transport._encode_payload: json.dumps, ASCII-only). It cannot
underestimate the tokens derived from Kalillac's own text:

1. A byte-level BPE tokenizer (the kind OpenAI's published tokenizers are)
   splits a string's UTF-8 bytes into tokens that are each at least one
   byte, so a string of n UTF-8 bytes encodes to at most n tokens.
2. Every string the model receives from Kalillac (instructions, messages,
   tool names, descriptions and schemas, replayed output, tool results) is
   a JSON string in the body. The ASCII JSON encoding never uses fewer
   bytes than UTF-8 for any character: ASCII 1 byte (escaped: 2 or 6);
   U+0080-U+07FF 2 bytes vs 6; U+0800-U+FFFF 3 vs 6; astral 4 vs 12; a
   lone surrogate 6 bytes vs 3 for the replacement character. Distinct
   strings occupy disjoint bytes of the body.
   So the tokens of all Kalillac text <= the bytes of those strings <=
   the body bytes, and the remaining body bytes (keys, brackets, quotes,
   separators) are left over as headroom.
3. Tokens the provider adds itself (message and role delimiters, tool
   formatting, any provider preamble) are not Kalillac text. Their count
   is not published, so it is not provable from public information. They
   are covered by the leftover structural bytes of step 2 plus the
   operator-configured overhead (KALILLAC_FINANCE_OPENAI_INPUT_OVERHEAD_TOKENS).
   That is a stated assumption, not a proof: settle_openai flags every
   response whose reported input_tokens exceed the bound, so a wrong
   overhead is detected rather than silently undercharged.

Images (input_image parts) are billed by their pixels, not their bytes,
so the bytes of an image's data URL are left out of the body count and each
image adds the configured per-image bound instead
(KALILLAC_FINANCE_OPENAI_IMAGE_INPUT_TOKENS). Only inline data URLs are
accepted; without that bound, or for any other image reference, the payload
is refused before dispatch. The same overrun flag applies.

Output tokens are bounded by the request's max_output_tokens, which the
Responses API applies to visible and reasoning tokens together.
"""
from dataclasses import dataclass

from kalillac_routing.bounded_transport import _encode_payload

from .money import MoneyError, TOKENS_PER_MTOK, amount, ceil_div, is_count
from .pricing import (MAX_CREDITS_PER_OPERATION, MAX_INPUT_BOUND_TOKENS, MAX_OUTPUT_TOKENS,
                      BravePricing, OpenAIPricing, TavilyPricing)
from .usage import USABLE, OpenAIUsage, normalize_openai_usage, normalize_tavily_credits


class ReservationError(MoneyError):
    """No safe reservation exists; the caller must not dispatch."""

    def __init__(self, message="No safe financial reservation."):
        super().__init__(message)


def _check(value, kind):
    if type(value) is not kind:
        raise MoneyError("Invalid pricing or usage object.")


# --- actual cost ----------------------------------------------------------------------


def openai_cost(pricing, usage):
    """µUSD for one OpenAI response's normalized usage.

    Input tokens are billed by their reported split. Tokens whose split was
    not reported are billed at the highest rate they could have had. Output
    tokens (including reasoning tokens) are billed at the output rate.
    """
    _check(pricing, OpenAIPricing)
    _check(usage, OpenAIUsage)
    rates = pricing.rates_for(usage.input_tokens)
    cached, written = usage.cached_input_tokens, usage.cache_write_tokens
    unattributed_rate = max(rates.input,
                            rates.cached_input if cached is None else 0,
                            rates.cache_write if written is None else 0)
    unattributed = usage.input_tokens - (cached or 0) - (written or 0)
    units = (unattributed * unattributed_rate
             + (cached or 0) * rates.cached_input
             + (written or 0) * rates.cache_write
             + usage.output_tokens * rates.output)
    return amount(ceil_div(units, TOKENS_PER_MTOK))


def brave_cost(pricing):
    """µUSD for one dispatched Brave request."""
    _check(pricing, BravePricing)
    return amount(pricing.per_request)


def tavily_cost(pricing, credits):
    """µUSD for a Tavily operation that billed `credits` credits."""
    _check(pricing, TavilyPricing)
    if not is_count(credits, MAX_CREDITS_PER_OPERATION):
        raise MoneyError("Invalid credit count.")
    return amount(credits * pricing.per_credit)


# --- worst-case reservation -----------------------------------------------------------


@dataclass(frozen=True)
class OpenAIReservation:
    microusd: int
    input_token_bound: int
    output_token_bound: int


def _without_image_data(value, found):
    """A copy of value with every input_image's data URL emptied; found counts the images."""
    if type(value) is dict:
        if value.get("type") == "input_image":
            url = value.get("image_url")
            inline = type(url) is str and url.startswith(("data:image/png;base64,", "data:image/jpeg;base64,"))
            if set(value) - {"type", "image_url", "detail"} or not inline:
                raise ReservationError("Only inline PNG or JPEG images can be reserved.")
            found.append(1)
            return {**value, "image_url": ""}
        return {key: _without_image_data(item, found) for key, item in value.items()}
    if type(value) is list:
        return [_without_image_data(item, found) for item in value]
    return value


def openai_input_token_bound(payload, overhead_tokens, image_tokens=None):
    """Upper bound on billed input tokens for this exact payload (see module doc)."""
    if type(payload) is not dict or not is_count(overhead_tokens, MAX_INPUT_BOUND_TOKENS):
        raise ReservationError()
    images = []
    counted = _without_image_data(payload, images)
    if images and (type(image_tokens) is not int or image_tokens < 1):
        raise ReservationError("Image input has no configured bound.")
    try:
        body = _encode_payload(counted)
    except ValueError:
        raise ReservationError() from None
    bound = len(body) + overhead_tokens + len(images) * (image_tokens or 0)
    if bound > MAX_INPUT_BOUND_TOKENS:
        raise ReservationError("Request input exceeds the reservable bound.")
    return bound


def reserve_openai(pricing, payload):
    """Worst-case µUSD for dispatching this Responses API payload once.

    Refuses (ReservationError) a payload for another model or without an
    explicit max_output_tokens: neither can be bounded.
    """
    _check(pricing, OpenAIPricing)
    if type(payload) is not dict or payload.get("model") != pricing.model:
        raise ReservationError("Payload model is not the priced model.")
    output_bound = payload.get("max_output_tokens")
    if type(output_bound) is not int or not 1 <= output_bound <= MAX_OUTPUT_TOKENS:
        raise ReservationError("Payload has no bounded max_output_tokens.")
    input_bound = openai_input_token_bound(payload, pricing.input_overhead_tokens, pricing.image_input_tokens)
    rates = pricing.reservation_rates(input_bound)
    units = input_bound * rates.max_input + output_bound * rates.output
    return OpenAIReservation(amount(ceil_div(units, TOKENS_PER_MTOK)), input_bound, output_bound)


def reserve_brave(pricing):
    return brave_cost(pricing)


def reserve_tavily(pricing, operation, depth):
    """Worst-case µUSD for one Tavily operation at the given depth."""
    _check(pricing, TavilyPricing)
    return tavily_cost(pricing, pricing.max_credits_for(operation, depth))


# --- settlement -----------------------------------------------------------------------


@dataclass(frozen=True)
class Settlement:
    """The final charge for one DISPATCHED provider operation.

    basis "usage": charged from provider-reported usage (may be below the
    reservation, which batch 2 then releases). Any other basis is the usage
    status that prevented pricing, and the full reservation stays charged.
    bound_exceeded: reported usage exceeded what the reservation assumed;
    charged is still the actual cost (never capped) and overrun is the
    amount above the reservation. Batch 2 must surface it.
    """

    charged: int
    reserved: int
    basis: str
    overrun: int = 0
    bound_exceeded: bool = False


def settle(reserved, actual, basis=USABLE, bound_exceeded=False):
    """Settlement from a reservation and an actual cost (None when unknown)."""
    amount(reserved)
    if actual is None or basis != USABLE:
        return Settlement(reserved, reserved, basis if basis != USABLE else "unknown")
    amount(actual)
    overrun = max(0, actual - reserved)
    return Settlement(actual, reserved, "usage", overrun, bound_exceeded or overrun > 0)


def settle_openai(pricing, reservation, response):
    _check(reservation, OpenAIReservation)
    status, usage = normalize_openai_usage(response)
    if usage is None:
        return settle(reservation.microusd, None, status)
    exceeded = usage.input_tokens > reservation.input_token_bound or usage.output_tokens > reservation.output_token_bound
    return settle(reservation.microusd, openai_cost(pricing, usage), USABLE, exceeded)


def settle_brave(pricing, reserved):
    """Brave reports no usage: a dispatched request costs its price."""
    return settle(reserved, brave_cost(pricing), USABLE)


def settle_tavily(pricing, reserved, response):
    status, credits = normalize_tavily_credits(response)
    if credits is None:
        return settle(reserved, None, status)
    return settle(reserved, tavily_cost(pricing, credits), USABLE)
