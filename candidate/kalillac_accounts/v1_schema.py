"""V1 account-security schema states and the application-startup authority gate.

Read-only. Three states are distinguished on PostgreSQL:

- legacy: no 0012 object exists; the 0011 catalog remains authoritative;
- prepared: every 0012 object matches the exact PostgreSQL definitions below
  (permanent tables, columns, constraints and indexes, not merely names), the
  0011 guards and triggers match their trusted definitions, no other writer
  and no view or rule bridge into protected state exists in any schema,
  neither the runtime role R nor the registration role A can reach
  owner authority or any V1 write, and the latch reads 'prepared';
- active: the same exact objects with the latch reading 'active'.

Anything partial, altered or more privileged is refused, never repaired.

Two prepared catalogs are accepted: 0012 alone, and 0012 with 0013's
private session-revocation authority (V1-02B): the exact function owned by
the NOLOGIN executor role E and executable only by the NOLOGIN service role
S, E's exact grants, schema USAGE for both, and the operator's single
owner->E SET edge. S and E are named by mandatory settings; before 0013
they may be absent, or present with nothing but that edge.

Qualifying an active latch also requires the complete private-authority
catalog (issuance, epoch, recovery and the guard version), which later
reviewed slices define, so application startup with V1 enabled refuses in
every state. Nothing here grants, migrates, writes or activates.
"""
from __future__ import annotations

LEGACY, PREPARED, ACTIVE = "legacy", "prepared", "active"

TABLES = ("account_outbox", "account_security_activation", "account_security_admission",
          "account_security_admission_aggregate", "account_security_events")

_TS, _TS_RING = "timestamp with time zone", "timestamp with time zone[]"
# (format_type, NOT NULL); no V1 table column has a default.
COLUMNS = {
    "account_outbox": {"operation_id": ("uuid", True), "task_type": ("character varying(32)", True),
                       "user_id": ("uuid", True), "created_at": (_TS, True)},
    "account_security_activation": {
        "singleton_id": ("smallint", True), "schema_version": ("smallint", True),
        "database_instance_id": ("uuid", True), "state": ("character varying(16)", True),
        "prepared_at": (_TS, True), "authority_version": ("smallint", False), "activation_id": ("uuid", False),
        "deployment_id": ("character varying(64)", False), "release_sha": ("character varying(40)", False),
        "activated_at": (_TS, False)},
    "account_security_admission": {"entry_slot": ("smallint", True), "user_id": ("uuid", True),
                                   "manual_charges": (_TS_RING, True), "recovery_charges": (_TS_RING, True),
                                   "last_seen_at": (_TS, True)},
    "account_security_admission_aggregate": {"admission_class": ("character varying(16)", True),
                                             "charges": (_TS_RING, True), "last_seen_at": (_TS, True)},
    "account_security_events": {"operation_id": ("uuid", True), "user_id": ("uuid", True),
                                "event_type": ("character varying(32)", True), "occurred_at": (_TS, True)},
}
# users additions: (format_type, NOT NULL, default expression)
USER_COLUMNS = {"deletion_requested_at": (_TS, False, None), "password_reset_required": ("boolean", True, "false")}


def _ring(column, bound):
    return (f"\nCASE\n    WHEN (cardinality({column}) = 0) THEN true\n"
            f"    WHEN ((array_ndims({column}) = 1) AND (array_lower({column}, 1) = 1) AND (cardinality({column}) <= {bound}))"
            f" THEN (array_position({column}, NULL::timestamp with time zone) IS NULL)\n    ELSE false\nEND")


def _among(column, values):
    return (f"CHECK ((({column})::text = ANY ((ARRAY["
            + ", ".join(f"'{v}'::character varying" for v in values) + "])::text[])))")


