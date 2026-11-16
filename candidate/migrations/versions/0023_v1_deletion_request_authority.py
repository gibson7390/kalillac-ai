"""Prepare account deletion requests (V1-02D), by the operator role P and by the customer through S, unexposed.

Revision ID: 0023_v1_deletion_request
Revises: 0022_v1_quarantine

PostgreSQL only. It installs two E-owned SECURITY DEFINER functions, E's UPDATE on users.deletion_requested_at,
and widens the audit event types by 'account_deletion_request'. No table, column, index, trigger, role,
membership or data changes; identity guard v3 is unchanged (deletion_requested_at is not an identity
column, and the epoch moves forward through v2's reviewed transition).

* `v1_request_deletion(op, account)`, executable only by the operator role P: locks the account, reports a
  recorded operation again, refuses an unknown account and one whose deletion is already requested, then
  sets `deletion_requested_at` and the epoch to one effective time strictly after the current epoch,
  revokes every session and records 'account_deletion_request'.
* `v1_request_own_deletion(op, session, token_hash, verified_hash)`, executable only by S: the customer's own
  request, proven exactly as a password change is (the account comes only from the exact original
  session; V1-01's live authorization; the stored hash the caller has just verified the current password
  against). A quarantined account answers 'account_unavailable'. It charges the 'manual' admission class
  with revocation's exact windows (a customer's own security operations share one budget) and then has
  the operator function's effects, the presented session included.

The request is the whole lifecycle here. Nothing clears `deletion_requested_at` (no withdrawal), nothing
erases or purges an account or its data, and no other function changes: issuance, revocation, reset and
change already refuse a deletion-requested account. Erasure follows the owner's retention decision (plan,
section 5) in a later checkpoint. The downgrade refuses while any deletion-request event exists (audit
history is never deleted to step down). V1 startup still refuses.
"""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

from alembic import op

revision = "0023_v1_deletion_request"
down_revision = "0022_v1_quarantine"
branch_labels = depends_on = None


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_quarantine = _load("v1_0022_for_0023", "0022_v1_quarantine_authority.py")
_change = _quarantine._change

REQUEST_SIGNATURE = "kalillac.v1_request_deletion(uuid, uuid)"
OWN_SIGNATURE = "kalillac.v1_request_own_deletion(uuid, uuid, text, text)"
EVENT_TYPES_BEFORE = _quarantine.EVENT_TYPES
EVENT_TYPES = EVENT_TYPES_BEFORE + ("account_deletion_request",)
ADDED_COLUMN_GRANTS = {("users", "UPDATE"): ("deletion_requested_at",)}
EXECUTOR_TABLE_GRANTS = _quarantine.EXECUTOR_TABLE_GRANTS
EXECUTOR_COLUMN_GRANTS = {key: tuple(_quarantine.EXECUTOR_COLUMN_GRANTS.get(key, ()))
                          + tuple(c for c in ADDED_COLUMN_GRANTS.get(key, ())
                                  if c not in _quarantine.EXECUTOR_COLUMN_GRANTS.get(key, ()))
                          for key in {**_quarantine.EXECUTOR_COLUMN_GRANTS, **ADDED_COLUMN_GRANTS}}

