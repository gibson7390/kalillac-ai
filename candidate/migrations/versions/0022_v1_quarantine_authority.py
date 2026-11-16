"""Prepare quarantine and reinstatement (V1-02D), operated by role P, unexposed.

Revision ID: 0022_v1_quarantine
Revises: 0021_registration_activation

PostgreSQL only. It installs identity guard v3, two E-owned SECURITY DEFINER functions executable only by
the operator role P, E's UPDATE on users.quarantined_at, P's USAGE on the schema, and widens the audit
event types by 'account_quarantine' and 'account_reinstate'. No table, column, index or data changes.

* Role P is NOLOGIN, operator-provisioned and named by KALILLAC_V1_SECURITY_OPERATOR_ROLE: no attribute,
  no membership, distinct from S, E, R, A and the owner. This revision verifies it and never creates or
  changes a role. S cannot execute the operator functions; P can execute nothing else.
* Identity guard v3 is v2 plus exactly two `quarantined_at` transitions: quarantine (set from NULL while
  the epoch moves strictly forward to at least the quarantine time, so every session ends) and
  reinstatement (cleared, nothing else moved). It replaces v2 in place (same OID, owner, ACL and trigger
  binding), by 0014's reviewed procedure.
* `v1_quarantine(op, account)`: locks the account, reports a recorded operation again, refuses an
  unknown or already quarantined account, sets `quarantined_at` and the epoch to one effective time,
  revokes every session and records 'account_quarantine'.
* `v1_reinstate(op, account)`: locks the account, reports a recorded operation again, refuses one that is
  not quarantined, clears `quarantined_at` and records 'account_reinstate'. Sessions stay ended.

The plan's reason code is not stored: the audit table has no column for it, and adding one is a table
change this revision avoids; the operator tool (D4) records the reason with the operation id instead.
The downgrade refuses while either new event exists. V1 startup still refuses.
"""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

from alembic import op

from kalillac_accounts.registration_schema import identity_guard_v2, identity_guard_v3

revision = "0022_v1_quarantine"
down_revision = "0021_registration_activation"
branch_labels = depends_on = None


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_issuance = _load("v1_0014_for_0022", "0014_v1_session_issuance_epoch.py")
_change = _load("v1_0020_for_0022", "0020_v1_password_change_authority.py")
_activation = _load("registration_0021_for_0022", "0021_registration_activation_guard.py")

QUARANTINE_SIGNATURE = "kalillac.v1_quarantine(uuid, uuid)"
REINSTATE_SIGNATURE = "kalillac.v1_reinstate(uuid, uuid)"
EVENT_TYPES_BEFORE = _change.EVENT_TYPES
EVENT_TYPES = EVENT_TYPES_BEFORE + ("account_quarantine", "account_reinstate")
ADDED_COLUMN_GRANTS = {("users", "UPDATE"): ("quarantined_at",)}
EXECUTOR_TABLE_GRANTS = _change.EXECUTOR_TABLE_GRANTS
EXECUTOR_COLUMN_GRANTS = {key: tuple(_change.EXECUTOR_COLUMN_GRANTS.get(key, ()))
                          + tuple(c for c in ADDED_COLUMN_GRANTS.get(key, ())
                                  if c not in _change.EXECUTOR_COLUMN_GRANTS.get(key, ()))
                          for key in {**_change.EXECUTOR_COLUMN_GRANTS, **ADDED_COLUMN_GRANTS}}
GUARD_V2, GUARD_V3 = identity_guard_v2(_issuance.MARKER), identity_guard_v3(_issuance.MARKER)

