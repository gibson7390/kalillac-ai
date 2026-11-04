"""Prepare private session issuance and the session epoch, unexposed.

Revision ID: 0014_v1_session_issuance
Revises: 0013_v1_session_revocation

PostgreSQL only; functions and grants only (no table, column, index, trigger,
view, rule, sequence, event type or data). It:

- installs kalillac.v1_issue_session, a fixed SECURITY DEFINER interface that
  issues one session for an account whose password the caller has just
  verified, under the account row lock, at database time strictly after the
  account's session epoch, with replay-safe caller-generated session IDs;
- makes a new revoke-all also advance the account's epoch once, strictly
  forward (kalillac.v1_revoke_sessions, otherwise unchanged);
- replaces 0011's identity guard with v2, which additionally allows only that
  forward epoch transition; UPDATE (sessions_valid_after) is granted to the
  executor role E alone.

Both functions are owned by E and executable only by the service role S,
both NOLOGIN, through the operator's single owner->E SET edge, exactly as
0013 arranged. No role or membership is created or changed, nothing the
application can call is added and V1 startup still refuses.

The legacy runtime role keeps its direct session DML, so a session it inserts
is outside this guarantee; no production-wide guarantee is claimed until
V1-02E removes that route.
"""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

from alembic import op

from kalillac_accounts.registration_schema import guard_functions, identity_guard_v2

revision = "0014_v1_session_issuance"
down_revision = "0013_v1_session_revocation"
branch_labels = depends_on = None

# 0013's frozen definitions: its revocation body, role checks and postcondition.
_spec = importlib.util.spec_from_file_location(
    "v1_0013_session_revocation", Path(__file__).with_name("0013_v1_session_revocation_authority.py"))
_revocation = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_revocation)

SIGNATURE = "kalillac.v1_issue_session(uuid, text, uuid, text, integer)"
REVOCATION_SIGNATURE = _revocation.SIGNATURE
EXECUTOR_TABLE_GRANTS = _revocation.EXECUTOR_TABLE_GRANTS
# E's exact authority after 0014: 0013's, plus the three users columns issuance
# reads, the epoch it advances, and the five session columns it inserts.
ADDED_COLUMN_GRANTS = {("users", "SELECT"): ("password_hash", "email_verified_at", "password_reset_required"),
                       ("users", "UPDATE"): ("sessions_valid_after",),
                       ("account_sessions", "INSERT"): ("id", "user_id", "token_hash", "created_at", "expires_at")}
EXECUTOR_COLUMN_GRANTS = {("users", "SELECT"): ("id", "password_hash", "is_active", "email_verified_at",
                                                "quarantined_at", "deletion_requested_at", "password_reset_required",
                                                "sessions_valid_after"),
                          ("users", "UPDATE"): ("updated_at", "sessions_valid_after"),
                          ("account_sessions", "SELECT"): ("id", "user_id", "token_hash", "created_at", "expires_at",
                                                           "revoked_at"),
                          ("account_sessions", "INSERT"): ("id", "user_id", "token_hash", "created_at", "expires_at"),
                          ("account_sessions", "UPDATE"): ("revoked_at",)}

