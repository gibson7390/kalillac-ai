"""Prepare the private password-reset authority (V1-02D, D2 second part), unexposed.

Revision ID: 0019_v1_password_reset
Revises: 0018_v1_session_end

PostgreSQL only; one function, its ACL and E's column grants for it (no table, column, index,
trigger, view, rule, sequence, event type or data). It installs kalillac.v1_reset_password, a fixed
SECURITY DEFINER interface that completes account recovery: given the digest of a reset token, a new
Argon2id hash the caller computed, and a caller-generated operation id, it

- locks the account row first (as revocation and issuance do), then the token, and re-checks the
  token under database time: unknown, consumed, expired or of an inactive or deleted account all
  answer the same 'invalid_token'; a quarantined account answers 'account_unavailable';
- charges the 'recovery' admission class that 0012 reserved, with exactly revocation's windows
  (per account 2 a minute and 6 an hour, all accounts 60 an hour), answering
  'too_many_operations' with the seconds to wait;
- replaces the password hash, clears password_reset_required (the only place it is cleared),
  consumes every open reset token of the account, revokes every session, advances the session epoch
  strictly forward, and records the 'password_reset' event 0012 reserved, all at one effective time;
- reports a recorded operation again ('reset', replayed) and never applies it twice.

Identity guard v2 already allows exactly the forward epoch move; password_hash and
password_reset_required are not guarded columns, so E needs only column grants: UPDATE on those two
users columns, and SELECT and UPDATE (consumed_at) on the reset tokens. E owns the function and only
S may execute it. No role or membership changes, nothing the application can call is added, and V1
startup still refuses.

The legacy recovery route keeps working unchanged until V1-02E removes it.
"""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

from alembic import op

revision = "0019_v1_password_reset"
down_revision = "0018_v1_session_end"
branch_labels = depends_on = None


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_revocation = _load("v1_0013_session_revocation", "0013_v1_session_revocation_authority.py")
_issuance = _load("v1_0014_session_issuance", "0014_v1_session_issuance_epoch.py")
_ending = _load("v1_0018_session_end", "0018_v1_session_end_authority.py")

SIGNATURE = "kalillac.v1_reset_password(uuid, text, text)"
# E's added authority: the two users columns reset writes, and the reset tokens it reads and consumes.
ADDED_COLUMN_GRANTS = {("users", "UPDATE"): ("password_hash", "password_reset_required"),
                       ("account_recovery_tokens", "SELECT"): ("id", "user_id", "purpose", "token_hash",
                                                               "expires_at", "consumed_at"),
                       ("account_recovery_tokens", "UPDATE"): ("consumed_at",)}
EXECUTOR_TABLE_GRANTS = _issuance.EXECUTOR_TABLE_GRANTS
EXECUTOR_COLUMN_GRANTS = {key: tuple(_issuance.EXECUTOR_COLUMN_GRANTS.get(key, ()))
                          + tuple(c for c in ADDED_COLUMN_GRANTS.get(key, ())
                                  if c not in _issuance.EXECUTOR_COLUMN_GRANTS.get(key, ()))
                          for key in {**_issuance.EXECUTOR_COLUMN_GRANTS, **ADDED_COLUMN_GRANTS}}


def _between(text, start, end):
    """The part of text from start up to and including end, each present exactly once."""
    if text.count(start) != 1 or text.count(end) != 1 or text.index(start) > text.index(end):
        raise RuntimeError("V1_PASSWORD_RESET_ADMISSION_UNRECOGNIZED")
    return text[text.index(start):text.index(end) + len(end)]


def _swap(text, old, new):
    if text.count(old) != 1:
        raise RuntimeError("V1_PASSWORD_RESET_ADMISSION_UNRECOGNIZED")
    return text.replace(old, new)


# Revocation's reviewed admission, verbatim except for its class: the 'recovery' aggregate and the
# account's recovery charges instead of the manual ones. Its expiry recheck now reads the token's.
ADMISSION = _between(_revocation.BODY,
                     "  INSERT INTO kalillac.account_security_admission_aggregate",
                     "   SET manual_charges = v_account || v_at, last_seen_at = v_at WHERE entry_slot = v_slot;\n")