# PostgreSQL 16 renderings, as pinned for the registration catalog.
CONSTRAINTS = {
    "account_outbox": {
        "ck_account_outbox_task_type": ("c", _among("task_type", ("notification", "reconciliation"))),
        "fk_account_outbox_operation_account": ("f", "FOREIGN KEY (operation_id, user_id) REFERENCES"
                                                     " kalillac.account_security_events(operation_id, user_id)"),
        "pk_account_outbox": ("p", "PRIMARY KEY (operation_id, task_type)")},
    "account_security_activation": {
        "ck_account_security_activation_singleton": ("c", "CHECK (((singleton_id = 1) AND (schema_version = 1)))"),
        "ck_account_security_activation_state": ("c",
            "CHECK (((((state)::text = 'prepared'::text) AND (authority_version IS NULL) AND (activation_id IS NULL)"
            " AND (deployment_id IS NULL) AND (release_sha IS NULL) AND (activated_at IS NULL)) OR"
            " (((state)::text = 'active'::text) AND (authority_version IS NOT NULL) AND (authority_version >= 1)"
            " AND (activation_id IS NOT NULL) AND (deployment_id IS NOT NULL)"
            " AND ((deployment_id)::text ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'::text)"
            " AND (release_sha IS NOT NULL) AND ((release_sha)::text ~ '^[0-9a-f]{40}$'::text)"
            " AND (activated_at IS NOT NULL) AND (activated_at >= prepared_at))))"),
        "fk_account_security_activation_database_instance": ("f", "FOREIGN KEY (database_instance_id) REFERENCES"
            " kalillac.account_registration_activation(database_instance_id)"),
        "pk_account_security_activation": ("p", "PRIMARY KEY (singleton_id)"),
        "uq_account_security_activation_activation_id": ("u", "UNIQUE (activation_id)"),
        "uq_account_security_activation_database_instance_id": ("u", "UNIQUE (database_instance_id)")},
    "account_security_admission": {
        "ck_account_security_admission_rings": ("c", "CHECK ((" + _ring("manual_charges", 6) + " AND"
                                                     + _ring("recovery_charges", 6) + "))"),
        "ck_account_security_admission_slot": ("c", "CHECK (((entry_slot >= 0) AND (entry_slot <= 4095)))"),
        "fk_account_security_admission_user": ("f", "FOREIGN KEY (user_id) REFERENCES kalillac.users(id)"),
        "pk_account_security_admission": ("p", "PRIMARY KEY (entry_slot)"),
        "uq_account_security_admission_user": ("u", "UNIQUE (user_id)")},
    "account_security_admission_aggregate": {
        "ck_account_security_admission_aggregate_class": ("c", _among("admission_class", ("manual", "recovery"))),
        "ck_account_security_admission_aggregate_ring": ("c", "CHECK (" + _ring("charges", 60) + ")"),
        "pk_account_security_admission_aggregate": ("p", "PRIMARY KEY (admission_class)")},
    "account_security_events": {
        "ck_account_security_events_type": ("c", _among("event_type", ("session_revoke_others", "session_revoke_all",
                                                                        "password_reset"))),
        "fk_account_security_events_user": ("f", "FOREIGN KEY (user_id) REFERENCES kalillac.users(id)"),
        "pk_account_security_events": ("p", "PRIMARY KEY (operation_id)"),
        "uq_account_security_events_operation_account": ("u", "UNIQUE (operation_id, user_id)")},
}
# The index a foreign key resolves through; key constraints use their own.
REFERENCED = {"fk_account_outbox_operation_account": "uq_account_security_events_operation_account",
              "fk_account_security_activation_database_instance":
                  "uq_account_registration_activation_database_instance_id",
              "fk_account_security_admission_user": "pk_users", "fk_account_security_events_user": "pk_users"}
# (table, unique, primary, key columns in order). Every index is a permanent,
# valid, ready, immediate btree with default operator classes and ordering and
# no expression, predicate, INCLUDE column or exclusion semantics.
INDEXES = {
    "pk_account_outbox": ("account_outbox", True, True, ("operation_id", "task_type")),
    "pk_account_security_activation": ("account_security_activation", True, True, ("singleton_id",)),
    "uq_account_security_activation_activation_id": ("account_security_activation", True, False, ("activation_id",)),
    "uq_account_security_activation_database_instance_id": ("account_security_activation", True, False,
                                                            ("database_instance_id",)),
    "pk_account_security_admission": ("account_security_admission", True, True, ("entry_slot",)),
    "uq_account_security_admission_user": ("account_security_admission", True, False, ("user_id",)),
    "pk_account_security_admission_aggregate": ("account_security_admission_aggregate", True, True,
                                                ("admission_class",)),
    "ix_account_security_events_user": ("account_security_events", False, False, ("user_id",)),
    "pk_account_security_events": ("account_security_events", True, True, ("operation_id",)),
    "uq_account_security_events_operation_account": ("account_security_events", True, False,
                                                     ("operation_id", "user_id")),
}
# Table and column rights that would let a principal read or write V1 state.
TABLE_RIGHTS = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
COLUMN_RIGHTS = ("SELECT", "INSERT", "UPDATE", "REFERENCES")
# Rights that change V1 state (TRIGGER can attach code that does).
WRITE_RIGHTS = ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "TRIGGER")

# The 0011 inventory stays exact: 0012 adds no function, trigger or rule.
FUNCTIONS = {"registration_control_lock", "registration_intent_lifecycle_guard",
             "registration_user_insert_guard", "registration_user_identity_guard"}
TRIGGERS = {"trg_registration_intent_lifecycle", "trg_users_registration_provenance",
            "trg_users_registration_identity"}
