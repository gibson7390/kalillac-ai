"""0019 without a database: its place in the graph, the exact statements it runs, the admission it
reuses, and the classifier's knowledge of the function and grants it installs."""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory

import kalillac_accounts.v1_schema as v1_schema
from test_account_migration import _config

VERSIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"
NAMES = {"service": "kalillac_s", "executor": "kalillac_e", "owner": "kalillac_o", "runtime": "kalillac_r"}


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, VERSIONS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _module():
    return _load("v1_0019_password_reset", "0019_v1_password_reset_authority.py")


def test_0019_sits_directly_after_0018_below_0020():
    script = ScriptDirectory.from_config(_config())
    assert script.get_revision("0020_v1_password_change").down_revision == "0019_v1_password_reset"
    assert script.get_revision("0019_v1_password_reset").down_revision == "0018_v1_session_end"
    module = _module()
    assert (module.revision, module.down_revision, module.branch_labels, module.depends_on) == (
        "0019_v1_password_reset", "0018_v1_session_end", None, None)


def test_the_classifier_knows_the_exact_body_and_grants_it_installs():
    module = _module()
    assert hashlib.sha256(module.BODY.encode()).hexdigest() == v1_schema.RESET_PASSWORD_SOURCE_SHA256
    assert v1_schema.RESET_PASSWORD_SIGNATURE == module.SIGNATURE.replace(", ", ",")
    assert "SECURITY DEFINER" in module.FUNCTION and "SET search_path = pg_catalog, pg_temp" in module.FUNCTION
    as_sets = lambda grants: {key: set(columns) for key, columns in grants.items()}  # noqa: E731
    assert as_sets(module.EXECUTOR_COLUMN_GRANTS) == as_sets(v1_schema.EXECUTOR_COLUMN_GRANTS_V3)
    # Exactly the reset's additions over 0014's grants; nothing else changes.
    added = {key: set(columns) - set(v1_schema.EXECUTOR_COLUMN_GRANTS_V2.get(key, ()))
             for key, columns in v1_schema.EXECUTOR_COLUMN_GRANTS_V3.items()}
    assert {key: columns for key, columns in added.items() if columns} == {
        ("users", "UPDATE"): {"password_hash", "password_reset_required"},
        ("account_recovery_tokens", "SELECT"): {"id", "user_id", "purpose", "token_hash", "expires_at", "consumed_at"},
        ("account_recovery_tokens", "UPDATE"): {"consumed_at"}}
    assert module.EXECUTOR_TABLE_GRANTS == v1_schema.EXECUTOR_TABLE_GRANTS


def test_the_admission_is_revocations_with_the_recovery_class():
    module = _module()
    revocation = _load("v1_0013_for_0019", "0013_v1_session_revocation_authority.py")
    assert module.ADMISSION in module.BODY
    # Same windows and caps, character for character, after swapping the class and its ring.
    as_manual = (module.ADMISSION.replace("'recovery'", "'manual'")
                 .replace("m.recovery_charges, m.last_seen_at", "m.manual_charges, m.last_seen_at")
                 .replace("SET recovery_charges = v_account", "SET manual_charges = v_account"))
    assert as_manual in revocation.BODY
    for window in ("(v_aggregate, interval '1 minute', 60), (v_aggregate, interval '1 hour', 60)",
                   "(v_account, interval '1 minute', 2), (v_account, interval '1 hour', 6)"):
        assert window in module.ADMISSION
    assert "'manual'" not in module.BODY and "SET manual_charges = v_account" not in module.BODY


def test_the_body_never_records_or_raises_its_secret_inputs():
    body = _module().BODY
    assert "RAISE" not in body and "NOTICE" not in body and "LOG" not in body.upper().replace("LOGIN", "")
    # Only the hash the caller computed is stored, and only in password_hash.
    assert body.count("p_new_password_hash") == 4
    assert "SET password_hash = p_new_password_hash" in body
    # The token digest is only compared, never written.
    assert all("p_token_hash" in line and "=" in line and "SET" not in line
               for line in body.splitlines() if "p_token_hash" in line and "IF" not in line)


def test_the_upgrade_proves_0018_installs_one_function_and_its_grants_and_proves_the_result():
    module = _module()
    statements = module.upgrade_statements(NAMES)
    assert "V1_PASSWORD_RESET_PREREQUISITE_INVALID" in statements[0] and "v1_end_session" in statements[0]
    assert "V1_PASSWORD_RESET_PREREQUISITE_INVALID" in statements[1] and "v1_reset_password" in statements[1]
    assert statements[2:-1] == [
        "GRANT CREATE ON SCHEMA kalillac TO kalillac_e",
        module.FUNCTION,
        "REVOKE ALL ON FUNCTION kalillac.v1_reset_password(uuid, text, text) FROM PUBLIC",
        "GRANT EXECUTE ON FUNCTION kalillac.v1_reset_password(uuid, text, text) TO kalillac_s",
        "ALTER FUNCTION kalillac.v1_reset_password(uuid, text, text) OWNER TO kalillac_e",
        "GRANT UPDATE (password_hash, password_reset_required) ON kalillac.users TO kalillac_e",
        "GRANT SELECT (id, user_id, purpose, token_hash, expires_at, consumed_at), UPDATE (consumed_at)"
        " ON kalillac.account_recovery_tokens TO kalillac_e",
        "REVOKE CREATE ON SCHEMA kalillac FROM kalillac_e",
    ]
    post = statements[-1]
    assert "V1_PASSWORD_RESET_POSTCONDITION_INVALID" in post and post.count("(fn, issue, ending, resetting)") == 6
    assert "<> 8" in post and "AND d.deptype = 'o') <> 4" in post and "(fn, issue, ending)" not in post
    assert hashlib.sha256(module.BODY.encode()).hexdigest() in post
    assert "('users', 'password_hash', 'UPDATE')" in post
    assert "('account_recovery_tokens', 'consumed_at', 'UPDATE')" in post
    # No role, membership, constraint or event-type change outside the function itself.
    others = [statement for statement in statements[2:-1] if statement != module.FUNCTION]
    assert not any(word in " ".join(others) for word in ("ROLE ", "event_type", "CONSTRAINT"))


def test_the_downgrade_proves_0019_revokes_its_grants_drops_the_function_as_e_and_proves_0018():
    statements = _module().downgrade_statements(NAMES)
    assert "V1_PASSWORD_RESET_POSTCONDITION_INVALID" in statements[0]
    assert statements[1:8] == [
        "REVOKE UPDATE (password_hash, password_reset_required) ON kalillac.users FROM kalillac_e",
        "REVOKE SELECT (id, user_id, purpose, token_hash, expires_at, consumed_at), UPDATE (consumed_at)"
        " ON kalillac.account_recovery_tokens FROM kalillac_e",
        "GRANT CREATE ON SCHEMA kalillac TO kalillac_e", "SET ROLE kalillac_e",
        "DROP FUNCTION kalillac.v1_reset_password(uuid, text, text)", "SET ROLE kalillac_o",
        "REVOKE CREATE ON SCHEMA kalillac FROM kalillac_e"]
    assert all("V1_PASSWORD_RESET_DOWNGRADE_INVALID" in statement for statement in statements[8:])
    assert len(statements) == 10


def test_offline_downgrade_is_refused(monkeypatch):
    module = _module()

    class Context:
        as_sql = True

        class dialect:
            name = "postgresql"
    monkeypatch.setattr(module.op, "get_context", lambda: Context())
    with pytest.raises(RuntimeError, match="V1_PASSWORD_RESET_OFFLINE_DOWNGRADE_UNSUPPORTED"):
        module.downgrade()
