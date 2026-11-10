"""Explicit email-provider selection for account recovery and email verification.

Called by the application only while recovery or email verification is
enabled; disabled paths never import it, so missing provider settings cannot
affect startup. Validation fails closed before any route is installed and
names settings, never their values. No provider is contacted here: building
the gateway makes no network call.

Settings:
- KALILLAC_EMAIL_PROVIDER: must be exactly "resend" (the only adapter);
  anything else, including blank, is refused.
- KALILLAC_RESEND_API_KEY: "re_" followed by a nonempty RFC 6750 b64token
  (letters, digits and - . _ ~ + /, then optional trailing "="), at most
  256 characters in total. This is transport safety for an HTTP Bearer
  credential, not Resend's key format or entropy: whitespace, CR/LF,
  control characters, quotes, commas and other header-unsafe characters are
  refused. For any future activation it must be a sending-only key
  restricted to the sending domain.
- KALILLAC_EMAIL_FROM: the existing account-email rules, plus one plain
  address with no display name or separators.

The Resend endpoint is fixed in code (resend_gateway.RESEND_ENDPOINT) and is
not configurable.
"""
from __future__ import annotations

import os
import re
from typing import Mapping

from .router import normalize_email

PROVIDER_SETTING = "KALILLAC_EMAIL_PROVIDER"
RESEND_KEY_SETTING = "KALILLAC_RESEND_API_KEY"
SENDER_SETTING = "KALILLAC_EMAIL_FROM"
PROVIDERS = ("resend",)
RESEND_KEY_MAX_LENGTH = 256
# "re_" plus an RFC 6750 b64token: 1*( ALPHA / DIGIT / "-" / "." / "_" / "~" / "+" / "/" ) *"="
_RESEND_KEY = re.compile(r"re_[A-Za-z0-9\-._~+/]+=*")


class EmailProviderConfigError(RuntimeError):
    """Safe configuration error naming settings, never supplied values."""


def build_email_gateway(environ: Mapping[str, str] | None = None):
    """The configured gateway, implementing both EmailGateway and
    EmailVerificationGateway. Raises EmailProviderConfigError when invalid."""

    env = os.environ if environ is None else environ
    provider = env.get(PROVIDER_SETTING, "")
    if provider not in PROVIDERS:
        raise EmailProviderConfigError(f"{PROVIDER_SETTING} must be one of: {', '.join(PROVIDERS)}.")
    key = env.get(RESEND_KEY_SETTING, "")
    if (not isinstance(key, str) or len(key) > RESEND_KEY_MAX_LENGTH
            or not _RESEND_KEY.fullmatch(key)):
        raise EmailProviderConfigError(f"{RESEND_KEY_SETTING} is missing or invalid.")
    from .resend_gateway import ADDRESS, ResendGateway
    sender = normalize_email(env.get(SENDER_SETTING))
    if sender is None or not ADDRESS.fullmatch(sender):
        raise EmailProviderConfigError(f"{SENDER_SETTING} is invalid.")
    return ResendGateway(key, sender)