# Static SQL only; nothing is logged or raised with the inputs. Expected outcomes are returned.
QUARANTINE_BODY = """
DECLARE
 v_quarantined timestamptz;
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
  SELECT u.quarantined_at, u.sessions_valid_after INTO v_quarantined, v_epoch
   FROM kalillac.users u WHERE u.id = p_user_id FOR UPDATE;
  IF NOT FOUND THEN
   outcome := 'not_found';
   RETURN NEXT;
   RETURN;
  END IF;
  SELECT e.user_id, e.event_type INTO v_event_user, v_event_type
   FROM kalillac.account_security_events e WHERE e.operation_id = p_operation_id;
  IF FOUND THEN
   IF v_event_user = p_user_id AND v_event_type = 'account_quarantine' THEN
    outcome := 'quarantined';
    replayed := true;
   END IF;
   RETURN NEXT;
   RETURN;
  END IF;
  IF v_quarantined IS NOT NULL THEN
   outcome := 'already_quarantined';
   RETURN NEXT;
   RETURN;
  END IF;
  -- One effective time, strictly after the current epoch: every session ends.
  v_at := greatest(clock_timestamp(), v_epoch + interval '1 microsecond');
  UPDATE kalillac.users SET quarantined_at = v_at, sessions_valid_after = v_at WHERE id = p_user_id;
  UPDATE kalillac.account_sessions SET revoked_at = v_at WHERE user_id = p_user_id AND revoked_at IS NULL;
  INSERT INTO kalillac.account_security_events (operation_id, user_id, event_type, occurred_at)
   VALUES (p_operation_id, p_user_id, 'account_quarantine', v_at);
  outcome := 'quarantined';
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

REINSTATE_BODY = """
DECLARE
 v_quarantined timestamptz;
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
  SELECT u.quarantined_at INTO v_quarantined FROM kalillac.users u WHERE u.id = p_user_id FOR UPDATE;
  IF NOT FOUND THEN
   outcome := 'not_found';
   RETURN NEXT;
   RETURN;
  END IF;
  SELECT e.user_id, e.event_type INTO v_event_user, v_event_type
   FROM kalillac.account_security_events e WHERE e.operation_id = p_operation_id;
  IF FOUND THEN
   IF v_event_user = p_user_id AND v_event_type = 'account_reinstate' THEN
    outcome := 'reinstated';
    replayed := true;
   END IF;
   RETURN NEXT;
   RETURN;
  END IF;
  IF v_quarantined IS NULL THEN
   outcome := 'not_quarantined';
   RETURN NEXT;
   RETURN;
  END IF;
  v_at := greatest(clock_timestamp(), v_quarantined);
  UPDATE kalillac.users SET quarantined_at = NULL WHERE id = p_user_id;
  INSERT INTO kalillac.account_security_events (operation_id, user_id, event_type, occurred_at)
   VALUES (p_operation_id, p_user_id, 'account_reinstate', v_at);
  outcome := 'reinstated';
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

_HEAD = (" RETURNS TABLE(outcome text, replayed boolean) LANGUAGE plpgsql VOLATILE SECURITY DEFINER PARALLEL UNSAFE"
         " SET search_path = pg_catalog, pg_temp AS ")
QUARANTINE_FUNCTION = ("CREATE FUNCTION kalillac.v1_quarantine(p_operation_id uuid, p_user_id uuid)" + _HEAD
                       + "$v1_quarantine$" + QUARANTINE_BODY + "$v1_quarantine$")
REINSTATE_FUNCTION = ("CREATE FUNCTION kalillac.v1_reinstate(p_operation_id uuid, p_user_id uuid)" + _HEAD
                      + "$v1_reinstate$" + REINSTATE_BODY + "$v1_reinstate$")


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _once(template, old, new, count=1):
    if template.count(old) != count:
        raise RuntimeError("V1_QUARANTINE_POSTCONDITION_UNRECOGNIZED")
    return template.replace(old, new)


_FUNCTIONS = "(fn, issue, ending, resetting, changing)"
_SERVICE_ALLOWED = "AND d.objid IN " + _FUNCTIONS + ")))))"
_RESULT = "TABLE(outcome text, replayed boolean)"

