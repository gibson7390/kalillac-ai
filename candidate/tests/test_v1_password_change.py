"""0020 without a database: its place in the graph, the exact statements it runs, the session proof
and admission it reuses, the widened event types, and the classifier's knowledge of all of it."""
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
    return _load("v1_0020_password_change", "0020_v1_password_change_authority.py")


def test_0020_is_the_head_directly_after_0019():
    script = ScriptDirectory.from_config(_config())
    assert script.get_heads() == ["0020_v1_password_change"]
    assert script.get_revision("0020_v1_password_change").down_revision == "0019_v1_password_reset"
    module = _module()
    assert (module.revision, module.down_revision, module.branch_labels, module.depends_on) == (
        "0020_v1_password_change", "0019_v1_password_reset", None, None)


def test_the_classifier_knows_the_exact_body_event_types_and_unchanged_grants():
    module = _module()
    assert hashlib.sha256(module.BODY.encode()).hexdigest() == v1_schema.CHANGE_PASSWORD_SOURCE_SHA256
    assert v1_schema.CHANGE_PASSWORD_SIGNATURE == module.SIGNATURE.replace(", ", ",")
    assert "SECURITY DEFINER" in module.FUNCTION and "SET search_path = pg_catalog, pg_temp" in module.FUNCTION
    assert module.EVENT_TYPES == v1_schema.EVENT_TYPES_V2 and module.EVENT_TYPES_BEFORE == v1_schema.EVENT_TYPES
    reserved = _load("v1_0012_for_0020", "0012_v1_account_security.py")
    assert reserved.EVENT_TYPES == v1_schema.EVENT_TYPES                  # 0012's list, unchanged.
    assert module.EXECUTOR_COLUMN_GRANTS == _load("v1_0019_for_0020", "0019_v1_password_reset_authority.py") \
        .EXECUTOR_COLUMN_GRANTS                                            # No new grant to E.
    assert module._rendered(v1_schema.EVENT_TYPES_V2) == v1_schema._among("event_type", v1_schema.EVENT_TYPES_V2)


def test_the_session_proof_and_admission_are_revocations_own():
    module = _module()
    revocation = _load("v1_0013_for_0020", "0013_v1_session_revocation_authority.py")
    assert module.ADMISSION in revocation.BODY and module.ADMISSION in module.BODY
    for clause in ("SELECT s.user_id INTO v_user FROM kalillac.account_sessions s\n"
                   "   WHERE s.id = p_session_id AND s.token_hash = p_token_hash;",
                   "WHERE s.id = p_session_id AND s.token_hash = p_token_hash AND s.user_id = v_user FOR UPDATE;"):
        assert clause in revocation.BODY and clause in module.BODY
    body = module.BODY
    assert body.index("FROM kalillac.users u WHERE u.id = v_user FOR UPDATE") < body.index("AND s.user_id = v_user FOR UPDATE")
    assert "v_hash IS DISTINCT FROM p_verified_password_hash" in body and "OR v_reset" in body
    # The presented session stays; the epoch does not move; only the hash column is written on users.
    assert "AND id <> p_session_id" in body and "sessions_valid_after =" not in body
    assert "UPDATE kalillac.users SET password_hash = p_new_password_hash WHERE id = v_user;" in body
    assert "RAISE" not in body and body.count("p_verified_password_hash") == 3


def test_the_upgrade_proves_0019_widens_the_event_types_installs_one_function_and_proves_the_result():
    module = _module()
    statements = module.upgrade_statements(NAMES)
    assert "V1_PASSWORD_CHANGE_PREREQUISITE_INVALID" in statements[0] and "v1_reset_password" in statements[0]
    assert "V1_PASSWORD_CHANGE_PREREQUISITE_INVALID" in statements[1] and "v1_change_password" in statements[1]
    assert "''password_reset''::character varying])" in statements[1]      # 0012's constraint, exactly.
    assert statements[2:-1] == [
        "ALTER TABLE kalillac.account_security_events DROP CONSTRAINT ck_account_security_events_type",
        "ALTER TABLE kalillac.account_security_events ADD CONSTRAINT ck_account_security_events_type CHECK "
        "(event_type IN ('session_revoke_others','session_revoke_all','password_reset','password_change'))",
        "GRANT CREATE ON SCHEMA kalillac TO kalillac_e",
        module.FUNCTION,
        "REVOKE ALL ON FUNCTION kalillac.v1_change_password(uuid, uuid, text, text, text) FROM PUBLIC",
        "GRANT EXECUTE ON FUNCTION kalillac.v1_change_password(uuid, uuid, text, text, text) TO kalillac_s",
        "ALTER FUNCTION kalillac.v1_change_password(uuid, uuid, text, text, text) OWNER TO kalillac_e",
        "REVOKE CREATE ON SCHEMA kalillac FROM kalillac_e",
    ]
    post = statements[-1]
    assert "V1_PASSWORD_CHANGE_POSTCONDITION_INVALID" in post
    assert post.count("(fn, issue, ending, resetting, changing)") == 6
    assert "<> 10" in post and "AND d.deptype = 'o') <> 5" in post
    assert hashlib.sha256(module.BODY.encode()).hexdigest() in post
    assert "''password_change''::character varying])" in post
    assert not any(word in " ".join(statements[2:-1]).replace(module.FUNCTION, "") for word in ("ROLE ", " ON kalillac.users"))


def test_the_downgrade_refuses_while_change_history_exists_and_restores_0019():
    statements = _module().downgrade_statements(NAMES)
    assert "V1_PASSWORD_CHANGE_POSTCONDITION_INVALID" in statements[0]
    assert "V1_PASSWORD_CHANGE_DOWNGRADE_REFUSED" in statements[1] and "event_type = 'password_change'" in statements[1]
    assert statements[2:9] == [
        "GRANT CREATE ON SCHEMA kalillac TO kalillac_e", "SET ROLE kalillac_e",
        "DROP FUNCTION kalillac.v1_change_password(uuid, uuid, text, text, text)", "SET ROLE kalillac_o",
        "REVOKE CREATE ON SCHEMA kalillac FROM kalillac_e",
        "ALTER TABLE kalillac.account_security_events DROP CONSTRAINT ck_account_security_events_type",
        "ALTER TABLE kalillac.account_security_events ADD CONSTRAINT ck_account_security_events_type CHECK "
        "(event_type IN ('session_revoke_others','session_revoke_all','password_reset'))"]
    assert all("V1_PASSWORD_CHANGE_DOWNGRADE_INVALID" in statement for statement in statements[9:])
    assert len(statements) == 11


def test_offline_downgrade_is_refused(monkeypatch):
    module = _module()

    class Context:
        as_sql = True

        class dialect:
            name = "postgresql"
    monkeypatch.setattr(module.op, "get_context", lambda: Context())
    with pytest.raises(RuntimeError, match="V1_PASSWORD_CHANGE_OFFLINE_DOWNGRADE_UNSUPPORTED"):
        module.downgrade()
