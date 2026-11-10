"""Bounded registration work budget: five attempts/client per rolling 15 minutes.

Only SHA-256 client digests are retained. Live charges and reservations are
never evicted; capacity exhaustion refuses admission without allocating state.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field
import hashlib
import math
import threading
import time

from .rate_limit import AttemptDecision, LimitKey, LimitRule

REGISTRATION_RULE = LimitRule("registration_client", 5, 15 * 60)


@dataclass
class _Bucket:
    failures: deque = field(default_factory=deque)
    in_flight: int = 0


class RegistrationStore:
    """Registration-only storage; every dictionary has at most max_keys entries."""

    def __init__(self, max_keys=100_000):
        if not isinstance(max_keys, int) or max_keys < 1:
            raise ValueError("REGISTRATION_CAPACITY_INVALID")
        self._max_keys = max_keys
        self._lock = threading.Lock()
        self._buckets = {}
        # One expiry per charged key, ordered by its last charge. No stale heap
        # entries accumulate. A key is globally reclaimable after its LAST charge.
        self._expiry = OrderedDict()
        self._empty_keys = 0
        self._now = float("-inf")

    def _advance(self, now):
        self._now = max(self._now, now)
        while self._expiry:
            key, deadline = next(iter(self._expiry.items()))
            if deadline > self._now:
                break
            self._expiry.pop(key)
            bucket = self._buckets[key]
            bucket.failures.clear()
            if bucket.in_flight:
                self._empty_keys += 1
            else:
                del self._buckets[key]
        return self._now

    @staticmethod
    def _key(keys):
        if len(keys) != 1 or keys[0].rule != REGISTRATION_RULE:
            raise ValueError("REGISTRATION_RULE_INVALID")
        return keys[0].key

    def begin(self, keys, now):
        key = self._key(keys)
        with self._lock:
            now = self._advance(now)
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) == self._max_keys:
                    # An empty reserved bucket may release immediately. Charged
                    # buckets cannot free capacity until their final expiry.
                    retry = 1 if self._empty_keys else max(
                        1, math.ceil(next(iter(self._expiry.values())) - now))
                    return AttemptDecision(False, retry)
                bucket = self._buckets[key] = _Bucket()
                self._empty_keys += 1
            while bucket.failures and bucket.failures[0] <= now - REGISTRATION_RULE.window_seconds:
                bucket.failures.popleft()
            if len(bucket.failures) + bucket.in_flight >= REGISTRATION_RULE.max_failures:
                retry = (math.ceil(bucket.failures[0] + REGISTRATION_RULE.window_seconds - now)
                         if bucket.failures else 1)
                return AttemptDecision(False, max(1, retry))
            bucket.in_flight += 1
            return AttemptDecision(True)

    def finish(self, keys, now, *, failed):
        key = self._key(keys)
        with self._lock:
            now = self._advance(now)
            bucket = self._buckets.get(key)
            if bucket is None or bucket.in_flight < 1:
                raise RuntimeError("REGISTRATION_RESERVATION_MISSING")
            bucket.in_flight -= 1
            if failed:
                if not bucket.failures:
                    self._empty_keys -= 1
                bucket.failures.append(now)
                self._expiry[key] = now + REGISTRATION_RULE.window_seconds
                self._expiry.move_to_end(key)
            elif not bucket.in_flight and not bucket.failures:
                del self._buckets[key]
                self._empty_keys -= 1


class RegistrationAttempt:
    def __init__(self, store, keys, clock):
        self._store, self._keys, self._clock = store, keys, clock
        self._lock = threading.Lock()
        self._finished = False

    def charge(self):
        with self._lock:
            if self._finished:
                raise RuntimeError("REGISTRATION_ATTEMPT_FINISHED")
            self._store.finish(self._keys, self._clock(), failed=True)
            self._finished = True

    def release(self):
        with self._lock:
            if not self._finished:
                self._store.finish(self._keys, self._clock(), failed=False)
                self._finished = True


class RegistrationRateLimiter:
    def __init__(self, *, clock=time.monotonic, store=None, max_keys=100_000):
        self._clock = clock
        self._store = store if store is not None else RegistrationStore(max_keys)

    def begin(self, client):
        digest = hashlib.sha256(client.encode("utf-8")).hexdigest()
        keys = (LimitKey(REGISTRATION_RULE, digest),)
        decision = self._store.begin(keys, self._clock())
        attempt = RegistrationAttempt(self._store, keys, self._clock) if decision.allowed else None
        return decision, attempt
