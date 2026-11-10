"""Recovery-specific, hashed sliding-window limits.

This store is in-memory and single-process. Replace it with a shared atomic store
before scaling beyond one worker; restarting a worker loses its limit state.
"""
from dataclasses import dataclass
import hashlib
import time

from .rate_limit import AttemptDecision, InMemoryFailureStore, LimitKey, LimitRule


@dataclass(frozen=True)
class RequestDecision:
    allowed: bool
    send: bool = False
    retry_after_seconds: int = 0


class RecoveryRateLimiter:
    def __init__(self, clock=time.monotonic, store=None):
        self._clock = clock
        self._store = store if store is not None else InMemoryFailureStore()

    @staticmethod
    def _key(scope, value, maximum, window):
        digest = hashlib.sha256((scope + "\0" + value).encode("utf-8")).hexdigest()
        return (LimitKey(LimitRule(scope, maximum, window), digest),)

    def _attempt(self, keys):
        now = self._clock()
        decision = self._store.begin(keys, now)
        if decision.allowed:
            self._store.finish(keys, now, failed=True)
        return decision

    def request(self, client, normalized_email):
        public = self._attempt(self._key("recovery_request_client", client, 5, 900))
        if not public.allowed:
            return RequestDecision(False, retry_after_seconds=public.retry_after_seconds)
        email = self._attempt(self._key("recovery_request_email", normalized_email, 3, 3600))
        return RequestDecision(True, send=email.allowed)

    def begin_confirm(self, client):
        keys = self._key("recovery_confirm_client", client, 10, 900)
        return self._store.begin(keys, self._clock()), keys

    def finish_confirm(self, keys, *, invalid):
        self._store.finish(keys, self._clock(), failed=invalid)