_post = _change.POSTCONDITION.replace("$v1_password_change_postcondition$", "$v1_quarantine_postcondition$")
_post = _once(_post, " changing oid := pg_catalog.to_regprocedure('kalillac.v1_change_password(uuid,uuid,text,text,text)');",
              " changing oid := pg_catalog.to_regprocedure('kalillac.v1_change_password(uuid,uuid,text,text,text)');\n"
              " quarantining oid := pg_catalog.to_regprocedure('kalillac.v1_quarantine(uuid,uuid)');\n"
              " reinstating oid := pg_catalog.to_regprocedure('kalillac.v1_reinstate(uuid,uuid)');\n"
              " operator oid := pg_catalog.to_regrole(pg_catalog.quote_ident(%(operator)s));")
_post = _once(_post, " OR changing IS NULL", " OR changing IS NULL OR quarantining IS NULL OR reinstating IS NULL")
# S's own allowed dependencies stay S's functions; every other list gains the operator functions.
_post = _once(_post, _SERVICE_ALLOWED, "@SERVICE_ALLOWED@")
_post = _once(_post, _FUNCTIONS, "(fn, issue, ending, resetting, changing, quarantining, reinstating)", count=5)
_post = _once(_post, "@SERVICE_ALLOWED@", _SERVICE_ALLOWED)
_post = _once(_post, " (changing, service, 'EXECUTE', false, executor)))",
              " (changing, service, 'EXECUTE', false, executor),\n"
              "                           (quarantining, executor, 'EXECUTE', false, executor),"
              " (quarantining, operator, 'EXECUTE', false, executor),\n"
              "                           (reinstating, executor, 'EXECUTE', false, executor),"
              " (reinstating, operator, 'EXECUTE', false, executor)))")
_post = _once(_post, "WHERE p.oid IN (fn, issue, ending, resetting, changing, quarantining, reinstating)) <> 10",
              "WHERE p.oid IN (fn, issue, ending, resetting, changing, quarantining, reinstating)) <> 14")
_post = _once(_post, "AND d.deptype = 'o') <> 5", "AND d.deptype = 'o') <> 7")
_post = _once(_post, " OR EXISTS (SELECT 1 FROM pg_catalog.pg_proc p WHERE p.oid IN "
                     "(fn, issue, ending, resetting, changing, quarantining, reinstating) AND p.proowner",
              "".join(f" OR (SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(p.prosrc, 'UTF8')), 'hex')\n"
                      f"     FROM pg_catalog.pg_proc p WHERE p.oid = {name}) IS DISTINCT FROM '{_sha(body)}'\n"
                      f" OR (SELECT pg_catalog.pg_get_function_result(p.oid) FROM pg_catalog.pg_proc p WHERE p.oid = {name})\n"
                      f"    IS DISTINCT FROM '{_RESULT}'\n"
                      for name, body in (("quarantining", QUARANTINE_BODY), ("reinstating", REINSTATE_BODY)))
              + " OR EXISTS (SELECT 1 FROM pg_catalog.pg_proc p WHERE p.oid IN "
                "(fn, issue, ending, resetting, changing, quarantining, reinstating) AND p.proowner")
_post = _once(_post, "'" + _change._rendered(_change.EVENT_TYPES).replace("'", "''") + "'",
              "'" + _change._rendered(EVENT_TYPES).replace("'", "''") + "'")
