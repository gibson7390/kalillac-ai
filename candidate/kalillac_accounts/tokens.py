"""Opaque sign-in session tokens.

The cookie carries 256 bits of CSPRNG randomness. The database stores only
the SHA-256 digest, so a leaked database row cannot be used as a cookie.
"""

from __future__ import annotations

import hashlib
import secrets


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
