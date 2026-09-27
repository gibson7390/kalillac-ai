import threading
import time

import kalillac_db.engine as db_engine
from kalillac_db.config import DatabaseConfig


def test_lazy_engine_is_published_atomically(monkeypatch):
    db_engine._ENGINE = None
    db_engine._SESSION_FACTORY = None

    config = DatabaseConfig(
        host="127.0.0.1",
        port=5432,
        database="kalillac",
        user="kalillac_app",
        password="test-only-not-used",
    )

    fake_engine = object()
    fake_factory = object()

    factory_started = threading.Event()
    release_factory = threading.Event()

    monkeypatch.setattr(
        db_engine,
        "load_database_config",
        lambda: config,
    )

    monkeypatch.setattr(
        db_engine,
        "create_engine",
        lambda *args, **kwargs: fake_engine,
    )

    def delayed_sessionmaker(*args, **kwargs):
        factory_started.set()
        assert release_factory.wait(timeout=2)
        return fake_factory

    monkeypatch.setattr(
        db_engine,
        "sessionmaker",
        delayed_sessionmaker,
    )

    results = []
    errors = []

    def caller():
        try:
            results.append(db_engine.get_engine())
        except Exception as exc:
            errors.append(exc)

    first = threading.Thread(target=caller)
    second = threading.Thread(target=caller)

    first.start()

    assert factory_started.wait(timeout=2)

    second.start()

    # Give the second caller a chance to hit the initialization path.
    time.sleep(0.1)

    # It must not return while the first thread has only partially initialized.
    assert second.is_alive()

    release_factory.set()

    first.join(timeout=2)
    second.join(timeout=2)

    assert not errors
    assert results == [fake_engine, fake_engine]
    assert db_engine._ENGINE is fake_engine
    assert db_engine._SESSION_FACTORY is fake_factory