# Static SQL only; nothing is logged or raised with the inputs. Expected outcomes are returned.
REQUEST_BODY = """
DECLARE
 v_deletion timestamptz;
 v_epoch timestamptz;
 v_event_user uuid;
 v_event_type text;
 v_at timestamptz;
BEGIN
 outcome := 'security_unavailable';
 replayed := false;
 IF p_operation_id IS NULL OR p_user_id IS NULL THEN
  RETURN NEXT;
  RETURN;
 END IF;
 BEGIN
  SELECT u.deletion_requested_at, u.sessions_valid_after INTO v_deletion, v_epoch
   FROM kalillac.users u WHERE u.id = p_user_id FOR UPDATE;
  IF NOT FOUND THEN
   outcome := 'not_found';
   RETURN NEXT;
   RETURN;
  END IF;
  SELECT e.user_id, e.event_type INTO v_event_user, v_event_type
   FROM kalillac.account_security_events e WHERE e.operation_id = p_operation_id;
  IF FOUND THEN
   IF v_event_user = p_user_id AND v_event_type = 'account_deletion_request' THEN
    outcome := 'requested';
    replayed := true;
   END IF;
   RETURN NEXT;
   RETURN;
  END IF;
  IF v_deletion IS NOT NULL THEN
   outcome := 'already_requested';
   RETURN NEXT;
   RETURN;
  END IF;
  -- One effective time, strictly after the current epoch: every session ends and none can start.
  v_at := greatest(clock_timestamp(), v_epoch + interval '1 microsecond');
  UPDATE kalillac.users SET deletion_requested_at = v_at, sessions_valid_after = v_at WHERE id = p_user_id;
  UPDATE kalillac.account_sessions SET revoked_at = v_at WHERE user_id = p_user_id AND revoked_at IS NULL;
  INSERT INTO kalillac.account_security_events (operation_id, user_id, event_type, occurred_at)
   VALUES (p_operation_id, p_user_id, 'account_deletion_request', v_at);
  outcome := 'requested';
  RETURN NEXT;
  RETURN;
 EXCEPTION WHEN unique_violation THEN
  outcome := 'security_unavailable';
  replayed := false;
  RETURN NEXT;
  RETURN;
 END;
END
"""

# Static SQL only. The trusted inputs are the session pair (the token's digest, never the token), the
# stored hash the caller verified (compared, never written) and the operation id. Nothing is logged,
# raised or recorded with them.
OWN_BODY = """
DECLARE
 v_user uuid;
 v_hash text;
 v_active boolean;
 v_quarantined timestamptz;
 v_deletion timestamptz;
 v_reset boolean;
 v_epoch timestamptz;
 v_created timestamptz;
 v_expires timestamptz;
 v_revoked timestamptz;
 v_event_user uuid;
 v_event_type text;
 v_now timestamptz;
 v_at timestamptz;
 v_aggregate timestamptz[];
 v_aggregate_seen timestamptz;
 v_slot smallint;
 v_account timestamptz[];
 v_account_seen timestamptz;
 v_wait numeric;
BEGIN
 outcome := 'security_unavailable';
 retry_after_seconds := NULL;
 replayed := false;
 IF p_operation_id IS NULL OR p_session_id IS NULL OR p_token_hash IS NULL OR p_token_hash !~ '^[0-9a-f]{64}$'
    OR p_verified_password_hash IS NULL OR length(p_verified_password_hash) > 255 THEN
  RETURN NEXT;
  RETURN;
 END IF;
 BEGIN
  outcome := 'not_authenticated';
  -- The account comes only from the exact original session, in any state.
  SELECT s.user_id INTO v_user FROM kalillac.account_sessions s
   WHERE s.id = p_session_id AND s.token_hash = p_token_hash;
  IF NOT FOUND THEN
   RETURN NEXT;
   RETURN;
  END IF;
  SELECT u.password_hash, u.is_active, u.quarantined_at, u.deletion_requested_at, u.password_reset_required,
         u.sessions_valid_after
   INTO v_hash, v_active, v_quarantined, v_deletion, v_reset, v_epoch
   FROM kalillac.users u WHERE u.id = v_user FOR UPDATE;
  IF NOT FOUND THEN
   RETURN NEXT;
   RETURN;
  END IF;
  SELECT s.created_at, s.expires_at, s.revoked_at INTO v_created, v_expires, v_revoked
   FROM kalillac.account_sessions s
   WHERE s.id = p_session_id AND s.token_hash = p_token_hash AND s.user_id = v_user FOR UPDATE;
  IF NOT FOUND THEN
   RETURN NEXT;
   RETURN;
  END IF;
  -- A recorded operation is reported again, never applied or charged twice.
  SELECT e.user_id, e.event_type INTO v_event_user, v_event_type
   FROM kalillac.account_security_events e WHERE e.operation_id = p_operation_id;
  IF FOUND THEN
   IF v_event_user = v_user AND v_event_type = 'account_deletion_request' THEN
    outcome := 'requested';
    replayed := true;
   ELSE
    outcome := 'security_unavailable';
   END IF;
   RETURN NEXT;
   RETURN;
  END IF;
  -- Live authorization of a new operation, in V1-01's order; denials store nothing.
  v_now := clock_timestamp();
  IF NOT v_active OR v_deletion IS NOT NULL OR v_revoked IS NOT NULL OR v_expires <= v_now
     OR (v_epoch IS NOT NULL AND v_created <= v_epoch) OR v_reset
     OR v_hash IS DISTINCT FROM p_verified_password_hash THEN
   RETURN NEXT;
   RETURN;
  END IF;
  IF v_quarantined IS NOT NULL THEN
   outcome := 'account_unavailable';
   RETURN NEXT;
   RETURN;
  END IF;
""" + _change.ADMISSION + """  -- One effective time, strictly after the epoch: every session ends, the presented one too.
  v_at := greatest(v_at, v_epoch + interval '1 microsecond');
  UPDATE kalillac.users SET deletion_requested_at = v_at, sessions_valid_after = v_at WHERE id = v_user;
  UPDATE kalillac.account_sessions SET revoked_at = v_at WHERE user_id = v_user AND revoked_at IS NULL;
  INSERT INTO kalillac.account_security_events (operation_id, user_id, event_type, occurred_at)
   VALUES (p_operation_id, v_user, 'account_deletion_request', v_at);
  outcome := 'requested';
  RETURN NEXT;
  RETURN;
 EXCEPTION WHEN unique_violation THEN
  -- The operation ID was taken concurrently by another account or operation.
  outcome := 'security_unavailable';
  retry_after_seconds := NULL;
  replayed := false;
  RETURN NEXT;
  RETURN;
 END;
END
"""

