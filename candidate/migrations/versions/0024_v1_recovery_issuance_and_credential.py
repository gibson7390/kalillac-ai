"""Prepare reset-token issuance and the credential read path (V1-02D, D5), through S, unexposed.

Revision ID: 0024_v1_recovery_credential
Revises: 0023_v1_deletion_request

PostgreSQL only. It installs two E-owned SECURITY DEFINER functions, executable only by E and S, and
E's column rights they need: SELECT (email) on users and INSERT on the reset token's own columns. No
table, column, index, trigger, role, membership, event type or data changes; identity guard v3 is
unchanged.

* `v1_issue_reset_token(token_id, email, token_hash)`: the account for exactly this (normalized) email,
  locked; only an active, verified account that is neither quarantined nor deletion-requested gets a token:
  its open reset tokens are consumed and one new token (the digest, never the token) is inserted for 30
  minutes. Everything else answers 'not_issued' and writes nothing. The caller learns only whether to send
  the link; the public response was already given (the recovery request answers 202 before this runs).
* `v1_credential(email)`: the account id and stored password hash for exactly this email, or no row. It
  is the reviewed path by which the application obtains a hash to verify a password against (sign-in,
  password change, the customer's deletion request) once E1 removes the runtime role's read of
  `users.password_hash`. It returns nothing else and writes nothing. Every caller passes that exact hash
  back to an authority, which compares it under the account lock.

Before this revision the runtime role did both. The downgrade drops the two functions and E's added
rights; nothing it removes holds history. V1 startup still refuses.
"""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

from alembic import op

revision = "0024_v1_recovery_credential"
down_revision = "0023_v1_deletion_request"
branch_labels = depends_on = None


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_deletion = _load("v1_0023_for_0024", "0023_v1_deletion_request_authority.py")
_change = _deletion._change

ISSUE_RESET_SIGNATURE = "kalillac.v1_issue_reset_token(uuid, text, text)"
CREDENTIAL_SIGNATURE = "kalillac.v1_credential(text)"
TOKEN_LIFETIME = "30 minutes"            # kalillac_accounts.recovery_tokens.TOKEN_LIFETIME_SECONDS
TOKEN_COLUMNS = ("id", "user_id", "purpose", "token_hash", "created_at", "expires_at")
ADDED_COLUMN_GRANTS = {("users", "SELECT"): ("email",),
                       ("account_recovery_tokens", "INSERT"): TOKEN_COLUMNS}
EXECUTOR_TABLE_GRANTS = _deletion.EXECUTOR_TABLE_GRANTS
EXECUTOR_COLUMN_GRANTS = {key: tuple(_deletion.EXECUTOR_COLUMN_GRANTS.get(key, ()))
                          + tuple(c for c in ADDED_COLUMN_GRANTS.get(key, ())
                                  if c not in _deletion.EXECUTOR_COLUMN_GRANTS.get(key, ()))
                          for key in {**_deletion.EXECUTOR_COLUMN_GRANTS, **ADDED_COLUMN_GRANTS}}

# Static SQL only. The trusted inputs are the token's digest (never the token), the normalized email and
# the new row's id. Nothing is logged, raised or recorded with them. Expected outcomes are returned.
ISSUE_RESET_BODY = """
DECLARE
 v_user uuid;
 v_active boolean;
 v_verified timestamptz;
 v_quarantined timestamptz;
 v_deletion timestamptz;
 v_at timestamptz;
BEGIN
 outcome := 'security_unavailable';
 IF p_token_id IS NULL OR p_email IS NULL OR length(p_email) > 320 OR p_token_hash IS NULL
    OR p_token_hash !~ '^[0-9a-f]{64}$' THEN
  RETURN NEXT;
  RETURN;
 END IF;
 BEGIN
  outcome := 'not_issued';
  SELECT u.id INTO v_user FROM kalillac.users u WHERE u.email = p_email;
  IF NOT FOUND THEN
   RETURN NEXT;
   RETURN;
  END IF;
  -- Locked as every account change is; the email is re-checked under the lock.
  SELECT u.is_active, u.email_verified_at, u.quarantined_at, u.deletion_requested_at
   INTO v_active, v_verified, v_quarantined, v_deletion
   FROM kalillac.users u WHERE u.id = v_user AND u.email = p_email FOR UPDATE;
  IF NOT FOUND OR NOT v_active OR v_verified IS NULL OR v_quarantined IS NOT NULL OR v_deletion IS NOT NULL THEN
   RETURN NEXT;
   RETURN;
  END IF;
  v_at := clock_timestamp();
  UPDATE kalillac.account_recovery_tokens SET consumed_at = v_at
   WHERE user_id = v_user AND purpose = 'password_reset' AND consumed_at IS NULL;
  INSERT INTO kalillac.account_recovery_tokens (id, user_id, purpose, token_hash, created_at, expires_at)
   VALUES (p_token_id, v_user, 'password_reset', p_token_hash, v_at, v_at + interval '""" + TOKEN_LIFETIME + """');
  outcome := 'issued';
  RETURN NEXT;
  RETURN;
 EXCEPTION WHEN unique_violation THEN
  -- The row id or the digest was already taken: nothing is issued.
  outcome := 'security_unavailable';
  RETURN NEXT;
  RETURN;
 END;
END
"""

