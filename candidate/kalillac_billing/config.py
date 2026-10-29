"""Billing configuration.

Secrets come from systemd credentials first (CREDENTIALS_DIRECTORY with
files stripe_secret_key and stripe_webhook_secret). Environment variables
KALILLAC_STRIPE_SECRET_KEY / KALILLAC_STRIPE_WEBHOOK_SECRET are a
development and test fallback only. Secret values never appear in repr,
logs, or error messages.

The paid recurring Price and every redirect URL are server configuration;
no client request can supply them.

KALILLAC_STRIPE_MODE (test or live) is required and must match the secret
key's own mode, so a test deployment cannot run with a live key or the
reverse; webhooks are then held to the same mode (service.process_webhook).

KALILLAC_STRIPE_PRICE_ID is the one Price new Checkouts use.
KALILLAC_STRIPE_ALLOWED_PRICE_IDS is the explicit comma-separated set of
Prices (for example monthly and annual) that all grant the same paid
entitlement; it must include the Checkout Price, so rotating the Checkout
Price never downgrades subscribers on a Price that is still allowed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from urllib.parse import urlparse


_TRUE_VALUES = {"1", "true", "yes", "on"}

SECRET_KEY_CREDENTIAL = "stripe_secret_key"
WEBHOOK_SECRET_CREDENTIAL = "stripe_webhook_secret"

MODE_TEST = "test"
MODE_LIVE = "live"
STRIPE_MODES = (MODE_TEST, MODE_LIVE)

_PRICE_ID_PATTERN = r"price_[A-Za-z0-9]+"


class BillingConfigError(RuntimeError):
    """Billing is enabled but its configuration is missing or invalid."""


@dataclass(frozen=True)
class BillingConfig:
    secret_key: str = field(repr=False)
    webhook_secret: str = field(repr=False)
    price_id: str
    success_url: str
    cancel_url: str
    portal_return_url: str
    mode: str
    allowed_price_ids: frozenset[str]

    @property
    def livemode(self) -> bool:
        """The Stripe Event.livemode value every webhook must carry."""

        return self.mode == MODE_LIVE


def billing_enabled() -> bool:
    value = os.getenv("KALILLAC_BILLING_ENABLED", "")
    return value.strip().lower() in _TRUE_VALUES


def _read_secret(credential: str, env_fallback: str) -> str | None:
    directory = os.getenv("CREDENTIALS_DIRECTORY")

    if directory:
        try:
            value = (Path(directory) / credential).read_text(encoding="utf-8")
        except FileNotFoundError:
            value = ""

        if value.strip():
            return value.strip()

    # Development/test fallback only.
    value = os.getenv(env_fallback, "").strip()
    return value or None


def _require(name: str, value: str | None, pattern: str) -> str:
    if not value:
        raise BillingConfigError(f"Billing is enabled but {name} is not set.")

    if not re.fullmatch(pattern, value):
        # The message names the setting, never the value.
        raise BillingConfigError(f"{name} has an invalid format.")

    return value


def _require_url(name: str) -> str:
    value = os.getenv(name, "").strip()

    if not value:
        raise BillingConfigError(f"Billing is enabled but {name} is not set.")

    parsed = urlparse(value)
    local = parsed.hostname in {"localhost", "127.0.0.1"}

    if not parsed.netloc or not (
        parsed.scheme == "https" or (parsed.scheme == "http" and local)
    ):
        raise BillingConfigError(
            f"{name} must be an https URL (http only for localhost)."
        )

    return value


def _require_mode() -> str:
    value = os.getenv("KALILLAC_STRIPE_MODE", "").strip()

    if not value:
        raise BillingConfigError(
            "Billing is enabled but KALILLAC_STRIPE_MODE is not set."
        )

    if value not in STRIPE_MODES:
        raise BillingConfigError("KALILLAC_STRIPE_MODE must be test or live.")

    return value


def _require_allowed_prices(checkout_price_id: str) -> frozenset[str]:
    name = "KALILLAC_STRIPE_ALLOWED_PRICE_IDS"
    value = os.getenv(name, "").strip()

    if not value:
        raise BillingConfigError(f"Billing is enabled but {name} is not set.")

    # Messages name the setting, never a value.
    prices = [item.strip() for item in value.split(",")]

    if not all(re.fullmatch(_PRICE_ID_PATTERN, price) for price in prices):
        raise BillingConfigError(f"{name} has an invalid format.")

    if len(set(prices)) != len(prices):
        raise BillingConfigError(f"{name} contains a duplicate Price.")

    if checkout_price_id not in prices:
        raise BillingConfigError(
            f"KALILLAC_STRIPE_PRICE_ID must be one of {name}."
        )

    return frozenset(prices)


def load_billing_config() -> BillingConfig:
    """Read and validate configuration. Performs no network access."""

    mode = _require_mode()
    price_id = _require(
        "KALILLAC_STRIPE_PRICE_ID",
        os.getenv("KALILLAC_STRIPE_PRICE_ID", "").strip() or None,
        _PRICE_ID_PATTERN,
    )

    secret_key = _require(
        "stripe_secret_key",
        _read_secret(SECRET_KEY_CREDENTIAL, "KALILLAC_STRIPE_SECRET_KEY"),
        r"(sk|rk)_(test|live)_[A-Za-z0-9]+",
    )

    # Only a key of the configured mode is accepted. The message names the
    # mismatch, never the key.
    if not secret_key.startswith((f"sk_{mode}_", f"rk_{mode}_")):
        raise BillingConfigError(
            "stripe_secret_key does not match KALILLAC_STRIPE_MODE."
        )

    return BillingConfig(
        secret_key=secret_key,
        webhook_secret=_require(
            "stripe_webhook_secret",
            _read_secret(
                WEBHOOK_SECRET_CREDENTIAL,
                "KALILLAC_STRIPE_WEBHOOK_SECRET",
            ),
            r"whsec_[A-Za-z0-9]+",
        ),
        price_id=price_id,
        success_url=_require_url("KALILLAC_BILLING_SUCCESS_URL"),
        cancel_url=_require_url("KALILLAC_BILLING_CANCEL_URL"),
        portal_return_url=_require_url("KALILLAC_BILLING_PORTAL_RETURN_URL"),
        mode=mode,
        allowed_price_ids=_require_allowed_prices(price_id),
    )
