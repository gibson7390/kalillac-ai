"""0023 without a database: its place in the graph, the exact bodies and statements, the classifier's
knowledge of both deletion functions, and the S connection's own-deletion call contract."""
from __future__ import annotations

import hashlib
import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory

import kalillac_accounts.v1_authority as v1_authority
import kalillac_accounts.v1_schema as v1_schema
from kalillac_accounts.v1_authority import V1AuthorityUnavailable
from test_account_migration import _config
from test_v1_authority import authority

PATH = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0023_v1_deletion_request_authority.py"
NAMES = {"service": "kalillac_s", "executor": "kalillac_e", "owner": "kalillac_o", "runtime": "kalillac_r",
         "operator": "kalillac_p"}
REQUEST = "kalillac.v1_request_deletion(uuid, uuid)"
OWN = "kalillac.v1_request_own_deletion(uuid, uuid, text, text)"


def _module():
    spec = importlib.util.spec_from_file_location("v1_0023_deletion_request", PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_0023_is_the_head_directly_after_0022():
    script = ScriptDirectory.from_config(_config())
    assert script.get_heads() == ["0023_v1_deletion_request"]
    module = _module()
    assert (module.revision, module.down_revision, module.branch_labels, module.depends_on) == (
        "0023_v1_deletion_request", "0022_v1_quarantine", None, None)


def test_the_classifier_knows_both_bodies_the_event_type_and_es_new_grant():
    module = _module()
    assert hashlib.sha256(module.REQUEST_BODY.encode()).hexdigest() == v1_schema.DELETION_REQUEST_SOURCE_SHA256
    assert hashlib.sha256(module.OWN_BODY.encode()).hexdigest() == v1_schema.OWN_DELETION_SOURCE_SHA256
    assert module.EVENT_TYPES == v1_schema.EVENT_TYPES_V4 == v1_schema.EVENT_TYPES_V3 + ("account_deletion_request",)
    as_sets = lambda grants: {key: set(columns) for key, columns in grants.items()}  # noqa: E731
    assert as_sets(module.EXECUTOR_COLUMN_GRANTS) == as_sets(v1_schema.EXECUTOR_COLUMN_GRANTS_V5)
    # Exactly one new right: E may write the deletion marker, nothing else changes.
    assert {key: set(v) for key, v in v1_schema.EXECUTOR_COLUMN_GRANTS_V5.items()} == {
        **{key: set(v) for key, v in v1_schema.EXECUTOR_COLUMN_GRANTS_V4.items()},
        ("users", "UPDATE"): set(v1_schema.EXECUTOR_COLUMN_GRANTS_V4[("users", "UPDATE")]) | {"deletion_requested_at"}}
    assert module.EXECUTOR_TABLE_GRANTS == v1_schema.EXECUTOR_TABLE_GRANTS
    # The S connection's reviewed inventory gains exactly the customer's function, never the operator's.
    reviewed = v1_authority._reviewed_functions()
    assert reviewed[v1_schema.OWN_DELETION_SIGNATURE] == v1_schema.OWN_DELETION_SOURCE_SHA256
    assert v1_schema.DELETION_REQUEST_SIGNATURE not in reviewed and v1_schema.QUARANTINE_SIGNATURE not in reviewed


def test_both_requests_mark_the_account_end_every_session_and_move_the_epoch_at_one_time():
    module = _module()
    for body in (module.REQUEST_BODY, module.OWN_BODY):
        assert "SET deletion_requested_at = v_at, sessions_valid_after = v_at" in body
        assert "SET revoked_at = v_at WHERE user_id = " in body and "AND revoked_at IS NULL;" in body
        assert "'account_deletion_request', v_at);" in body
        assert "RAISE" not in body and "FOR UPDATE" in body
        # Nothing is erased, cleared or purged here: no DELETE, no reset of the marker, no other table.
        assert "DELETE" not in body and "deletion_requested_at = NULL" not in body
        assert "password_hash =" not in body.replace("u.password_hash", "")
    assert "v_at := greatest(clock_timestamp(), v_epoch + interval '1 microsecond');" in module.REQUEST_BODY
    assert "v_at := greatest(v_at, v_epoch + interval '1 microsecond');" in module.OWN_BODY
    # The operator's request is refused once requested; the customer's is denied like any stale proof.
    assert "outcome := 'already_requested';" in module.REQUEST_BODY
    assert "id <> p_session_id" not in module.OWN_BODY                  # The presented session ends too.


def test_the_customers_request_is_proven_and_admitted_exactly_as_a_password_change_is():
    module = _module()
    change = module._change.BODY
    own = module.OWN_BODY
    # The same proof and live authorization, the same admission, verbatim.
    proof = change[change.index("  outcome := 'not_authenticated';"):change.index("  -- A recorded operation")]
    assert proof in own
    live = change[change.index("  -- Live authorization"):change.index(module._change.ADMISSION)]
    assert live in own and own.count(module._change.ADMISSION) == 1
    assert "p_new_password_hash" not in own


def test_the_upgrade_grants_p_the_operators_function_and_s_the_customers_only():
    module = _module()
    statements = module.upgrade_statements(NAMES)
    assert f"GRANT EXECUTE ON FUNCTION {REQUEST} TO kalillac_p" in statements
    assert f"GRANT EXECUTE ON FUNCTION {OWN} TO kalillac_s" in statements
    assert not any(f"ON FUNCTION {REQUEST} TO kalillac_s" in s or f"ON FUNCTION {OWN} TO kalillac_p" in s
                   for s in statements)
    for signature in (REQUEST, OWN):
        assert f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC" in statements
        assert f"ALTER FUNCTION {signature} OWNER TO kalillac_e" in statements
    assert "GRANT UPDATE (deletion_requested_at) ON kalillac.users TO kalillac_e" in statements
    assert not any("ROLE" in s and "LOGIN" in s for s in statements)                # No role changes.
    post = statements[-1]
    assert "V1_DELETION_REQUEST_POSTCONDITION_INVALID" in post and "<> 18" in post and "deptype = 'o') <> 9" in post
    assert "''account_deletion_request''::character varying])" in post
    assert "d.objid IN (fn, issue, ending, resetting, changing, deleting_own)))))" in post     # S's list.
    assert "AND d.objid IN (quarantining, reinstating, deleting))" in post                     # P's list.
    assert "AND p.oid NOT IN (quarantining, reinstating, deleting))" in post
    assert "AND d.refobjid = operator AND d.dbid IN (0, here)) <> 4" in post
    assert "('users', 'deletion_requested_at', 'UPDATE')" in post
    # The preflight proves 0022's catalog exactly (guard v3 and P) and that no deletion function exists.
    assert "V1_DELETION_REQUEST_PREREQUISITE_INVALID" in statements[0]
    assert "OLD.quarantined_at IS NULL AND NEW.quarantined_at IS NOT NULL" in statements[0]
    assert "v1_request_own_deletion" in statements[2]


def test_identity_guard_v3_is_unchanged():
    statements = _module().upgrade_statements(NAMES) + _module().downgrade_statements(NAMES)
    assert not any("registration_user_identity_guard " in s and "CREATE OR REPLACE" in s for s in statements)


def test_the_downgrade_refuses_with_history_and_restores_0022():
    statements = _module().downgrade_statements(NAMES)
    assert "V1_DELETION_REQUEST_POSTCONDITION_INVALID" in statements[0]
    assert "V1_DELETION_REQUEST_DOWNGRADE_REFUSED" in statements[1] and "'account_deletion_request'" in statements[1]
    assert statements[2] == "REVOKE UPDATE (deletion_requested_at) ON kalillac.users FROM kalillac_e"
    assert all("V1_DELETION_REQUEST_DOWNGRADE_INVALID" in s for s in statements[-3:])


def test_offline_downgrade_is_refused(monkeypatch):
    module = _module()

    class Context:
        as_sql = True

        class dialect:
            name = "postgresql"
    monkeypatch.setattr(module.op, "get_context", lambda: Context())
    with pytest.raises(RuntimeError, match="V1_DELETION_REQUEST_OFFLINE_DOWNGRADE_UNSUPPORTED"):
        module.downgrade()


# --- the S connection's call -------------------------------------------------------------------------

GOOD_OWN = dict(operation_id=uuid.uuid4(), session_id=uuid.uuid4(), token_hash="f" * 64,
                verified_password_hash="$argon2id$v=19$m=65536,t=3,p=4$" + "o" * 22 + "$" + "p" * 43)


@pytest.mark.parametrize("change", [
    {"operation_id": None}, {"operation_id": str(uuid.uuid4())}, {"session_id": str(uuid.uuid4())},
    {"token_hash": "F" * 64}, {"token_hash": "f" * 63}, {"token_hash": None},
    {"verified_password_hash": ""}, {"verified_password_hash": None}, {"verified_password_hash": "v" * 256},
])
def test_own_deletion_arguments_outside_the_contract_send_nothing(change):
    log = []
    with pytest.raises(V1AuthorityUnavailable):
        authority(log).request_own_deletion(**{**GOOD_OWN, **change})
    assert log == []


def test_a_well_formed_own_deletion_reaches_the_connection_check():
    log = []
    with pytest.raises(V1AuthorityUnavailable):
        authority(log).request_own_deletion(**GOOD_OWN)
    assert log == ["invalidated", "closed"]
