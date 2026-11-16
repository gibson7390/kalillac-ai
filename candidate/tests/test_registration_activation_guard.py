"""0021 without a database, and the structural half of owner decision 14."""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory

import kalillac_accounts.v1_schema as v1_schema
from test_account_migration import _config

CANDIDATE = Path(__file__).resolve().parents[1]
PATH = CANDIDATE / "migrations" / "versions" / "0021_registration_activation_guard.py"


def _module():
    spec = importlib.util.spec_from_file_location("registration_0021_activation", PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_0021_is_the_head_directly_after_0020():
    script = ScriptDirectory.from_config(_config())
    assert script.get_heads() == ["0021_registration_activation"]
    module = _module()
    assert (module.revision, module.down_revision) == ("0021_registration_activation", "0020_v1_password_change")


def test_the_classifier_knows_the_exact_guard():
    module = _module()
    assert hashlib.sha256(module.BODY.encode()).hexdigest() == v1_schema.ACTIVATION_GUARD_SOURCE_SHA256
    assert module.TRIGGER == v1_schema.ACTIVATION_GUARD_TRIGGER
    assert module.TRIGGER_DEFINITION == ("CREATE TRIGGER " + v1_schema.ACTIVATION_GUARD_TRIGGER + " "
                                         + v1_schema.ACTIVATION_GUARD_TRIGGER_DEFINITION)
    assert "SECURITY DEFINER" not in module.FUNCTION and "SET search_path = pg_catalog, pg_temp" in module.FUNCTION


def test_the_guard_counts_unverified_accounts_on_the_transition_only():
    body = _module().BODY
    assert "NEW.state = 'active_zero_legacy' AND OLD.state IS DISTINCT FROM 'active_zero_legacy'" in body
    assert "EXISTS (SELECT 1 FROM kalillac.users u WHERE u.email_verified_at IS NULL)" in body
    assert "NEW.legacy_unverified_count IS DISTINCT FROM 0" in body and "LOCK TABLE" not in body


def test_the_upgrade_and_downgrade_are_exact():
    module = _module()
    up = module.upgrade_statements()
    assert "REGISTRATION_ACTIVATION_GUARD_PREREQUISITE_INVALID" in up[0]
    assert up[1:4] == [module.FUNCTION, "REVOKE ALL ON FUNCTION kalillac.registration_activation_guard() FROM PUBLIC",
                       module.CREATE_TRIGGER]
    assert "REGISTRATION_ACTIVATION_GUARD_POSTCONDITION_INVALID" in up[4] and module.BODY_SHA256 in up[4]
    down = module.downgrade_statements()
    assert down[0] == up[4] and "REGISTRATION_ACTIVATION_GUARD_DOWNGRADE_INVALID" in down[-1]


def test_offline_downgrade_is_refused(monkeypatch):
    module = _module()

    class Context:
        as_sql = True

        class dialect:
            name = "postgresql"
    monkeypatch.setattr(module.op, "get_context", lambda: Context())
    with pytest.raises(RuntimeError, match="REGISTRATION_ACTIVATION_GUARD_OFFLINE_DOWNGRADE_UNSUPPORTED"):
        module.downgrade()


def test_nothing_depends_on_a_v1_email_verification_authority():
    """Decision 14, condition 4: no route, module or migration calls or creates v1_verify_email, and
    password recovery runs on its own reset tokens (0019), not on email verification."""
    sources = [path for folder in ("kalillac_accounts", "kalillac_db", "migrations", "tools")
               for path in (CANDIDATE / folder).rglob("*.py")] + [CANDIDATE / "app_fastapi_candidate.py"]
    assert not [path.name for path in sources if "v1_verify_email" in path.read_text(encoding="utf-8")]
    recovery = (CANDIDATE / "kalillac_accounts" / "recovery.py").read_text(encoding="utf-8")
    assert "email_verif" not in recovery