# Role P: exact attributes, no membership, distinct, and only its schema USAGE and the two EXECUTEs.
_OPERATOR_CHECKS = """
 OR operator IS NULL
 OR operator IN (service, executor, runtime, registration, keeper)
 OR EXISTS (SELECT 1 FROM pg_catalog.pg_roles r WHERE r.oid = operator
            AND (r.rolcanlogin OR r.rolinherit OR r.rolsuper OR r.rolcreatedb OR r.rolcreaterole
                 OR r.rolreplication OR r.rolbypassrls))
 OR EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m WHERE m.roleid = operator OR m.member = operator)
 OR EXISTS (SELECT 1 FROM pg_catalog.pg_shdepend d WHERE d.refclassid = 'pg_catalog.pg_authid'::pg_catalog.regclass
            AND d.refobjid = operator AND d.dbid IN (0, here)
            AND NOT (d.deptype = 'a' AND ((d.classid = 'pg_catalog.pg_proc'::pg_catalog.regclass
                                           AND d.objid IN (quarantining, reinstating))
                                          OR d.classid = 'pg_catalog.pg_namespace'::pg_catalog.regclass)))
 OR (SELECT pg_catalog.count(*) FROM pg_catalog.pg_shdepend d WHERE d.refclassid = 'pg_catalog.pg_authid'::pg_catalog.regclass
     AND d.refobjid = operator AND d.dbid IN (0, here)) <> 3
 OR NOT pg_catalog.has_schema_privilege(operator, 'kalillac', 'USAGE')
 OR pg_catalog.has_schema_privilege(operator, 'kalillac', 'CREATE')
 OR EXISTS (SELECT 1 FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'kalillac' AND pg_catalog.has_function_privilege(operator, p.oid, 'EXECUTE')
            AND p.oid NOT IN (quarantining, reinstating))
 THEN RAISE EXCEPTION USING MESSAGE = 'V1_PASSWORD_CHANGE_POSTCONDITION_INVALID';"""
_post = _once(_post, "\n THEN RAISE EXCEPTION USING MESSAGE = 'V1_PASSWORD_CHANGE_POSTCONDITION_INVALID';", _OPERATOR_CHECKS)
POSTCONDITION = _once(_post, "'V1_PASSWORD_CHANGE_POSTCONDITION_INVALID'", "'V1_QUARANTINE_POSTCONDITION_INVALID'")

ABSENT = """DO $v1_quarantine_absent$
DECLARE
 operator oid := pg_catalog.to_regrole(pg_catalog.quote_ident(%(operator)s));
BEGIN
 IF EXISTS (SELECT 1 FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'kalillac' AND p.proname IN ('v1_quarantine', 'v1_reinstate'))
    OR operator IS NULL
    OR EXISTS (SELECT 1 FROM pg_catalog.pg_roles r WHERE r.oid = operator
               AND (r.rolcanlogin OR r.rolinherit OR r.rolsuper OR r.rolcreatedb OR r.rolcreaterole
                    OR r.rolreplication OR r.rolbypassrls))
    OR EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m WHERE m.roleid = operator OR m.member = operator)
    OR EXISTS (SELECT 1 FROM pg_catalog.pg_shdepend d WHERE d.refclassid = 'pg_catalog.pg_authid'::pg_catalog.regclass
               AND d.refobjid = operator AND d.dbid IN (0, (SELECT oid FROM pg_catalog.pg_database
                                                           WHERE datname = pg_catalog.current_database()))) THEN
  RAISE EXCEPTION USING MESSAGE = %(message)s;
 END IF;
END $v1_quarantine_absent$"""

NO_OPERATOR_EVENTS = """DO $v1_quarantine_history$
BEGIN
 IF EXISTS (SELECT 1 FROM kalillac.account_security_events
            WHERE event_type IN ('account_quarantine', 'account_reinstate')) THEN
  RAISE EXCEPTION USING MESSAGE = 'V1_QUARANTINE_DOWNGRADE_REFUSED';
 END IF;
END $v1_quarantine_history$"""


def _names():
    from kalillac_db.migration_roles import v1_security_operator_role
    return {**_change._names(), "operator": v1_security_operator_role()}


def _guard_swap(source, target, message):
    return _issuance._swap(source, target).replace("'V1_ISSUANCE_GUARD_INVALID'", "'" + message + "'")


