"""Prepare default-off V1 account-security state (prepared, never active).

Revision ID: 0012_v1_account_security
Revises: 0011_registration_intents

Additive only. users gains a nullable deletion request time and a
default-false password_reset_required restore gate on every dialect, so the
User model and SQLite fixtures stay identical. On PostgreSQL the owner also
receives the prepared security tables and a singleton activation latch in
state 'prepared'.

Nothing here grants, creates functions or triggers, writes security rows or
activates V1: the new tables are owner-only, so R, A and PUBLIC have no
access and no runtime principal can write them. That is checked inside the
revision's transaction: owner default privileges or any other effective
authority over the new tables abort it completely. Offline downgrade through
this revision is unsupported; online downgrade refuses non-pristine state.
Event/outbox payload,
retention, claim/retry and capacity rules are deliberately absent until a
reviewed writer defines them from measured rates. Admission uses only the
approved bounds: at most 4096 account entries, two rings of at most six
timestamps per account, at most 60 per aggregate class. (Manual entry
creation stopping at 2048 occupied entries is a writer rule, not storage.)
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from kalillac_db.migration_roles import migration_app_role, migration_owner_role, validate_role_name

revision = "0012_v1_account_security"
down_revision = "0011_registration_intents"
branch_labels = depends_on = None
SCHEMA = "kalillac"

V1_TABLES = ("account_outbox", "account_security_events", "account_security_admission_aggregate",
             "account_security_admission", "account_security_activation")
EVENT_TYPES = ("session_revoke_others", "session_revoke_all", "password_reset")
TASK_TYPES = ("notification", "reconciliation")
ADMISSION_CLASSES = ("manual", "recovery")


def _in(column, values):
    return column + " IN (" + ",".join("'" + v + "'" for v in values) + ")"


def _ring(column, bound):
    # Empty, or one-dimensional, 1-based, at most `bound` timestamps and no NULL.
    # CASE orders evaluation: array_position() would raise on a 2-D array.
    return (f"CASE WHEN cardinality({column}) = 0 THEN true"
            f" WHEN array_ndims({column}) = 1 AND array_lower({column}, 1) = 1 AND cardinality({column}) <= {bound}"
            f" THEN array_position({column}, NULL) IS NULL ELSE false END")


def upgrade():
    op.add_column("users", sa.Column("deletion_requested_at", sa.DateTime(timezone=True)), schema=SCHEMA)
    op.add_column("users", sa.Column("password_reset_required", sa.Boolean(), nullable=False,
                                     server_default=sa.false()), schema=SCHEMA)
    if op.get_context().dialect.name == "postgresql":
        _postgres()


def _postgres():
    stamp = postgresql.ARRAY(sa.DateTime(timezone=True))
    op.create_table("account_security_activation",
        sa.Column("singleton_id", sa.SmallInteger(), nullable=False, autoincrement=False),
        sa.Column("schema_version", sa.SmallInteger(), nullable=False),
        sa.Column("database_instance_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("prepared_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("authority_version", sa.SmallInteger()),
        sa.Column("activation_id", sa.Uuid()),
        sa.Column("deployment_id", sa.String(64)),
        sa.Column("release_sha", sa.String(40)),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("singleton_id", name=op.f("pk_account_security_activation")),
        sa.UniqueConstraint("database_instance_id", name=op.f("uq_account_security_activation_database_instance_id")),
        sa.UniqueConstraint("activation_id", name=op.f("uq_account_security_activation_activation_id")),
        sa.ForeignKeyConstraint(["database_instance_id"], [f"{SCHEMA}.account_registration_activation.database_instance_id"],
                                name=op.f("fk_account_security_activation_database_instance")),
        sa.CheckConstraint("singleton_id = 1 AND schema_version = 1", name=op.f("ck_account_security_activation_singleton")),
        # Every active binding field is explicitly NOT NULL: a NULL comparison or
        # regex match is unknown, and CHECK accepts unknown.
        sa.CheckConstraint(
            "(state = 'prepared' AND authority_version IS NULL AND activation_id IS NULL AND deployment_id IS NULL"
            " AND release_sha IS NULL AND activated_at IS NULL) OR "
            "(state = 'active' AND authority_version IS NOT NULL AND authority_version >= 1"
            " AND activation_id IS NOT NULL"
            " AND deployment_id IS NOT NULL AND deployment_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'"
            " AND release_sha IS NOT NULL AND release_sha ~ '^[0-9a-f]{40}$'"
            " AND activated_at IS NOT NULL AND activated_at >= prepared_at)",
            name=op.f("ck_account_security_activation_state")),
        schema=SCHEMA)
    op.create_table("account_security_events",
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("operation_id", name=op.f("pk_account_security_events")),
        sa.UniqueConstraint("operation_id", "user_id", name=op.f("uq_account_security_events_operation_account")),
        sa.ForeignKeyConstraint(["user_id"], [f"{SCHEMA}.users.id"], name=op.f("fk_account_security_events_user")),
        sa.CheckConstraint(_in("event_type", EVENT_TYPES), name=op.f("ck_account_security_events_type")),
        schema=SCHEMA)
    op.create_index("ix_account_security_events_user", "account_security_events", ["user_id"], schema=SCHEMA)
    op.create_table("account_outbox",
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("task_type", sa.String(32), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("operation_id", "task_type", name=op.f("pk_account_outbox")),
        # A task belongs to its own account's audited operation, never another account's.
        sa.ForeignKeyConstraint(["operation_id", "user_id"],
                                [f"{SCHEMA}.account_security_events.operation_id", f"{SCHEMA}.account_security_events.user_id"],
                                name=op.f("fk_account_outbox_operation_account")),
        sa.CheckConstraint(_in("task_type", TASK_TYPES), name=op.f("ck_account_outbox_task_type")),
        schema=SCHEMA)
    op.create_table("account_security_admission",
        # Assigned by a future reviewed writer, never by a sequence.
        sa.Column("entry_slot", sa.SmallInteger(), nullable=False, autoincrement=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("manual_charges", stamp, nullable=False),
        sa.Column("recovery_charges", stamp, nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("entry_slot", name=op.f("pk_account_security_admission")),
        sa.UniqueConstraint("user_id", name=op.f("uq_account_security_admission_user")),
        sa.ForeignKeyConstraint(["user_id"], [f"{SCHEMA}.users.id"], name=op.f("fk_account_security_admission_user")),
        sa.CheckConstraint("entry_slot BETWEEN 0 AND 4095", name=op.f("ck_account_security_admission_slot")),
        sa.CheckConstraint("(" + _ring("manual_charges", 6) + ") AND (" + _ring("recovery_charges", 6) + ")",
                           name=op.f("ck_account_security_admission_rings")),
        schema=SCHEMA)
    op.create_table("account_security_admission_aggregate",
        sa.Column("admission_class", sa.String(16), nullable=False),
        sa.Column("charges", stamp, nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("admission_class", name=op.f("pk_account_security_admission_aggregate")),
        sa.CheckConstraint(_in("admission_class", ADMISSION_CLASSES), name=op.f("ck_account_security_admission_aggregate_class")),
        sa.CheckConstraint(_ring("charges", 60), name=op.f("ck_account_security_admission_aggregate_ring")),
        schema=SCHEMA)
    # The latch is bound to the database instance identity recorded by 0011.
    op.execute("INSERT INTO kalillac.account_security_activation"
               " (singleton_id, schema_version, database_instance_id, state, prepared_at)"
               " SELECT 1, 1, database_instance_id, 'prepared', clock_timestamp()"
               " FROM kalillac.account_registration_activation WHERE singleton_id = 1")
    # Server-side, so offline SQL carries the same refusal.
    op.execute(privilege_guard(migration_app_role(), migration_owner_role()))
    if not op.get_context().as_sql:
        if op.get_bind().execute(sa.text("SELECT count(*) FROM kalillac.account_security_activation")).scalar_one() != 1:
            raise RuntimeError("V1_SECURITY_PREPARATION_INVALID")


# Owner-only is verified, not assumed. Read-only: the statement raises and the
# whole revision rolls back in its own transaction; nothing is revoked or
# repaired.
#
# First the configured runtime role R and the registration role A that 0011
# recorded, by the startup boundary: no superuser-class attribute and no role
# membership at all, direct or multi-hop, whatever its inherit_option or
# set_option (INHERIT confers the role's privileges, SET confers SET ROLE to
# it, which no effective-privilege check sees), including the owner and the
# predefined write roles. The migration login's trusted membership in the
# owner is never an exception for R or A.
#
# Then everyone else: owner default privileges, a grant on the new tables, or
# effective write authority over them. Superusers and roles inheriting the
# owner's privileges (the trusted migration path) already hold owner
# authority; predefined roles are skipped, their members are not.
PRIVILEGE_GUARD = """DO $v1_guard$
DECLARE
 keeper oid := (SELECT c.relowner FROM pg_catalog.pg_class c WHERE c.oid = 'kalillac.users'::pg_catalog.regclass);
 runtime oid := pg_catalog.to_regrole(pg_catalog.quote_ident(%(runtime)s));
 registration oid := pg_catalog.to_regrole(pg_catalog.quote_ident(pg_catalog.obj_description(
  'kalillac.account_registration_activation'::pg_catalog.regclass, 'pg_class')::pg_catalog.json ->> 'registration_role'));