_TAIL = " LANGUAGE plpgsql VOLATILE SECURITY DEFINER PARALLEL UNSAFE SET search_path = pg_catalog, pg_temp AS "
REQUEST_FUNCTION = ("CREATE FUNCTION kalillac.v1_request_deletion(p_operation_id uuid, p_user_id uuid)"
                    " RETURNS TABLE(outcome text, replayed boolean)" + _TAIL
                    + "$v1_request_deletion$" + REQUEST_BODY + "$v1_request_deletion$")
OWN_FUNCTION = ("CREATE FUNCTION kalillac.v1_request_own_deletion(p_operation_id uuid, p_session_id uuid,"
                " p_token_hash text, p_verified_password_hash text)"
                " RETURNS TABLE(outcome text, retry_after_seconds integer, replayed boolean)" + _TAIL
                + "$v1_request_own_deletion$" + OWN_BODY + "$v1_request_own_deletion$")


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _once(template, old, new, count=1):
    if template.count(old) != count:
        raise RuntimeError("V1_DELETION_REQUEST_POSTCONDITION_UNRECOGNIZED")
    return template.replace(old, new)


_BEFORE = "(fn, issue, ending, resetting, changing, quarantining, reinstating)"
_AFTER = "(fn, issue, ending, resetting, changing, quarantining, reinstating, deleting, deleting_own)"

# 0022's postcondition extended by the two functions: exact properties, body digests and results, owned
# by E; the operator's function executable by E and P only, the customer's by E and S only; P's and S's
# exact dependencies; E's widened column grants; the widened event-type constraint. Guard v3 unchanged.
_post = _quarantine.POSTCONDITION.replace("$v1_quarantine_postcondition$", "$v1_deletion_request_postcondition$")
_post = _once(_post, " reinstating oid := pg_catalog.to_regprocedure('kalillac.v1_reinstate(uuid,uuid)');",
              " reinstating oid := pg_catalog.to_regprocedure('kalillac.v1_reinstate(uuid,uuid)');\n"
              " deleting oid := pg_catalog.to_regprocedure('kalillac.v1_request_deletion(uuid,uuid)');\n"
              " deleting_own oid := pg_catalog.to_regprocedure('kalillac.v1_request_own_deletion(uuid,uuid,text,text)');")