# Static SQL only; returns at most one row and writes nothing.
CREDENTIAL_BODY = """
BEGIN
 IF p_email IS NULL OR length(p_email) > 320 THEN
  RETURN;
 END IF;
 RETURN QUERY SELECT u.id, u.password_hash::text FROM kalillac.users u WHERE u.email = p_email;
END
"""

_TAIL = _deletion._TAIL
ISSUE_RESET_FUNCTION = ("CREATE FUNCTION kalillac.v1_issue_reset_token(p_token_id uuid, p_email text, p_token_hash text)"
                        " RETURNS TABLE(outcome text)" + _TAIL
                        + "$v1_issue_reset_token$" + ISSUE_RESET_BODY + "$v1_issue_reset_token$")
CREDENTIAL_FUNCTION = ("CREATE FUNCTION kalillac.v1_credential(p_email text)"
                       " RETURNS TABLE(account_id uuid, stored_hash text)" + _TAIL
                       + "$v1_credential$" + CREDENTIAL_BODY + "$v1_credential$")


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _once(template, old, new, count=1):
    if template.count(old) != count:
        raise RuntimeError("V1_RECOVERY_CREDENTIAL_POSTCONDITION_UNRECOGNIZED")
    return template.replace(old, new)


_BEFORE = "(fn, issue, ending, resetting, changing, quarantining, reinstating, deleting, deleting_own)"
_AFTER = ("(fn, issue, ending, resetting, changing, quarantining, reinstating, deleting, deleting_own,"
          " issuing_reset, crediting)")

# 0023's postcondition extended by the two functions: exact properties, body digests and results, owned by
# E, executable by E and S only; S's exact dependencies; E's widened column grants (rendered). The event
# types, guard v3 and role P's checks are unchanged.
_post = _deletion.POSTCONDITION.replace("$v1_deletion_request_postcondition$", "$v1_recovery_credential_postcondition$")
_post = _once(_post, " deleting_own oid := pg_catalog.to_regprocedure('kalillac.v1_request_own_deletion(uuid,uuid,text,text)');",
              " deleting_own oid := pg_catalog.to_regprocedure('kalillac.v1_request_own_deletion(uuid,uuid,text,text)');\n"
              " issuing_reset oid := pg_catalog.to_regprocedure('kalillac.v1_issue_reset_token(uuid,text,text)');\n"
              " crediting oid := pg_catalog.to_regprocedure('kalillac.v1_credential(text)');")
_post = _once(_post, " OR deleting IS NULL OR deleting_own IS NULL",
              " OR deleting IS NULL OR deleting_own IS NULL OR issuing_reset IS NULL OR crediting IS NULL")
_post = _once(_post, "AND d.objid IN (fn, issue, ending, resetting, changing, deleting_own)))))",
              "AND d.objid IN (fn, issue, ending, resetting, changing, deleting_own, issuing_reset, crediting)))))")
_post = _once(_post, _BEFORE, _AFTER, count=5)
_post = _once(_post, " (deleting_own, service, 'EXECUTE', false, executor)))",
              " (deleting_own, service, 'EXECUTE', false, executor),\n"
              "                           (issuing_reset, executor, 'EXECUTE', false, executor),"
              " (issuing_reset, service, 'EXECUTE', false, executor),\n"
              "                           (crediting, executor, 'EXECUTE', false, executor),"
              " (crediting, service, 'EXECUTE', false, executor)))")
_post = _once(_post, "WHERE p.oid IN " + _AFTER + ") <> 18", "WHERE p.oid IN " + _AFTER + ") <> 22")
_post = _once(_post, "AND d.deptype = 'o') <> 9", "AND d.deptype = 'o') <> 11")
_post = _once(_post, " OR EXISTS (SELECT 1 FROM pg_catalog.pg_proc p WHERE p.oid IN " + _AFTER + " AND p.proowner",
              "".join(f" OR (SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(p.prosrc, 'UTF8')), 'hex')\n"
                      f"     FROM pg_catalog.pg_proc p WHERE p.oid = {name}) IS DISTINCT FROM '{_sha(body)}'\n"
                      f" OR (SELECT pg_catalog.pg_get_function_result(p.oid) FROM pg_catalog.pg_proc p WHERE p.oid = {name})\n"
                      f"    IS DISTINCT FROM '{result}'\n"
                      for name, body, result in (
                          ("issuing_reset", ISSUE_RESET_BODY, "TABLE(outcome text)"),
                          ("crediting", CREDENTIAL_BODY, "TABLE(account_id uuid, stored_hash text)")))
              + " OR EXISTS (SELECT 1 FROM pg_catalog.pg_proc p WHERE p.oid IN " + _AFTER + " AND p.proowner")
