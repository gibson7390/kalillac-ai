"""Failed-login abuse protection.

Failures are counted in a sliding window under three independent keys:

- client + email: the tight limit. Stops guessing one account from one
  client without letting anyone else lock that account.
- client: stops one client spraying passwords across many emails.
- email: a much higher limit that catches guessing one account from many
  clients. Reaching it takes sustained failures from many clients, so a
  victim address is not trivially lockable, and the block expires on its own.

An attempt is refused (HTTP 429) while any key is at its limit. The check
runs before any password verification and is identical for registered and
unregistered emails, so it reveals nothing about which emails exist.

Concurrency: `begin` counts in-flight attempts together with recorded
failures under one lock, so parallel requests cannot slip past a limit.

Deployment limitation: the bundled store is in-process memory. That is
correct for the current single Uvicorn worker. With multiple workers or
hosts each process would count separately; replace InMemoryFailureStore
with a shared implementation of FailureStore (for example Redis with an
atomic script) before scaling out. State is lost on restart, which only
shortens an active block.

Only hashed identifiers are kept: never raw emails, passwords, or any
conversation content.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass
import hashlib
import ipaddress
import os
import threading
import time
from typing import Callable, Protocol, Sequence


@dataclass(frozen=True)
class LimitRule:
    scope: str
    max_failures: int
    window_seconds: float


DEFAULT_RULES = (
    LimitRule("client_email", max_failures=5, window_seconds=15 * 60),
    LimitRule("client", max_failures=30, window_seconds=15 * 60),
    LimitRule("email", max_failures=50, window_seconds=15 * 60),
)


@dataclass(frozen=True)
class LimitKey:
    rule: LimitRule
    key: str


@dataclass(frozen=True)
class AttemptDecision:
    allowed: bool
    retry_after_seconds: int = 0


class FailureStore(Protocol):
    """Shared-store contract. Each method must be atomic across all keys."""

    def begin(self, keys: Sequence[LimitKey], now: float) -> AttemptDecision:
        """Refuse if any key is at its limit; otherwise reserve a slot."""

    def finish(
        self,
        keys: Sequence[LimitKey],
        now: float,
        *,
        failed: bool,
        reset: Sequence[LimitKey] = (),
    ) -> None:
        """Release the reserved slot, record a failure, or reset keys."""


class _Bucket:
    __slots__ = ("failures", "in_flight")

    def __init__(self) -> None:
        self.failures: deque[float] = deque()
        self.in_flight = 0


class InMemoryFailureStore:
    """Thread-safe, bounded, in-process FailureStore (one worker only)."""

    def __init__(self, max_keys: int = 100_000) -> None:
        self._lock = threading.Lock()
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()
        self._max_keys = max_keys

    @staticmethod
    def _expire(bucket: _Bucket, rule: LimitRule, now: float) -> None:
        cutoff = now - rule.window_seconds

        while bucket.failures and bucket.failures[0] <= cutoff:
            bucket.failures.popleft()

    def _bucket(self, key: str) -> _Bucket:
        bucket = self._buckets.get(key)

        if bucket is None:
            bucket = _Bucket()
            self._buckets[key] = bucket

            # Bound memory: drop the least recently used idle buckets.
            while len(self._buckets) > self._max_keys:
                oldest_key, oldest = next(iter(self._buckets.items()))

                if oldest.in_flight:
                    self._buckets.move_to_end(oldest_key)
                    break

                del self._buckets[oldest_key]
        else:
            self._buckets.move_to_end(key)

        return bucket

    def begin(self, keys: Sequence[LimitKey], now: float) -> AttemptDecision:
        with self._lock:
            retry_after = 0.0

            for limit_key in keys:
                bucket = self._bucket(limit_key.key)
                self._expire(bucket, limit_key.rule, now)

                used = len(bucket.failures) + bucket.in_flight

                if used >= limit_key.rule.max_failures:
                    if bucket.failures:
                        reopens = bucket.failures[0] + limit_key.rule.window_seconds
                        retry_after = max(retry_after, reopens - now)
                    else:
                        retry_after = max(retry_after, 1.0)

            if retry_after > 0:
                return AttemptDecision(
                    allowed=False,
                    retry_after_seconds=max(1, int(retry_after + 0.999)),
                )

            for limit_key in keys:
                self._bucket(limit_key.key).in_flight += 1

            return AttemptDecision(allowed=True)

    def finish(
        self,
        keys: Sequence[LimitKey],
        now: float,
        *,
        failed: bool,
        reset: Sequence[LimitKey] = (),
    ) -> None:
        with self._lock:
            for limit_key in keys:
                bucket = self._bucket(limit_key.key)
                bucket.in_flight = max(0, bucket.in_flight - 1)

                if failed:
                    bucket.failures.append(now)
                    self._expire(bucket, limit_key.rule, now)

            for limit_key in reset:
                bucket = self._buckets.get(limit_key.key)

                if bucket is not None:
                    bucket.failures.clear()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class LoginRateLimiter:
    """Applies the limit rules to one login attempt."""

    def __init__(
        self,
        store: FailureStore | None = None,
        rules: Sequence[LimitRule] = DEFAULT_RULES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store or InMemoryFailureStore()
        self._rules = {rule.scope: rule for rule in rules}
        self._clock = clock

    def keys_for(self, client: str, email: str) -> list[LimitKey]:
        client_id = _digest(f"client:{client}")
        email_id = _digest(f"email:{email}")
        values = {
            "client_email": f"client_email:{client_id}:{email_id}",
            "client": f"client:{client_id}",
            "email": f"email:{email_id}",
        }

        return [
            LimitKey(rule, values[scope])
            for scope, rule in self._rules.items()
            if scope in values
        ]

    def begin(self, keys: Sequence[LimitKey]) -> AttemptDecision:
        return self._store.begin(keys, self._clock())

    def record_failure(self, keys: Sequence[LimitKey]) -> None:
        self._store.finish(keys, self._clock(), failed=True)

    def release(self, keys: Sequence[LimitKey]) -> None:
        """End an attempt that neither failed nor succeeded (server error)."""
        self._store.finish(keys, self._clock(), failed=False)

    def record_success(self, keys: Sequence[LimitKey]) -> None:
        # Success clears the account-specific keys. The client-wide key is
        # deliberately NOT reset: otherwise a client could interleave logins
        # to its own account to keep spraying guesses at other accounts.
        self._store.finish(
            keys,
            self._clock(),
            failed=False,
            reset=[k for k in keys if k.rule.scope in {"client_email", "email"}],
        )


_TRUE_VALUES = {"1", "true", "yes", "on"}


def trust_cloudflare_client_ip() -> bool:
    value = os.getenv("KALILLAC_TRUST_CF_CONNECTING_IP", "")
    return value.strip().lower() in _TRUE_VALUES


def client_identifier(
    peer_host: str | None,
    headers: dict[str, str] | None,
    trust_cloudflare: bool,
) -> str:
    """The address used for client-scoped limits.

    By default this is the connection peer as reported by Uvicorn. Behind
    Cloudflare and Nginx that peer is shared by many users, so set
    KALILLAC_TRUST_CF_CONNECTING_IP=true only where the origin accepts
    traffic exclusively from Cloudflare; otherwise the header is spoofable.
    """

    if trust_cloudflare and headers:
        candidate = (headers.get("cf-connecting-ip") or "").strip()

        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            pass

    return peer_host or "unknown"