_post = _once(_post, " OR quarantining IS NULL OR reinstating IS NULL",
              " OR quarantining IS NULL OR reinstating IS NULL OR deleting IS NULL OR deleting_own IS NULL")
# S's own allowed dependencies gain exactly the customer's function.
_post = _once(_post, "AND d.objid IN (fn, issue, ending, resetting, changing)))))",
              "AND d.objid IN (fn, issue, ending, resetting, changing, deleting_own)))))")
_post = _once(_post, _BEFORE, _AFTER, count=5)
_post = _once(_post, " (reinstating, operator, 'EXECUTE', false, executor)))",
              " (reinstating, operator, 'EXECUTE', false, executor),\n"
              "                           (deleting, executor, 'EXECUTE', false, executor),"
              " (deleting, operator, 'EXECUTE', false, executor),\n"
              "                           (deleting_own, executor, 'EXECUTE', false, executor),"
              " (deleting_own, service, 'EXECUTE', false, executor)))")
_post = _once(_post, "WHERE p.oid IN " + _AFTER + ") <> 14", "WHERE p.oid IN " + _AFTER + ") <> 18")
_post = _once(_post, "AND d.deptype = 'o') <> 7", "AND d.deptype = 'o') <> 9")
_post = _once(_post, " OR EXISTS (SELECT 1 FROM pg_catalog.pg_proc p WHERE p.oid IN " + _AFTER + " AND p.proowner",
              "".join(f" OR (SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(p.prosrc, 'UTF8')), 'hex')\n"
                      f"     FROM pg_catalog.pg_proc p WHERE p.oid = {name}) IS DISTINCT FROM '{_sha(body)}'\n"
                      f" OR (SELECT pg_catalog.pg_get_function_result(p.oid) FROM pg_catalog.pg_proc p WHERE p.oid = {name})\n"
                      f"    IS DISTINCT FROM '{result}'\n"
                      for name, body, result in (
                          ("deleting", REQUEST_BODY, "TABLE(outcome text, replayed boolean)"),
                          ("deleting_own", OWN_BODY,
                           "TABLE(outcome text, retry_after_seconds integer, replayed boolean)")))
              + " OR EXISTS (SELECT 1 FROM pg_catalog.pg_proc p WHERE p.oid IN " + _AFTER + " AND p.proowner")
_post = _once(_post, "'" + _change._rendered(EVENT_TYPES_BEFORE).replace("'", "''") + "'",
              "'" + _change._rendered(EVENT_TYPES).replace("'", "''") + "'")
# Role P: its function list, dependency count and executable set gain exactly the operator's function.
_post = _once(_post, "AND d.objid IN (quarantining, reinstating))",
              "AND d.objid IN (quarantining, reinstating, deleting))")
_post = _once(_post, "AND d.refobjid = operator AND d.dbid IN (0, here)) <> 3",
              "AND d.refobjid = operator AND d.dbid IN (0, here)) <> 4")
_post = _once(_post, "AND p.oid NOT IN (quarantining, reinstating))", "AND p.oid NOT IN (quarantining, reinstating, deleting))")
POSTCONDITION = _once(_post, "'V1_QUARANTINE_POSTCONDITION_INVALID'", "'V1_DELETION_REQUEST_POSTCONDITION_INVALID'")

ABSENT = """DO $v1_deletion_request_absent$
BEGIN
 IF EXISTS (SELECT 1 FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'kalillac' AND p.proname IN ('v1_request_deletion', 'v1_request_own_deletion')) THEN
  RAISE EXCEPTION USING MESSAGE = %(message)s;
 END IF;
END $v1_deletion_request_absent$"""

