"""Prepare the private session-revocation authority, unexposed.

Revision ID: 0013_v1_session_revocation
Revises: 0012_v1_account_security

(The revision ID is shorter than this file's name: alembic_version holds at
most 32 characters.)

PostgreSQL only; functions and grants only (no table, column, trigger, view,
rule, sequence or data). Installs kalillac.v1_revoke_sessions, a fixed
SECURITY DEFINER interface that revokes a user's other or all sessions,
proven only by that user's original session, with replay-safe operation IDs,
V1-02A admission and an audit event in one transaction.

It is owned by the executor role E and executable only by the service role
S. Both are operator-provisioned NOLOGIN roles named by mandatory settings,
and the only membership involving them is the operator's owner->E edge with
SET only, which lets the owner transfer ownership to E. This revision
verifies that layout, never creates or changes a role or membership, and
leaves nothing the application can call: S cannot log in and holds no
credential, and V1 startup still refuses.

Legacy session issuance does not take the user lock or follow an epoch, so a
session it inserts after the revocation's snapshot can survive; that is why
the authority stays unexposed until issuance and the epoch are ported.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from kalillac_db.migration_roles import (migration_app_role, migration_owner_role, v1_security_executor_role,
                                         v1_security_service_role)

revision = "0013_v1_session_revocation"
down_revision = "0012_v1_account_security"
branch_labels = depends_on = None

SIGNATURE = "kalillac.v1_revoke_sessions(uuid, uuid, text, text)"
# E's exact authority: the columns the function reads, the single users
# column whose UPDATE privilege is required by FOR UPDATE (never updated),
# revoked_at, the audit table and the two admission tables.
EXECUTOR_TABLE_GRANTS = {"account_security_events": ("SELECT", "INSERT"),
                         "account_security_admission": ("SELECT", "INSERT", "UPDATE"),
                         "account_security_admission_aggregate": ("SELECT", "INSERT", "UPDATE")}
EXECUTOR_COLUMN_GRANTS = {("users", "SELECT"): ("id", "is_active", "quarantined_at", "deletion_requested_at",
                                                "sessions_valid_after"),
                          ("users", "UPDATE"): ("updated_at",),
                          ("account_sessions", "SELECT"): ("id", "user_id", "token_hash", "created_at", "expires_at",
                                                           "revoked_at"),
                          ("account_sessions", "UPDATE"): ("revoked_at",)}

# Static SQL only. Lock order: the user row, the exact original session row,
# the aggregate manual row, the account admission entry, the affected session
# rows, then the event. Timeouts, cancellation and deadlocks propagate and roll
# the whole call back; only an operation-ID collision is reported as an outcome.
BODY = """
DECLARE
 v_type text;
 v_user uuid;
 v_active boolean;
 v_quarantined timestamptz;
 v_deletion timestamptz;
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
 IF p_operation_id IS NULL OR p_scope IS NULL OR p_scope NOT IN ('others', 'all') THEN
  RETURN NEXT;
  RETURN;
 END IF;
 v_type := CASE p_scope WHEN 'others' THEN 'session_revoke_others' ELSE 'session_revoke_all' END;
 BEGIN
  outcome := 'not_authenticated';
  -- The account comes only from the exact original session, in any state.
  SELECT s.user_id INTO v_user FROM kalillac.account_sessions s
   WHERE s.id = p_session_id AND s.token_hash = p_token_hash;
  IF NOT FOUND THEN
   RETURN NEXT;
   RETURN;
  END IF;
  SELECT u.is_active, u.quarantined_at, u.deletion_requested_at, u.sessions_valid_after
   INTO v_active, v_quarantined, v_deletion, v_epoch
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
   IF v_event_user = v_user AND v_event_type = v_type THEN
    outcome := 'revoked';
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
     OR (v_epoch IS NOT NULL AND v_created <= v_epoch) THEN
   RETURN NEXT;
   RETURN;
  END IF;
  IF v_quarantined IS NOT NULL THEN
   outcome := 'account_unavailable';
   RETURN NEXT;
   RETURN;
  END IF;
  INSERT INTO kalillac.account_security_admission_aggregate (admission_class, charges, last_seen_at)
   VALUES ('manual', '{}', v_now) ON CONFLICT (admission_class) DO NOTHING;
  SELECT a.charges, a.last_seen_at INTO v_aggregate, v_aggregate_seen
   FROM kalillac.account_security_admission_aggregate a WHERE a.admission_class = 'manual' FOR UPDATE;
  SELECT m.entry_slot, m.manual_charges, m.last_seen_at INTO v_slot, v_account, v_account_seen
   FROM kalillac.account_security_admission m WHERE m.user_id = v_user FOR UPDATE;
  -- One effective time for every effect; it never moves backward.
  v_at := greatest(v_now, v_aggregate_seen, v_account_seen);
  IF v_expires <= v_at THEN
   RETURN NEXT;
   RETURN;
  END IF;
  v_aggregate := ARRAY(SELECT c FROM unnest(v_aggregate) c WHERE c > v_at - interval '1 hour' ORDER BY c);
  v_account := ARRAY(SELECT c FROM unnest(coalesce(v_account, '{}'::timestamptz[])) c
                     WHERE c > v_at - interval '1 hour' ORDER BY c);
  -- Seconds until every exceeded window admits one more operation.
  SELECT max(extract(epoch FROM w.c + l.span - v_at)) INTO v_wait
   FROM (VALUES (v_aggregate, interval '1 minute', 60), (v_aggregate, interval '1 hour', 60),
                (v_account, interval '1 minute', 2), (v_account, interval '1 hour', 6)) AS l(ring, span, cap)
   CROSS JOIN LATERAL (SELECT r.c FROM (SELECT x.c, row_number() OVER (ORDER BY x.c DESC) AS n
                                        FROM unnest(l.ring) AS x(c) WHERE x.c > v_at - l.span) r
                       WHERE r.n = l.cap) w;
  IF v_wait IS NOT NULL THEN
   outcome := 'too_many_operations';
   retry_after_seconds := greatest(1, ceil(v_wait))::integer;
   RETURN NEXT;
   RETURN;
  END IF;
  IF v_slot IS NULL THEN
   -- Reuse only an entry with no charge left in any window and in use by nobody.
   SELECT m.entry_slot INTO v_slot FROM kalillac.account_security_admission m
    WHERE NOT EXISTS (SELECT 1 FROM unnest(m.manual_charges || m.recovery_charges) c
                      WHERE c > v_at - interval '1 hour')
    ORDER BY m.entry_slot LIMIT 1 FOR UPDATE SKIP LOCKED;
   IF FOUND THEN
    UPDATE kalillac.account_security_admission
     SET user_id = v_user, manual_charges = '{}', recovery_charges = '{}', last_seen_at = v_at
     WHERE entry_slot = v_slot;
   ELSIF (SELECT count(*) FROM kalillac.account_security_admission) < 2048 THEN
    SELECT min(g)::smallint INTO v_slot FROM generate_series(0, 4095) g
     WHERE NOT EXISTS (SELECT 1 FROM kalillac.account_security_admission m WHERE m.entry_slot = g);
    INSERT INTO kalillac.account_security_admission (entry_slot, user_id, manual_charges, recovery_charges,
                                                     last_seen_at)
     VALUES (v_slot, v_user, '{}', '{}', v_at);
   ELSE
    outcome := 'security_unavailable';
    RETURN NEXT;
    RETURN;
   END IF;
   v_account := '{}';
  END IF;
  UPDATE kalillac.account_security_admission_aggregate
   SET charges = v_aggregate || v_at, last_seen_at = v_at WHERE admission_class = 'manual';
  UPDATE kalillac.account_security_admission
   SET manual_charges = v_account || v_at, last_seen_at = v_at WHERE entry_slot = v_slot;
  -- The session rows visible to this statement; a successful no-op is still charged.
  UPDATE kalillac.account_sessions SET revoked_at = v_at
   WHERE user_id = v_user AND revoked_at IS NULL AND (p_scope = 'all' OR id <> p_session_id);
  INSERT INTO kalillac.account_security_events (operation_id, user_id, event_type, occurred_at)
   VALUES (p_operation_id, v_user, v_type, v_at);
  outcome := 'revoked';
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

