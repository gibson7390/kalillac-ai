"""Default-off recovery configuration; explicit code-supplied gateway required."""
from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import os
from typing import Mapping
from urllib.parse import urlsplit

from .email_gateway import EmailGateway
from .router import normalize_email

_TRUE_VALUES = {"1", "true", "yes", "on"}
ENABLED = "KALILLAC_ACCOUNT_RECOVERY_ENABLED"


class RecoveryConfigError(RuntimeError):
    """Safe configuration error naming settings, never supplied values."""


@dataclass(frozen=True)
class RecoveryConfig:
    recovery_url: str = field(repr=False)
    email_from: str = field(repr=False)


def recovery_enabled(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return env.get(ENABLED, "").strip().lower() in _TRUE_VALUES


def _valid_url(value: str) -> bool:
    try:
        if not value or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
            return False
        if "?" in value or "#" in value:
            return False
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port  # Validate syntax and range even though it is otherwise optional.
        if not parsed.netloc or not host or parsed.username is not None or parsed.password is not None:
            return False
        if "\\" in value or (port is not None and port == 0):
            return False
        local = host == "localhost"
        try:
            local = local or ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
        return parsed.scheme == "https" or (parsed.scheme == "http" and local)
    except ValueError:
        return False


def load_recovery_config(
    gateway: EmailGateway | None,
    environ: Mapping[str, str] | None = None,
) -> RecoveryConfig | None:
    env = os.environ if environ is None else environ
    if not recovery_enabled(env):
        return None
    for name in ("KALILLAC_ACCOUNTS_ENABLED", "KALILLAC_DB_ENABLED"):
        if env.get(name, "").strip().lower() not in _TRUE_VALUES:
            raise RecoveryConfigError(f"{ENABLED} requires {name}.")
    url = env.get("KALILLAC_ACCOUNT_RECOVERY_URL", "")
    if not _valid_url(url):
        raise RecoveryConfigError("KALILLAC_ACCOUNT_RECOVERY_URL is invalid.")
    sender = normalize_email(env.get("KALILLAC_EMAIL_FROM"))
    if sender is None:
        raise RecoveryConfigError("KALILLAC_EMAIL_FROM is invalid.")
    if gateway is None or not all(
        callable(getattr(gateway, name, None))
        for name in ("send_password_reset", "send_password_changed")
    ):
        raise RecoveryConfigError("Recovery requires an explicitly provided EmailGateway.")
    return RecoveryConfig(url, sender)
