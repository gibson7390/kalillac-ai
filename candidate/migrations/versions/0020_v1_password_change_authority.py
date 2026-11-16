"""Prepare the private password-change authority (V1-02D, D2 third part), unexposed.

Revision ID: 0020_v1_password_change
Revises: 0019_v1_password_reset

PostgreSQL only; one function and its ACL, and one widened CHECK constraint (no table, column, index,
trigger, view, rule, sequence, grant on data or data). It installs kalillac.v1_change_password, a
fixed SECURITY DEFINER interface for a signed-in customer changing their password. Given a
caller-generated operation id, the exact original session (its id and the digest of the token the
caller holds), the stored password hash the caller has just verified the current password against,
and an Argon2id hash the caller computed from the new password, it

- proves the session exactly as revocation does (the account comes only from it; the account row is
  locked first, then the session row), applies V1-01's live authorization in the same order, and
  answers 'not_authenticated' uniformly when the session is not live, the account needs a password
  reset, or the verified hash is no longer the stored one (a concurrent change won); a quarantined
  account answers 'account_unavailable';
- charges the 'manual' admission class with revocation's exact windows (a customer's own security
  operations share one budget), answering 'too_many_operations' with the seconds to wait;
- replaces the password hash, revokes every other session of the account, keeps the presented one,
  and records a 'password_change' event, all at one effective time;
- reports a recorded operation again ('changed', replayed) and never applies it twice.

The session epoch does not move (as with revoking the other sessions): issuance re-checks the stored
hash under the account lock, so a sign-in with the old password that is still in flight is refused
once the change commits, and the presented session must survive.

'password_change' is a new audit event type: 0012's CHECK constraint on account_security_events is
widened in this migration, and the downgrade refuses while any such event exists (audit history is
never deleted to step down). E needs no new grant (0014 and 0019 already gave it every column this
function reads or writes). E owns the function and only S may execute it. No role or membership
changes, nothing the application can call is added, and V1 startup still refuses.
"""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

from alembic import op

revision = "0020_v1_password_change"
down_revision = "0019_v1_password_reset"
branch_labels = depends_on = None


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_revocation = _load("v1_0013_session_revocation", "0013_v1_session_revocation_authority.py")
_reset = _load("v1_0019_password_reset", "0019_v1_password_reset_authority.py")

SIGNATURE = "kalillac.v1_change_password(uuid, uuid, text, text, text)"
EXECUTOR_TABLE_GRANTS = _reset.EXECUTOR_TABLE_GRANTS
EXECUTOR_COLUMN_GRANTS = _reset.EXECUTOR_COLUMN_GRANTS        # Unchanged from 0019.
EVENT_TYPES_BEFORE = ("session_revoke_others", "session_revoke_all", "password_reset")
EVENT_TYPES = EVENT_TYPES_BEFORE + ("password_change",)


def _rendered(values):
    """PostgreSQL 16's rendering of the event-type constraint over values."""
    return ("CHECK (((event_type)::text = ANY ((ARRAY["
            + ", ".join(f"'{value}'::character varying" for value in values) + "])::text[])))")


def _constraint(values):
    return [
        "ALTER TABLE kalillac.account_security_events DROP CONSTRAINT ck_account_security_events_type",
        "ALTER TABLE kalillac.account_security_events ADD CONSTRAINT ck_account_security_events_type CHECK "
        "(event_type IN (" + ",".join("'" + value + "'" for value in values) + "))",
    ]


def _between(text, start, end):
    """The part of text from start up to and including end, each present exactly once."""
    if text.count(start) != 1 or text.count(end) != 1 or text.index(start) > text.index(end):
        raise RuntimeError("V1_PASSWORD_CHANGE_ADMISSION_UNRECOGNIZED")
    return text[text.index(start):text.index(end) + len(end)]


# Revocation's reviewed admission, verbatim: the same 'manual' class and windows.
ADMISSION = _between(_revocation.BODY,
                     "  INSERT INTO kalillac.account_security_admission_aggregate",
                     "   SET manual_charges = v_account || v_at, last_seen_at = v_at WHERE entry_slot = v_slot;\n")