for _old, _new in (("VALUES ('manual', '{}', v_now)", "VALUES ('recovery', '{}', v_now)"),
                   ("WHERE a.admission_class = 'manual' FOR UPDATE", "WHERE a.admission_class = 'recovery' FOR UPDATE"),
                   ("SELECT m.entry_slot, m.manual_charges, m.last_seen_at", "SELECT m.entry_slot, m.recovery_charges, m.last_seen_at"),
                   ("last_seen_at = v_at WHERE admission_class = 'manual';", "last_seen_at = v_at WHERE admission_class = 'recovery';"),
                   ("   SET manual_charges = v_account || v_at, last_seen_at = v_at WHERE entry_slot = v_slot;\n",
                    "   SET recovery_charges = v_account || v_at, last_seen_at = v_at WHERE entry_slot = v_slot;\n")):
    ADMISSION = _swap(ADMISSION, _old, _new)
# Only entry allocation may still name the manual ring: it checks, clears or creates both rings of an
# entry together.
if "manual" in (ADMISSION.replace("manual_charges = '{}'", "").replace("m.manual_charges || m.recovery_charges", "")
                .replace("user_id, manual_charges, recovery_charges,", "")):
    raise RuntimeError("V1_PASSWORD_RESET_ADMISSION_UNRECOGNIZED")