POSTCONDITION = _once(_post, "'V1_DELETION_REQUEST_POSTCONDITION_INVALID'", "'V1_RECOVERY_CREDENTIAL_POSTCONDITION_INVALID'")

ABSENT = """DO $v1_recovery_credential_absent$
BEGIN
 IF EXISTS (SELECT 1 FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'kalillac' AND p.proname IN ('v1_issue_reset_token', 'v1_credential')) THEN
  RAISE EXCEPTION USING MESSAGE = %(message)s;
 END IF;
END $v1_recovery_credential_absent$"""


def _names():
    return _deletion._names()


def _preflight(names, message):
    """0023's exact catalog (as 0023 proves it), 0021's guard, and neither function in any form."""
    return [_deletion._postcondition(names).replace("'V1_DELETION_REQUEST_POSTCONDITION_INVALID'", "'" + message + "'"),
            _deletion._quarantine._activation.POSTCONDITION.replace(
                "'REGISTRATION_ACTIVATION_GUARD_POSTCONDITION_INVALID'", "'" + message + "'"),
            ABSENT % {"message": "'" + message + "'"}]


def _postcondition(names):
    return _change._reset._render(POSTCONDITION, names, EXECUTOR_COLUMN_GRANTS)


def upgrade_statements(names):
    """Every statement 0024 runs, in order, for validated role names."""
    executor, service = names["executor"], names["service"]
    return [
        *_preflight(names, "V1_RECOVERY_CREDENTIAL_PREREQUISITE_INVALID"),
        # Temporary: the owner can transfer ownership only to a role with CREATE.
        f"GRANT CREATE ON SCHEMA kalillac TO {executor}",
        ISSUE_RESET_FUNCTION,
        CREDENTIAL_FUNCTION,
        f"REVOKE ALL ON FUNCTION {ISSUE_RESET_SIGNATURE} FROM PUBLIC",
        f"REVOKE ALL ON FUNCTION {CREDENTIAL_SIGNATURE} FROM PUBLIC",
        f"GRANT EXECUTE ON FUNCTION {ISSUE_RESET_SIGNATURE} TO {service}",
        f"GRANT EXECUTE ON FUNCTION {CREDENTIAL_SIGNATURE} TO {service}",
        # Through the operator's owner->E SET edge; the ACLs are rewritten to E.
        f"ALTER FUNCTION {ISSUE_RESET_SIGNATURE} OWNER TO {executor}",
        f"ALTER FUNCTION {CREDENTIAL_SIGNATURE} OWNER TO {executor}",
        f"GRANT SELECT (email) ON kalillac.users TO {executor}",
        f"GRANT INSERT ({', '.join(TOKEN_COLUMNS)}) ON kalillac.account_recovery_tokens TO {executor}",
        f"REVOKE CREATE ON SCHEMA kalillac FROM {executor}",
        _postcondition(names),
    ]


def downgrade_statements(names):
    """From the exact 0024 catalog back to the exact 0023 one. Tokens already issued stay (the legacy path
    reads and consumes them the same way)."""
    executor, owner = names["executor"], names["owner"]
    return [
        _postcondition(names),
        f"REVOKE INSERT ({', '.join(TOKEN_COLUMNS)}) ON kalillac.account_recovery_tokens FROM {executor}",
        f"REVOKE SELECT (email) ON kalillac.users FROM {executor}",
        f"GRANT CREATE ON SCHEMA kalillac TO {executor}",
        f"SET ROLE {executor}",
        f"DROP FUNCTION {ISSUE_RESET_SIGNATURE}",
        f"DROP FUNCTION {CREDENTIAL_SIGNATURE}",
        f"SET ROLE {owner}",
        f"REVOKE CREATE ON SCHEMA kalillac FROM {executor}",
        *_preflight(names, "V1_RECOVERY_CREDENTIAL_DOWNGRADE_INVALID"),
    ]


def upgrade():
    if op.get_context().dialect.name != "postgresql":
        return
    for statement in upgrade_statements(_names()):
        op.execute(statement)


def downgrade():
    if op.get_context().as_sql:
        # Offline SQL cannot see the catalog it would change.
        raise RuntimeError("V1_RECOVERY_CREDENTIAL_OFFLINE_DOWNGRADE_UNSUPPORTED")
    if op.get_context().dialect.name != "postgresql":
        return
    for statement in downgrade_statements(_names()):
        op.execute(statement)