# Static SQL only. The trusted identity is p_user_id, from the caller's
# server-side lookup of the normalized email, and p_verified_password_hash, the
# exact stored hash the caller's Argon2 check just verified; the raw token, the
# password and the email never reach the database, and nothing is logged,
# raised or recorded with them. Comparing the hash under the row lock is a
# stale-credential guard, not authentication. Expected outcomes are returned,
# never raised; timeouts, cancellation and deadlocks propagate and roll the
# whole call back.
BODY = """
DECLARE
 v_hash text;
 v_active boolean;
 v_verified timestamptz;
 v_quarantined timestamptz;
 v_deletion timestamptz;
 v_reset boolean;
 v_epoch timestamptz;
 v_user uuid;
 v_token text;
 v_created timestamptz;
 v_expires timestamptz;
 v_revoked timestamptz;
 v_at timestamptz;
BEGIN
 outcome := 'security_unavailable';
 issued_at := NULL;
 valid_until := NULL;
 replayed := false;
 IF p_session_id IS NULL OR p_user_id IS NULL OR p_token_hash IS NULL OR p_token_hash !~ '^[0-9a-f]{64}$'
    OR p_lifetime_days IS NULL OR p_lifetime_days < 1 OR p_lifetime_days > 366 THEN
  RETURN NEXT;
  RETURN;
 END IF;
 BEGIN
  outcome := 'not_authenticated';
  -- The account row first, as revocation locks it; every denial below is uniform.
  SELECT u.password_hash, u.is_active, u.email_verified_at, u.quarantined_at, u.deletion_requested_at,
         u.password_reset_required, u.sessions_valid_after
   INTO v_hash, v_active, v_verified, v_quarantined, v_deletion, v_reset, v_epoch
   FROM kalillac.users u WHERE u.id = p_user_id FOR UPDATE;
  IF NOT FOUND OR v_hash IS DISTINCT FROM p_verified_password_hash OR NOT v_active OR v_verified IS NULL
     OR v_quarantined IS NOT NULL OR v_deletion IS NOT NULL OR v_reset THEN
   RETURN NEXT;
   RETURN;
  END IF;
  SELECT s.user_id, s.token_hash, s.created_at, s.expires_at, s.revoked_at
   INTO v_user, v_token, v_created, v_expires, v_revoked
   FROM kalillac.account_sessions s WHERE s.id = p_session_id FOR SHARE;
  IF FOUND THEN
   -- A replay repeats the exact binding and lifetime and is still live; a
   -- different binding or lifetime is a collision.
   IF v_user IS DISTINCT FROM p_user_id OR v_token IS DISTINCT FROM p_token_hash
      OR v_expires - v_created IS DISTINCT FROM p_lifetime_days * interval '24 hours' THEN
    outcome := 'security_unavailable';
   ELSIF v_revoked IS NULL AND v_expires > clock_timestamp() AND (v_epoch IS NULL OR v_created > v_epoch) THEN
    outcome := 'issued';
    issued_at := v_created;
    valid_until := v_expires;
    replayed := true;
   END IF;
   RETURN NEXT;
   RETURN;
  END IF;
  IF EXISTS (SELECT 1 FROM kalillac.account_sessions s WHERE s.token_hash = p_token_hash) THEN
   outcome := 'security_unavailable';
   RETURN NEXT;
   RETURN;
  END IF;
  -- Database time, strictly after the epoch even when the clock is behind it.
  v_at := greatest(clock_timestamp(), v_epoch + interval '1 microsecond');
  INSERT INTO kalillac.account_sessions (id, user_id, token_hash, created_at, expires_at)
   VALUES (p_session_id, p_user_id, p_token_hash, v_at, v_at + p_lifetime_days * interval '24 hours')
   RETURNING created_at, expires_at INTO issued_at, valid_until;
  outcome := 'issued';
  RETURN NEXT;
  RETURN;
 EXCEPTION WHEN unique_violation THEN
  -- The session ID or token hash was taken concurrently; nothing of this call remains.
  outcome := 'security_unavailable';
  issued_at := NULL;
  valid_until := NULL;
  replayed := false;
  RETURN NEXT;
  RETURN;
 END;
END
"""

FUNCTION = ("CREATE FUNCTION kalillac.v1_issue_session(p_session_id uuid, p_token_hash text, p_user_id uuid,"
            " p_verified_password_hash text, p_lifetime_days integer)"
            " RETURNS TABLE(outcome text, issued_at timestamptz, valid_until timestamptz, replayed boolean)"
            " LANGUAGE plpgsql VOLATILE SECURITY DEFINER PARALLEL UNSAFE"
            " SET search_path = pg_catalog, pg_temp AS $v1_issue$" + BODY + "$v1_issue$")