FUNCTION = ("CREATE FUNCTION kalillac.v1_revoke_sessions(p_operation_id uuid, p_session_id uuid,"
            " p_token_hash text, p_scope text)"
            " RETURNS TABLE(outcome text, retry_after_seconds integer, replayed boolean)"
            " LANGUAGE plpgsql VOLATILE SECURITY DEFINER PARALLEL UNSAFE"
            " SET search_path = pg_catalog, pg_temp AS $v1_revoke$" + BODY + "$v1_revoke$")

# Shared by the preflight and the postcondition. Names are validated simple
# identifiers, embedded as literals and resolved server-side; A is the role
# 0011 recorded.
DECLARATIONS = """
 keeper oid := (SELECT c.relowner FROM pg_catalog.pg_class c WHERE c.oid = 'kalillac.users'::pg_catalog.regclass);
 service oid := pg_catalog.to_regrole(pg_catalog.quote_ident(%(service)s));
 executor oid := pg_catalog.to_regrole(pg_catalog.quote_ident(%(executor)s));
 runtime oid := pg_catalog.to_regrole(pg_catalog.quote_ident(%(runtime)s));
 registration oid := pg_catalog.to_regrole(pg_catalog.quote_ident(pg_catalog.obj_description(
  'kalillac.account_registration_activation'::pg_catalog.regclass, 'pg_class')::pg_catalog.json ->> 'registration_role'));
 here oid := (SELECT d.oid FROM pg_catalog.pg_database d WHERE d.datname = pg_catalog.current_database());
 fn oid := pg_catalog.to_regprocedure('kalillac.v1_revoke_sessions(uuid,uuid,text,text)');"""

