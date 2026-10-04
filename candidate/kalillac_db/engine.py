"""Lazy synchronous SQLAlchemy engine for optional PostgreSQL features."""

from __future__ import annotations

from contextlib import contextmanager
from threading import Lock
from typing import Iterator

from sqlalchemy import URL, create_engine, make_url
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import load_database_config, load_database_url


_ENGINE: Engine | None = None
_SESSION_FACTORY: sessionmaker[Session] | None = None
_ENGINE_LOCK = Lock()


def get_engine() -> Engine | None:
    """Return the lazy engine, or None when DB support is disabled.

    Importing this module and calling this function while DB support is
    disabled performs no database I/O.

    Engine/session-factory initialization is published atomically so another
    thread cannot observe a partially initialized database layer.
    """

    global _ENGINE
    global _SESSION_FACTORY

    database_url = load_database_url()
    config = None if database_url else load_database_config()

    if database_url is None and config is None:
        return None

    if _ENGINE is not None and _SESSION_FACTORY is not None:
        return _ENGINE

    with _ENGINE_LOCK:
        if _ENGINE is None or _SESSION_FACTORY is None:
            if database_url is not None:
                url = make_url(database_url)
            else:
                url = URL.create(
                    drivername="postgresql+psycopg",
                    username=config.user,
                    password=config.password,
                    host=config.host,
                    port=config.port,
                    database=config.database,
                )

            new_engine = create_engine(
                url,
                pool_size=2,
                max_overflow=2,
                pool_timeout=2,
                pool_pre_ping=True,
                pool_recycle=1800,
                echo=False,
                hide_parameters=True,
                connect_args={
                    "connect_timeout": 3,
                    "application_name": "kalillac-v31",
                },
            )

            new_session_factory = sessionmaker(
                bind=new_engine,
                expire_on_commit=False,
            )

            # Publish the factory first and the engine last. The fast path
            # requires both, so callers can never observe half-initialized state.
            _SESSION_FACTORY = new_session_factory
            _ENGINE = new_engine

    return _ENGINE


@contextmanager
def session_scope() -> Iterator[Session]:
    """Provide a short-lived transaction.

    Callers must never hold this open across model/search calls or while
    holding Kalillac's global session lock.
    """

    engine = get_engine()

    if engine is None or _SESSION_FACTORY is None:
        raise RuntimeError("Kalillac database support is disabled.")

    session = _SESSION_FACTORY()

    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session_factory() -> sessionmaker[Session] | None:
    """Return the session factory, or None when DB support is disabled."""

    if get_engine() is None:
        return None

    return _SESSION_FACTORY
