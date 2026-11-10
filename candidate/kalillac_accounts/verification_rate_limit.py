"""Email-verification sliding-window limits, independent of login, recovery and
anonymous-usage limits (each limiter owns its own store).

Only SHA-256 digests of identifiers are kept. The store is in-memory and
single-process: restarting a worker loses its state, and it is not
distributed enforcement. Replace it with a shared atomic store before
scaling beyond one worker.
"""
from dataclasses import dataclass
import hashlib
import time

from .rate_limit import InMemoryFailureStore, LimitKey, LimitRule

REQUEST_CLIENT = LimitRule("verification_request_client", 5, 15 * 60)
REQUEST_ACCOUNT = LimitRule("verification_request_account", 3, 60 * 60)
CONFIRM_CLIENT = LimitRule("verification_confirm_client", 10, 15 * 60)


@dataclass(frozen=True)
class RequestDecision:
    allowed: bool
    send: bool = False
    retry_after_seconds: int = 0


class VerificationRateLimiter:
    def __init__(self, clock=time.monotonic, store=None):
        self._clock = clock
        self._store = store if store is not None else InMemoryFailureStore()

    @staticmethod
    def _key(rule, value):
        digest = hashlib.sha256((rule.scope + "\0" + value).encode("utf-8")).hexdigest()
        return (LimitKey(rule, digest),)

    def _attempt(self, keys):
        # Every request attempt counts, successful or not.
        now = self._clock()
        decision = self._store.begin(keys, now)
        if decision.allowed:
            self._store.finish(keys, now, failed=True)
        return decision

    def request_client(self, client):
        """Public client budget, checked before authentication."""
        decision = self._attempt(self._key(REQUEST_CLIENT, client))
        return RequestDecision(decision.allowed, retry_after_seconds=decision.retry_after_seconds)

    def request_account(self, user_id, normalized_email):
        """True when this account/address may be sent another message; excess is silent."""
        return self._attempt(self._key(REQUEST_ACCOUNT, f"{user_id}\0{normalized_email}")).allowed

    def begin_confirm(self, client):
        keys = self._key(CONFIRM_CLIENT, client)
        return self._store.begin(keys, self._clock()), keys

    def finish_confirm(self, keys, *, invalid):
        self._store.finish(keys, self._clock(), failed=invalid)
