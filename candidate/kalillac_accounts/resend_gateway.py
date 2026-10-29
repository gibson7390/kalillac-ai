"""Resend transactional-email adapter for the account gateways.

Implements EmailGateway (send_password_reset, send_password_changed) and
EmailVerificationGateway (send_email_verification) with one bounded HTTPS
request per call to the fixed endpoint https://api.resend.com/emails. It is
constructed only by email_provider.build_email_gateway, and only while
recovery or email verification is enabled with KALILLAC_EMAIL_PROVIDER=resend.
Construction makes no network call.

Bounds: explicit connect/read/write/pool timeouts; the whole attempt runs in a
worker thread that the caller waits on for at most TOTAL_SECONDS (DNS lookups
are not covered by socket timeouts), with at most MAX_IN_FLIGHT attempts at
once; no retries; redirects are failures; at most MAX_RESPONSE_BYTES of an
uncompressed JSON response are read. An abandoned attempt that still
delivers is harmless: the caller has already rolled back, so the delivered
link is unusable.

Every ordinary failure becomes EmailDeliveryError with no detail. Nothing is
logged, and the API key, addresses, links, rendered messages, idempotency
inputs and the provider's response and message id are never stored or
exposed. KeyboardInterrupt, SystemExit and other BaseException values are
re-raised unchanged.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid

import httpx

from .email_gateway import EmailDeliveryError
from . import email_messages

RESEND_ENDPOINT = "https://api.resend.com/emails"
TIMEOUT = httpx.Timeout(connect=1.5, read=2.0, write=1.0, pool=0.5)
TOTAL_SECONDS = 5.0
MAX_IN_FLIGHT = 4
MAX_RESPONSE_BYTES = 4096
IDEMPOTENCY_VERSION = "kalillac-email-v1"
SENDER_NAME = "Kalillac AI"

# One plain address: no display name, comma, angle bracket, quote or space,
# so the provider can never read a value as additional recipients.
ADDRESS = re.compile(r"[A-Za-z0-9._%+-]{1,64}@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}")
_MESSAGE_ID = re.compile(r"[A-Za-z0-9-]{1,128}")


def _default_transport() -> httpx.BaseTransport:
    return httpx.HTTPTransport(
        verify=httpx.create_ssl_context(verify=True, trust_env=False),
        trust_env=False,
        retries=0,
    )


def _link_key(purpose: str, to: str, link: str) -> str:
    """Deterministic for one stable link-bearing message; reveals no input."""
    digest = hashlib.sha256("\0".join((IDEMPOTENCY_VERSION, purpose, to, link)).encode("utf-8")).hexdigest()
    return f"{IDEMPOTENCY_VERSION}/{purpose}/{digest}"


def _unique_key(purpose: str) -> str:
    return f"{IDEMPOTENCY_VERSION}/{purpose}/{uuid.uuid4().hex}"


class _Refused(Exception):
    """Internal: any response other than the documented success."""


class ResendGateway:
    def __init__(self, api_key: str, sender: str, *, transport_factory=_default_transport,
                 total_seconds: float = TOTAL_SECONDS, max_in_flight: int = MAX_IN_FLIGHT):
        self._api_key = api_key
        self._sender = f"{SENDER_NAME} <{sender}>"
        self._transport_factory = transport_factory
        self._total_seconds = total_seconds
        self._slots = threading.BoundedSemaphore(max_in_flight)

    def __repr__(self) -> str:
        return "ResendGateway()"

    # --- gateway operations -------------------------------------------------

    def send_password_reset(self, to: str, link: str) -> None:
        self._deliver(to, email_messages.password_reset(self._checked_link(link)),
                      _link_key("password-reset", to, link))

    def send_password_changed(self, to: str) -> None:
        # No credential or stable event id exists; each notification is distinct.
        self._deliver(to, email_messages.password_changed(), _unique_key("password-changed"))

    def send_email_verification(self, to: str, link: str) -> None:
        self._deliver(to, email_messages.email_verification(self._checked_link(link)),
                      _link_key("email-verification", to, link))

    # --- delivery -------------------------------------------------------------

    @staticmethod
    def _checked_link(link: str) -> str:
        if not isinstance(link, str) or not link.startswith(("https://", "http://")):
            raise EmailDeliveryError()
        return link

    def _deliver(self, to, message, idempotency_key) -> None:
        if not isinstance(to, str) or not ADDRESS.fullmatch(to):
            raise EmailDeliveryError()
        body = json.dumps({
            "from": self._sender, "to": [to], "subject": message.subject,
            "text": message.text, "html": message.html,
        }).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
        if not self._slots.acquire(blocking=False):
            raise EmailDeliveryError()
        outcome = {}
        done = threading.Event()

        def attempt():
            try:
                self._send(headers, body)
                outcome["ok"] = True
            except BaseException as error:  # handed to the caller, which re-raises non-ordinary ones
                outcome["error"] = error
            finally:
                self._slots.release()
                done.set()

        threading.Thread(target=attempt, name="kalillac-email-delivery", daemon=True).start()
        if not done.wait(self._total_seconds):
            raise EmailDeliveryError()
        error = outcome.get("error")
        if error is not None and not isinstance(error, Exception):
            raise error
        if not outcome.get("ok"):
            raise EmailDeliveryError() from None

    def _send(self, headers, body) -> None:
        deadline = time.monotonic() + self._total_seconds
        with httpx.Client(transport=self._transport_factory(), timeout=TIMEOUT,
                          follow_redirects=False, trust_env=False) as client:
            request = client.build_request("POST", RESEND_ENDPOINT, headers=headers, content=body)
            response = client.send(request, stream=True)
            try:
                if response.status_code != 200:
                    raise _Refused()
                media = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                encoding = response.headers.get("content-encoding", "identity").strip().lower()
                declared = response.headers.get("content-length")
                if media != "application/json" or encoding not in ("", "identity"):
                    raise _Refused()
                if declared is not None and (not declared.isdigit() or int(declared) > MAX_RESPONSE_BYTES):
                    raise _Refused()
                received = bytearray()
                # Identity encoding was required above, so these are the raw bytes.
                for chunk in response.iter_bytes():
                    received.extend(chunk)
                    if len(received) > MAX_RESPONSE_BYTES or time.monotonic() > deadline:
                        raise _Refused()
            finally:
                response.close()
        # The id is validated and discarded; nothing from the response is kept.
        try:
            parsed = json.loads(bytes(received))
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise _Refused() from None
        if (not isinstance(parsed, dict) or set(parsed) != {"id"}
                or not isinstance(parsed["id"], str) or not _MESSAGE_ID.fullmatch(parsed["id"])):
            raise _Refused()