# Static SQL only. The trusted inputs are the token digest (never the token), the hash the caller
# computed from the new password (never the password), and the operation id. Nothing is logged,
# raised or recorded with them. Expected outcomes are returned, never raised; timeouts, cancellation
# and deadlocks propagate and roll the whole call back.
BODY = """
DECLARE
 v_user uuid;
 v_active boolean;
 v_quarantined timestamptz;
 v_deletion timestamptz;
 v_epoch timestamptz;
 v_purpose text;
 v_expires timestamptz;
 v_consumed timestamptz;
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
 IF p_operation_id IS NULL OR p_token_hash IS NULL OR p_token_hash !~ '^[0-9a-f]{64}$'
    OR p_new_password_hash IS NULL OR length(p_new_password_hash) > 255
    OR p_new_password_hash !~ '^[$]argon2id[$]v=19[$]m=[0-9]{1,10},t=[0-9]{1,10},p=[0-9]{1,3}[$][A-Za-z0-9+/]{11,}[$][A-Za-z0-9+/]{16,}$' THEN
  RETURN NEXT;
  RETURN;
 END IF;
 BEGIN
  outcome := 'invalid_token';
  SELECT t.user_id INTO v_user FROM kalillac.account_recovery_tokens t WHERE t.token_hash = p_token_hash;
  IF NOT FOUND THEN
   RETURN NEXT;
   RETURN;
  END IF;
  -- The account row first, as revocation and issuance lock it.
  SELECT u.is_active, u.quarantined_at, u.deletion_requested_at, u.sessions_valid_after
   INTO v_active, v_quarantined, v_deletion, v_epoch
   FROM kalillac.users u WHERE u.id = v_user FOR UPDATE;
  IF NOT FOUND THEN
   RETURN NEXT;
   RETURN;
  END IF;
  -- A recorded operation is reported again, never applied or charged twice.
  SELECT e.user_id, e.event_type INTO v_event_user, v_event_type
   FROM kalillac.account_security_events e WHERE e.operation_id = p_operation_id;
  IF FOUND THEN
   IF v_event_user = v_user AND v_event_type = 'password_reset' THEN
    outcome := 'reset';
    replayed := true;
   ELSE
    outcome := 'security_unavailable';
   END IF;
   RETURN NEXT;
   RETURN;
  END IF;
  SELECT t.purpose, t.expires_at, t.consumed_at INTO v_purpose, v_expires, v_consumed
   FROM kalillac.account_recovery_tokens t
   WHERE t.token_hash = p_token_hash AND t.user_id = v_user FOR UPDATE;
  v_now := clock_timestamp();
  IF NOT FOUND OR v_purpose IS DISTINCT FROM 'password_reset' OR v_consumed IS NOT NULL OR v_expires <= v_now
     OR NOT v_active OR v_deletion IS NOT NULL THEN
   RETURN NEXT;
   RETURN;
  END IF;
  IF v_quarantined IS NOT NULL THEN
   outcome := 'account_unavailable';
   RETURN NEXT;
   RETURN;
  END IF;
""" + ADMISSION + """  -- One effective time for every effect; the epoch moves strictly forward.
  UPDATE kalillac.users SET password_hash = p_new_password_hash, password_reset_required = false,
   sessions_valid_after = greatest(v_at, v_epoch + interval '1 microsecond')
   WHERE id = v_user;
  UPDATE kalillac.account_recovery_tokens SET consumed_at = v_at
   WHERE user_id = v_user AND purpose = 'password_reset' AND consumed_at IS NULL;
  UPDATE kalillac.account_sessions SET revoked_at = v_at WHERE user_id = v_user AND revoked_at IS NULL;
  INSERT INTO kalillac.account_security_events (operation_id, user_id, event_type, occurred_at)
   VALUES (p_operation_id, v_user, 'password_reset', v_at);
  outcome := 'reset';
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

FUNCTION = ("CREATE FUNCTION kalillac.v1_reset_password(p_operation_id uuid, p_token_hash text,"
            " p_new_password_hash text)"
            " RETURNS TABLE(outcome text, retry_after_seconds integer, replayed boolean)"
            " LANGUAGE plpgsql VOLATILE SECURITY DEFINER PARALLEL UNSAFE"
            " SET search_path = pg_catalog, pg_temp AS $v1_reset$" + BODY + "$v1_reset$")


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _once(template, old, new, count=1):
    if template.count(old) != count:
        raise RuntimeError("V1_PASSWORD_RESET_POSTCONDITION_UNRECOGNIZED")
    return template.replace(old, new)


# 0018's postcondition extended by the fourth function, with the same exact properties, its own
# body digest and result, owned by E, executable by E and S only.
_post = _ending.POSTCONDITION.replace("$v1_session_end_postcondition$", "$v1_password_reset_postcondition$")
_post = _once(_post, " ending oid := pg_catalog.to_regprocedure('kalillac.v1_end_session(uuid,text)');",
              " ending oid := pg_catalog.to_regprocedure('kalillac.v1_end_session(uuid,text)');\n"
              " resetting oid := pg_catalog.to_regprocedure('kalillac.v1_reset_password(uuid,text,text)');")
_post = _once(_post, " OR ending IS NULL", " OR ending IS NULL OR resetting IS NULL")
_post = _once(_post, "(fn, issue, ending)", "(fn, issue, ending, resetting)", count=6)
_post = _once(_post, " (ending, service, 'EXECUTE', false, executor)))",
              " (ending, service, 'EXECUTE', false, executor),\n"
              "                           (resetting, executor, 'EXECUTE', false, executor),"
              " (resetting, service, 'EXECUTE', false, executor)))")
_post = _once(_post, "WHERE p.oid IN (fn, issue, ending, resetting)) <> 6",
              "WHERE p.oid IN (fn, issue, ending, resetting)) <> 8")
_post = _once(_post, "AND d.deptype = 'o') <> 3", "AND d.deptype = 'o') <> 4")
_post = _once(_post, " OR EXISTS (SELECT 1 FROM pg_catalog.pg_proc p WHERE p.oid IN (fn, issue, ending, resetting)"
                     " AND p.proowner",
              " OR (SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(p.prosrc, 'UTF8')), 'hex')\n"
              "     FROM pg_catalog.pg_proc p WHERE p.oid = resetting) IS DISTINCT FROM '" + _sha(BODY) + "'\n"
              " OR (SELECT pg_catalog.pg_get_function_result(p.oid) FROM pg_catalog.pg_proc p WHERE p.oid = resetting)\n"
              "    IS DISTINCT FROM 'TABLE(outcome text, retry_after_seconds integer, replayed boolean)'\n"
              " OR EXISTS (SELECT 1 FROM pg_catalog.pg_proc p WHERE p.oid IN (fn, issue, ending, resetting)"
              " AND p.proowner")
POSTCONDITION = _once(_post, "'V1_SESSION_END_POSTCONDITION_INVALID'", "'V1_PASSWORD_RESET_POSTCONDITION_INVALID'")

ABSENT = """DO $v1_password_reset_absent$
BEGIN
 IF EXISTS (SELECT 1 FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'kalillac' AND p.proname = 'v1_reset_password') THEN
  RAISE EXCEPTION USING MESSAGE = %(message)s;
 END IF;