# Revocation v2: 0013's body with one addition. A new successful 'all' moves the
# epoch once, strictly forward, at its single effective time; sessions are
# never consulted for it, a replay returns before any write, and 'others'
# leaves it alone.
_SESSIONS_REVOKED = "   WHERE user_id = v_user AND revoked_at IS NULL AND (p_scope = 'all' OR id <> p_session_id);\n"
EPOCH = """  IF p_scope = 'all' THEN
   UPDATE kalillac.users SET sessions_valid_after = greatest(v_at, v_epoch + interval '1 microsecond')
    WHERE id = v_user;
  END IF;
"""
if _revocation.BODY.count(_SESSIONS_REVOKED) != 1:
    raise RuntimeError("V1_ISSUANCE_REVOCATION_BODY_UNRECOGNIZED")
REVOCATION_BODY = _revocation.BODY.replace(_SESSIONS_REVOKED, _SESSIONS_REVOKED + EPOCH)
_REVOCATION_HEAD = _revocation.FUNCTION.split(" AS $v1_revoke$")[0].replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION")
REVOCATION_FUNCTION = _REVOCATION_HEAD + " AS $v1_revoke$" + REVOCATION_BODY + "$v1_revoke$"
REVOCATION_FUNCTION_V1 = _REVOCATION_HEAD + " AS $v1_revoke$" + _revocation.BODY + "$v1_revoke$"

# The identity guard, as templates for the registration role 0011 recorded,
# which only the database knows (offline SQL included).
MARKER = "@registration_role@"
GUARD_V1 = guard_functions(MARKER, MARKER)["registration_user_identity_guard"]
GUARD_V2 = identity_guard_v2(MARKER)


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


# 0014's additions to 0013's declarations.
DECLARATIONS = _revocation.DECLARATIONS + """
 issue oid := pg_catalog.to_regprocedure('kalillac.v1_issue_session(uuid,text,uuid,text,integer)');
 guard oid := pg_catalog.to_regprocedure('kalillac.registration_user_identity_guard()');
 recorded text := pg_catalog.obj_description('kalillac.account_registration_activation'::pg_catalog.regclass,
  'pg_class')::pg_catalog.json ->> 'registration_role';"""

# Exact revocation properties and body, and the guard's body, owner, ACL and
# trigger binding. The guard's ACL must be exactly its owner's own EXECUTE,
# granted by itself without grant option, compared by OID: no PUBLIC or other
# grantee, no grant option and no other item. A NULL ACL stands for the
# default, which includes PUBLIC EXECUTE, so it never qualifies. Anything else
# is refused and left for the operator; nothing here revokes it.
DEFINITIONS = """
 OR EXISTS (SELECT 1 FROM pg_catalog.pg_proc p WHERE p.oid IN (fn, %(issue)s)
            AND (NOT p.prosecdef OR p.proleakproof OR p.proisstrict OR p.provolatile <> 'v' OR p.proparallel <> 'u'
                 OR p.prokind <> 'f' OR p.proconfig IS DISTINCT FROM ARRAY['search_path=pg_catalog, pg_temp']
                 OR p.prolang <> (SELECT l.oid FROM pg_catalog.pg_language l WHERE l.lanname = 'plpgsql')))
 OR (SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(p.prosrc, 'UTF8')), 'hex')
     FROM pg_catalog.pg_proc p WHERE p.oid = fn) IS DISTINCT FROM %(revocation_sha)s
 OR recorded IS NULL OR recorded !~ '^[a-z_][a-z0-9_]{0,62}$' OR guard IS NULL
 OR (SELECT p.prosrc FROM pg_catalog.pg_proc p WHERE p.oid = guard)
    IS DISTINCT FROM pg_catalog.replace(%(guard_body)s, '@registration_role@', recorded)
 OR (SELECT p.proowner FROM pg_catalog.pg_proc p WHERE p.oid = guard) <> keeper
 OR (SELECT p.proacl IS NULL FROM pg_catalog.pg_proc p WHERE p.oid = guard) IS NOT FALSE
 OR (SELECT pg_catalog.count(*) FROM pg_catalog.pg_proc p, pg_catalog.aclexplode(p.proacl) x WHERE p.oid = guard) <> 1
 OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_proc p, pg_catalog.aclexplode(p.proacl) x WHERE p.oid = guard
                AND x.grantee = keeper AND x.grantor = keeper AND x.privilege_type = 'EXECUTE' AND NOT x.is_grantable)
 OR (SELECT pg_catalog.count(*) FROM pg_catalog.pg_trigger t WHERE t.tgfoid = guard) <> 1
 OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_trigger t WHERE t.tgfoid = guard AND t.tgname = 'trg_users_registration_identity'
                AND t.tgrelid = 'kalillac.users'::pg_catalog.regclass AND t.tgenabled = 'A' AND NOT t.tgisinternal)"""