# Exact identities and attributes, and the operator's single owner->E edge.
ROLE_CHECKS = """
 keeper IS DISTINCT FROM pg_catalog.to_regrole(pg_catalog.quote_ident(%(owner)s))
 OR keeper IS DISTINCT FROM (SELECT r.oid FROM pg_catalog.pg_roles r WHERE r.rolname = CURRENT_USER)
 OR service IS NULL OR executor IS NULL OR runtime IS NULL OR registration IS NULL
 OR (SELECT pg_catalog.count(DISTINCT x) FROM pg_catalog.unnest(ARRAY[service, executor, runtime, registration, keeper]) x) <> 5
 OR (SELECT r.oid FROM pg_catalog.pg_roles r WHERE r.rolname = SESSION_USER) IN (service, executor, runtime, registration)
 OR EXISTS (SELECT 1 FROM pg_catalog.pg_roles r WHERE r.oid IN (service, executor)
            AND (r.rolcanlogin OR r.rolinherit OR r.rolsuper OR r.rolcreatedb OR r.rolcreaterole
                 OR r.rolreplication OR r.rolbypassrls))
 OR (SELECT pg_catalog.count(*) FROM pg_catalog.pg_auth_members m
     WHERE m.roleid IN (service, executor) OR m.member IN (service, executor)) <> 1
 OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m WHERE m.roleid = executor AND m.member = keeper
                AND NOT m.admin_option AND NOT m.inherit_option AND m.set_option)
 OR EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m WHERE m.member IN (runtime, registration))
 OR EXISTS (SELECT 1 FROM pg_catalog.pg_roles g
            WHERE (g.oid <> runtime AND pg_catalog.pg_has_role(runtime, g.oid, 'MEMBER'))
            OR (g.oid <> registration AND pg_catalog.pg_has_role(registration, g.oid, 'MEMBER')))
 OR EXISTS (SELECT 1 FROM pg_catalog.pg_default_acl a WHERE a.defaclrole IN (service, executor)
            OR EXISTS (SELECT 1 FROM pg_catalog.aclexplode(a.defaclacl) x WHERE x.grantee IN (service, executor)))"""

PREFLIGHT = ("DO $v1_revocation_preflight$\nDECLARE" + DECLARATIONS + "\nBEGIN\n IF" + ROLE_CHECKS + """
 OR fn IS NOT NULL
 OR EXISTS (SELECT 1 FROM pg_catalog.pg_shdepend d WHERE d.refclassid = 'pg_catalog.pg_authid'::pg_catalog.regclass
            AND d.refobjid IN (service, executor) AND d.dbid IN (0, here))
 THEN RAISE EXCEPTION USING MESSAGE = 'V1_REVOCATION_PREREQUISITE_INVALID';
 END IF;
END $v1_revocation_preflight$""")

# The complete result: E owns exactly the function, its ACL is E and S only,
# S and E hold exactly their declared grants and schema USAGE (no CREATE), and
# the membership layout is unchanged.
POSTCONDITION = ("DO $v1_revocation_postcondition$\nDECLARE" + DECLARATIONS + "\nBEGIN\n IF" + ROLE_CHECKS + """
 OR fn IS NULL
 OR (SELECT p.proowner FROM pg_catalog.pg_proc p WHERE p.oid = fn) <> executor
 OR EXISTS ((SELECT x.grantee, x.privilege_type, x.is_grantable, x.grantor
             FROM pg_catalog.pg_proc p, pg_catalog.aclexplode(p.proacl) x WHERE p.oid = fn)
            EXCEPT (VALUES (executor, 'EXECUTE', false, executor), (service, 'EXECUTE', false, executor)))
 OR (SELECT pg_catalog.count(*) FROM pg_catalog.pg_proc p, pg_catalog.aclexplode(p.proacl) x WHERE p.oid = fn) <> 2
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
            AND NOT (d.refobjid = executor AND d.classid = 'pg_catalog.pg_proc'::pg_catalog.regclass AND d.objid = fn))
 OR (SELECT pg_catalog.count(*) FROM pg_catalog.pg_shdepend d WHERE d.refclassid = 'pg_catalog.pg_authid'::pg_catalog.regclass
     AND d.refobjid = executor AND d.dbid = here AND d.deptype = 'o') <> 1
 OR EXISTS (SELECT 1 FROM pg_catalog.pg_shdepend d WHERE d.refclassid = 'pg_catalog.pg_authid'::pg_catalog.regclass
            AND d.refobjid IN (service, executor) AND d.dbid IN (0, here) AND d.deptype NOT IN ('o', 'a'))
 OR EXISTS (SELECT 1 FROM pg_catalog.pg_shdepend d WHERE d.refclassid = 'pg_catalog.pg_authid'::pg_catalog.regclass
            AND d.refobjid IN (service, executor) AND d.dbid IN (0, here) AND d.deptype = 'a'
            AND NOT ((d.refobjid = executor AND d.classid IN ('pg_catalog.pg_class'::pg_catalog.regclass,
                                                              'pg_catalog.pg_namespace'::pg_catalog.regclass))
                     OR (d.refobjid = service AND (d.classid = 'pg_catalog.pg_namespace'::pg_catalog.regclass
                         OR (d.classid = 'pg_catalog.pg_proc'::pg_catalog.regclass AND d.objid = fn)))))
 THEN RAISE EXCEPTION USING MESSAGE = 'V1_REVOCATION_POSTCONDITION_INVALID';
 END IF;
END $v1_revocation_postcondition$""")


