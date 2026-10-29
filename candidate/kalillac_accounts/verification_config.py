"""Default-off email-verification configuration; explicit code-supplied gateway required.

The gateway is supplied in code. The application builds one only through
email_provider.build_email_gateway, which requires an explicit, valid
KALILLAC_EMAIL_PROVIDER and its settings; anything invalid fails at startup.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from typing import Mapping

from .email_gateway import EmailVerificationGateway
from .recovery_config import _valid_url
from .router import normalize_email

_TRUE_VALUES = {"1", "true", "yes", "on"}
ENABLED = "KALILLAC_ACCOUNT_EMAIL_VERIFICATION_ENABLED"
URL_SETTING = "KALILLAC_ACCOUNT_EMAIL_VERIFICATION_URL"


class VerificationConfigError(RuntimeError):
    """Safe configuration error naming settings, never supplied values."""


@dataclass(frozen=True)
class VerificationConfig:
    verification_url: str = field(repr=False)
    email_from: str = field(repr=False)


def verification_enabled(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return env.get(ENABLED, "").strip().lower() in _TRUE_VALUES


def load_verification_config(
    gateway: EmailVerificationGateway | None,
    environ: Mapping[str, str] | None = None,
) -> VerificationConfig | None:
    env = os.environ if environ is None else environ
    if not verification_enabled(env):
        return None
    for name in ("KALILLAC_ACCOUNTS_ENABLED", "KALILLAC_DB_ENABLED"):
        if env.get(name, "").strip().lower() not in _TRUE_VALUES:
            raise VerificationConfigError(f"{ENABLED} requires {name}.")
    # HTTPS (HTTP only for loopback/localhost); no credentials, query or fragment.
    if not _valid_url(env.get(URL_SETTING, "")):
        raise VerificationConfigError(f"{URL_SETTING} is invalid.")
    sender = normalize_email(env.get("KALILLAC_EMAIL_FROM"))
    if sender is None:
        raise VerificationConfigError("KALILLAC_EMAIL_FROM is invalid.")
    if gateway is None or not callable(getattr(gateway, "send_email_verification", None)):
        raise VerificationConfigError(
            "Email verification requires an explicitly provided EmailVerificationGateway."
        )
    return VerificationConfig(env[URL_SETTING], sender)
