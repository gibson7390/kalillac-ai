"""Tavily adaptation for the request-budgeted bounded transport.

This module adapts Tavily search and extraction requests to the
provider-neutral bounded transport. It holds no search-routing policy:
callers decide when to search, admit each attempt, select its timeout, and
decide what a failure means for the user.

- Endpoints: one POST per operation to SEARCH_URL or EXTRACT_URL.
- Payloads reproduce what tavily-python 0.7.26 sends for the arguments
  Kalillac uses: None values are dropped, the search timeout is a client
  setting only, and the extraction timeout is also sent in the body.
- Tavily accepts an extraction body timeout only between 1 and 120 seconds,
  so the body value is clamped to that range; the local HTTP deadline is
  never extended by the clamp.
- Responses must be a JSON object whose "results" (absent means none, as
  in the SDK) is a list of objects; anything else is TavilyResponseInvalid,
  an ordinary remote failure.
- The holder is search-owned: shared with Brave, separate from OpenAI's holder,
  with its own quarantine and closed state. It is created on the first
  bounded operation, never by shutdown, and close_transport() closes only a
  holder that exists.

post_json() makes exactly one transport request through
post_json_within_budget(), so the stop precedence is the shared one:
cancellation, then the request deadline, then local transport state
(TransportUnavailable), then the remote error itself. TypeError and
ValueError are argument or configuration defects. Nothing here logs, and no
exception raised here carries request or response content.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Mapping

from kalillac_routing.provider_transport import (
    TransportHolder,
    TransportUnavailable,
    post_json_within_budget,
)


SEARCH_URL = "https://api.tavily.com/search"
EXTRACT_URL = "https://api.tavily.com/extract"

# Tavily's accepted range for the extraction body's timeout field.
EXTRACT_PAYLOAD_TIMEOUT_MIN_SECONDS = 1.0
EXTRACT_PAYLOAD_TIMEOUT_MAX_SECONDS = 120.0

# Search arguments Kalillac passes, in the SDK's payload order. "timeout" is
# accepted but is a client setting, never part of the search body.
_SEARCH_FIELDS = (
    "query",
    "search_depth",
    "topic",
    "time_range",
    "max_results",
    "include_domains",
    "chunks_per_source",
)
_SEARCH_CLIENT_ONLY = ("timeout",)

# Extraction arguments Kalillac passes, in the SDK's payload order; the body
# timeout is inserted after "format", where the SDK puts it.
_EXTRACT_FIELDS = (
    "urls",
    "extract_depth",
    "format",
    "query",
    "chunks_per_source",
)


class TavilyResponseInvalid(Exception):
    """A 2xx Tavily response that is not the expected shape: not a JSON
    object, or "results" that is not a list of objects. A remote failure.
    The message is fixed."""

    MESSAGE = "Search provider returned an invalid response."

    def __init__(self) -> None:
        super().__init__(self.MESSAGE)


def search_payload(search_args: Mapping[str, Any]) -> dict:
    """The /search JSON body for the SDK-style search arguments."""

    if not isinstance(search_args, Mapping):
        raise TypeError("search arguments must be a mapping.")

    if set(search_args) - set(_SEARCH_FIELDS) - set(_SEARCH_CLIENT_ONLY):
        raise ValueError("unsupported search argument.")

    return {
        field: search_args[field]
        for field in _SEARCH_FIELDS
        if search_args.get(field) is not None
    }


def extract_payload_timeout(selected_timeout: float) -> float:
    """The body timeout for an extraction whose local HTTP deadline is
    selected_timeout: clamped to Tavily's accepted range. The caller keeps
    using selected_timeout itself for the HTTP request."""

    if isinstance(selected_timeout, bool) or not isinstance(selected_timeout, (int, float)):
        raise TypeError("timeout must be a number of seconds.")

    if not selected_timeout > 0:
        raise ValueError("timeout must be positive.")

    return max(
        EXTRACT_PAYLOAD_TIMEOUT_MIN_SECONDS,
        min(EXTRACT_PAYLOAD_TIMEOUT_MAX_SECONDS, float(selected_timeout)),
    )


def extract_payload(extract_args: Mapping[str, Any], selected_timeout: float) -> dict:
    """The /extract JSON body for the SDK-style extraction arguments; the
    body timeout is derived from the already-selected HTTP timeout."""

    if not isinstance(extract_args, Mapping):
        raise TypeError("extract arguments must be a mapping.")

    if set(extract_args) - set(_EXTRACT_FIELDS):
        raise ValueError("unsupported extract argument.")

    body_timeout = extract_payload_timeout(selected_timeout)
    payload = {}

    for field in _EXTRACT_FIELDS:
        value = extract_args.get(field)

        if value is not None:
            payload[field] = value

        if field == "format":
            payload["timeout"] = body_timeout

    return payload


def validate_response(response: Any) -> dict:
    """The response as a dict whose "results" is a list of objects (an
    absent "results" becomes [], as tavily-python does); otherwise
    TavilyResponseInvalid."""

    if not isinstance(response, dict):
        raise TavilyResponseInvalid()

    results = response.get("results", [])

    if not isinstance(results, list) or not all(
        isinstance(item, dict) for item in results
    ):
        raise TavilyResponseInvalid()

    return {**response, "results": results}


class TavilyTransportSlot:
    """Owns Tavily's TransportHolder: created lazily on the first bounded
    operation, never by close(); after close() no holder is created and an
    existing one refuses later requests."""

    def __init__(self, factory: Callable[..., Any] | None = None) -> None:
        # Passed to TransportHolder; None means BoundedTransport.
        self._factory = factory
        self._lock = threading.Lock()
        self._holder: TransportHolder | None = None
        self._closed = False

    def holder(self) -> TransportHolder:
        with self._lock:
            if self._closed:
                raise TransportUnavailable("closed")

            if self._holder is None:
                # Construction performs no I/O and starts nothing.
                self._holder = TransportHolder(factory=self._factory)

            return self._holder

    def existing(self) -> TransportHolder | None:
        """The holder if one was created; never creates one."""

        with self._lock:
            return self._holder

    def close(self):
        """Refuse later requests and close an existing holder (bounded by
        its configured close timeout). Returns the shutdown report, or None
        when no transport was ever created. Safe to repeat."""

        with self._lock:
            self._closed = True
            holder = self._holder

        if holder is None:
            return None

        return holder.close()


_SLOT = TavilyTransportSlot()


def search_holder() -> TransportHolder:
    """Shared bounded search capacity; creation remains lazy."""
    return _SLOT.holder()


def existing_holder() -> TransportHolder | None:
    return _SLOT.existing()


def quarantined() -> bool:
    holder = _SLOT.existing()
    return holder is not None and holder.quarantined


def close_transport():
    """Shutdown: close only an existing Tavily transport. Idempotent."""

    return _SLOT.close()


def post_json(
    url: str,
    payload: Mapping[str, Any],
    *,
    api_key: str,
    settings,
    budget,
    timeout: float,
    request_deadline_selected: bool,
    max_bytes: int,
) -> dict:
    """POST one Tavily request through Tavily's own bounded transport and
    return the validated response. The caller has already admitted exactly
    one search attempt and selected timeout and its provenance once."""

    if url not in (SEARCH_URL, EXTRACT_URL):
        raise ValueError("unsupported search endpoint.")

    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("search credential is not configured.")

    response = post_json_within_budget(
        _SLOT.holder(),
        settings,
        url,
        payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        budget=budget,
        timeout=timeout,
        request_deadline_selected=request_deadline_selected,
        max_bytes=max_bytes,
    )
    return validate_response(response)