# The exact canonical 0013 catalog: 0013's own postcondition, then no
# issuance, revocation v1 and guard v1.
PREFLIGHT = [
    _revocation.POSTCONDITION.replace("v1_revocation_postcondition", "v1_issuance_prerequisite_0013")
    .replace("'V1_REVOCATION_POSTCONDITION_INVALID'", "%(message)s"),
    "DO $v1_issuance_prerequisite$\nDECLARE" + DECLARATIONS + "\nBEGIN\n IF issue IS NOT NULL OR fn IS NULL" + DEFINITIONS
    % {"issue": "fn", "revocation_sha": "'" + _sha(_revocation.BODY) + "'", "guard_body": "$v1_guard$"
       + GUARD_V1.split("$guard$")[1] + "$v1_guard$"} + """
 THEN RAISE EXCEPTION USING MESSAGE = %(message)s;
 END IF;
END $v1_issuance_prerequisite$""",
]

# The complete result: E owns exactly both functions, each executable by E and
# S only, with exact properties and bodies; the guard is v2 on its trigger; S
# and E hold exactly their declared grants and schema USAGE (no CREATE); the
# membership layout is unchanged.
POSTCONDITION = ("DO $v1_issuance_postcondition$\nDECLARE" + DECLARATIONS + "\nBEGIN\n IF" + _revocation.ROLE_CHECKS
                 + """
 OR fn IS NULL OR issue IS NULL""" + DEFINITIONS % {
                     "issue": "issue", "revocation_sha": "'" + _sha(REVOCATION_BODY) + "'",
                     "guard_body": "$v1_guard$" + GUARD_V2.split("$guard$")[1] + "$v1_guard$"} + """
 OR (SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(p.prosrc, 'UTF8')), 'hex')
     FROM pg_catalog.pg_proc p WHERE p.oid = issue) IS DISTINCT FROM '""" + _sha(BODY) + """'
 OR EXISTS (SELECT 1 FROM pg_catalog.pg_proc p WHERE p.oid IN (fn, issue) AND p.proowner <> executor)
 OR EXISTS ((SELECT p.oid, x.grantee, x.privilege_type, x.is_grantable, x.grantor
             FROM pg_catalog.pg_proc p, pg_catalog.aclexplode(p.proacl) x WHERE p.oid IN (fn, issue))
            EXCEPT (VALUES (fn, executor, 'EXECUTE', false, executor), (fn, service, 'EXECUTE', false, executor),
                           (issue, executor, 'EXECUTE', false, executor), (issue, service, 'EXECUTE', false, executor)))
 OR (SELECT pg_catalog.count(*) FROM pg_catalog.pg_proc p, pg_catalog.aclexplode(p.proacl) x
     WHERE p.oid IN (fn, issue)) <> 4
 OR EXISTS ((SELECT x.grantee, x.privilege_type, x.is_grantable, x.grantor
             FROM pg_catalog.pg_namespace n, pg_catalog.aclexplode(n.nspacl) x
             WHERE n.nspname = 'kalillac' AND x.grantee IN (service, executor))
            EXCEPT (VALUES (executor, 'USAGE', false, keeper), (service, 'USAGE', false, keeper)))
 OR (SELECT pg_catalog.count(*) FROM pg_catalog.pg_namespace n, pg_catalog.aclexplode(n.nspacl) x
     WHERE n.nspname = 'kalillac' AND x.grantee IN (service, executor)) <> 2
 OR EXISTS (SELECT 1 FROM pg_catalog.pg_class c, pg_catalog.aclexplode(c.relacl) x
            WHERE x.grantee = service OR (x.grantee = executor AND (x.is_grantable OR x.grantor <> keeper)))
 OR EXISTS (SELECT 1 FROM pg_catalog.pg_attribute a, pg_catalog.aclexplode(a.attacl) x
            WHERE x.grantee = service OR (x.grantee = executor AND (x.is_grantable OR x.grantor <> keeper)))
 OR EXISTS (((SELECT n.nspname::text, c.relname::text, x.privilege_type::text
              FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace,
              pg_catalog.aclexplode(c.relacl) x WHERE x.grantee = executor)
             EXCEPT (VALUES %(table_grants)s))
            UNION ALL ((VALUES %(table_grants)s)
             EXCEPT (SELECT n.nspname::text, c.relname::text, x.privilege_type::text
              FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace,
              pg_catalog.aclexplode(c.relacl) x WHERE x.grantee = executor)))
 OR EXISTS (((SELECT c.relname::text, a.attname::text, x.privilege_type::text
              FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
              JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace,
              pg_catalog.aclexplode(a.attacl) x WHERE x.grantee = executor AND n.nspname = 'kalillac')
             EXCEPT (VALUES %(column_grants)s))
            UNION ALL ((VALUES %(column_grants)s)
             EXCEPT (SELECT c.relname::text, a.attname::text, x.privilege_type::text
              FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
              JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace,
              pg_catalog.aclexplode(a.attacl) x WHERE x.grantee = executor AND n.nspname = 'kalillac')))
 OR pg_catalog.has_schema_privilege(executor, 'kalillac', 'CREATE')
 OR pg_catalog.has_schema_privilege(service, 'kalillac', 'CREATE')
 OR EXISTS (SELECT 1 FROM pg_catalog.pg_shdepend d WHERE d.refclassid = 'pg_catalog.pg_authid'::pg_catalog.regclass
            AND d.refobjid IN (service, executor) AND d.dbid IN (0, here) AND d.deptype = 'o'
            AND NOT (d.refobjid = executor AND d.classid = 'pg_catalog.pg_proc'::pg_catalog.regclass
                     AND d.objid IN (fn, issue)))
 OR (SELECT pg_catalog.count(*) FROM pg_catalog.pg_shdepend d WHERE d.refclassid = 'pg_catalog.pg_authid'::pg_catalog.regclass
     AND d.refobjid = executor AND d.dbid = here AND d.deptype = 'o') <> 2
 OR EXISTS (SELECT 1 FROM pg_catalog.pg_shdepend d WHERE d.refclassid = 'pg_catalog.pg_authid'::pg_catalog.regclass
            AND d.refobjid IN (service, executor) AND d.dbid IN (0, here) AND d.deptype NOT IN ('o', 'a'))
 OR EXISTS (SELECT 1 FROM pg_catalog.pg_shdepend d WHERE d.refclassid = 'pg_catalog.pg_authid'::pg_catalog.regclass
            AND d.refobjid IN (service, executor) AND d.dbid IN (0, here) AND d.deptype = 'a'
            AND NOT ((d.refobjid = executor AND d.classid IN ('pg_catalog.pg_class'::pg_catalog.regclass,
                                                              'pg_catalog.pg_namespace'::pg_catalog.regclass))
                     OR (d.refobjid = service AND (d.classid = 'pg_catalog.pg_namespace'::pg_catalog.regclass
                         OR (d.classid = 'pg_catalog.pg_proc'::pg_catalog.regclass AND d.objid IN (fn, issue))))))
 THEN RAISE EXCEPTION USING MESSAGE = 'V1_ISSUANCE_POSTCONDITION_INVALID';
 END IF;
END $v1_issuance_postcondition$""")