# Static SQL only. The trusted inputs are the session pair (the token's digest, never the token), the
# stored hash the caller verified (compared, never written), the hash the caller computed from the new
# password (never the password) and the operation id. Nothing is logged, raised or recorded with them.
BODY = """
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
    OR p_verified_password_hash IS NULL OR length(p_verified_password_hash) > 255
    OR p_new_password_hash IS NULL OR length(p_new_password_hash) > 255
    OR p_new_password_hash !~ '^[$]argon2id[$]v=19[$]m=[0-9]{1,10},t=[0-9]{1,10},p=[0-9]{1,3}[$][A-Za-z0-9+/]{11,}[$][A-Za-z0-9+/]{16,}$' THEN
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
   IF v_event_user = v_user AND v_event_type = 'password_change' THEN
    outcome := 'changed';
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
""" + ADMISSION + """  -- One effective time for every effect. The presented session stays; every other one ends.
  UPDATE kalillac.users SET password_hash = p_new_password_hash WHERE id = v_user;
  UPDATE kalillac.account_sessions SET revoked_at = v_at
   WHERE user_id = v_user AND revoked_at IS NULL AND id <> p_session_id;
  INSERT INTO kalillac.account_security_events (operation_id, user_id, event_type, occurred_at)
   VALUES (p_operation_id, v_user, 'password_change', v_at);
  outcome := 'changed';
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

FUNCTION = ("CREATE FUNCTION kalillac.v1_change_password(p_operation_id uuid, p_session_id uuid, p_token_hash text,"
            " p_verified_password_hash text, p_new_password_hash text)"
            " RETURNS TABLE(outcome text, retry_after_seconds integer, replayed boolean)"
            " LANGUAGE plpgsql VOLATILE SECURITY DEFINER PARALLEL UNSAFE"
            " SET search_path = pg_catalog, pg_temp AS $v1_change$" + BODY + "$v1_change$")


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _once(template, old, new, count=1):
    if template.count(old) != count:
        raise RuntimeError("V1_PASSWORD_CHANGE_POSTCONDITION_UNRECOGNIZED")
    return template.replace(old, new)


_EVENT_CONSTRAINT = """
 OR (SELECT pg_catalog.count(*) FROM pg_catalog.pg_constraint c
     WHERE c.conrelid = 'kalillac.account_security_events'::pg_catalog.regclass
     AND c.conname = 'ck_account_security_events_type' AND c.contype = 'c' AND c.convalidated
     AND NOT c.condeferrable AND pg_catalog.pg_get_constraintdef(c.oid, false) = @EVENT_TYPES@) <> 1"""

# 0019's postcondition extended by the fifth function, with the same exact properties, its own body
# digest and result, owned by E, executable by E and S only, and the widened event-type constraint.
_post = _reset.POSTCONDITION.replace("$v1_password_reset_postcondition$", "$v1_password_change_postcondition$")
_post = _once(_post, " resetting oid := pg_catalog.to_regprocedure('kalillac.v1_reset_password(uuid,text,text)');",
              " resetting oid := pg_catalog.to_regprocedure('kalillac.v1_reset_password(uuid,text,text)');\n"
              " changing oid := pg_catalog.to_regprocedure('kalillac.v1_change_password(uuid,uuid,text,text,text)');")
_post = _once(_post, " OR resetting IS NULL", " OR resetting IS NULL OR changing IS NULL")
_post = _once(_post, "(fn, issue, ending, resetting)", "(fn, issue, ending, resetting, changing)", count=6)
_post = _once(_post, " (resetting, service, 'EXECUTE', false, executor)))",
              " (resetting, service, 'EXECUTE', false, executor),\n"
              "                           (changing, executor, 'EXECUTE', false, executor),"
              " (changing, service, 'EXECUTE', false, executor)))")
_post = _once(_post, "WHERE p.oid IN (fn, issue, ending, resetting, changing)) <> 8",
              "WHERE p.oid IN (fn, issue, ending, resetting, changing)) <> 10")
_post = _once(_post, "AND d.deptype = 'o') <> 4", "AND d.deptype = 'o') <> 5")
_post = _once(_post, " OR EXISTS (SELECT 1 FROM pg_catalog.pg_proc p WHERE p.oid IN (fn, issue, ending, resetting, changing)"
                     " AND p.proowner",
              " OR (SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(p.prosrc, 'UTF8')), 'hex')\n"
              "     FROM pg_catalog.pg_proc p WHERE p.oid = changing) IS DISTINCT FROM '" + _sha(BODY) + "'\n"
              " OR (SELECT pg_catalog.pg_get_function_result(p.oid) FROM pg_catalog.pg_proc p WHERE p.oid = changing)\n"
              "    IS DISTINCT FROM 'TABLE(outcome text, retry_after_seconds integer, replayed boolean)'"
              + _EVENT_CONSTRAINT.replace("@EVENT_TYPES@", "'" + _rendered(EVENT_TYPES).replace("'", "''") + "'") + "\n"
              " OR EXISTS (SELECT 1 FROM pg_catalog.pg_proc p WHERE p.oid IN (fn, issue, ending, resetting, changing)"
              " AND p.proowner")
POSTCONDITION = _once(_post, "'V1_PASSWORD_RESET_POSTCONDITION_INVALID'", "'V1_PASSWORD_CHANGE_POSTCONDITION_INVALID'")

ABSENT = """DO $v1_password_change_absent$
BEGIN
 IF EXISTS (SELECT 1 FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'kalillac' AND p.proname = 'v1_change_password')
    OR (SELECT pg_catalog.count(*) FROM pg_catalog.pg_constraint c
        WHERE c.conrelid = 'kalillac.account_security_events'::pg_catalog.regclass
        AND c.conname = 'ck_account_security_events_type' AND c.contype = 'c' AND c.convalidated
        AND pg_catalog.pg_get_constraintdef(c.oid, false) = %(before)s) <> 1 THEN
  RAISE EXCEPTION USING MESSAGE = %(message)s;
 END IF;
