"""Per-request budget settings for /api/chat.

KALILLAC_REQUEST_BUDGET_ENABLED is off by default. While it is off nothing
else here is read and /api/chat behaves exactly as before. When it is on,
every setting below must be set explicitly; there are no built-in values,
so no limit exists that the operator did not choose:

- KALILLAC_REQUEST_DEADLINE_SECONDS: total time for one request, from
  acceptance to the final response, including queue waits.
- KALILLAC_REQUEST_QUEUE_WAIT_SECONDS: the most one request may wait, in
  total, for its session lock and an execution slot. Must be less than the
  deadline. Exceeding it is HTTP 429 busy; reaching the overall deadline
  first is HTTP 504 request_timeout.
- KALILLAC_REQUEST_MAX_MODEL_ATTEMPTS: model network attempts per request,
  counting every primary call, continuation, fallback, repair and
  native-tool round.
- KALILLAC_REQUEST_MAX_SEARCH_ATTEMPTS: search-provider network operations
  per request. This counts every Tavily search, the domain retry search,
  AND every article extraction, because each is a separate network
  operation.

Validation errors name the setting, never its value. Seconds accept plain
decimal numbers only ("90", "12.5"); counts accept ASCII digits only.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
import re


_TRUE_VALUES = {"1", "true", "yes", "on"}

ENABLED_FLAG = "KALILLAC_REQUEST_BUDGET_ENABLED"
DEADLINE_SECONDS = "KALILLAC_REQUEST_DEADLINE_SECONDS"
QUEUE_WAIT_SECONDS = "KALILLAC_REQUEST_QUEUE_WAIT_SECONDS"
MAX_MODEL_ATTEMPTS = "KALILLAC_REQUEST_MAX_MODEL_ATTEMPTS"
MAX_SEARCH_ATTEMPTS = "KALILLAC_REQUEST_MAX_SEARCH_ATTEMPTS"

_SECONDS_RE = re.compile(r"[0-9]+(\.[0-9]+)?")
_COUNT_RE = re.compile(r"[0-9]+")


class RequestLimitConfigError(RuntimeError):
    """The request budget is enabled but its configuration is invalid."""


@dataclass(frozen=True)
class RequestLimits:
    deadline_seconds: float
    queue_wait_seconds: float
    max_model_attempts: int
    max_search_attempts: int


def request_budget_enabled() -> bool:
    value = os.getenv(ENABLED_FLAG, "")
    return value.strip().lower() in _TRUE_VALUES


def _raw(name: str) -> str:
    value = os.getenv(name, "").strip()

    if not value:
        raise RequestLimitConfigError(
            f"{ENABLED_FLAG} is enabled but {name} is not set."
        )

    return value


def _positive_seconds(name: str) -> float:
    value = _raw(name)
    # The message names the setting, never the value.
    invalid = RequestLimitConfigError(f"{name} must be a positive number of seconds.")

    if not _SECONDS_RE.fullmatch(value):
        raise invalid

    seconds = float(value)

    if not math.isfinite(seconds) or seconds <= 0:
        raise invalid

    return seconds


def _positive_count(name: str) -> int:
    value = _raw(name)
    invalid = RequestLimitConfigError(f"{name} must be a positive integer.")

    if not _COUNT_RE.fullmatch(value):
        raise invalid

    try:
        count = int(value)
    except ValueError:
        # Beyond Python's int conversion limit.
        raise invalid from None

    if count <= 0:
        raise invalid

    return count


def load_request_limits() -> RequestLimits | None:
    """None while disabled; otherwise validated limits.

    Raises RequestLimitConfigError naming the first invalid setting.
    """

    if not request_budget_enabled():
        return None

    deadline = _positive_seconds(DEADLINE_SECONDS)
    queue_wait = _positive_seconds(QUEUE_WAIT_SECONDS)

    if queue_wait >= deadline:
        raise RequestLimitConfigError(
            f"{QUEUE_WAIT_SECONDS} must be less than {DEADLINE_SECONDS}."
        )

    return RequestLimits(
        deadline_seconds=deadline,
        queue_wait_seconds=queue_wait,
        max_model_attempts=_positive_count(MAX_MODEL_ATTEMPTS),
        max_search_attempts=_positive_count(MAX_SEARCH_ATTEMPTS),
    )