def _names():
    return {"service": v1_security_service_role(), "executor": v1_security_executor_role(),
            "owner": migration_owner_role(), "runtime": migration_app_role()}


def _guard(template, names):
    table_grants = ", ".join(f"('kalillac', '{table}', '{right}')"
                             for table, rights in EXECUTOR_TABLE_GRANTS.items() for right in rights)
    column_grants = ", ".join(f"('{table}', '{column}', '{right}')"
                              for (table, right), columns in EXECUTOR_COLUMN_GRANTS.items() for column in columns)
    return template % {"table_grants": table_grants, "column_grants": column_grants,
                       **{key: "'" + name + "'" for key, name in names.items()}}


def upgrade_statements(names):
    """Every statement 0013 runs, in order, for validated role names."""
    executor, service = names["executor"], names["service"]
    column_grants = {}
    for (table, right), columns in EXECUTOR_COLUMN_GRANTS.items():
        column_grants.setdefault(table, []).append(f"{right} ({', '.join(columns)})")
    return [
        _guard(PREFLIGHT, names),
        # Temporary: the owner can transfer ownership only to a role with CREATE.
        f"GRANT CREATE ON SCHEMA kalillac TO {executor}",
        FUNCTION,
        f"REVOKE ALL ON FUNCTION {SIGNATURE} FROM PUBLIC",
        f"GRANT EXECUTE ON FUNCTION {SIGNATURE} TO {service}",
        # Through the operator's owner->E SET edge; the ACL is rewritten to E.
        f"ALTER FUNCTION {SIGNATURE} OWNER TO {executor}",
        f"GRANT USAGE ON SCHEMA kalillac TO {executor}, {service}",
        *(f"GRANT {', '.join(grants)} ON kalillac.{table} TO {executor}" for table, grants in column_grants.items()),
        *(f"GRANT {', '.join(rights)} ON kalillac.{table} TO {executor}"
          for table, rights in EXECUTOR_TABLE_GRANTS.items()),
        f"REVOKE CREATE ON SCHEMA kalillac FROM {executor}",
        _guard(POSTCONDITION, names),
    ]


def upgrade():
    if op.get_context().dialect.name != "postgresql":
        return
    for statement in upgrade_statements(_names()):
        op.execute(statement)


def downgrade():
    if op.get_context().as_sql:
        # Offline SQL cannot see whether security state exists.
        raise RuntimeError("V1_REVOCATION_OFFLINE_DOWNGRADE_UNSUPPORTED")
    if op.get_context().dialect.name != "postgresql":
        return
    names = _names()
    bind = op.get_bind()
    for table in ("account_security_events", "account_security_admission", "account_security_admission_aggregate"):
        if bind.execute(sa.text(f"SELECT count(*) FROM kalillac.{table}")).scalar_one():
            raise RuntimeError("V1_REVOCATION_DOWNGRADE_REFUSED")
    executor, service = names["executor"], names["service"]
    for table in ("users", "account_sessions", *EXECUTOR_TABLE_GRANTS):
        op.execute(f"REVOKE ALL ON kalillac.{table} FROM {executor}")
    op.execute(f"REVOKE ALL (id, is_active, quarantined_at, deletion_requested_at, sessions_valid_after, updated_at)"
               f" ON kalillac.users FROM {executor}")
    op.execute(f"REVOKE ALL (id, user_id, token_hash, created_at, expires_at, revoked_at)"
               f" ON kalillac.account_sessions FROM {executor}")
    # Only its owner may drop the function; the owner reaches E through SET only.
    op.execute(f"SET ROLE {executor}")
    op.execute(f"DROP FUNCTION {SIGNATURE}")
    op.execute(f"SET ROLE {names['owner']}")
    op.execute(f"REVOKE USAGE ON SCHEMA kalillac FROM {executor}, {service}")
