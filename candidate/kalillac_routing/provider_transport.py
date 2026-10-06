"""Provider-neutral adapter between a request budget and BoundedTransport.

This module holds transport mechanics only. It knows no provider, model,
endpoint, prompt or fallback order: callers pass the URL, headers, payload
and limits, and decide what a provider failure means for them.

TransportHolder is the process-local owner of one BoundedTransport:
- created lazily, under a lock, on first use, from validated settings;
- never created by shutdown or by reading it (existing());
- later settings that differ are refused as a configuration defect
  (ValueError), never silently applied;
- quarantined permanently after any TransportCleanupUnconfirmed, so later
  calls fail locally without touching the network;
- closed permanently by close(), which closes only an existing transport,
  bounded by the configured close timeout.

post_json_within_budget() makes exactly one transport request (it never
retries and never admits budget attempts; the caller admits once per
request) and decides the outcome of a transport failure, in this order:

1. the request was cancelled            -> RequestCancelled
2. the request deadline has passed, or the deadline-type failure came from
   a request-selected timeout          -> RequestDeadlineExceeded
3. local transport state (overloaded, quarantined, closed, unconfirmed
   cleanup, an unexpected transport fault, a cancellation the request did
   not ask for)                         -> TransportUnavailable
4. a remote failure (connection, HTTP status, invalid or oversized body,
   unsupported encoding, or a timeout the shorter per-call cap selected)
                                        -> the original transport error

Rows 1 and 2 use the budget's own state and clock (ensure_open), so an
unconfirmed cleanup never replaces a cancellation or deadline that has
already happened; the holder is still quarantined first. TypeError and
ValueError (invalid arguments or settings) propagate unchanged: they are
programming or configuration defects, not transport outcomes.

Nothing here logs, and no exception raised here carries request content.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Mapping

from kalillac_routing import bounded_transport
from kalillac_routing.bounded_transport import (
    InvalidJSONResponse,
    ResponseTooLarge,
    TransportCleanupUnconfirmed,
    TransportConnectionError,
    TransportDeadlineExceeded,
    TransportError,
    TransportHTTPError,
    UnsupportedContentEncoding,
)
from kalillac_routing.request_budget import (
    RequestBudgetError,
    RequestDeadlineExceeded,
)
from kalillac_routing.request_limits import TransportLimits


class TransportUnavailable(RuntimeError):
    """The local transport cannot serve this request (capacity, quarantine,
    closure, unconfirmed cleanup, or an internal transport fault). Not a
    remote provider failure. The message is fixed; reason is a fixed label
    (a transport error class name, "quarantined" or "closed")."""

    MESSAGE = "Provider transport unavailable."

    def __init__(self, reason: str) -> None:
        super().__init__(self.MESSAGE)
        self.reason = reason


# Failures the remote side caused. Everything else is local state.
_REMOTE_FAILURES = (
    TransportConnectionError,
    TransportHTTPError,
    InvalidJSONResponse,
    UnsupportedContentEncoding,
    ResponseTooLarge,
)


class TransportHolder:
    def __init__(self, factory: Callable[..., Any] | None = None) -> None:
        # None means BoundedTransport, looked up when first needed.
        self._factory = factory
        self._lock = threading.Lock()
        self._transport = None
        self._settings: TransportLimits | None = None
        self._quarantined = False
        self._closed = False

    @property
    def quarantined(self) -> bool:
        with self._lock:
            return self._quarantined

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def existing(self):
        """The transport if one was created; never creates one."""

        with self._lock:
            return self._transport

    def get_or_create(self, settings: TransportLimits):
        if not isinstance(settings, TransportLimits):
            raise TypeError("settings must be TransportLimits.")

        with self._lock:
            if self._closed:
                raise TransportUnavailable("closed")

            if self._quarantined:
                raise TransportUnavailable("quarantined")

            if self._transport is None:
                factory = self._factory or bounded_transport.BoundedTransport
                # Construction performs no I/O and starts no threads.
                self._transport = factory(
                    max_outstanding=settings.max_outstanding,
                    dns_threads=settings.dns_threads,
                    max_pending_dns=settings.max_pending_dns,
                    cancel_poll_interval=settings.cancel_poll_interval_seconds,
                    backstop_grace=settings.backstop_grace_seconds,
                    cleanup_grace=settings.cleanup_grace_seconds,
                )
                self._settings = settings
            elif settings != self._settings:
                # A defect: one process, one transport configuration.
                raise ValueError("transport settings differ from the active transport.")

            return self._transport

    def quarantine(self) -> None:
        """Refuse every later request. Permanent for this holder."""

        with self._lock:
            self._quarantined = True

    def close(self):
        """Refuse later requests and close an existing transport, waiting at
        most the configured close timeout. Returns the transport's shutdown
        report, or None when no transport was ever created. Safe to repeat."""

        with self._lock:
            self._closed = True
            transport = self._transport
            settings = self._settings

        if transport is None:
            return None

        return transport.close(settings.close_timeout_seconds)


def _outcome(
    error: TransportError,
    holder: TransportHolder,
    budget,
    request_deadline_selected: bool,
) -> BaseException:
    unconfirmed = isinstance(error, TransportCleanupUnconfirmed)

    if unconfirmed:
        # Before anything else: whatever the request outcome, this
        # transport's resource state is no longer trusted.
        holder.quarantine()

    # 1-2: a cancellation or an expired deadline, by the budget's own state
    # and clock, has already decided the request.
    try:
        budget.ensure_open()
    except RequestBudgetError as stop:
        return stop

    deadline_type = isinstance(error, TransportDeadlineExceeded) or (
        unconfirmed and error.reason == "deadline"
    )

    if deadline_type and request_deadline_selected:
        return RequestDeadlineExceeded()

    # 4: remote failures, including a deadline the shorter per-call cap set
    # whose cleanup was confirmed.
    if isinstance(error, _REMOTE_FAILURES) or isinstance(error, TransportDeadlineExceeded):
        return error

    # 3: everything else is local transport state; fail closed.
    return TransportUnavailable(type(error).__name__)


def post_json_within_budget(
    holder: TransportHolder,
    settings: TransportLimits,
    url: str,
    payload: Any,
    *,
    headers: Mapping[str, str],
    budget,
    timeout: float,
    request_deadline_selected: bool,
    max_bytes: int,
) -> Any:
    """POST payload as JSON once, within the request budget; return the
    decoded JSON response. See the module docstring for failure mapping."""

    transport = holder.get_or_create(settings)

    try:
        return transport.post_json(
            url,
            payload,
            headers=headers,
            timeout=timeout,
            max_bytes=max_bytes,
            cancelled=lambda: budget.cancelled,
        )
    except TransportError as error:
        outcome = _outcome(error, holder, budget, request_deadline_selected)

        if outcome is error:
            raise

        raise outcome from None