NO_DELETION_EVENTS = """DO $v1_deletion_request_history$
BEGIN
 IF EXISTS (SELECT 1 FROM kalillac.account_security_events WHERE event_type = 'account_deletion_request') THEN
  RAISE EXCEPTION USING MESSAGE = 'V1_DELETION_REQUEST_DOWNGRADE_REFUSED';
 END IF;
END $v1_deletion_request_history$"""


def _names():
    return _quarantine._names()


def _preflight(names, message):
    """0022's exact catalog (as 0022 proves it, guard v3 and P included), 0021's guard, and no deletion
    function."""
    return [_quarantine._postcondition(names).replace("'V1_QUARANTINE_POSTCONDITION_INVALID'", "'" + message + "'"),
            _quarantine._activation.POSTCONDITION.replace("'REGISTRATION_ACTIVATION_GUARD_POSTCONDITION_INVALID'",
                                                          "'" + message + "'"),
            ABSENT % {"message": "'" + message + "'"}]


def _postcondition(names):
    return _change._reset._render(POSTCONDITION, names, EXECUTOR_COLUMN_GRANTS)


def upgrade_statements(names):
    """Every statement 0023 runs, in order, for validated role names."""
    executor, service, operator = names["executor"], names["service"], names["operator"]
    return [
        *_preflight(names, "V1_DELETION_REQUEST_PREREQUISITE_INVALID"),
        *_change._constraint(EVENT_TYPES),
        # Temporary: the owner can transfer ownership only to a role with CREATE.
        f"GRANT CREATE ON SCHEMA kalillac TO {executor}",
        REQUEST_FUNCTION,
        OWN_FUNCTION,
        f"REVOKE ALL ON FUNCTION {REQUEST_SIGNATURE} FROM PUBLIC",
        f"REVOKE ALL ON FUNCTION {OWN_SIGNATURE} FROM PUBLIC",
        f"GRANT EXECUTE ON FUNCTION {REQUEST_SIGNATURE} TO {operator}",
        f"GRANT EXECUTE ON FUNCTION {OWN_SIGNATURE} TO {service}",
        # Through the operator's owner->E SET edge; the ACLs are rewritten to E.
        f"ALTER FUNCTION {REQUEST_SIGNATURE} OWNER TO {executor}",
        f"ALTER FUNCTION {OWN_SIGNATURE} OWNER TO {executor}",
        f"GRANT UPDATE (deletion_requested_at) ON kalillac.users TO {executor}",
        f"REVOKE CREATE ON SCHEMA kalillac FROM {executor}",
        _postcondition(names),
    ]


def downgrade_statements(names):
    """From the exact 0023 catalog back to the exact 0022 one. Refused while any deletion-request event
    exists: 0022's constraint cannot hold it, and audit history is never deleted to step down."""
    executor, owner = names["executor"], names["owner"]
    return [
        _postcondition(names),
        NO_DELETION_EVENTS,
        f"REVOKE UPDATE (deletion_requested_at) ON kalillac.users FROM {executor}",
        f"GRANT CREATE ON SCHEMA kalillac TO {executor}",
        f"SET ROLE {executor}",
        f"DROP FUNCTION {REQUEST_SIGNATURE}",
        f"DROP FUNCTION {OWN_SIGNATURE}",
        f"SET ROLE {owner}",
        f"REVOKE CREATE ON SCHEMA kalillac FROM {executor}",
        *_change._constraint(EVENT_TYPES_BEFORE),
        *_preflight(names, "V1_DELETION_REQUEST_DOWNGRADE_INVALID"),
    ]


def upgrade():
    if op.get_context().dialect.name != "postgresql":
        return
    for statement in upgrade_statements(_names()):
        op.execute(statement)


def downgrade():
    if op.get_context().as_sql:
        # Offline SQL cannot see the catalog it would change.
        raise RuntimeError("V1_DELETION_REQUEST_OFFLINE_DOWNGRADE_UNSUPPORTED")
    if op.get_context().dialect.name != "postgresql":
        return
    for statement in downgrade_statements(_names()):
        op.execute(statement)