BEGIN
 IF keeper IS DISTINCT FROM pg_catalog.to_regrole(pg_catalog.quote_ident(%(owner)s))
 OR runtime IS NULL OR registration IS NULL OR runtime IN (keeper, registration) OR registration = keeper
 OR (SELECT r.oid FROM pg_catalog.pg_roles r WHERE r.rolname = SESSION_USER) IN (runtime, registration)
 OR EXISTS (SELECT 1 FROM pg_catalog.pg_roles r WHERE r.oid IN (runtime, registration)
            AND (r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication OR r.rolbypassrls))
 OR EXISTS (WITH RECURSIVE reach(roleid) AS (
             SELECT m.roleid FROM pg_catalog.pg_auth_members m WHERE m.member IN (runtime, registration)
             UNION SELECT m.roleid FROM pg_catalog.pg_auth_members m JOIN reach ON m.member = reach.roleid)
            SELECT 1 FROM reach)
 OR EXISTS (SELECT 1 FROM pg_catalog.pg_roles g
            WHERE (g.oid <> runtime AND pg_catalog.pg_has_role(runtime, g.oid, 'MEMBER'))
            OR (g.oid <> registration AND pg_catalog.pg_has_role(registration, g.oid, 'MEMBER')))
 THEN RAISE EXCEPTION USING MESSAGE = 'V1_SECURITY_PRIVILEGES_UNSAFE';
 END IF;
 IF EXISTS (SELECT 1 FROM pg_catalog.pg_default_acl d, pg_catalog.aclexplode(d.defaclacl) x
            WHERE d.defaclrole = keeper AND x.grantee <> keeper)
 OR EXISTS (SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace,
            pg_catalog.aclexplode(COALESCE(c.relacl, pg_catalog.acldefault('r', c.relowner))) x
            WHERE n.nspname = 'kalillac' AND c.relname = ANY (%(tables)s)
            AND (c.relowner <> keeper OR x.grantee <> keeper))
 OR EXISTS (SELECT 1 FROM (SELECT r.rolname::text FROM pg_catalog.pg_roles r WHERE NOT r.rolsuper AND r.oid >= 16384
            AND (r.oid IN (runtime, registration) OR NOT pg_catalog.pg_has_role(r.oid, keeper, 'USAGE'))
            UNION ALL SELECT 'public') p(principal),
            pg_catalog.unnest(%(tables)s) t(relation),
            pg_catalog.unnest(ARRAY['INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER']) w(privilege)
            WHERE pg_catalog.has_table_privilege(p.principal::name, 'kalillac.' || t.relation, w.privilege)
            OR (w.privilege IN ('INSERT', 'UPDATE', 'REFERENCES')
                AND pg_catalog.has_any_column_privilege(p.principal::name, 'kalillac.' || t.relation, w.privilege)))
 THEN RAISE EXCEPTION USING MESSAGE = 'V1_SECURITY_PRIVILEGES_UNSAFE';
 END IF;