# The reviewed SECURITY DEFINER guards, by exact signature.
REVIEWED_GUARDS = ("kalillac.registration_control_lock(boolean)", "kalillac.registration_intent_lifecycle_guard()",
                   "kalillac.registration_user_insert_guard()", "kalillac.registration_user_identity_guard()")

# 0013: the private session-revocation authority, PostgreSQL 16 renderings.
REVOCATION_FUNCTION = "v1_revoke_sessions"
REVOCATION_SIGNATURE = "kalillac.v1_revoke_sessions(uuid,uuid,text,text)"
# (identity arguments, result, language, volatility, SECURITY DEFINER,
# LEAKPROOF, STRICT, parallel, kind, configuration, cost, rows)
REVOCATION_DEFINITION = ("p_operation_id uuid, p_session_id uuid, p_token_hash text, p_scope text",
                         "TABLE(outcome text, retry_after_seconds integer, replayed boolean)", "plpgsql", "v",
                         True, False, False, "u", "f", ["search_path=pg_catalog, pg_temp"], 100.0, 1000.0)
# SHA-256 of the exact body (prosrc) installed by 0013.
REVOCATION_SOURCE_SHA256 = "3dfe00d672e179ca8cf4fbfdb91e4ac29dbc24009f96da94fbea3063771fabb6"
EXECUTOR_TABLE_GRANTS = {"account_security_events": ("SELECT", "INSERT"),
                         "account_security_admission": ("SELECT", "INSERT", "UPDATE"),
                         "account_security_admission_aggregate": ("SELECT", "INSERT", "UPDATE")}
EXECUTOR_COLUMN_GRANTS = {("users", "SELECT"): ("id", "is_active", "quarantined_at", "deletion_requested_at",
                                                "sessions_valid_after"),
                          ("users", "UPDATE"): ("updated_at",),
                          ("account_sessions", "SELECT"): ("id", "user_id", "token_hash", "created_at", "expires_at",
                                                           "revoked_at"),
                          ("account_sessions", "UPDATE"): ("revoked_at",)}


class V1SchemaRefused(RuntimeError):
    """A fixed, non-secret refusal reason."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _rows(session, sql, **params):
    from sqlalchemy import text
    return session.execute(text(sql), params).all()


def _require(condition):
    if not condition:
        raise V1SchemaRefused("inconsistent_schema")


def _expected_constraint(name, kind, definition):
    index = name if kind in "pu" else REFERENCED.get(name)
    return kind, definition, (True, False, False, kind != "c"), index, "kalillac" if index else None


def _expected_index(table, unique, primary, keys):
    return (table, unique, primary, "btree", list(keys), len(keys), len(keys), True, True,
            True, True, True, True, False, False, "p", f"ON kalillac.{table} USING btree ({', '.join(keys)})")


def classify(session) -> str:
    """Classify a fresh transaction's database; raises V1SchemaRefused."""
    from sqlalchemy import text
    if session.get_bind().dialect.name != "postgresql":
        raise V1SchemaRefused("unsupported_database")
    session.execute(text("SET LOCAL search_path=pg_catalog,pg_temp"))
    tables = {r[0] for r in _rows(session, """SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='kalillac' AND c.relname=ANY(:t)""", t=list(TABLES))}
    added = {r[0]: tuple(r[1:]) for r in _rows(session, """SELECT a.attname,format_type(a.atttypid,a.atttypmod),
        a.attnotnull,pg_get_expr(d.adbin,d.adrelid) FROM pg_attribute a LEFT JOIN pg_attrdef d
        ON d.adrelid=a.attrelid AND d.adnum=a.attnum WHERE a.attrelid='kalillac.users'::regclass
        AND a.attnum>0 AND NOT a.attisdropped AND a.attname=ANY(:c)""", c=list(USER_COLUMNS))}
    if not tables and not added:
        return LEGACY
    _require(tables == set(TABLES) and added == USER_COLUMNS)
    roles = _security_roles()
    revocation = bool(_rows(session, """SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='kalillac' AND p.proname=:f""", f=REVOCATION_FUNCTION))
    owner = _catalog(session, roles, revocation)
    principals = _authority(session, owner)
    _security_principals(session, owner, roles, principals, revocation)
    if not _rows(session, "SELECT has_table_privilege('kalillac.account_security_activation','SELECT')")[0][0]:
        raise V1SchemaRefused("authority_state_unreadable")
    latch = _rows(session, "SELECT state FROM kalillac.account_security_activation")
    if len(latch) != 1 or latch[0][0] not in (PREPARED, ACTIVE):
        raise V1SchemaRefused("inconsistent_schema")
    return latch[0][0]


