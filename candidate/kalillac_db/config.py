"""Configuration for the optional Kalillac PostgreSQL layer."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


_TRUE_VALUES = {"1", "true", "yes", "on"}


class DatabaseConfigError(RuntimeError):
    """Raised when database mode is enabled but configuration is incomplete."""


@dataclass(frozen=True)
class DatabaseConfig:
    host: str
    port: int
    database: str
    user: str
    password: str


def database_enabled() -> bool:
    value = os.getenv("KALILLAC_DB_ENABLED", "")
    return value.strip().lower() in _TRUE_VALUES


def _read_password() -> str | None:
    credentials_directory = os.getenv("CREDENTIALS_DIRECTORY")

    if credentials_directory:
        credential_path = Path(credentials_directory) / "kalillac_db_password"

        try:
            password = credential_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            password = ""

        if password:
            return password

    # Development/test fallback only.
    password = os.getenv("KALILLAC_DB_PASSWORD", "").strip()
    return password or None


def load_database_url() -> str | None:
    """Return an environment-provided database URL, or None.

    KALILLAC_DATABASE_URL takes precedence over the component settings
    below when database mode is enabled. Only PostgreSQL through psycopg is
    accepted, so a typo cannot silently point Kalillac at another engine.
    No connection is attempted here.
    """

    if not database_enabled():
        return None

    url = os.getenv("KALILLAC_DATABASE_URL", "").strip()

    if not url:
        return None

    if not url.startswith("postgresql+psycopg://"):
        raise DatabaseConfigError(
            "KALILLAC_DATABASE_URL must use the postgresql+psycopg:// driver."
        )

    return url


def load_database_config() -> DatabaseConfig | None:
    """Return None while database features are disabled.

    No connection is attempted here.
    """

    if not database_enabled() or load_database_url() is not None:
        return None

    password = _read_password()

    if not password:
        raise DatabaseConfigError(
            "Kalillac database is enabled but no database password is available."
        )

    try:
        port = int(os.getenv("KALILLAC_DB_PORT", "5432"))
    except ValueError as exc:
        raise DatabaseConfigError("KALILLAC_DB_PORT must be an integer.") from exc

    return DatabaseConfig(
        host=os.getenv("KALILLAC_DB_HOST", "127.0.0.1"),
        port=port,
        database=os.getenv("KALILLAC_DB_NAME", "kalillac"),
        user=os.getenv("KALILLAC_DB_USER", "kalillac_app"),
        password=password,
    )