END $v1_guard$"""


def privilege_guard(runtime, owner):
    """The guard for the configured R and owner; names are validated simple
    identifiers, then quoted again and resolved server-side."""
    names = {"runtime": validate_role_name(runtime, "KALILLAC_MIGRATION_APP_ROLE"),
             "owner": validate_role_name(owner, "KALILLAC_MIGRATION_OWNER_ROLE")}
    return PRIVILEGE_GUARD % {"tables": "ARRAY[" + ", ".join("'" + t + "'" for t in V1_TABLES) + "]",
                              **{key: "'" + name + "'" for key, name in names.items()}}


def downgrade():
    if op.get_context().as_sql:
        # Offline SQL cannot see whether security history exists, so it could
        # be used to erase it. Refused before anything destructive is emitted.
        raise RuntimeError("V1_SECURITY_OFFLINE_DOWNGRADE_UNSUPPORTED")
    postgres = op.get_context().dialect.name == "postgresql"
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT count(*) FROM kalillac.users WHERE deletion_requested_at IS NOT NULL"
                            " OR password_reset_required")).scalar_one():
        raise RuntimeError("V1_SECURITY_DOWNGRADE_REFUSED")
    if postgres:
        if bind.execute(sa.text("SELECT count(*) FROM kalillac.account_security_activation"
                                " WHERE state <> 'prepared'")).scalar_one():
            raise RuntimeError("V1_SECURITY_DOWNGRADE_REFUSED")
        for table in V1_TABLES[:-1]:
            if bind.execute(sa.text(f"SELECT count(*) FROM kalillac.{table}")).scalar_one():
                raise RuntimeError("V1_SECURITY_DOWNGRADE_REFUSED")
    if postgres:
        for table in V1_TABLES:
            op.drop_table(table, schema=SCHEMA)
    op.drop_column("users", "password_reset_required", schema=SCHEMA)
    op.drop_column("users", "deletion_requested_at", schema=SCHEMA)
