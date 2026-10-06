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

Bounded provider transport (the request-budgeted OpenAI path):

- KALILLAC_TRANSPORT_MAX_OUTSTANDING: admitted, unfinished transport
  requests at once (per process).
- KALILLAC_TRANSPORT_DNS_THREADS: threads for DNS lookups.
- KALILLAC_TRANSPORT_MAX_PENDING_DNS: queued plus running DNS lookups.
- KALILLAC_TRANSPORT_CANCEL_POLL_INTERVAL_SECONDS: how often a waiting call
  checks for request cancellation.
- KALILLAC_TRANSPORT_BACKSTOP_GRACE_SECONDS: how long past a call's own
  deadline the caller waits before cancelling it from outside.
- KALILLAC_TRANSPORT_CLEANUP_GRACE_SECONDS: how long a cancelled call may
  take to confirm its cleanup.
- KALILLAC_TRANSPORT_CLOSE_TIMEOUT_SECONDS: the most shutdown waits for the
  transport to close.
- KALILLAC_OPENAI_MAX_BYTES: the largest OpenAI response body accepted, on
  the wire and decoded.

Bounded Tavily transport (the request-budgeted search path). Tavily has its
own transport and capacity, never OpenAI's; the same rules apply to each
setting as to its OpenAI counterpart above:

- KALILLAC_TAVILY_TRANSPORT_MAX_OUTSTANDING
- KALILLAC_TAVILY_TRANSPORT_DNS_THREADS
- KALILLAC_TAVILY_TRANSPORT_MAX_PENDING_DNS
- KALILLAC_TAVILY_TRANSPORT_CANCEL_POLL_INTERVAL_SECONDS
- KALILLAC_TAVILY_TRANSPORT_BACKSTOP_GRACE_SECONDS
- KALILLAC_TAVILY_TRANSPORT_CLEANUP_GRACE_SECONDS
- KALILLAC_TAVILY_TRANSPORT_CLOSE_TIMEOUT_SECONDS
- KALILLAC_TAVILY_SEARCH_MAX_BYTES: the largest Tavily search response body
  accepted, on the wire and decoded. The selected value is 262144 (the
  largest measured search response was 8,667 bytes).
- KALILLAC_TAVILY_EXTRACT_MAX_BYTES: the largest Tavily extraction response
  body accepted, on the wire and decoded. The selected value is 524288 (the
  largest measured extraction response was 1,081 bytes).

These mirror BoundedTransport's own constructor rules: counts are positive
integers, seconds positive finite numbers.

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
TRANSPORT_MAX_OUTSTANDING = "KALILLAC_TRANSPORT_MAX_OUTSTANDING"
TRANSPORT_DNS_THREADS = "KALILLAC_TRANSPORT_DNS_THREADS"
TRANSPORT_MAX_PENDING_DNS = "KALILLAC_TRANSPORT_MAX_PENDING_DNS"
TRANSPORT_CANCEL_POLL_INTERVAL_SECONDS = "KALILLAC_TRANSPORT_CANCEL_POLL_INTERVAL_SECONDS"
TRANSPORT_BACKSTOP_GRACE_SECONDS = "KALILLAC_TRANSPORT_BACKSTOP_GRACE_SECONDS"
TRANSPORT_CLEANUP_GRACE_SECONDS = "KALILLAC_TRANSPORT_CLEANUP_GRACE_SECONDS"
TRANSPORT_CLOSE_TIMEOUT_SECONDS = "KALILLAC_TRANSPORT_CLOSE_TIMEOUT_SECONDS"
OPENAI_MAX_BYTES = "KALILLAC_OPENAI_MAX_BYTES"
TAVILY_TRANSPORT_MAX_OUTSTANDING = "KALILLAC_TAVILY_TRANSPORT_MAX_OUTSTANDING"
TAVILY_TRANSPORT_DNS_THREADS = "KALILLAC_TAVILY_TRANSPORT_DNS_THREADS"
TAVILY_TRANSPORT_MAX_PENDING_DNS = "KALILLAC_TAVILY_TRANSPORT_MAX_PENDING_DNS"
TAVILY_TRANSPORT_CANCEL_POLL_INTERVAL_SECONDS = (
    "KALILLAC_TAVILY_TRANSPORT_CANCEL_POLL_INTERVAL_SECONDS"
)
TAVILY_TRANSPORT_BACKSTOP_GRACE_SECONDS = "KALILLAC_TAVILY_TRANSPORT_BACKSTOP_GRACE_SECONDS"
TAVILY_TRANSPORT_CLEANUP_GRACE_SECONDS = "KALILLAC_TAVILY_TRANSPORT_CLEANUP_GRACE_SECONDS"
TAVILY_TRANSPORT_CLOSE_TIMEOUT_SECONDS = "KALILLAC_TAVILY_TRANSPORT_CLOSE_TIMEOUT_SECONDS"
TAVILY_SEARCH_MAX_BYTES = "KALILLAC_TAVILY_SEARCH_MAX_BYTES"
TAVILY_EXTRACT_MAX_BYTES = "KALILLAC_TAVILY_EXTRACT_MAX_BYTES"