END $v1_password_change_absent$"""

NO_CHANGE_EVENTS = """DO $v1_password_change_history$
BEGIN
 IF EXISTS (SELECT 1 FROM kalillac.account_security_events WHERE event_type = 'password_change') THEN
  RAISE EXCEPTION USING MESSAGE = 'V1_PASSWORD_CHANGE_DOWNGRADE_REFUSED';
 END IF;
END $v1_password_change_history$"""


def _names():
    return _reset._names()


def _preflight(names, message):
    """The exact 0019 catalog, as 0019 itself proves it, 0012's event types, and no change function."""
    return [_reset._postcondition(names).replace("'V1_PASSWORD_RESET_POSTCONDITION_INVALID'", "'" + message + "'"),
            ABSENT % {"message": "'" + message + "'",
                      "before": "'" + _rendered(EVENT_TYPES_BEFORE).replace("'", "''") + "'"}]


def _postcondition(names):
    return _reset._render(POSTCONDITION, names, EXECUTOR_COLUMN_GRANTS)


def upgrade_statements(names):
    """Every statement 0020 runs, in order, for validated role names."""
    executor, service = names["executor"], names["service"]
    return [
        *_preflight(names, "V1_PASSWORD_CHANGE_PREREQUISITE_INVALID"),
        *_constraint(EVENT_TYPES),
        # Temporary: the owner can transfer ownership only to a role with CREATE.
        f"GRANT CREATE ON SCHEMA kalillac TO {executor}",
        FUNCTION,
        f"REVOKE ALL ON FUNCTION {SIGNATURE} FROM PUBLIC",
        f"GRANT EXECUTE ON FUNCTION {SIGNATURE} TO {service}",
        # Through the operator's owner->E SET edge; the ACL is rewritten to E.
        f"ALTER FUNCTION {SIGNATURE} OWNER TO {executor}",
        f"REVOKE CREATE ON SCHEMA kalillac FROM {executor}",
        _postcondition(names),
    ]


def downgrade_statements(names):
    """From the exact 0020 catalog back to the exact 0019 one. Refused while any password_change event
    exists: 0019's constraint cannot hold it, and audit history is never deleted to step down. Changed
    hashes and revoked sessions remain valid 0019 data."""
    executor, owner = names["executor"], names["owner"]
    return [
        _postcondition(names),
        NO_CHANGE_EVENTS,
        f"GRANT CREATE ON SCHEMA kalillac TO {executor}",
        f"SET ROLE {executor}",
        f"DROP FUNCTION {SIGNATURE}",
        f"SET ROLE {owner}",
        f"REVOKE CREATE ON SCHEMA kalillac FROM {executor}",
        *_constraint(EVENT_TYPES_BEFORE),
        *_preflight(names, "V1_PASSWORD_CHANGE_DOWNGRADE_INVALID"),
    ]


def upgrade():
    if op.get_context().dialect.name != "postgresql":
        return
    for statement in upgrade_statements(_names()):
        op.execute(statement)


def downgrade():
    if op.get_context().as_sql:
        # Offline SQL cannot see the catalog it would change.
        raise RuntimeError("V1_PASSWORD_CHANGE_OFFLINE_DOWNGRADE_UNSUPPORTED")
    if op.get_context().dialect.name != "postgresql":
        return
    for statement in downgrade_statements(_names()):
        op.execute(statement)
