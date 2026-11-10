"""256-bit reset tokens. Only the SHA-256 digest of the encoded token is persisted."""
import base64
import hashlib
import re
import secrets

TOKEN_LIFETIME_SECONDS = 30 * 60
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")


def new_recovery_token() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")


def valid_recovery_token(value) -> bool:
    if not isinstance(value, str) or not _TOKEN_RE.fullmatch(value):
        return False
    raw = base64.urlsafe_b64decode(value + "=")
    return len(raw) == 32 and base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") == value


def hash_recovery_token(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()