def _security_roles():
    """(S, E) from their mandatory settings; there is no default name."""
    from kalillac_db.migration_roles import v1_security_executor_role, v1_security_service_role
    try:
        return v1_security_service_role(), v1_security_executor_role()
    except ValueError:
        raise V1SchemaRefused("authority_roles_unconfigured") from None


def _catalog(session, roles, revocation) -> str:
    """Exact definitions of every 0012 object, not names; returns their owner."""
    executor = roles[1]
    # Permanent ordinary tables (never UNLOGGED, temporary, partitioned, typed or
    # inherited), all owned by the users table's owner.
    relations = [tuple(r) for r in _rows(session, """SELECT c.relname,c.relkind::text,c.relpersistence::text,
        c.relispartition,c.reloftype<>0 OR EXISTS(SELECT 1 FROM pg_inherits i WHERE c.oid IN (i.inhrelid,i.inhparent)),
        pg_get_userbyid(c.relowner) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='kalillac' AND c.relname=ANY(:t)""", t=[*TABLES, "users"])]
    owners = {r[5] for r in relations}
    _require(len(relations) == len(TABLES) + 1 and len(owners) == 1
             and all(r[1:5] == ("r", "p", False, False) for r in relations))
    # Columns in exact order with no dropped slot, type, nullability, default,
    # identity/generation, collation and no column-level grant.
    columns = {}
    for table, *column in _rows(session, """SELECT c.relname,a.attnum,a.attname,format_type(a.atttypid,a.atttypmod),
        a.attnotnull,pg_get_expr(d.adbin,d.adrelid),a.attidentity::text,a.attgenerated::text,
        CASE WHEN a.attcollation<>0 THEN a.attcollation::regcollation::text END,a.attacl IS NOT NULL
        FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace
        LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
        WHERE n.nspname='kalillac' AND c.relname=ANY(:t) AND a.attnum>0 ORDER BY 1,2""", t=list(TABLES)):
        columns.setdefault(table, []).append(tuple(column))
    _require(columns == {table: [(number, name, kind, not_null, None, "", "",
                                  '"default"' if kind.startswith("character varying") else None, False)
                                 for number, (name, (kind, not_null)) in enumerate(spec.items(), 1)]
                         for table, spec in COLUMNS.items()})
    users = {r[0]: tuple(r[1:]) for r in _rows(session, """SELECT a.attname,format_type(a.atttypid,a.atttypmod),
        a.attnotnull,pg_get_expr(d.adbin,d.adrelid),a.attidentity::text,a.attgenerated::text,a.attcollation<>0,
        a.attacl IS NOT NULL FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
        WHERE a.attrelid='kalillac.users'::regclass AND a.attnum>0 AND NOT a.attisdropped AND a.attname=ANY(:c)""",
        c=list(USER_COLUMNS))}
    # 0013 grants E SELECT on deletion_requested_at (compared exactly later).
    _require(users == {name: (*spec, "", "", False, revocation and name == "deletion_requested_at")
                       for name, spec in USER_COLUMNS.items()})
    # Every primary, unique, foreign and check constraint on, or referencing, a
    # V1 table: exact definition, validated, immediate, and its backing index.
    constraints = {}
    for table, name, kind, definition, flags, index, index_schema in _rows(session, """SELECT t.relname,c.conname,
        c.contype::text,pg_get_constraintdef(c.oid,false),
        ARRAY[c.convalidated,c.condeferrable,c.condeferred,c.connoinherit],i.relname,ins.nspname
        FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid JOIN pg_namespace n ON n.oid=t.relnamespace
        LEFT JOIN pg_class f ON f.oid=c.confrelid LEFT JOIN pg_namespace fn ON fn.oid=f.relnamespace
        LEFT JOIN pg_class i ON i.oid=c.conindid LEFT JOIN pg_namespace ins ON ins.oid=i.relnamespace
        WHERE c.contype<>'n' AND ((n.nspname='kalillac' AND t.relname=ANY(:t))
        OR (fn.nspname='kalillac' AND f.relname=ANY(:t)))""", t=list(TABLES)):
        constraints.setdefault(table, {})[name] = (kind, definition, tuple(flags), index, index_schema)
    _require(constraints == {table: {name: _expected_constraint(name, *spec) for name, spec in specs.items()}
                             for table, specs in CONSTRAINTS.items()})
    # Every index: method, key columns and order, no expression or predicate,
    # validity and readiness, and PostgreSQL's own rendering of the remainder.
    indexes = {}
    for name, *index in _rows(session, """SELECT i.relname,t.relname,x.indisunique,x.indisprimary,a.amname,
        ARRAY(SELECT k.attname::text FROM unnest(x.indkey::int2[]) WITH ORDINALITY u(n,o)
              JOIN pg_attribute k ON k.attrelid=x.indrelid AND k.attnum=u.n ORDER BY u.o),
        x.indnatts,x.indnkeyatts,x.indexprs IS NULL,x.indpred IS NULL,x.indisvalid,x.indisready,x.indislive,
        x.indimmediate,x.indisexclusion,x.indnullsnotdistinct,i.relpersistence::text,
        substr(pg_get_indexdef(i.oid),strpos(pg_get_indexdef(i.oid),' ON ')+1)
        FROM pg_index x JOIN pg_class i ON i.oid=x.indexrelid JOIN pg_class t ON t.oid=x.indrelid
        JOIN pg_am a ON a.oid=i.relam JOIN pg_namespace n ON n.oid=t.relnamespace
        WHERE n.nspname='kalillac' AND t.relname=ANY(:t)""", t=list(TABLES)):
        indexes[name] = tuple(index)
    _require(indexes == {name: _expected_index(*spec) for name, spec in INDEXES.items()})
    # No writer: the trigger/function inventory is exactly 0011's (the trusted
    # definitions are compared in _authority), no rule and no event trigger.
    triggers = {r[0] for r in _rows(session, """SELECT t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='kalillac' AND NOT t.tgisinternal""")}
    functions = {r[0] for r in _rows(session, """SELECT p.proname FROM pg_proc p JOIN pg_namespace n
        ON n.oid=p.pronamespace WHERE n.nspname='kalillac'""")}
    rules = _rows(session, """SELECT r.rulename FROM pg_rewrite r JOIN pg_class c ON c.oid=r.ev_class
        JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='kalillac' AND r.rulename<>'_RETURN'""")
    events = _rows(session, "SELECT evtname FROM pg_event_trigger WHERE evtenabled<>'D'")
    _require(triggers == TRIGGERS and functions == FUNCTIONS | ({REVOCATION_FUNCTION} if revocation else set())
             and not rules and not events)
    # No bridge, in any schema. A view runs with its owner's rights and a rule
    # action with its table owner's, so a view or rule over protected state
    # can hand it to R, A or PUBLIC whatever the tables' own grants say. None is
    # reviewed for V1, so every rewrite (view, materialized view or rule) that
    # depends on a V1 table, on a new users column, or (for rules) on users at
    # all is refused, and so is every rewrite built on such a relation in
    # turn, to any depth. Resolved by OID through pg_depend; no name is
    # interpolated.
    bridges = _rows(session, """SELECT EXISTS (WITH RECURSIVE protected AS (
        SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='kalillac' AND c.relname=ANY(:t)),
      rewrites AS (SELECT r.ev_class,r.rulename,d.refobjid,d.refobjsubid FROM pg_depend d
        JOIN pg_rewrite r ON r.oid=d.objid
        WHERE d.classid='pg_rewrite'::regclass AND d.refclassid='pg_class'::regclass),
      reach(relid) AS (
        SELECT w.ev_class FROM rewrites w WHERE w.refobjid IN (SELECT oid FROM protected)
        OR (w.refobjid='kalillac.users'::regclass AND (w.rulename<>'_RETURN' OR w.refobjsubid IN (
            SELECT a.attnum FROM pg_attribute a WHERE a.attrelid='kalillac.users'::regclass AND a.attname=ANY(:c))))
        UNION SELECT w.ev_class FROM rewrites w JOIN reach ON w.refobjid=reach.relid WHERE w.ev_class<>reach.relid)
      SELECT 1 FROM reach)""", t=list(TABLES), c=list(USER_COLUMNS))[0][0]
    # Nor, in any schema, an unreviewed SECURITY DEFINER function whose owner
    # holds protected authority. It runs with its owner's rights wherever it
    # is called from, and a trigger on any table needs no EXECUTE grant at
    # all, so revoking PUBLIC EXECUTE does not make it safe. Protected
    # authority, all evaluated by PostgreSQL itself (no recursion here; role
    # membership cannot be cyclic):
    # - superuser;
    # - any membership in the owner or pg_write_all_data, direct or multi-hop,
    #   inherited or SET-only. A definer function cannot SET ROLE, so SET-only
    #   is refused conservatively; the migration login's SET membership in the
    #   owner is unaffected because it owns no such function;
    # - an effective (direct, PUBLIC or inherited) write right on a V1 table,
    #   or on a new users column.
    # The four reviewed guards, and 0013's revocation function, are excluded
    # by exact signature; their definitions are compared separately
    # (validate_security_definitions, _security_principals).
    definers = _rows(session, """SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        JOIN pg_roles o ON o.oid=p.proowner
        WHERE p.prosecdef AND n.nspname NOT IN ('pg_catalog','information_schema')
        AND p.oid<>ALL(array_remove(ARRAY(SELECT to_regprocedure(g) FROM unnest(CAST(:g AS text[])) g)::oid[],NULL))
        AND (o.rolsuper OR pg_has_role(o.oid,(SELECT relowner FROM pg_class WHERE oid='kalillac.users'::regclass),
                                       'MEMBER')
          OR pg_has_role(o.oid,'pg_write_all_data','MEMBER')
          OR EXISTS (SELECT 1 FROM unnest(CAST(:t AS text[])) t CROSS JOIN unnest(CAST(:w AS text[])) r
                     WHERE has_table_privilege(o.oid,'kalillac.'||t,r)
                     OR (r IN ('INSERT','UPDATE') AND has_any_column_privilege(o.oid,'kalillac.'||t,r)))
          OR EXISTS (SELECT 1 FROM unnest(CAST(:c AS text[])) c CROSS JOIN unnest(ARRAY['INSERT','UPDATE']) r
                     WHERE has_column_privilege(o.oid,'kalillac.users',c,r)))""",
        g=[*REVIEWED_GUARDS, *([REVOCATION_SIGNATURE] if revocation else [])], t=list(TABLES),
        w=list(WRITE_RIGHTS), c=list(USER_COLUMNS))
    _require(not bridges and not definers)
    # Owner-only: no grantee but the owner (and, after 0013, E's exact grants)
    # on any V1 table, and nobody but the owner may insert/update users at
    # table level.
    owner = owners.pop()
    exposed = {tuple(r) for r in _rows(session, """SELECT c.relname,pg_get_userbyid(a.grantee),a.privilege_type,
        a.is_grantable,pg_get_userbyid(a.grantor) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl,acldefault('r',c.relowner))) a
        WHERE n.nspname='kalillac' AND (c.relname=ANY(:t) OR (c.relname='users' AND a.privilege_type IN ('INSERT','UPDATE')))
        AND a.grantee<>c.relowner""", t=list(TABLES))}
    _require(exposed == ({(table, executor, right, False, owner) for table, rights in EXECUTOR_TABLE_GRANTS.items()
                          for right in rights} if revocation else set()))
    return owner