# Replace the identity guard in place, as its owner: same OID, so the same
# owner, ACL, configuration and trigger binding, which are compared before and
# after. Only the role name 0011 recorded is substituted, validated first.
GUARD_SWAP = """DO $v1_identity_guard$
DECLARE
 recorded text := pg_catalog.obj_description('kalillac.account_registration_activation'::pg_catalog.regclass,
  'pg_class')::pg_catalog.json ->> 'registration_role';
 guard oid := pg_catalog.to_regprocedure('kalillac.registration_user_identity_guard()');
 before text;
BEGIN
 IF recorded IS NULL OR recorded !~ '^[a-z_][a-z0-9_]{0,62}$' OR guard IS NULL
    OR (SELECT p.prosrc FROM pg_catalog.pg_proc p WHERE p.oid = guard)
       IS DISTINCT FROM pg_catalog.replace(%(from)s, '@registration_role@', recorded) THEN
  RAISE EXCEPTION USING MESSAGE = 'V1_ISSUANCE_GUARD_INVALID';
 END IF;
 before := (SELECT ROW(p.proname, p.pronamespace, p.proowner, p.prolang, p.prosecdef, p.proleakproof, p.proisstrict,
                       p.provolatile, p.proparallel, p.prokind, p.proconfig, p.proacl, p.procost, p.prorettype,
                       p.proargtypes, (SELECT pg_catalog.array_agg(ROW(t.oid, t.tgrelid, t.tgname, t.tgtype, t.tgenabled,
                                                                       t.tgisinternal, t.tgdeferrable, t.tginitdeferred,
                                                                       t.tgqual IS NULL)::text)
                                       FROM pg_catalog.pg_trigger t WHERE t.tgfoid = guard))::text
            FROM pg_catalog.pg_proc p WHERE p.oid = guard);
 EXECUTE 'CREATE OR REPLACE FUNCTION kalillac.registration_user_identity_guard '
         || pg_catalog.replace(%(to)s, '@registration_role@', recorded);
 IF pg_catalog.to_regprocedure('kalillac.registration_user_identity_guard()') IS DISTINCT FROM guard
    OR (SELECT p.prosrc FROM pg_catalog.pg_proc p WHERE p.oid = guard)
       IS DISTINCT FROM pg_catalog.replace(%(to_body)s, '@registration_role@', recorded)
    OR (SELECT ROW(p.proname, p.pronamespace, p.proowner, p.prolang, p.prosecdef, p.proleakproof, p.proisstrict,
                   p.provolatile, p.proparallel, p.prokind, p.proconfig, p.proacl, p.procost, p.prorettype,
                   p.proargtypes, (SELECT pg_catalog.array_agg(ROW(t.oid, t.tgrelid, t.tgname, t.tgtype, t.tgenabled,
                                                                   t.tgisinternal, t.tgdeferrable, t.tginitdeferred,
                                                                   t.tgqual IS NULL)::text)
                                   FROM pg_catalog.pg_trigger t WHERE t.tgfoid = guard))::text
        FROM pg_catalog.pg_proc p WHERE p.oid = guard) IS DISTINCT FROM before THEN
  RAISE EXCEPTION USING MESSAGE = 'V1_ISSUANCE_GUARD_INVALID';
 END IF;
END $v1_identity_guard$"""