END $v1_password_reset_absent$"""


def _names():
    return _issuance._names()


def _column_grants(grants):
    by_table = {}
    for (table, right), columns in grants.items():
        by_table.setdefault(table, []).append(f"{right} ({', '.join(columns)})")
    return by_table


def _render(template, names, column_grants):
    table_grants = ", ".join(f"('kalillac', '{table}', '{right}')"
                             for table, rights in EXECUTOR_TABLE_GRANTS.items() for right in rights)
    columns = ", ".join(f"('{table}', '{column}', '{right}')"
                        for (table, right), cols in column_grants.items() for column in cols)
    return template % {"table_grants": table_grants, "column_grants": columns,
                       **{key: "'" + name + "'" for key, name in names.items()}}


def _preflight(names, message):
    """The exact 0018 catalog, as 0018 itself proves it, and no reset function in any form."""
    return [_ending._postcondition(names).replace("'V1_SESSION_END_POSTCONDITION_INVALID'", "'" + message + "'"),
            ABSENT % {"message": "'" + message + "'"}]


def _postcondition(names):
    return _render(POSTCONDITION, names, EXECUTOR_COLUMN_GRANTS)


def upgrade_statements(names):
    """Every statement 0019 runs, in order, for validated role names."""
    executor, service = names["executor"], names["service"]
    return [
        *_preflight(names, "V1_PASSWORD_RESET_PREREQUISITE_INVALID"),
        # Temporary: the owner can transfer ownership only to a role with CREATE.
        f"GRANT CREATE ON SCHEMA kalillac TO {executor}",
        FUNCTION,
        f"REVOKE ALL ON FUNCTION {SIGNATURE} FROM PUBLIC",
        f"GRANT EXECUTE ON FUNCTION {SIGNATURE} TO {service}",
        # Through the operator's owner->E SET edge; the ACL is rewritten to E.
        f"ALTER FUNCTION {SIGNATURE} OWNER TO {executor}",
        *(f"GRANT {', '.join(grants)} ON kalillac.{table} TO {executor}"
          for table, grants in _column_grants(ADDED_COLUMN_GRANTS).items()),
        f"REVOKE CREATE ON SCHEMA kalillac FROM {executor}",
        _postcondition(names),
    ]


def downgrade_statements(names):
    """From the exact 0019 catalog back to the exact 0018 one. Completed resets stay: new hashes,
    consumed tokens, revoked sessions, advanced epochs and their events remain valid 0018 data."""
    executor, owner = names["executor"], names["owner"]
    return [
        _postcondition(names),
        *(f"REVOKE {', '.join(grants)} ON kalillac.{table} FROM {executor}"
          for table, grants in _column_grants(ADDED_COLUMN_GRANTS).items()),
        f"GRANT CREATE ON SCHEMA kalillac TO {executor}",
        f"SET ROLE {executor}",
        f"DROP FUNCTION {SIGNATURE}",
        f"SET ROLE {owner}",
        f"REVOKE CREATE ON SCHEMA kalillac FROM {executor}",
        *_preflight(names, "V1_PASSWORD_RESET_DOWNGRADE_INVALID"),
    ]


def upgrade():
    if op.get_context().dialect.name != "postgresql":
        return
    for statement in upgrade_statements(_names()):
        op.execute(statement)


def downgrade():
    if op.get_context().as_sql:
        # Offline SQL cannot see the catalog it would change.
        raise RuntimeError("V1_PASSWORD_RESET_OFFLINE_DOWNGRADE_UNSUPPORTED")
    if op.get_context().dialect.name != "postgresql":
        return
    for statement in downgrade_statements(_names()):
        op.execute(statement)
