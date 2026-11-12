"""Exact integer money.

The unit is the micro-US-dollar (µUSD): 1 µUSD = $0.000001. Every amount is
a Python int; no float or Decimal value is ever built.

Why µUSD: the smallest published provider prices are fractions of a dollar
per million tokens, so a token rate stored as µUSD per 1,000,000 tokens is
an exact integer for any price quoted to six decimal places of a dollar.
A single call's cost is rounded up once, to a whole µUSD, so the most a
call can be over-counted by rounding is $0.000001. Every amount produced
here stays at or below MAX_MICROUSD, far inside PostgreSQL BIGINT, so the
batch 2 ledger can store them unchanged.

Operator text is matched against a short ASCII grammar BEFORE any integer is
built: exponents, signs, NaN/Infinity, digit separators, Unicode digits and
over-long input are rejected, so no input can expand into a huge integer or
spend unbounded time.
"""
import re

MICROUSD_PER_USD = 1_000_000
TOKENS_PER_MTOK = 1_000_000

# $1,000,000,000. PostgreSQL BIGINT holds 9,223,372,036,854,775,807.
MAX_MICROUSD = 10**15

# Longest accepted text: "999999999.999999" (16 characters).
MAX_USD_TEXT = 16
_USD = re.compile(r"(0|[1-9][0-9]{0,8})(?:\.([0-9]{1,6}))?", re.ASCII)


class MoneyError(ValueError):
    """An amount outside the exact, bounded representation. Fixed message;
    the offending value is never echoed."""

    def __init__(self, message="Invalid money amount."):
        super().__init__(message)


def is_count(value, maximum):
    """A non-negative builtin int (not bool) no larger than maximum."""
    return type(value) is int and 0 <= value <= maximum


def require_count(value, maximum, message="Invalid count."):
    if not is_count(value, maximum):
        raise MoneyError(message)
    return value


def parse_usd(text, *, maximum_microusd, minimum_microusd=0):
    """Exact µUSD for a plain decimal dollar string such as "1.25".

    Grammar: digits without a leading zero (or a lone "0"), then optionally
    a point and one to six digits. At most 999,999,999.999999 dollars.
    """
    if not (is_count(maximum_microusd, MAX_MICROUSD) and is_count(minimum_microusd, maximum_microusd)):
        raise MoneyError("Invalid money bounds.")
    if type(text) is not str or len(text) > MAX_USD_TEXT:
        raise MoneyError()
    match = _USD.fullmatch(text)
    if match is None:
        raise MoneyError()
    whole, fraction = match.group(1), match.group(2) or ""
    value = int(whole) * MICROUSD_PER_USD + int(fraction.ljust(6, "0"))
    if not minimum_microusd <= value <= maximum_microusd:
        raise MoneyError("Money amount is outside the supported range.")
    return value


def ceil_div(numerator, denominator):
    """Exact ceiling of numerator / denominator for non-negative ints."""
    if type(numerator) is not int or type(denominator) is not int or numerator < 0 or denominator <= 0:
        raise MoneyError("Invalid division.")
    return -(-numerator // denominator)


def amount(value):
    """A checked µUSD result: an int from 0 to MAX_MICROUSD."""
    return require_count(value, MAX_MICROUSD, "Money amount is outside the supported range.")
