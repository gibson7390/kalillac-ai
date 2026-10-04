"""Password hashing with Argon2id (argon2-cffi).

Only PHC-format Argon2id hashes are stored. Plaintext passwords are never
stored or logged.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError


# argon2-cffi defaults follow RFC 9106's low-memory profile (Argon2id).
_HASHER = PasswordHasher()

# Verified when an email is unknown, so a missing account costs the same
# hashing work as a wrong password and response timing does not reveal
# which emails are registered.
_DUMMY_HASH = _HASHER.hash("kalillac-dummy-password-for-timing")

MIN_PASSWORD_LENGTH = 12

# Bounds hashing work per request.
MAX_PASSWORD_LENGTH = 256


def password_length_ok(password: str) -> bool:
    return MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH


def hash_password(password: str) -> str:
    return _HASHER.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    """Constant-work verification; an absent hash still runs Argon2."""

    try:
        return _HASHER.verify(password_hash or _DUMMY_HASH, password) and (
            password_hash is not None
        )
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _HASHER.check_needs_rehash(password_hash)
