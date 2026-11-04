"""V1-02B private session-revocation authority: migration graph, DDL, roles and source. No PostgreSQL."""
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
from kalillac_db import migration_roles

CANDIDATE = Path(__file__).resolve().parents[1]
MIGRATION = CANDIDATE / "migrations" / "versions" / "0013_v1_session_revocation_authority.py"
REVISION = "0013_v1_session_revocation"
PREVIOUS = "0012_v1_account_security"
NAMES = {"service": "kalillac_v1_security_service", "executor": "kalillac_v1_security_executor",
         "owner": "kalillac_owner", "runtime": "kalillac_app"}


@pytest.fixture
def roles(monkeypatch):
    monkeypatch.setenv(migration_roles.V1_SECURITY_SERVICE_ROLE_ENV, NAMES["service"])
    monkeypatch.setenv(migration_roles.V1_SECURITY_EXECUTOR_ROLE_ENV, NAMES["executor"])
    monkeypatch.delenv(migration_roles.OWNER_ROLE_ENV, raising=False)
    monkeypatch.delenv(migration_roles.APP_ROLE_ENV, raising=False)


def _module():
    spec = importlib.util.spec_from_file_location("v1_02b_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render(revisions, downgrade=False):
    config = _config()
    config.set_main_option("sqlalchemy.url", "postgresql+psycopg://offline@localhost/kalillac")
    config.output_buffer = buffer = io.StringIO()
    (command.downgrade if downgrade else command.upgrade)(config, revisions, sql=True)
    return buffer.getvalue()


# --- graph -----------------------------------------------------------------------------------

def test_0013_is_the_sole_head_directly_after_0012():
    script = ScriptDirectory.from_config(_config())
    assert script.get_heads() == [REVISION]
    assert script.get_revision(REVISION).down_revision == PREVIOUS
    module = _module()
    assert (module.revision, module.down_revision, module.branch_labels, module.depends_on) == (
        REVISION, PREVIOUS, None, None)
    # alembic_version.version_num is VARCHAR(32).
    assert len(REVISION) <= 32


# --- roles -------------------------------------------------------------------------------------

@pytest.mark.parametrize("accessor,setting", [
    (migration_roles.v1_security_service_role, migration_roles.V1_SECURITY_SERVICE_ROLE_ENV),
    (migration_roles.v1_security_executor_role, migration_roles.V1_SECURITY_EXECUTOR_ROLE_ENV),
])
def test_v1_security_role_names_are_mandatory_and_safe(monkeypatch, accessor, setting):
    for unset in (None, "", "   "):
        if unset is None:
            monkeypatch.delenv(setting, raising=False)
        else:
            monkeypatch.setenv(setting, unset)
        with pytest.raises(migration_roles.RoleNameRequired, match=setting):
            accessor()
    for unsafe in ("Kalillac_Service", "x'; DROP SCHEMA kalillac; --", "a b", "a" * 64, "1role"):
        monkeypatch.setenv(setting, unsafe)
        with pytest.raises(migration_roles.InvalidRoleName):
            accessor()
    monkeypatch.setenv(setting, " kalillac_v1_role ")
    assert accessor() == "kalillac_v1_role"


def test_there_is_no_role_name_fallback_anywhere():
    for path in (CANDIDATE / "kalillac_db" / "migration_roles.py", MIGRATION,
                 CANDIDATE / "kalillac_accounts" / "v1_schema.py"):
        source = path.read_text(encoding="utf-8")
        assert "kalillac_v1_security_service" not in source and "kalillac_v1_security_executor" not in source
    assert not hasattr(migration_roles, "DEFAULT_V1_SECURITY_SERVICE_ROLE")


def test_offline_upgrade_refuses_without_the_role_settings(monkeypatch):
    monkeypatch.delenv(migration_roles.V1_SECURITY_SERVICE_ROLE_ENV, raising=False)
    monkeypatch.setenv(migration_roles.V1_SECURITY_EXECUTOR_ROLE_ENV, NAMES["executor"])
    with pytest.raises(migration_roles.RoleNameRequired):
        _render(PREVIOUS + ":" + REVISION)


# --- DDL ---------------------------------------------------------------------------------------

def _statements(sql):
    # Drop only Alembic's own progress comments; the function body keeps its own.
    body = sql.split("BEGIN;", 1)[1].rsplit("COMMIT;", 1)[0]
    body = "\n".join(line for line in body.splitlines() if not line.startswith("-- Running "))
    return [s.strip() for s in body.split(";\n\n") if s.strip() and "alembic_version" not in s]


def test_offline_upgrade_is_exactly_the_guarded_function_and_grant_sequence(roles):
    statements = _statements(_render(PREVIOUS + ":" + REVISION))
    module = _module()
    assert statements == [s.strip() for s in module.upgrade_statements(NAMES)]
    service, executor = NAMES["service"], NAMES["executor"]
    signature = "kalillac.v1_revoke_sessions(uuid, uuid, text, text)"
    assert statements[0].startswith("DO $v1_revocation_preflight$")
    assert statements[1:6] == [
        f"GRANT CREATE ON SCHEMA kalillac TO {executor}",
        module.FUNCTION,
        f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC",
        f"GRANT EXECUTE ON FUNCTION {signature} TO {service}",
        f"ALTER FUNCTION {signature} OWNER TO {executor}",
    ]
    assert statements[6:-1] == [
        f"GRANT USAGE ON SCHEMA kalillac TO {executor}, {service}",
        f"GRANT SELECT (id, is_active, quarantined_at, deletion_requested_at, sessions_valid_after),"
        f" UPDATE (updated_at) ON kalillac.users TO {executor}",
        f"GRANT SELECT (id, user_id, token_hash, created_at, expires_at, revoked_at), UPDATE (revoked_at)"
        f" ON kalillac.account_sessions TO {executor}",
        f"GRANT SELECT, INSERT ON kalillac.account_security_events TO {executor}",
        f"GRANT SELECT, INSERT, UPDATE ON kalillac.account_security_admission TO {executor}",
        f"GRANT SELECT, INSERT, UPDATE ON kalillac.account_security_admission_aggregate TO {executor}",
        f"REVOKE CREATE ON SCHEMA kalillac FROM {executor}",
    ]
    assert statements[-1].startswith("DO $v1_revocation_postcondition$")
    # Both guards carry the validated names as quoted literals only.
    for guard in (statements[0], statements[-1]):
        for name in NAMES.values():
            assert f"quote_ident('{name}')" in guard
    # Functions and grants only: no table, column, trigger, view, rule,
    # sequence, data, role or membership change outside the function body.
    outside = " ".join(s for s in statements if s is not statements[2]).upper()
    for word in ("CREATE TABLE", "ALTER TABLE", "CREATE INDEX", "CREATE TRIGGER", "CREATE VIEW", "CREATE RULE",
                 "CREATE SEQUENCE", "INSERT INTO", "UPDATE KALILLAC", "DELETE FROM", "CREATE ROLE", "ALTER ROLE",
                 "DROP ", "TRUNCATE", "DEFAULT PRIVILEGES", "WITH ADMIN", "WITH INHERIT", "SET ROLE"):
        assert word not in outside, word
    assert not re.search(r"\bGRANT\s+(?!CREATE ON SCHEMA|EXECUTE ON FUNCTION|USAGE ON SCHEMA|SELECT)", outside)
    assert not re.search(rf"\bGRANT \w+ TO {NAMES['owner'].upper()}\b", outside)


def test_declared_grants_match_the_classifier():
    module = _module()
    assert module.EXECUTOR_TABLE_GRANTS == v1_schema.EXECUTOR_TABLE_GRANTS
    assert module.EXECUTOR_COLUMN_GRANTS == v1_schema.EXECUTOR_COLUMN_GRANTS
    # No DELETE, TRUNCATE, REFERENCES or TRIGGER anywhere.
    rights = {r for rights in module.EXECUTOR_TABLE_GRANTS.values() for r in rights} | {
        right for _, right in module.EXECUTOR_COLUMN_GRANTS}
    assert rights == {"SELECT", "INSERT", "UPDATE"}
    assert module.EXECUTOR_COLUMN_GRANTS[("users", "UPDATE")] == ("updated_at",)
    assert "UPDATE kalillac.users" not in module.BODY


def test_function_definition_is_static_and_pinned():
    module = _module()
    assert hashlib.sha256(module.BODY.encode()).hexdigest() == v1_schema.REVOCATION_SOURCE_SHA256
    assert module.FUNCTION.startswith(
        "CREATE FUNCTION kalillac.v1_revoke_sessions(p_operation_id uuid, p_session_id uuid,"
        " p_token_hash text, p_scope text) RETURNS TABLE(outcome text, retry_after_seconds integer,"
        " replayed boolean) LANGUAGE plpgsql VOLATILE SECURITY DEFINER PARALLEL UNSAFE"
        " SET search_path = pg_catalog, pg_temp AS $v1_revoke$")
    assert module.FUNCTION.endswith("$v1_revoke$") and module.FUNCTION.count("$v1_revoke$") == 2
    for absent in ("STRICT", "LEAKPROOF", "IMMUTABLE", "STABLE"):
        assert absent not in module.FUNCTION.split("AS $v1_revoke$")[0]
    body = module.BODY.upper()
    # No dynamic SQL, identifier construction, role switching or settings.
    for word in ("EXECUTE ", "FORMAT(", "QUOTE_IDENT", "QUOTE_LITERAL", "SET ROLE", "SET_CONFIG", "SET SESSION",
                 "COPY", "DBLINK", "PG_READ", "LO_", "CREATE ", "ALTER ", "DROP ", "GRANT ", "REVOKE ",
                 "DELETE ", "TRUNCATE", "COMMIT", "ROLLBACK", "SAVEPOINT", "NOTIFY", "PG_SLEEP"):
        assert word not in body, word
    # Every table is fully qualified; only the declared tables are touched.
    tables = set(re.findall(r"\bkalillac\.(\w+)", module.BODY))
    assert tables == {"users", "account_sessions", "account_security_events", "account_security_admission",
                      "account_security_admission_aggregate"}
    # Exactly one exception handler, for operation-ID collisions only.
    assert re.findall(r"EXCEPTION WHEN (\w+)", module.BODY) == ["unique_violation"]
    # No caller-supplied account ID: the parameters are exactly these.
    assert re.findall(r"\bp_\w+", module.BODY) and set(re.findall(r"\bp_\w+", module.BODY)) == {
        "p_operation_id", "p_session_id", "p_token_hash", "p_scope"}


def test_event_rows_carry_no_secret():
    body = _module().BODY
    insert = body[body.index("INSERT INTO kalillac.account_security_events"):]
    insert = insert[:insert.index(";")]
    assert "(operation_id, user_id, event_type, occurred_at)" in insert
    for secret in ("token", "hash", "email", "password"):
        assert secret not in insert.lower()


def test_offline_downgrade_is_refused_before_destructive_sql(roles):
    with pytest.raises(RuntimeError, match="^V1_REVOCATION_OFFLINE_DOWNGRADE_UNSUPPORTED$"):
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


def test_classifier_constants_describe_the_installed_definition():
    module = _module()
    assert v1_schema.REVOCATION_SIGNATURE == module.SIGNATURE.replace(", ", ",")
    (arguments, result, language, volatility, definer, leakproof, strict, parallel, kind, config, cost,
     rows) = v1_schema.REVOCATION_DEFINITION
    head = module.FUNCTION.split("AS $v1_revoke$")[0]
    assert f"({arguments})" in head and f"RETURNS {result}" in head and f"LANGUAGE {language}" in head
    assert (volatility, definer, leakproof, strict, parallel, kind) == ("v", True, False, False, "u", "f")
    assert config == ["search_path=pg_catalog, pg_temp"] and (cost, rows) == (100.0, 1000.0)


def test_runtime_never_imports_the_migration_or_names_its_sql():
    tree = ast.parse((CANDIDATE / "kalillac_accounts" / "v1_schema.py").read_text(encoding="utf-8"))
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(name and ("alembic" in name or "migrations" in name) for name in imported)
    # No application module calls the function or uses the service role: it
    # stays unexposed. Only the classifier reads the role names, to qualify them.
    for path in CANDIDATE.rglob("*.py"):
        parts = path.relative_to(CANDIDATE).parts
        if parts[0] in ("tests", "migrations") or "__pycache__" in parts:
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        assert "FROM kalillac.v1_revoke_sessions" not in source and "v1_revoke_sessions(:" not in source, path
        if path.name not in ("v1_schema.py", "migration_roles.py"):
            assert "V1_SECURITY_SERVICE_ROLE" not in source and "v1_security_service_role" not in source, path
