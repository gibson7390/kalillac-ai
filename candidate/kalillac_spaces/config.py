"""Spaces configuration: the global flag, the beta allowlist, the site origin
and internal safety ceilings.

Fail closed:
- KALILLAC_SPACES_ENABLED off: nothing Spaces-related is installed.
- An empty KALILLAC_SPACES_BETA_ACCOUNT_IDS grants access to nobody.
- Any malformed setting raises SpacesConfigError at startup; it never
  silently broadens access. Error messages name the setting, never a value
  (an allowlist entry might be someone's email typed by mistake).

The ceilings are implementation safeguards, not plan promises.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import re
from typing import Mapping
from urllib.parse import urlsplit
import uuid


_TRUE_VALUES = {"1", "true", "yes", "on"}

ENABLED_ENV = "KALILLAC_SPACES_ENABLED"
BETA_ACCOUNTS_ENV = "KALILLAC_SPACES_BETA_ACCOUNT_IDS"
SITE_ORIGIN_ENV = "KALILLAC_SITE_ORIGIN"

# Safety ceilings (architecture §3; owner decision 7).
MAX_SPACES_PER_ACCOUNT = 50
MAX_CONVERSATIONS_PER_SPACE = 500
MAX_MESSAGES_PER_CONVERSATION = 400
SPACE_NAME_MAX = 80
SPACE_INSTRUCTIONS_MAX = 8000
CONVERSATION_TITLE_MAX = 120
DEFAULT_CONVERSATION_TITLE = "New conversation"
USER_MESSAGE_MAX = 4000

# Requests and pages.
MAX_BODY_BYTES = 16 * 1024
PAGE_DEFAULT = 20
PAGE_MAX = 50
MESSAGE_PAGE_DEFAULT = 50
MESSAGE_PAGE_MAX = 100

# Context window for one Space send: newest whole exchanges first, within
# both bounds (architecture §6).
HISTORY_MESSAGES_MAX = 40
HISTORY_CHARS_MAX = 60000

# A send's lease outlives the request deadline by this much, so only a
# crashed request leaves a lease to expire.
LEASE_GRACE_SECONDS = 30

_CANONICAL_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost"}


class SpacesConfigError(RuntimeError):
    """A Spaces setting is missing or malformed. The message names the
    setting only."""


@dataclass(frozen=True)
class SpacesConfig:
    beta_account_ids: frozenset[uuid.UUID]
    site_origin: str

    def __repr__(self) -> str:
        return f"SpacesConfig(beta_accounts={len(self.beta_account_ids)})"

    def has_beta_access(self, user_id: uuid.UUID) -> bool:
        return user_id in self.beta_account_ids


def spaces_enabled(environ: Mapping[str, str] = os.environ) -> bool:
    return environ.get(ENABLED_ENV, "").strip().lower() in _TRUE_VALUES


def parse_beta_account_ids(value: str | None) -> frozenset[uuid.UUID]:
    """Comma-separated canonical lowercase account UUIDs. Blank means
    nobody. Anything else (an email, a partial id, braces) is an error."""
    ids = set()
    for entry in (value or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        if not _CANONICAL_UUID.fullmatch(entry):
            raise SpacesConfigError(f"{BETA_ACCOUNTS_ENV} must list account UUIDs only.")
        ids.add(uuid.UUID(entry))
    return frozenset(ids)


def parse_site_origin(value: str | None) -> str:
    """An exact origin: https://host[:port], or http:// on a loopback host
    for local development. No path, query, fragment or credentials."""
    origin = (value or "").strip()
    try:
        parts = urlsplit(origin)
        port = parts.port
    except ValueError:
        raise SpacesConfigError(f"{SITE_ORIGIN_ENV} must be an exact origin.") from None
    host = parts.hostname or ""
    secure = parts.scheme == "https" and bool(host)
    local = parts.scheme == "http" and host in _LOOPBACK_HOSTS
    rebuilt = f"{parts.scheme}://{host}" + (f":{port}" if port is not None else "")
    if not (secure or local) or parts.username or parts.password or rebuilt != origin:
        raise SpacesConfigError(f"{SITE_ORIGIN_ENV} must be an exact origin.")
    return origin


def load_spaces_config(environ: Mapping[str, str] = os.environ) -> SpacesConfig:
    return SpacesConfig(
        beta_account_ids=parse_beta_account_ids(environ.get(BETA_ACCOUNTS_ENV)),
        site_origin=parse_site_origin(environ.get(SITE_ORIGIN_ENV)),
    )
