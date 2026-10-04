"""Billing configuration.

Secrets come from systemd credentials first (CREDENTIALS_DIRECTORY with
files stripe_secret_key and stripe_webhook_secret). Environment variables
KALILLAC_STRIPE_SECRET_KEY / KALILLAC_STRIPE_WEBHOOK_SECRET are a
development and test fallback only. Secret values never appear in repr,
logs, or error messages.

The paid recurring Price and every redirect URL are server configuration;
no client request can supply them.
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


def load_billing_config() -> BillingConfig:
    """Read and validate configuration. Performs no network access."""

    return BillingConfig(
        secret_key=_require(
            "stripe_secret_key",
            _read_secret(SECRET_KEY_CREDENTIAL, "KALILLAC_STRIPE_SECRET_KEY"),
            r"(sk|rk)_(test|live)_[A-Za-z0-9]+",
        ),
        webhook_secret=_require(
            "stripe_webhook_secret",
            _read_secret(
                WEBHOOK_SECRET_CREDENTIAL,
                "KALILLAC_STRIPE_WEBHOOK_SECRET",
            ),
            r"whsec_[A-Za-z0-9]+",
        ),
        price_id=_require(
            "KALILLAC_STRIPE_PRICE_ID",
            os.getenv("KALILLAC_STRIPE_PRICE_ID", "").strip() or None,
            r"price_[A-Za-z0-9]+",
        ),
        success_url=_require_url("KALILLAC_BILLING_SUCCESS_URL"),
        cancel_url=_require_url("KALILLAC_BILLING_CANCEL_URL"),
        portal_return_url=_require_url("KALILLAC_BILLING_PORTAL_RETURN_URL"),
    )
