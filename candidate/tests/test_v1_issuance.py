"""V1-02C private session issuance and the session epoch: migration graph, DDL, roles, guard and source. No PostgreSQL."""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import io
from pathlib import Path
import re

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from account_test_db import make_sqlite_engine
from test_account_migration import _config
import kalillac_accounts.v1_schema as v1_schema
from kalillac_accounts import registration_schema
from kalillac_db import migration_roles

CANDIDATE = Path(__file__).resolve().parents[1]
MIGRATION = CANDIDATE / "migrations" / "versions" / "0014_v1_session_issuance_epoch.py"
REVISION = "0014_v1_session_issuance"
PREVIOUS = "0013_v1_session_revocation"
NAMES = {"service": "kalillac_v1_security_service", "executor": "kalillac_v1_security_executor",
         "owner": "kalillac_owner", "runtime": "kalillac_app"}
# SHA-256 of 0011's four guard definitions for these names, exactly as committed
# before V1-02C: 0011 imports them, so they must never change.
FROZEN_0011 = {"registration_control_lock": "f4dd3505086454f6aae58ef0036c821a788ea6140d6abd1ab7c5a2f8f51c0e5b",
               "registration_intent_lifecycle_guard": "36547282f7959663308ec45624ed68b447fed69cf80519edd952eec63af6dd2f",
               "registration_user_insert_guard": "406cb8fee0b8a5f0a448da54c1304b5139aa44daea75aea25fb530740ca81335",
               "registration_user_identity_guard": "104e362a40ffecbd0f017a62168fe639cc0325f35e659e00867457e0758a8431"}
IDENTITY_V2 = "3cea398426064eea9f937f44db27e1f09f56a8c586c89f61395a4d4599a16132"


@pytest.fixture
def roles(monkeypatch):
    monkeypatch.setenv(migration_roles.V1_SECURITY_SERVICE_ROLE_ENV, NAMES["service"])
    monkeypatch.setenv(migration_roles.V1_SECURITY_EXECUTOR_ROLE_ENV, NAMES["executor"])
    monkeypatch.delenv(migration_roles.OWNER_ROLE_ENV, raising=False)
    monkeypatch.delenv(migration_roles.APP_ROLE_ENV, raising=False)