def _quoted(text):
    if "$v1_guard$" in text:
        raise RuntimeError("V1_ISSUANCE_GUARD_UNQUOTABLE")
    return "$v1_guard$" + text + "$v1_guard$"


def _swap(source, target):
    return GUARD_SWAP % {"from": _quoted(source.split("$guard$")[1]), "to": _quoted(target),
                         "to_body": _quoted(target.split("$guard$")[1])}


def _names():
    return _revocation._names()


def _column_grants(grants):
    by_table = {}
    for (table, right), columns in grants.items():
        by_table.setdefault(table, []).append(f"{right} ({', '.join(columns)})")
    return by_table


def _preflight(names, message):
    return [_revocation._guard(check.replace("%(message)s", "'" + message + "'"), names) for check in PREFLIGHT]


def _postcondition(names):
    table_grants = ", ".join(f"('kalillac', '{table}', '{right}')"
                             for table, rights in EXECUTOR_TABLE_GRANTS.items() for right in rights)
    column_grants = ", ".join(f"('{table}', '{column}', '{right}')"
                              for (table, right), columns in EXECUTOR_COLUMN_GRANTS.items() for column in columns)
    return POSTCONDITION % {"table_grants": table_grants, "column_grants": column_grants,
                            **{key: "'" + name + "'" for key, name in names.items()}}


