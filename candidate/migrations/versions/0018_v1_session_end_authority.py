"""Prepare the private sign-out authority (V1-02D, D2 first part), unexposed.

Revision ID: 0018_v1_session_end
Revises: 0017_billing_cases

PostgreSQL only; one function and its ACL (no table, column, index, trigger, view, rule, sequence,
grant on data, event type or data). It installs kalillac.v1_end_session, a fixed SECURITY DEFINER
interface that revokes exactly the presented session: the caller passes the session id and the
digest of the token it holds, and the session is revoked only when both match an unrevoked row.
Every input, matching or not, answers the same 'ended' (sign-out is idempotent and reveals
nothing); only a malformed call answers 'security_unavailable'. It locks only that session row, so
it cannot deadlock with revocation or issuance, which lock the account row first. No audit event is
written: sign-out ends the presenter's own session and is not one of the V1 audited operations.

E owns it and only S may execute it, exactly like 0013's and 0014's functions; E needs no new right
(0013 already granted it SELECT and UPDATE (revoked_at) on account_sessions). No role, membership
or grant on data changes, nothing the application can call is added, and V1 startup still refuses.

Preflight: the exact 0014 V1 catalog (0014's own postcondition) and no sign-out function.
Postcondition: 0014's, extended to exactly three E-owned functions, each with its exact properties,
body digest and an ACL of E and S only.
"""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

from alembic import op

revision = "0018_v1_session_end"
down_revision = "0017_billing_cases"
branch_labels = depends_on = None

_spec = importlib.util.spec_from_file_location(
    "v1_0014_session_issuance", Path(__file__).with_name("0014_v1_session_issuance_epoch.py"))
_issuance = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_issuance)

SIGNATURE = "kalillac.v1_end_session(uuid, text)"

# Static SQL only. The trusted identity is the pair the caller presents: the session id and the
# digest of the token it holds (never the token). Nothing is logged, raised or recorded with them.
BODY = """
DECLARE
 v_token text;
 v_revoked timestamptz;
BEGIN
 outcome := 'security_unavailable';
 IF p_session_id IS NULL OR p_token_hash IS NULL OR p_token_hash !~ '^[0-9a-f]{64}$' THEN
  RETURN NEXT;
  RETURN;
 END IF;
 outcome := 'ended';
 SELECT s.token_hash, s.revoked_at INTO v_token, v_revoked
  FROM kalillac.account_sessions s WHERE s.id = p_session_id FOR UPDATE;
 IF FOUND AND v_token = p_token_hash AND v_revoked IS NULL THEN
  UPDATE kalillac.account_sessions SET revoked_at = clock_timestamp() WHERE id = p_session_id;
 END IF;
 RETURN NEXT;
 RETURN;
END
"""

FUNCTION = ("CREATE FUNCTION kalillac.v1_end_session(p_session_id uuid, p_token_hash text)"
            " RETURNS TABLE(outcome text)"
            " LANGUAGE plpgsql VOLATILE SECURITY DEFINER PARALLEL UNSAFE"
            " SET search_path = pg_catalog, pg_temp AS $v1_end$" + BODY + "$v1_end$")


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _once(template, old, new, count=1):
    if template.count(old) != count:
        raise RuntimeError("V1_SESSION_END_POSTCONDITION_UNRECOGNIZED")
    return template.replace(old, new)


# 0014's postcondition extended by the third function: declared, present, with the same exact
# properties as the other two, its own body digest, owned by E, executable by E and S only.
_post = _issuance.POSTCONDITION.replace("$v1_issuance_postcondition$", "$v1_session_end_postcondition$")
_post = _once(_post, "'kalillac.v1_issue_session(uuid,text,uuid,text,integer)');",
              "'kalillac.v1_issue_session(uuid,text,uuid,text,integer)');\n"
              " ending oid := pg_catalog.to_regprocedure('kalillac.v1_end_session(uuid,text)');")
_post = _once(_post, " OR fn IS NULL OR issue IS NULL", " OR fn IS NULL OR issue IS NULL OR ending IS NULL")
_post = _once(_post, "(fn, issue)", "(fn, issue, ending)", count=6)
_post = _once(_post, "(issue, service, 'EXECUTE', false, executor)))",
              "(issue, service, 'EXECUTE', false, executor),\n"
              "                           (ending, executor, 'EXECUTE', false, executor),"
              " (ending, service, 'EXECUTE', false, executor)))")