def _authority(session, owner) -> None:
    """R, A and PUBLIC hold no owner authority and no V1 right by any route.

    The roles come from the same configuration registration bootstrap uses; A is
    the role 0011 recorded. effective_privileges is the trusted 0011 boundary:
    no superuser, BYPASSRLS, CREATEDB/CREATEROLE or replication attribute, no
    role membership of any kind (direct, multi-hop, inherited or SET-only,
    which includes predefined backup/server-file/program roles), no executable
    SECURITY DEFINER bridge but the reviewed lock, no server-file function, no
    schema CREATE, exact column grants and the canonical guard and trigger
    definitions. The V1 rights below are effective (has_*_privilege), so they
    include PUBLIC and every inherited grant.
    """
    import json
    from kalillac_db.migration_roles import migration_app_role, migration_owner_role, validate_role_name
    from .registration_crypto import RegistrationUnavailable
    from .registration_startup import effective_privileges
    try:
        receipt = json.loads(_rows(session, "SELECT obj_description("
                                            "'kalillac.account_registration_activation'::regclass,'pg_class')")[0][0])
        registration = validate_role_name(receipt["registration_role"], "registration role")
        runtime = migration_app_role()
        _require(migration_owner_role() == owner and len({registration, runtime, owner}) == 3)
        effective_privileges(session, registration, runtime, owner)
    except (RegistrationUnavailable, ValueError, TypeError, KeyError):
        raise V1SchemaRefused("inconsistent_schema") from None
    principals = [runtime, registration, "public"]
    reach = _rows(session, """SELECT 1 FROM unnest(CAST(:p AS text[])) p WHERE pg_has_role(p::name,:o,'MEMBER')
        OR pg_has_role(p::name,:o,'USAGE') OR pg_has_role(p::name,:o,'SET')""", p=principals[:2], o=owner)
    rights = _rows(session, """SELECT 1 FROM unnest(CAST(:p AS text[])) p CROSS JOIN unnest(CAST(:t AS text[])) t
        CROSS JOIN unnest(CAST(:r AS text[])) r WHERE has_table_privilege(p::name,'kalillac.'||t,r)
        OR (r=ANY(CAST(:c AS text[])) AND has_any_column_privilege(p::name,'kalillac.'||t,r))""",
        p=principals, t=list(TABLES), r=list(TABLE_RIGHTS), c=list(COLUMN_RIGHTS))
    user_columns = _rows(session, """SELECT 1 FROM unnest(CAST(:p AS text[])) p CROSS JOIN unnest(CAST(:c AS text[])) c
        CROSS JOIN unnest(ARRAY['INSERT','UPDATE','REFERENCES']) r
        WHERE has_column_privilege(p::name,'kalillac.users',c,r)""", p=principals, c=list(USER_COLUMNS))
    sequences = _rows(session, """SELECT 1 FROM pg_class s JOIN pg_namespace n ON n.oid=s.relnamespace
        CROSS JOIN unnest(CAST(:p AS text[])) p WHERE n.nspname='kalillac' AND s.relkind='S'
        AND (has_sequence_privilege(p::name,s.oid,'USAGE') OR has_sequence_privilege(p::name,s.oid,'UPDATE'))""",
        p=principals)
    schema = _rows(session, """SELECT 1 FROM pg_namespace WHERE nspname='kalillac' AND (pg_get_userbyid(nspowner)<>:o
        OR has_schema_privilege('public',oid,'CREATE'))""", o=owner)
    # R or A owning anything in the schema, or the database, would be owner authority.
    owned = _rows(session, """SELECT 1 FROM (SELECT relowner,relnamespace FROM pg_class
        UNION ALL SELECT proowner,pronamespace FROM pg_proc UNION ALL SELECT typowner,typnamespace FROM pg_type) x(o,s)
        JOIN pg_namespace n ON n.oid=x.s WHERE n.nspname='kalillac' AND pg_get_userbyid(x.o)=ANY(:p)
        UNION ALL SELECT 1 FROM pg_database WHERE datname=current_database() AND pg_get_userbyid(datdba)=ANY(:p)""",
        p=principals[:2])
    # Owner default privileges would hand later objects to someone else.
    defaults = _rows(session, """SELECT 1 FROM pg_default_acl d CROSS JOIN LATERAL aclexplode(d.defaclacl) a
        WHERE pg_get_userbyid(d.defaclrole)=:o AND a.grantee<>d.defaclrole""", o=owner)
    _require(not (reach or rights or user_columns or sequences or schema or owned or defaults))
    return runtime, registration


