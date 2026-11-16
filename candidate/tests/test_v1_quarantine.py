"""0022 without a database: its place in the graph, guard v3, the exact statements, and the classifier's
knowledge of the operator functions, P and the widened event types."""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory

import kalillac_accounts.v1_schema as v1_schema
from kalillac_accounts import registration_schema
from test_account_migration import _config

PATH = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0022_v1_quarantine_authority.py"
NAMES = {"service": "kalillac_s", "executor": "kalillac_e", "owner": "kalillac_o", "runtime": "kalillac_r",
         "operator": "kalillac_p"}


def _module():
    spec = importlib.util.spec_from_file_location("v1_0022_quarantine", PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_0022_is_the_head_directly_after_0021():
    script = ScriptDirectory.from_config(_config())
    assert script.get_heads() == ["0022_v1_quarantine"]
    assert (_module().revision, _module().down_revision) == ("0022_v1_quarantine", "0021_registration_activation")


def test_guard_v3_is_v2_plus_exactly_the_two_transitions():
    v2 = registration_schema.identity_guard_v2("kalillac_registration")
    v3 = registration_schema.identity_guard_v3("kalillac_registration")
    deny = registration_schema._IDENTITY_DENY
    assert v3 == v2.replace(deny, ")\n   AND NOT (" + registration_schema.QUARANTINE + ")\n   AND NOT ("
                            + registration_schema.REINSTATE + deny)
    assert v3.count(registration_schema.QUARANTINE) == 1 and v3.count(registration_schema.REINSTATE) == 1
    assert "NEW.sessions_valid_after >= NEW.quarantined_at" in registration_schema.QUARANTINE
    assert "OLD.quarantined_at IS NOT NULL AND NEW.quarantined_at IS NULL" in registration_schema.REINSTATE
    # Reinstatement moves nothing else: the epoch is among the columns that must not change.
    assert "NEW.sessions_valid_after) IS NOT DISTINCT FROM" in registration_schema.REINSTATE


def test_the_classifier_knows_both_bodies_the_event_types_and_es_new_grant():
    module = _module()
    assert hashlib.sha256(module.QUARANTINE_BODY.encode()).hexdigest() == v1_schema.QUARANTINE_SOURCE_SHA256
    assert hashlib.sha256(module.REINSTATE_BODY.encode()).hexdigest() == v1_schema.REINSTATE_SOURCE_SHA256
    assert module.EVENT_TYPES == v1_schema.EVENT_TYPES_V3
    as_sets = lambda grants: {key: set(columns) for key, columns in grants.items()}  # noqa: E731
    assert as_sets(module.EXECUTOR_COLUMN_GRANTS) == as_sets(v1_schema.EXECUTOR_COLUMN_GRANTS_V4)
    assert set(v1_schema.EXECUTOR_COLUMN_GRANTS_V4[("users", "UPDATE")]) - set(
        v1_schema.EXECUTOR_COLUMN_GRANTS_V3[("users", "UPDATE")]) == {"quarantined_at"}


def test_quarantine_ends_every_session_at_one_time_and_reinstatement_restores_none():
    module = _module()
    body = module.QUARANTINE_BODY
    assert "v_at := greatest(clock_timestamp(), v_epoch + interval '1 microsecond');" in body
    assert "SET quarantined_at = v_at, sessions_valid_after = v_at" in body
    assert "SET revoked_at = v_at WHERE user_id = p_user_id AND revoked_at IS NULL" in body
    assert "account_sessions" not in module.REINSTATE_BODY and "sessions_valid_after" not in module.REINSTATE_BODY
    for text in (body, module.REINSTATE_BODY):
        assert "RAISE" not in text and "FOR UPDATE" in text


def test_the_upgrade_grants_p_only_the_two_functions_and_s_neither():
    module = _module()
    statements = module.upgrade_statements(NAMES)
    joined = "\n".join(statements[3:-1])
    assert "TO kalillac_s" not in joined
    for signature in ("kalillac.v1_quarantine(uuid, uuid)", "kalillac.v1_reinstate(uuid, uuid)"):
        assert f"GRANT EXECUTE ON FUNCTION {signature} TO kalillac_p" in statements
        assert f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC" in statements
        assert f"ALTER FUNCTION {signature} OWNER TO kalillac_e" in statements
    assert "GRANT USAGE ON SCHEMA kalillac TO kalillac_p" in statements
    assert "GRANT UPDATE (quarantined_at) ON kalillac.users TO kalillac_e" in statements
    post = statements[-1]
    assert "V1_QUARANTINE_POSTCONDITION_INVALID" in post and "<> 14" in post and "deptype = 'o') <> 7" in post
    assert "'kalillac_p'" in post and "''account_reinstate''::character varying])" in post
    assert "d.objid IN (fn, issue, ending, resetting, changing)))))" in post      # S's own list is unchanged.


def test_the_downgrade_refuses_with_history_and_swaps_the_guard_back():
    statements = _module().downgrade_statements(NAMES)
    assert "V1_QUARANTINE_POSTCONDITION_INVALID" in statements[0]
    assert "V1_QUARANTINE_DOWNGRADE_REFUSED" in statements[1]
    assert any("CREATE OR REPLACE FUNCTION kalillac.registration_user_identity_guard" in s for s in statements)
    assert all("V1_QUARANTINE_DOWNGRADE_INVALID" in s for s in statements[-3:])


def test_offline_downgrade_is_refused(monkeypatch):
    module = _module()

    class Context:
        as_sql = True

        class dialect:
            name = "postgresql"
    monkeypatch.setattr(module.op, "get_context", lambda: Context())
    with pytest.raises(RuntimeError, match="V1_QUARANTINE_OFFLINE_DOWNGRADE_UNSUPPORTED"):
        module.downgrade()