def _module():
    spec = importlib.util.spec_from_file_location("v1_02c_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render(revisions, downgrade=False):
    config = _config()
    config.set_main_option("sqlalchemy.url", "postgresql+psycopg://offline@localhost/kalillac")
    config.output_buffer = buffer = io.StringIO()
    (command.downgrade if downgrade else command.upgrade)(config, revisions, sql=True)
    return buffer.getvalue()


def _statements(sql):
    # Drop only Alembic's own progress comments; function bodies keep their own.
    body = sql.split("BEGIN;", 1)[1].rsplit("COMMIT;", 1)[0]
    body = "\n".join(line for line in body.splitlines() if not line.startswith("-- Running "))
    return [s.strip() for s in body.split(";\n\n") if s.strip() and "alembic_version" not in s]


# --- graph -----------------------------------------------------------------------------------

def test_0014_is_the_sole_head_directly_after_0013():
    script = ScriptDirectory.from_config(_config())
    assert script.get_heads() == [REVISION]
    assert script.get_revision(REVISION).down_revision == PREVIOUS
    module = _module()
    assert (module.revision, module.down_revision, module.branch_labels, module.depends_on) == (
        REVISION, PREVIOUS, None, None)
    # alembic_version.version_num is VARCHAR(32).
    assert len(REVISION) <= 32


# --- identity guard ----------------------------------------------------------------------------

def test_0011_guard_text_is_frozen():
    guards = registration_schema.guard_functions("kalillac_registration", "kalillac_migrate")
    assert {name: hashlib.sha256(body.encode()).hexdigest() for name, body in guards.items()} == FROZEN_0011


def test_guard_v2_differs_from_v1_only_by_the_epoch_clause():
    v1 = registration_schema.guard_functions("kalillac_registration", "unused")["registration_user_identity_guard"]
    v2 = registration_schema.identity_guard_v2("kalillac_registration")
    assert hashlib.sha256(v2.encode()).hexdigest() == IDENTITY_V2
    added = "\n   AND NOT (" + registration_schema.EPOCH_ADVANCE + ")"
    cut = v1.index(") THEN\n   RAISE EXCEPTION USING ERRCODE='23514',CONSTRAINT='ck_users_registration_identity'") + 1
    assert v2 == v1[:cut] + added + v1[cut:]
    # The clause: every other protected identity value unchanged, the epoch non-null and strictly later.
    assert registration_schema.EPOCH_ADVANCE == (
        "ROW(NEW.id,NEW.email,NEW.created_at,NEW.registration_intent_id,NEW.quarantined_at) IS NOT DISTINCT FROM"
        " ROW(OLD.id,OLD.email,OLD.created_at,OLD.registration_intent_id,OLD.quarantined_at)"
        " AND NEW.sessions_valid_after IS NOT NULL"
        " AND (OLD.sessions_valid_after IS NULL OR NEW.sessions_valid_after > OLD.sessions_valid_after)")
    # The repair transition and the verification gate are unchanged, and v1 does not contain the clause.
    assert registration_schema.EPOCH_ADVANCE not in v1 and v2.count(registration_schema.EPOCH_ADVANCE) == 1
    assert v2.count("session_user=") == v1.count("session_user=") == 1
    assert v2.split(" THEN\n   RAISE EXCEPTION")[1] == v1.split(" THEN\n   RAISE EXCEPTION")[1]


def test_migration_guard_templates_are_the_registration_definitions():
    module = _module()
    assert module.GUARD_V1.replace(module.MARKER, "kalillac_registration") == registration_schema.guard_functions(
        "kalillac_registration", "unused")["registration_user_identity_guard"]
    assert module.GUARD_V2.replace(module.MARKER, "kalillac_registration") == registration_schema.identity_guard_v2(
        "kalillac_registration")
    # The marker can never be a validated role name, and the guard has no other dynamic part.
    assert not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", module.MARKER)
    assert module.GUARD_V1.count(module.MARKER) == module.GUARD_V2.count(module.MARKER) == 1


# --- DDL ---------------------------------------------------------------------------------------

def test_offline_upgrade_is_exactly_the_guarded_function_guard_and_grant_sequence(roles):
    statements = _statements(_render(PREVIOUS + ":" + REVISION))
    module = _module()
    assert statements == [s.strip() for s in module.upgrade_statements(NAMES)]
    service, executor, owner = NAMES["service"], NAMES["executor"], NAMES["owner"]
    signature = "kalillac.v1_issue_session(uuid, text, uuid, text, integer)"
    assert statements[0].startswith("DO $v1_issuance_prerequisite_0013$")
    assert statements[1].startswith("DO $v1_issuance_prerequisite$")
    assert statements[2:10] == [
        f"GRANT CREATE ON SCHEMA kalillac TO {executor}",
        module.FUNCTION,
        f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC",
        f"GRANT EXECUTE ON FUNCTION {signature} TO {service}",
        f"ALTER FUNCTION {signature} OWNER TO {executor}",
        f"SET ROLE {executor}",
        module.REVOCATION_FUNCTION,
        f"SET ROLE {owner}",
    ]
    assert statements[10].startswith("DO $v1_identity_guard$")
    assert statements[11:-1] == [
        f"GRANT SELECT (password_hash, email_verified_at, password_reset_required), UPDATE (sessions_valid_after)"
        f" ON kalillac.users TO {executor}",
        f"GRANT INSERT (id, user_id, token_hash, created_at, expires_at) ON kalillac.account_sessions TO {executor}",
        f"REVOKE CREATE ON SCHEMA kalillac FROM {executor}",
    ]
    assert statements[-1].startswith("DO $v1_issuance_postcondition$")
    # The guards carry the validated names as quoted literals only; the role
    # checks (with the owner) run in the first and the last.
    for guard in (statements[0], statements[-1]):
        for name in NAMES.values():
            assert f"quote_ident('{name}')" in guard
    for name in (service, executor, NAMES["runtime"]):
        assert f"quote_ident('{name}')" in statements[1]
    assert "'V1_ISSUANCE_PREREQUISITE_INVALID'" in statements[0] and "'V1_ISSUANCE_PREREQUISITE_INVALID'" in statements[1]
    # Functions and grants only: no table, column, index, trigger, view, rule,
    # sequence, data, role, membership or event-type change.
    outside = " ".join(s for s in statements if s not in (module.FUNCTION, module.REVOCATION_FUNCTION)
                       and not s.startswith("DO $v1_identity_guard$")).upper()
    for word in ("CREATE TABLE", "ALTER TABLE", "CREATE INDEX", "CREATE TRIGGER", "CREATE VIEW", "CREATE RULE",
                 "CREATE SEQUENCE", "INSERT INTO", "UPDATE KALILLAC", "DELETE FROM", "CREATE ROLE", "ALTER ROLE",
                 "DROP ", "TRUNCATE", "DEFAULT PRIVILEGES", "WITH ADMIN", "WITH INHERIT", "WITH GRANT OPTION"):
        assert word not in outside, word
    assert [s for s in statements if s.upper().startswith("SET ")] == [f"SET ROLE {executor}", f"SET ROLE {owner}"]
    assert not re.search(r"\bGRANT\s+(?!CREATE ON SCHEMA|EXECUTE ON FUNCTION|SELECT \(|INSERT \()", outside)


def test_the_guard_swap_is_the_only_dynamic_sql_and_substitutes_only_the_recorded_role(roles):
    module = _module()
    statements = module.upgrade_statements(NAMES)
    swap = statements[10]
    assert [s for s in statements if "EXECUTE '" in s] == [swap]
    assert swap.count("EXECUTE '") == 1
    assert "EXECUTE 'CREATE OR REPLACE FUNCTION kalillac.registration_user_identity_guard '" in swap
    # Validated before use, compared before and after, and owner/ACL/config/trigger state unchanged.
    assert "recorded !~ '^[a-z_][a-z0-9_]{0,62}$'" in swap
    assert swap.count("pg_catalog.replace(") == 3 and swap.count("'@registration_role@', recorded)") == 3
    assert "p.proowner" in swap and "p.proacl" in swap and "p.proconfig" in swap and "t.tgenabled" in swap
    assert swap.count("RAISE EXCEPTION USING MESSAGE = 'V1_ISSUANCE_GUARD_INVALID'") == 2


def test_downgrade_restores_exact_0013_from_exact_0014():
    module = _module()
    statements = module.downgrade_statements(NAMES)
    executor, owner = NAMES["executor"], NAMES["owner"]
    assert statements[0].startswith("DO $v1_issuance_postcondition$")
    assert statements[1:3] == [
        f"REVOKE SELECT (password_hash, email_verified_at, password_reset_required), UPDATE (sessions_valid_after)"
        f" ON kalillac.users FROM {executor}",
        f"REVOKE INSERT (id, user_id, token_hash, created_at, expires_at) ON kalillac.account_sessions FROM {executor}",
    ]
    assert statements[3].startswith("DO $v1_identity_guard$")
    assert statements[4:10] == [
        f"GRANT CREATE ON SCHEMA kalillac TO {executor}",
        f"SET ROLE {executor}",
        "DROP FUNCTION kalillac.v1_issue_session(uuid, text, uuid, text, integer)",
        module.REVOCATION_FUNCTION_V1,
        f"SET ROLE {owner}",
        f"REVOKE CREATE ON SCHEMA kalillac FROM {executor}",
    ]
    assert len(statements) == 12 and statements[10].startswith("DO $v1_issuance_prerequisite_0013$")
    assert statements[11].startswith("DO $v1_issuance_prerequisite$")
    assert all("'V1_ISSUANCE_DOWNGRADE_INVALID'" in s for s in statements[10:])
    # The guard's exact owner-only ACL (never NULL, i.e. default with PUBLIC) is
    # asserted before either direction mutates anything, and after.
    upgrade = module.upgrade_statements(NAMES)
    acl = ("(SELECT p.proacl IS NULL FROM pg_catalog.pg_proc p WHERE p.oid = guard) IS NOT FALSE",
           "aclexplode(p.proacl) x WHERE p.oid = guard) <> 1",
           "x.grantee = keeper AND x.grantor = keeper AND x.privilege_type = 'EXECUTE' AND NOT x.is_grantable")
    for check in (upgrade[1], upgrade[-1], statements[0], statements[11]):
        assert all(fragment in check for fragment in acl)
    assert not any("REVOKE" in s and "registration_user_identity_guard" in s for s in upgrade + statements)
    # The restored revocation is 0013's exact definition.
    revocation = module._revocation
    assert module.REVOCATION_FUNCTION_V1 == revocation.FUNCTION.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION")


def test_declared_grants_match_the_classifier():
    module = _module()
    assert module.EXECUTOR_TABLE_GRANTS == v1_schema.EXECUTOR_TABLE_GRANTS
    assert module.EXECUTOR_COLUMN_GRANTS == v1_schema.EXECUTOR_COLUMN_GRANTS_V2
    # Exactly 0013's grants plus the declared additions.
    combined = {key: set(columns) for key, columns in v1_schema.EXECUTOR_COLUMN_GRANTS.items()}
    for key, columns in module.ADDED_COLUMN_GRANTS.items():
        assert not combined.get(key, set()) & set(columns)
        combined.setdefault(key, set()).update(columns)
    assert combined == {key: set(columns) for key, columns in module.EXECUTOR_COLUMN_GRANTS.items()}
    assert module.ADDED_COLUMN_GRANTS == {
        ("users", "SELECT"): ("password_hash", "email_verified_at", "password_reset_required"),
        ("users", "UPDATE"): ("sessions_valid_after",),
        ("account_sessions", "INSERT"): ("id", "user_id", "token_hash", "created_at", "expires_at")}
    # No DELETE, TRUNCATE, REFERENCES or TRIGGER; never revoked_at on insert, never quarantine or the hash on update.
    rights = {r for rights in module.EXECUTOR_TABLE_GRANTS.values() for r in rights} | {
        right for _, right in module.EXECUTOR_COLUMN_GRANTS}
    assert rights == {"SELECT", "INSERT", "UPDATE"}
    assert module.EXECUTOR_COLUMN_GRANTS[("users", "UPDATE")] == ("updated_at", "sessions_valid_after")


def test_issuance_definition_is_static_and_pinned():
    module = _module()
    assert hashlib.sha256(module.BODY.encode()).hexdigest() == v1_schema.ISSUANCE_SOURCE_SHA256
    assert module.FUNCTION.startswith(
        "CREATE FUNCTION kalillac.v1_issue_session(p_session_id uuid, p_token_hash text, p_user_id uuid,"
        " p_verified_password_hash text, p_lifetime_days integer) RETURNS TABLE(outcome text,"
        " issued_at timestamptz, valid_until timestamptz, replayed boolean) LANGUAGE plpgsql VOLATILE"
        " SECURITY DEFINER PARALLEL UNSAFE SET search_path = pg_catalog, pg_temp AS $v1_issue$")
    assert module.FUNCTION.endswith("$v1_issue$") and module.FUNCTION.count("$v1_issue$") == 2
    head = module.FUNCTION.split("AS $v1_issue$")[0]
    for absent in ("STRICT", "LEAKPROOF", "IMMUTABLE", "STABLE", "log_parameter"):
        assert absent not in head
    body = module.BODY.upper()
    # No dynamic SQL, identifier construction, role switching, settings or side channel.
    for word in ("EXECUTE ", "FORMAT(", "QUOTE_IDENT", "QUOTE_LITERAL", "SET ROLE", "SET_CONFIG", "SET SESSION",
                 "COPY", "DBLINK", "PG_READ", "LO_", "CREATE ", "ALTER ", "DROP ", "GRANT ", "REVOKE ",
                 "DELETE ", "TRUNCATE", "COMMIT", "ROLLBACK", "SAVEPOINT", "NOTIFY", "PG_SLEEP", "RAISE",
                 "NOW()", "CURRENT_TIMESTAMP", "UPDATE KALILLAC"):
        assert word not in body, word
    # Fully qualified, and only the users row and the session table are touched.
    assert set(re.findall(r"\bkalillac\.(\w+)", module.BODY)) == {"users", "account_sessions"}
    assert re.findall(r"EXCEPTION WHEN (\w+)", module.BODY) == ["unique_violation"]
    assert set(re.findall(r"\bp_\w+", module.BODY)) == {
        "p_session_id", "p_token_hash", "p_user_id", "p_verified_password_hash", "p_lifetime_days"}
    # The outcomes are exactly these.
    assert set(re.findall(r"outcome := '(\w+)'", module.BODY)) == {"issued", "not_authenticated",
                                                                    "security_unavailable"}


def test_issuance_never_receives_or_writes_a_secret():
    module = _module()
    # Never a raw token, password or email; the verified hash is only compared, once.
    head = module.FUNCTION.split("AS $v1_issue$")[0].lower()
    for text_ in (module.BODY.lower(), head):
        assert not re.search(r"\bemail\b|\bp_password\b|\bp_token\b|\bp_email\b", text_)
        for absent in ("account_security_events", "account_outbox", "account_security_admission"):
            assert absent not in text_
    assert module.BODY.count("p_verified_password_hash") == 1
    assert "v_hash IS DISTINCT FROM p_verified_password_hash" in module.BODY
    insert = module.BODY[module.BODY.index("INSERT INTO kalillac.account_sessions"):]
    insert = insert[:insert.index(";")]
    assert "(id, user_id, token_hash, created_at, expires_at)" in insert
    assert "p_verified_password_hash" not in insert and "v_hash" not in insert


def test_issuance_times_come_from_the_database_after_the_epoch():
    module = _module()
    assert "v_at := greatest(clock_timestamp(), v_epoch + interval '1 microsecond');" in module.BODY
    assert "VALUES (p_session_id, p_user_id, p_token_hash, v_at, v_at + p_lifetime_days * interval '24 hours')" \
        in module.BODY
    # Validity under the epoch is strictly later, as V1-01 authorizes it.
    assert "(v_epoch IS NULL OR v_created > v_epoch)" in module.BODY
    assert "p_token_hash !~ '^[0-9a-f]{64}$'" in module.BODY
    assert "p_lifetime_days < 1 OR p_lifetime_days > 366" in module.BODY


def test_revocation_v2_differs_from_0013_only_by_the_epoch_advance():
    module = _module()
    revocation = module._revocation
    assert hashlib.sha256(revocation.BODY.encode()).hexdigest() == v1_schema.REVOCATION_SOURCE_SHA256
    assert hashlib.sha256(module.REVOCATION_BODY.encode()).hexdigest() == v1_schema.REVOCATION_SOURCE_SHA256_V2
    anchor = "(p_scope = 'all' OR id <> p_session_id);\n"
    cut = revocation.BODY.index(anchor) + len(anchor)
    assert module.REVOCATION_BODY == revocation.BODY[:cut] + module.EPOCH + revocation.BODY[cut:]
    assert module.EPOCH == ("  IF p_scope = 'all' THEN\n"
                            "   UPDATE kalillac.users SET sessions_valid_after = greatest(v_at, v_epoch + interval"
                            " '1 microsecond')\n    WHERE id = v_user;\n  END IF;\n")
    # After the replay check and every denial: a replay returns before any write.
    assert module.REVOCATION_BODY.index("replayed := true") < module.REVOCATION_BODY.index(module.EPOCH)
    assert "account_sessions.created_at" not in module.EPOCH and "max(" not in module.EPOCH.lower()
    assert module.REVOCATION_FUNCTION == module.REVOCATION_FUNCTION_V1.replace(revocation.BODY, module.REVOCATION_BODY)
    assert module.REVOCATION_FUNCTION.startswith("CREATE OR REPLACE FUNCTION kalillac.v1_revoke_sessions(")


def test_offline_downgrade_is_refused_before_destructive_sql(roles):
    with pytest.raises(RuntimeError, match="^V1_ISSUANCE_OFFLINE_DOWNGRADE_UNSUPPORTED$"):
        _render(REVISION + ":" + PREVIOUS, downgrade=True)


def test_sqlite_upgrade_and_downgrade_are_noops(monkeypatch):
    # No role settings: SQLite never reads them.
    monkeypatch.delenv(migration_roles.V1_SECURITY_SERVICE_ROLE_ENV, raising=False)
    monkeypatch.delenv(migration_roles.V1_SECURITY_EXECUTOR_ROLE_ENV, raising=False)
    engine = make_sqlite_engine()
    with engine.begin() as connection:
        command.upgrade(_config(connection=connection), PREVIOUS)
        before = {t: [c["name"] for c in inspect(connection).get_columns(t, schema="kalillac")]
                  for t in inspect(connection).get_table_names(schema="kalillac")}
        command.upgrade(_config(connection=connection), REVISION)
        assert connection.execute(text("SELECT version_num FROM kalillac.alembic_version")).scalar_one() == REVISION
        after = {t: [c["name"] for c in inspect(connection).get_columns(t, schema="kalillac")]
                 for t in inspect(connection).get_table_names(schema="kalillac")}
        assert after == before
        command.downgrade(_config(connection=connection), PREVIOUS)
        assert connection.execute(text("SELECT version_num FROM kalillac.alembic_version")).scalar_one() == PREVIOUS


# --- roles -------------------------------------------------------------------------------------

def test_offline_upgrade_refuses_without_the_role_settings(monkeypatch):
    monkeypatch.setenv(migration_roles.V1_SECURITY_SERVICE_ROLE_ENV, NAMES["service"])
    monkeypatch.delenv(migration_roles.V1_SECURITY_EXECUTOR_ROLE_ENV, raising=False)
    with pytest.raises(migration_roles.RoleNameRequired):
        _render(PREVIOUS + ":" + REVISION)


def test_there_is_no_role_name_fallback():
    source = MIGRATION.read_text(encoding="utf-8")
    assert "kalillac_v1_security_service" not in source and "kalillac_v1_security_executor" not in source
    assert "kalillac_registration" not in source and "kalillac_owner" not in source


# --- classifier and exposure -------------------------------------------------------------------

def test_classifier_constants_describe_the_installed_definition():
    module = _module()
    assert v1_schema.ISSUANCE_SIGNATURE == module.SIGNATURE.replace(", ", ",")
    (arguments, result, language, volatility, definer, leakproof, strict, parallel, kind, config, cost,
     rows) = v1_schema.ISSUANCE_DEFINITION
    head = module.FUNCTION.split("AS $v1_issue$")[0]
    assert f"({arguments})" in head and f"LANGUAGE {language}" in head
    assert result == ("TABLE(outcome text, issued_at timestamp with time zone, valid_until timestamp with time zone,"
                      " replayed boolean)")
    assert "RETURNS TABLE(outcome text, issued_at timestamptz, valid_until timestamptz, replayed boolean)" in head
    assert (volatility, definer, leakproof, strict, parallel, kind) == ("v", True, False, False, "u", "f")
    # No log_parameter_max_length_on_error pin: PostgreSQL renders bound values at Bind,
    # before a function's configuration applies, so only the caller's setting governs.
    assert config == ["search_path=pg_catalog, pg_temp"] and (cost, rows) == (100.0, 1000.0)


def test_runtime_never_imports_the_migration_or_calls_issuance():
    for name in ("v1_schema.py", "registration_schema.py"):
        tree = ast.parse((CANDIDATE / "kalillac_accounts" / name).read_text(encoding="utf-8"))
        imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {
            a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert not any(module and ("alembic" in module or "migrations" in module) for module in imported)
    # No application module calls either function or uses the service role: both stay unexposed.
    for path in CANDIDATE.rglob("*.py"):
        parts = path.relative_to(CANDIDATE).parts
        if parts[0] in ("tests", "migrations") or "__pycache__" in parts:
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        assert "v1_issue_session(:" not in source and "FROM kalillac.v1_issue_session" not in source, path
        if path.name != "v1_schema.py":
            assert "v1_issue_session" not in source, path
