"""Add default-off mailbox-first registration and separate-principal fences.

Revision ID: 0011_registration_intents
Revises: 0010_spaces
"""
from __future__ import annotations
import os
import uuid
from alembic import op
import sqlalchemy as sa
from kalillac_accounts.registration_schema import guard_functions as _functions
from kalillac_db.migration_roles import migration_app_role, migration_owner_role, validate_role_name

revision = "0011_registration_intents"
down_revision = "0010_spaces"
branch_labels = depends_on = None
SCHEMA = "kalillac"
INTENT_COLUMNS = "id,address_id,generation,purpose,format_version,address_lookup_key_version,address_lookup_digest,token_digest,email_ciphertext,email_nonce,email_key_version,is_current,state,created_at,expires_at,consumed_at,superseded_at,expired_at"
INTENT_UPDATES = "email_ciphertext,email_nonce,email_key_version,is_current,state,consumed_at,superseded_at,expired_at"
USER_COLUMNS = "id,email,password_hash,created_at,updated_at,is_active,email_verified_at,registration_intent_id,quarantined_at,sessions_valid_after"
USER_UPDATES = "password_hash,updated_at,is_active,email_verified_at"

class _Xid8(sa.types.UserDefinedType):
    cache_ok = True
    def get_col_spec(self, **kw):
        return "xid8"

def _roles():
    a = validate_role_name(os.getenv("KALILLAC_MIGRATION_REGISTRATION_ROLE", "kalillac_registration"), "KALILLAC_MIGRATION_REGISTRATION_ROLE")
    r, o = migration_app_role(), migration_owner_role()
    if len({a, r, o}) != 3:
        raise RuntimeError("REGISTRATION_ROLE_INVALID")
    return r, a, o

