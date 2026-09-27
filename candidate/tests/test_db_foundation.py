import os

os.environ.pop("KALILLAC_DB_ENABLED", None)
os.environ.pop("KALILLAC_DB_PASSWORD", None)

from kalillac_db.config import load_database_config
from kalillac_db.engine import get_engine
import kalillac_db.engine as engine_module
from kalillac_db.models import Base


def test_database_disabled_by_default():
    assert load_database_config() is None


def test_import_does_not_create_engine():
    assert engine_module._ENGINE is None


def test_disabled_get_engine_returns_none():
    assert get_engine() is None
    assert engine_module._ENGINE is None


def test_metadata_uses_private_schema():
    assert Base.metadata.schema == "kalillac"


def test_no_business_models_exist_yet():
    assert len(Base.metadata.tables) == 0