def _security_principals(session, owner, roles, principals, revocation) -> None:
    """S and E: exact NOLOGIN attributes, the operator's single owner->E SET
    edge, and no ownership, grant or default privilege but 0013's exact set.

    Before 0013 they may be absent, or present holding nothing but the edge.
    R and A hold no membership at all (_authority), so neither they nor S can
    reach E; only principals that can already SET ROLE to the owner can.
    Ownership and grants are read from pg_shdepend for this database and
    shared objects, then compared exactly per object.
    """
    import hashlib
    service, executor = roles
    _require(len({service, executor, owner, *principals}) == 5)
    present = {r[0]: tuple(r[1:]) for r in _rows(session, """SELECT rolname,rolcanlogin,rolinherit,rolsuper,
        rolcreatedb,rolcreaterole,rolreplication,rolbypassrls FROM pg_roles WHERE rolname=ANY(:r)""",
        r=[service, executor])}
    _require(set(present) == {service, executor} if revocation else set(present) <= {service, executor})
    _require(all(flags == (False,) * 7 for flags in present.values()))
    edges = {tuple(r) for r in _rows(session, """SELECT pg_get_userbyid(m.roleid),pg_get_userbyid(m.member),
        m.admin_option,m.inherit_option,m.set_option FROM pg_auth_members m
        WHERE pg_get_userbyid(m.roleid)=ANY(:r) OR pg_get_userbyid(m.member)=ANY(:r)""", r=[service, executor])}
    edge = (executor, owner, False, False, True)
    _require(edges == {edge} if revocation else edges <= ({edge} if executor in present else set()))
    defaults = _rows(session, """SELECT 1 FROM pg_default_acl d WHERE pg_get_userbyid(d.defaclrole)=ANY(:r)
        OR EXISTS (SELECT 1 FROM aclexplode(d.defaclacl) a WHERE pg_get_userbyid(a.grantee)=ANY(:r))""",
        r=[service, executor])
    dependencies = {tuple(r) for r in _rows(session, """SELECT pg_get_userbyid(d.refobjid),d.classid::regclass::text,
        d.objid,d.deptype::text FROM pg_shdepend d WHERE d.refclassid='pg_authid'::regclass
        AND pg_get_userbyid(d.refobjid)=ANY(:r)
        AND d.dbid IN (0,(SELECT oid FROM pg_database WHERE datname=current_database()))""", r=[service, executor])}
    _require(not defaults)
    if not revocation:
        _require(not dependencies)
        return
    # The exact function: definition, body digest, owner E, ACL E and S only.
    definition = _rows(session, """SELECT p.oid,pg_get_function_identity_arguments(p.oid),pg_get_function_result(p.oid),
        l.lanname,p.provolatile::text,p.prosecdef,p.proleakproof,p.proisstrict,p.proparallel::text,p.prokind::text,
        p.proconfig,p.procost::float8,p.prorows::float8,p.prosrc,pg_get_userbyid(p.proowner),
        ARRAY(SELECT ARRAY[pg_get_userbyid(a.grantee),a.privilege_type,a.is_grantable::text,pg_get_userbyid(a.grantor)]
              FROM aclexplode(p.proacl) a ORDER BY 1)
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_language l ON l.oid=p.prolang
        WHERE n.nspname='kalillac' AND p.proname=:f""", f=REVOCATION_FUNCTION)
    _require(len(definition) == 1)
    function, *attributes, source, function_owner, acl = definition[0]
    _require(tuple(attributes) == REVOCATION_DEFINITION and function_owner == executor
             and hashlib.sha256(source.encode()).hexdigest() == REVOCATION_SOURCE_SHA256
             and sorted(acl) == sorted([[executor, "EXECUTE", "false", executor],
                                        [service, "EXECUTE", "false", executor]]))
    relations = {r[0]: r[1] for r in _rows(session, """SELECT c.relname,c.oid FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='kalillac' AND c.relname=ANY(:t)""",
        t=["users", "account_sessions", *EXECUTOR_TABLE_GRANTS])}
    schema = _rows(session, "SELECT oid FROM pg_namespace WHERE nspname='kalillac'")[0][0]
    _require(dependencies == {(executor, "pg_proc", function, "o"), (executor, "pg_namespace", schema, "a"),
                              (service, "pg_namespace", schema, "a"), (service, "pg_proc", function, "a"),
                              *((executor, "pg_class", oid, "a") for oid in relations.values())})
    # Exact grants on those objects: schema USAGE (never CREATE) for both,
    # E's table and column privileges, nothing for S.
    granted = {tuple(r) for r in _rows(session, """SELECT 'schema',NULL::text,pg_get_userbyid(a.grantee),
        a.privilege_type,a.is_grantable,pg_get_userbyid(a.grantor) FROM pg_namespace n
        CROSS JOIN LATERAL aclexplode(n.nspacl) a WHERE n.nspname='kalillac' AND pg_get_userbyid(a.grantee)=ANY(:r)
        UNION ALL SELECT c.relname,NULL,pg_get_userbyid(a.grantee),a.privilege_type,a.is_grantable,
        pg_get_userbyid(a.grantor) FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) a
        WHERE c.oid=ANY(CAST(:o AS oid[])) AND pg_get_userbyid(a.grantee)=ANY(:r)
        UNION ALL SELECT c.relname,t.attname,pg_get_userbyid(a.grantee),a.privilege_type,a.is_grantable,
        pg_get_userbyid(a.grantor) FROM pg_attribute t JOIN pg_class c ON c.oid=t.attrelid
        CROSS JOIN LATERAL aclexplode(t.attacl) a WHERE c.oid=ANY(CAST(:o AS oid[])) AND pg_get_userbyid(a.grantee)=ANY(:r)""",
        r=[service, executor], o=list(relations.values()))}
    _require(granted == {("schema", None, executor, "USAGE", False, owner), ("schema", None, service, "USAGE", False, owner),
                         *((table, None, executor, right, False, owner)
                           for table, rights in EXECUTOR_TABLE_GRANTS.items() for right in rights),
                         *((table, column, executor, right, False, owner)
                           for (table, right), columns in EXECUTOR_COLUMN_GRANTS.items() for column in columns)})


def require_qualified_authority(session_factory) -> None:
    """Application-startup gate for V1; refuses unless fully qualified (never, here)."""
    from sqlalchemy import text
    try:
        factory = session_factory()
        if factory is None:
            raise V1SchemaRefused("database_disabled")
        with factory() as session, session.begin():
            if session.get_bind().dialect.name != "postgresql":
                raise V1SchemaRefused("unsupported_database")
            session.execute(text("SET TRANSACTION READ ONLY"))
            session.execute(text("SET LOCAL lock_timeout = '3s'"))
            session.execute(text("SET LOCAL statement_timeout = '3s'"))
            state = classify(session)
    except V1SchemaRefused:
        raise
    except Exception:
        raise V1SchemaRefused("state_unreadable") from None
    if state == LEGACY:
        raise V1SchemaRefused("schema_not_prepared")
    if state == PREPARED:
        raise V1SchemaRefused("prepared_inactive")
    # An active latch without a catalog this release can qualify is refused,
    # never trusted: the S/E inventory is incomplete until issuance, the epoch
    # and recovery are ported in their reviewed slices.
    raise V1SchemaRefused("authority_catalog_unqualified")