def upgrade():
    xid = _Xid8().with_variant(sa.String(20), "sqlite")
    op.create_table("account_registration_intents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("address_id", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("format_version", sa.SmallInteger(), nullable=False),
        sa.Column("address_lookup_key_version", sa.String(32), nullable=False),
        sa.Column("address_lookup_digest", sa.String(64), nullable=False),
        sa.Column("token_digest", sa.String(64), nullable=False),
        sa.Column("email_ciphertext", sa.LargeBinary()),
        sa.Column("email_nonce", sa.LargeBinary()),
        sa.Column("email_key_version", sa.String(32)),
        sa.Column("consumed_xid", xid),
        sa.Column("provenance_claimed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("superseded_at", sa.DateTime(timezone=True)),
        sa.Column("expired_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("token_digest", name="uq_account_registration_intents_token_digest"),
        sa.UniqueConstraint("address_id", "generation", name="uq_account_registration_intents_address_id"),
        sa.CheckConstraint("generation > 0", name=op.f("ck_account_registration_intents_generation")),
        sa.CheckConstraint("purpose = 'finish_registration' AND format_version = 1", name=op.f("ck_account_registration_intents_purpose_format")),
        sa.CheckConstraint("expires_at > created_at", name=op.f("ck_account_registration_intents_expiry")),
        sa.CheckConstraint("substr(address_lookup_key_version,1,12) = 'reg-lookup-v' AND length(address_lookup_key_version) BETWEEN 13 AND 21 AND (email_key_version IS NULL OR (substr(email_key_version,1,9) = 'reg-enc-v' AND length(email_key_version) BETWEEN 10 AND 18))", name=op.f("ck_account_registration_intents_key_versions")),
        sa.CheckConstraint("length(token_digest) = 64 AND length(address_lookup_digest) = 64 AND length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(token_digest, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0 AND length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(address_lookup_digest, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0", name=op.f("ck_account_registration_intents_digest_length")),
        sa.CheckConstraint("email_nonce IS NULL OR length(email_nonce) = 12", name=op.f("ck_account_registration_intents_nonce_length")),
        sa.CheckConstraint("email_ciphertext IS NULL OR length(email_ciphertext) BETWEEN 17 AND 1296", name=op.f("ck_account_registration_intents_ciphertext_length")),
        sa.CheckConstraint(
            "(state = 'pending' AND is_current AND email_ciphertext IS NOT NULL AND email_nonce IS NOT NULL AND email_key_version IS NOT NULL AND consumed_at IS NULL AND superseded_at IS NULL AND expired_at IS NULL) OR "
            "(state = 'consumed' AND NOT is_current AND email_ciphertext IS NULL AND email_nonce IS NULL AND email_key_version IS NULL AND consumed_at IS NOT NULL AND consumed_at >= created_at AND superseded_at IS NULL AND expired_at IS NULL) OR "
            "(state = 'superseded' AND NOT is_current AND email_ciphertext IS NULL AND email_nonce IS NULL AND email_key_version IS NULL AND consumed_at IS NULL AND superseded_at IS NOT NULL AND superseded_at >= created_at AND expired_at IS NULL) OR "
            "(state = 'expired' AND NOT is_current AND email_ciphertext IS NULL AND email_nonce IS NULL AND email_key_version IS NULL AND consumed_at IS NULL AND superseded_at IS NULL AND expired_at IS NOT NULL AND expired_at >= created_at)",
            name=op.f("ck_account_registration_intents_lifecycle")),
        sa.CheckConstraint("(state = 'consumed' AND consumed_xid IS NOT NULL) OR (state <> 'consumed' AND consumed_xid IS NULL AND NOT provenance_claimed)", name=op.f("ck_intents_provenance_state")),
        schema=SCHEMA)
    for name, cols, unique, where in [
        ("uq_registration_current_address", ["address_id"], True, "is_current"),
        ("uq_registration_current_lookup", ["address_lookup_key_version", "address_lookup_digest"], True, "is_current"),
        ("ix_registration_lookup", ["address_lookup_key_version", "address_lookup_digest", "generation"], False, None),
        ("ix_registration_cleanup", ["expires_at", "id"], False, None)]:
        kw = {"postgresql_where": sa.text(where), "sqlite_where": sa.text(where)} if where else {}
        op.create_index(name, "account_registration_intents", cols, unique=unique, schema=SCHEMA, **kw)
    op.create_table("account_registration_activation",
        sa.Column("singleton_id", sa.SmallInteger(), primary_key=True),
        sa.Column("database_instance_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("policy_version", sa.SmallInteger(), nullable=False),
        sa.Column("activation_id", sa.Uuid(), unique=True),
        sa.Column("cutover_id", sa.Uuid()),
        sa.Column("deployment_id", sa.String(64)),
        sa.Column("evidence_sha256", sa.String(64)),
        sa.Column("inventory_at", sa.DateTime(timezone=True)),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.Column("legacy_unverified_count", sa.BigInteger()),
        sa.CheckConstraint("singleton_id = 1 AND policy_version = 1", name=op.f("ck_account_registration_activation_activation_singleton")),
        sa.CheckConstraint(
            "(state = 'unactivated' AND activation_id IS NULL AND cutover_id IS NULL AND deployment_id IS NULL AND evidence_sha256 IS NULL AND inventory_at IS NULL AND activated_at IS NULL AND legacy_unverified_count IS NULL) OR "
            "(state = 'active_zero_legacy' AND activation_id IS NOT NULL AND cutover_id IS NOT NULL AND deployment_id IS NOT NULL AND evidence_sha256 IS NOT NULL AND length(evidence_sha256) = 64 AND length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(evidence_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0 AND inventory_at IS NOT NULL AND activated_at IS NOT NULL AND activated_at >= inventory_at AND legacy_unverified_count = 0)",
            name=op.f("ck_account_registration_activation_activation_shape")), schema=SCHEMA)
    t = sa.table("account_registration_activation", sa.column("singleton_id", sa.SmallInteger()), sa.column("database_instance_id", sa.Uuid()), sa.column("state", sa.String(32)), sa.column("policy_version", sa.SmallInteger()), schema=SCHEMA)
    op.bulk_insert(t, [{"singleton_id": 1, "database_instance_id": uuid.uuid4(), "state": "unactivated", "policy_version": 1}])
    if op.get_context().dialect.name == "sqlite":
        # Native nullable ADD preserves child rows with foreign_keys=ON. Rebuilding
        # users would cascade-delete account-linked data inside callers' transactions.
        op.execute("ALTER TABLE kalillac.users ADD COLUMN registration_intent_id CHAR(32) REFERENCES account_registration_intents(id) ON DELETE SET NULL")
    else:
        op.add_column("users", sa.Column("registration_intent_id", sa.Uuid()), schema=SCHEMA)
        op.create_foreign_key("fk_users_registration_intent", "users", "account_registration_intents", ["registration_intent_id"], ["id"], source_schema=SCHEMA, referent_schema=SCHEMA, ondelete="SET NULL")
    op.add_column("users", sa.Column("quarantined_at", sa.DateTime(timezone=True)), schema=SCHEMA)
    op.add_column("users", sa.Column("sessions_valid_after", sa.DateTime(timezone=True)), schema=SCHEMA)
    op.create_index("uq_users_registration_intent", "users", ["registration_intent_id"], unique=True, schema=SCHEMA)
    if op.get_context().dialect.name == "postgresql":
        _postgres()

def _postgres():
    r, a, o = _roles()
    bind = op.get_bind()
    offline = op.get_context().as_sql
    m = "kalillac_migrate" if offline else bind.exec_driver_sql("SELECT session_user").scalar_one()
    validate_role_name(m, "migration session")
    if len({r, a, o, m}) != 4:
        raise RuntimeError("REGISTRATION_ROLE_INVALID")
    if not offline:
        row = bind.execute(sa.text("SELECT rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls,rolinherit FROM pg_catalog.pg_roles WHERE rolname=:a"), {"a": a}).one_or_none()
        if row is None or tuple(row) != (True, False, False, False, False, False, False):
            raise RuntimeError("REGISTRATION_ROLE_INVALID")
        for left, right in [(r,a),(r,o),(r,m),(a,r),(a,o),(a,m)]:
            if bind.execute(sa.text("SELECT pg_catalog.pg_has_role(:l,:r,'MEMBER')"), {"l":left,"r":right}).scalar_one():
                raise RuntimeError("REGISTRATION_ROLE_INVALID")
        for principal in (r,a):
            if bind.execute(sa.text("SELECT count(*) FROM pg_catalog.pg_roles WHERE rolname<>:p AND pg_catalog.pg_has_role(:p,oid,'MEMBER')"), {"p":principal}).scalar_one():
                raise RuntimeError("REGISTRATION_ROLE_INVALID")
    functions = _functions(a,m)
    for name, body in functions.items():
        op.execute(f"CREATE FUNCTION kalillac.{name} {body}")
        signature = name + ("(boolean)" if name == "registration_control_lock" else "()")
        op.execute(f"ALTER FUNCTION kalillac.{signature} OWNER TO {o}")
        op.execute(f"REVOKE ALL ON FUNCTION kalillac.{signature} FROM PUBLIC, {r}, {a}")
    for name, table, events, function in [
        ("trg_registration_intent_lifecycle","account_registration_intents","INSERT OR UPDATE OR DELETE","registration_intent_lifecycle_guard"),
        ("trg_users_registration_provenance","users","INSERT","registration_user_insert_guard"),
        ("trg_users_registration_identity","users","UPDATE","registration_user_identity_guard")]:
        op.execute(f"CREATE TRIGGER {name} BEFORE {events} ON kalillac.{table} FOR EACH ROW EXECUTE FUNCTION kalillac.{function}()")
        op.execute(f"ALTER TABLE kalillac.{table} ENABLE ALWAYS TRIGGER {name}")
    op.execute(f"REVOKE ALL ON kalillac.account_registration_intents,kalillac.account_registration_activation FROM PUBLIC,{r},{a}")
    op.execute(f"GRANT SELECT ON kalillac.account_registration_intents,kalillac.account_registration_activation TO {r},{a}")
    op.execute(f"GRANT DELETE,INSERT({INTENT_COLUMNS}),UPDATE({INTENT_UPDATES}) ON kalillac.account_registration_intents TO {a}")
    op.execute(f"REVOKE INSERT,UPDATE ON kalillac.users FROM {r}")
    op.execute(f"REVOKE INSERT({USER_COLUMNS}),UPDATE({USER_COLUMNS}) ON kalillac.users FROM {r},{a}")
    op.execute(f"REVOKE ALL ON kalillac.users,kalillac.account_entitlements FROM {a}")
    op.execute(f"GRANT INSERT({USER_COLUMNS}),UPDATE({USER_UPDATES}) ON kalillac.users TO {r}")
    op.execute(f"GRANT SELECT,INSERT({USER_COLUMNS}) ON kalillac.users TO {a}")
    op.execute(f"GRANT SELECT,INSERT(user_id,tier,source,created_at,updated_at,expires_at) ON kalillac.account_entitlements TO {a}")
    op.execute(f"GRANT EXECUTE ON FUNCTION kalillac.registration_control_lock(boolean) TO {r},{a}")
    _grant_access(a)


def downgrade():
    bind = op.get_bind()
    if not op.get_context().as_sql:
        if bind.execute(sa.text("SELECT count(*) FROM kalillac.account_registration_activation WHERE state <> 'unactivated'")).scalar_one():
            raise RuntimeError("REGISTRATION_ACTIVE_DOWNGRADE_REFUSED")
        if bind.execute(sa.text("SELECT count(*) FROM kalillac.users WHERE quarantined_at IS NOT NULL OR sessions_valid_after IS NOT NULL")).scalar_one():
            raise RuntimeError("REGISTRATION_TRANSITION_DOWNGRADE_REFUSED")
    if op.get_context().dialect.name == "postgresql":
        r,a,o = _roles()
        _revoke_owned_access(a)
        for name, table in [("trg_users_registration_identity","users"),("trg_users_registration_provenance","users"),("trg_registration_intent_lifecycle","account_registration_intents")]:
            op.execute(f"DROP TRIGGER {name} ON kalillac.{table}")
        for fn in ["registration_user_identity_guard()","registration_user_insert_guard()","registration_intent_lifecycle_guard()","registration_control_lock(boolean)"]:
            op.execute(f"DROP FUNCTION kalillac.{fn}")
        op.execute(f"REVOKE ALL ON kalillac.users,kalillac.account_entitlements FROM {a}")
        op.execute(f"REVOKE INSERT({USER_COLUMNS}),UPDATE({USER_COLUMNS}) ON kalillac.users FROM {a},{r}")
        op.execute(f"REVOKE INSERT(user_id,tier,source,created_at,updated_at,expires_at) ON kalillac.account_entitlements FROM {a}")
        op.execute(f"GRANT SELECT,INSERT,UPDATE,DELETE ON kalillac.users TO {r}")
    op.drop_index("uq_users_registration_intent", table_name="users", schema=SCHEMA)
    if op.get_context().dialect.name != "sqlite":
        op.drop_constraint("fk_users_registration_intent", "users", type_="foreignkey", schema=SCHEMA)
    for column in ["registration_intent_id", "quarantined_at", "sessions_valid_after"]:
        op.drop_column("users", column, schema=SCHEMA)
    op.drop_table("account_registration_intents", schema=SCHEMA)
    op.drop_table("account_registration_activation", schema=SCHEMA)

def _grant_access(a):
    # Remember only grants this migration actually adds. Pre-existing direct
    # grants and PUBLIC access are never owned or revoked by this revision.
    op.execute(f"""DO $access$
DECLARE schema_had boolean; database_had boolean;
BEGIN
 SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_namespace n,
  LATERAL pg_catalog.aclexplode(COALESCE(n.nspacl,pg_catalog.acldefault('n',n.nspowner))) x
  WHERE n.nspname='kalillac' AND x.grantee='{a}'::regrole AND x.privilege_type='USAGE') INTO schema_had;
 SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_database d,
  LATERAL pg_catalog.aclexplode(COALESCE(d.datacl,pg_catalog.acldefault('d',d.datdba))) x
  WHERE d.datname=current_database() AND x.grantee='{a}'::regrole AND x.privilege_type='CONNECT') INTO database_had;
 IF NOT schema_had THEN GRANT USAGE ON SCHEMA kalillac TO {a}; END IF;
 IF NOT database_had THEN EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I',current_database(),'{a}'); END IF;
 EXECUTE format('COMMENT ON TABLE kalillac.account_registration_activation IS %L',
  json_build_object('format_version',1,'registration_role','{a}',
   'schema_usage_added',NOT schema_had,'database_connect_added',NOT database_had)::text);
END $access$""")

def _revoke_owned_access(a):
    op.execute(f"""DO $access$
DECLARE receipt jsonb;
BEGIN
 receipt := pg_catalog.obj_description('kalillac.account_registration_activation'::regclass,'pg_class')::jsonb;
 IF receipt IS NULL OR jsonb_typeof(receipt)<>'object'
 OR (SELECT array_agg(k ORDER BY k) FROM jsonb_object_keys(receipt) AS x(k))
    IS DISTINCT FROM ARRAY['database_connect_added','format_version','registration_role','schema_usage_added']::text[]
 OR receipt->>'format_version'<>'1' OR receipt->>'registration_role'<>'{a}'
 OR jsonb_typeof(receipt->'schema_usage_added')<>'boolean'
 OR jsonb_typeof(receipt->'database_connect_added')<>'boolean'
 THEN RAISE EXCEPTION 'REGISTRATION_GRANT_STATE_UNREADABLE'; END IF;
 IF (receipt->>'schema_usage_added')::boolean THEN REVOKE USAGE ON SCHEMA kalillac FROM {a}; END IF;
 IF (receipt->>'database_connect_added')::boolean THEN
  EXECUTE format('REVOKE CONNECT ON DATABASE %I FROM %I',current_database(),'{a}');
 END IF;
END $access$""")