def upgrade_statements(names):
    """Every statement 0014 runs, in order, for validated role names."""
    executor, service, owner = names["executor"], names["service"], names["owner"]
    return [
        *_preflight(names, "V1_ISSUANCE_PREREQUISITE_INVALID"),
        # Temporary: E needs CREATE to receive ownership and to replace its function.
        f"GRANT CREATE ON SCHEMA kalillac TO {executor}",
        FUNCTION,
        f"REVOKE ALL ON FUNCTION {SIGNATURE} FROM PUBLIC",
        f"GRANT EXECUTE ON FUNCTION {SIGNATURE} TO {service}",
        # Through the operator's owner->E SET edge; the ACL is rewritten to E.
        f"ALTER FUNCTION {SIGNATURE} OWNER TO {executor}",
        # Only its owner may replace the revocation function; the chain M->O->E is SET-only.
        f"SET ROLE {executor}",
        REVOCATION_FUNCTION,
        f"SET ROLE {owner}",
        _swap(GUARD_V1, GUARD_V2),
        *(f"GRANT {', '.join(grants)} ON kalillac.{table} TO {executor}"
          for table, grants in _column_grants(ADDED_COLUMN_GRANTS).items()),
        f"REVOKE CREATE ON SCHEMA kalillac FROM {executor}",
        _postcondition(names),
    ]


def downgrade_statements(names):
    """Every statement the online downgrade runs: from the exact 0014 catalog
    back to the exact 0013 one. Issued sessions and advanced epochs stay; both
    remain valid 0013 data."""
    executor, owner = names["executor"], names["owner"]
    return [
        _postcondition(names),
        *(f"REVOKE {', '.join(grants)} ON kalillac.{table} FROM {executor}"
          for table, grants in _column_grants(ADDED_COLUMN_GRANTS).items()),
        _swap(GUARD_V2, GUARD_V1),
        f"GRANT CREATE ON SCHEMA kalillac TO {executor}",
        f"SET ROLE {executor}",
        f"DROP FUNCTION {SIGNATURE}",
        REVOCATION_FUNCTION_V1,
        f"SET ROLE {owner}",
        f"REVOKE CREATE ON SCHEMA kalillac FROM {executor}",
        *_preflight(names, "V1_ISSUANCE_DOWNGRADE_INVALID"),
    ]


def upgrade():
    if op.get_context().dialect.name != "postgresql":
        return
    for statement in upgrade_statements(_names()):
        op.execute(statement)


def downgrade():
    if op.get_context().as_sql:
        # Offline SQL cannot see the catalog it would change.
        raise RuntimeError("V1_ISSUANCE_OFFLINE_DOWNGRADE_UNSUPPORTED")
    if op.get_context().dialect.name != "postgresql":
        return
    for statement in downgrade_statements(_names()):
        op.execute(statement)