_post = _once(_post, "WHERE p.oid IN (fn, issue, ending)) <> 4", "WHERE p.oid IN (fn, issue, ending)) <> 6")
_post = _once(_post, "AND d.deptype = 'o') <> 2", "AND d.deptype = 'o') <> 3")
_post = _once(_post, " OR EXISTS (SELECT 1 FROM pg_catalog.pg_proc p WHERE p.oid IN (fn, issue, ending) AND p.proowner",
              " OR (SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(p.prosrc, 'UTF8')), 'hex')\n"
              "     FROM pg_catalog.pg_proc p WHERE p.oid = ending) IS DISTINCT FROM '" + _sha(BODY) + "'\n"
              " OR (SELECT pg_catalog.pg_get_function_result(p.oid) FROM pg_catalog.pg_proc p WHERE p.oid = ending)\n"
              "    IS DISTINCT FROM 'TABLE(outcome text)'\n"
              " OR EXISTS (SELECT 1 FROM pg_catalog.pg_proc p WHERE p.oid IN (fn, issue, ending) AND p.proowner")
POSTCONDITION = _once(_post, "'V1_ISSUANCE_POSTCONDITION_INVALID'", "'V1_SESSION_END_POSTCONDITION_INVALID'")

ABSENT = """DO $v1_session_end_absent$
BEGIN
 IF pg_catalog.to_regprocedure('kalillac.v1_end_session(uuid,text)') IS NOT NULL
    OR EXISTS (SELECT 1 FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
               WHERE n.nspname = 'kalillac' AND p.proname = 'v1_end_session') THEN
  RAISE EXCEPTION USING MESSAGE = %(message)s;
 END IF;
END $v1_session_end_absent$"""


def _names():
    return _issuance._names()


def _render(template, names):
    """A postcondition template with 0014's (unchanged) grant lists and the validated role names."""
    table_grants = ", ".join(f"('kalillac', '{table}', '{right}')"
                             for table, rights in _issuance.EXECUTOR_TABLE_GRANTS.items() for right in rights)
    column_grants = ", ".join(f"('{table}', '{column}', '{right}')"
                              for (table, right), columns in _issuance.EXECUTOR_COLUMN_GRANTS.items()
                              for column in columns)
    return template % {"table_grants": table_grants, "column_grants": column_grants,
                       **{key: "'" + name + "'" for key, name in names.items()}}


def _preflight(names, message):
    """The exact 0014 catalog, as 0014 itself proves it, and no sign-out function in any form."""
    return [_issuance._postcondition(names).replace("'V1_ISSUANCE_POSTCONDITION_INVALID'", "'" + message + "'"),
            ABSENT % {"message": "'" + message + "'"}]


def _postcondition(names):
    return _render(POSTCONDITION, names)


def upgrade_statements(names):
    """Every statement 0018 runs, in order, for validated role names."""
    executor, service = names["executor"], names["service"]
    return [
        *_preflight(names, "V1_SESSION_END_PREREQUISITE_INVALID"),
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
    """From the exact 0018 catalog back to the exact 0014 one. Sessions ended through the function
    stay ended; they remain valid 0014 data."""
    executor, owner = names["executor"], names["owner"]
    return [
        _postcondition(names),
        f"GRANT CREATE ON SCHEMA kalillac TO {executor}",
        f"SET ROLE {executor}",
        f"DROP FUNCTION {SIGNATURE}",
        f"SET ROLE {owner}",
        f"REVOKE CREATE ON SCHEMA kalillac FROM {executor}",
        *_preflight(names, "V1_SESSION_END_DOWNGRADE_INVALID"),
    ]


def upgrade():
    if op.get_context().dialect.name != "postgresql":
        return
    for statement in upgrade_statements(_names()):
        op.execute(statement)


def downgrade():
    if op.get_context().as_sql:
        # Offline SQL cannot see the catalog it would change.
        raise RuntimeError("V1_SESSION_END_OFFLINE_DOWNGRADE_UNSUPPORTED")
    if op.get_context().dialect.name != "postgresql":
        return
    for statement in downgrade_statements(_names()):
        op.execute(statement)