_SECONDS_RE = re.compile(r"[0-9]+(\.[0-9]+)?")
_COUNT_RE = re.compile(r"[0-9]+")


class RequestLimitConfigError(RuntimeError):
    """The request budget is enabled but its configuration is invalid."""


@dataclass(frozen=True)
class TransportLimits:
    max_outstanding: int
    dns_threads: int
    max_pending_dns: int
    cancel_poll_interval_seconds: float
    backstop_grace_seconds: float
    cleanup_grace_seconds: float
    close_timeout_seconds: float


@dataclass(frozen=True)
class RequestLimits:
    deadline_seconds: float
    queue_wait_seconds: float
    max_model_attempts: int
    max_search_attempts: int
    transport: TransportLimits
    openai_max_bytes: int
    tavily_transport: TransportLimits
    tavily_search_max_bytes: int
    tavily_extract_max_bytes: int


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

    max_model_attempts = _positive_count(MAX_MODEL_ATTEMPTS)
    max_search_attempts = _positive_count(MAX_SEARCH_ATTEMPTS)

    transport = TransportLimits(
        max_outstanding=_positive_count(TRANSPORT_MAX_OUTSTANDING),
        dns_threads=_positive_count(TRANSPORT_DNS_THREADS),
        max_pending_dns=_positive_count(TRANSPORT_MAX_PENDING_DNS),
        cancel_poll_interval_seconds=_positive_seconds(
            TRANSPORT_CANCEL_POLL_INTERVAL_SECONDS
        ),
        backstop_grace_seconds=_positive_seconds(TRANSPORT_BACKSTOP_GRACE_SECONDS),
        cleanup_grace_seconds=_positive_seconds(TRANSPORT_CLEANUP_GRACE_SECONDS),
        close_timeout_seconds=_positive_seconds(TRANSPORT_CLOSE_TIMEOUT_SECONDS),
    )
    openai_max_bytes = _positive_count(OPENAI_MAX_BYTES)

    tavily_transport = TransportLimits(
        max_outstanding=_positive_count(TAVILY_TRANSPORT_MAX_OUTSTANDING),
        dns_threads=_positive_count(TAVILY_TRANSPORT_DNS_THREADS),
        max_pending_dns=_positive_count(TAVILY_TRANSPORT_MAX_PENDING_DNS),
        cancel_poll_interval_seconds=_positive_seconds(
            TAVILY_TRANSPORT_CANCEL_POLL_INTERVAL_SECONDS
        ),
        backstop_grace_seconds=_positive_seconds(
            TAVILY_TRANSPORT_BACKSTOP_GRACE_SECONDS
        ),
        cleanup_grace_seconds=_positive_seconds(TAVILY_TRANSPORT_CLEANUP_GRACE_SECONDS),
        close_timeout_seconds=_positive_seconds(TAVILY_TRANSPORT_CLOSE_TIMEOUT_SECONDS),
    )

    return RequestLimits(
        deadline_seconds=deadline,
        queue_wait_seconds=queue_wait,
        max_model_attempts=max_model_attempts,
        max_search_attempts=max_search_attempts,
        transport=transport,
        openai_max_bytes=openai_max_bytes,
        tavily_transport=tavily_transport,
        tavily_search_max_bytes=_positive_count(TAVILY_SEARCH_MAX_BYTES),
        tavily_extract_max_bytes=_positive_count(TAVILY_EXTRACT_MAX_BYTES),
    )