def _preflight(names, message):
    """0020's exact catalog (as 0020 proves it), 0021's guard, identity guard v2, and P provisioned and
    holding nothing; no operator function."""
    return [_change._postcondition(names).replace("'V1_PASSWORD_CHANGE_POSTCONDITION_INVALID'", "'" + message + "'"),
            _activation.POSTCONDITION.replace("'REGISTRATION_ACTIVATION_GUARD_POSTCONDITION_INVALID'", "'" + message + "'"),
            ABSENT % {"message": "'" + message + "'", "operator": "'" + names["operator"] + "'"}]


def _postcondition(names):
    return _change._reset._render(POSTCONDITION, names, EXECUTOR_COLUMN_GRANTS)


def upgrade_statements(names):
    """Every statement 0022 runs, in order, for validated role names."""
    executor, operator = names["executor"], names["operator"]
    return [
        *_preflight(names, "V1_QUARANTINE_PREREQUISITE_INVALID"),
        *_change._constraint(EVENT_TYPES),
        _guard_swap(GUARD_V2, GUARD_V3, "V1_QUARANTINE_GUARD_INVALID"),
        f"GRANT CREATE ON SCHEMA kalillac TO {executor}",
        QUARANTINE_FUNCTION,
        REINSTATE_FUNCTION,
        f"REVOKE ALL ON FUNCTION {QUARANTINE_SIGNATURE} FROM PUBLIC",
        f"REVOKE ALL ON FUNCTION {REINSTATE_SIGNATURE} FROM PUBLIC",
        f"GRANT EXECUTE ON FUNCTION {QUARANTINE_SIGNATURE} TO {operator}",
        f"GRANT EXECUTE ON FUNCTION {REINSTATE_SIGNATURE} TO {operator}",
        f"ALTER FUNCTION {QUARANTINE_SIGNATURE} OWNER TO {executor}",
        f"ALTER FUNCTION {REINSTATE_SIGNATURE} OWNER TO {executor}",
        f"GRANT USAGE ON SCHEMA kalillac TO {operator}",
        f"GRANT UPDATE (quarantined_at) ON kalillac.users TO {executor}",
        f"REVOKE CREATE ON SCHEMA kalillac FROM {executor}",
        _postcondition(names),
    ]


def downgrade_statements(names):
    """From the exact 0022 catalog back to the exact 0021 one. Refused while any quarantine or
    reinstatement event exists (audit history is never deleted to step down). A quarantined account
    stays quarantined under guard v2, which only freezes the column."""
    executor, owner, operator = names["executor"], names["owner"], names["operator"]
    return [
        _postcondition(names),
        NO_OPERATOR_EVENTS,
        f"REVOKE UPDATE (quarantined_at) ON kalillac.users FROM {executor}",
        f"REVOKE USAGE ON SCHEMA kalillac FROM {operator}",
        f"GRANT CREATE ON SCHEMA kalillac TO {executor}",
        f"SET ROLE {executor}",
        f"DROP FUNCTION {QUARANTINE_SIGNATURE}",
        f"DROP FUNCTION {REINSTATE_SIGNATURE}",
        f"SET ROLE {owner}",
        f"REVOKE CREATE ON SCHEMA kalillac FROM {executor}",
        _guard_swap(GUARD_V3, GUARD_V2, "V1_QUARANTINE_DOWNGRADE_INVALID"),
        *_change._constraint(EVENT_TYPES_BEFORE),
        *_preflight(names, "V1_QUARANTINE_DOWNGRADE_INVALID"),
    ]


def upgrade():
    if op.get_context().dialect.name != "postgresql":
        return
    for statement in upgrade_statements(_names()):
        op.execute(statement)


def downgrade():
    if op.get_context().as_sql:
        raise RuntimeError("V1_QUARANTINE_OFFLINE_DOWNGRADE_UNSUPPORTED")
    if op.get_context().dialect.name != "postgresql":
        return
    for statement in downgrade_statements(_names()):
        op.execute(statement)
