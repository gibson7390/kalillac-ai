"""V1-02A prepared account-security schema: graph, DDL, startup gate, logging. No PostgreSQL."""
from __future__ import annotations

import ast
from contextlib import contextmanager
import importlib.util
import io
import json
import logging
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import traceback
from types import SimpleNamespace
import uuid

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, inspect, text
from sqlalchemy.exc import IntegrityError

from account_test_db import make_session_factory, make_sqlite_engine
from test_account_migration import _config
import kalillac_accounts.v1_schema as v1_schema
import kalillac_accounts.v1_security as v1_security
from kalillac_accounts.auth import RequestRejected
from kalillac_accounts.router import build_account_router
from kalillac_accounts.tokens import hash_session_token
from kalillac_accounts.v1_security import (FLAG, V1AccountSecurity, V1SecurityConfigError,
                                         V1SecurityUnavailable, load_v1_startup)
from kalillac_db.models import User

CANDIDATE = Path(__file__).resolve().parents[1]
MIGRATION = CANDIDATE / "migrations" / "versions" / "0012_v1_account_security.py"
HEAD = "0012_v1_account_security"
CHAIN = (HEAD, "0011_registration_intents", "0010_spaces", "0009_account_email_verification",
         "0008_account_recovery_tokens", "0007_anonymous_usage", "0006_provider_usage_pools",
         "0005_account_usage_slots", "0004_account_billing", "0003_account_usage_daily",
         "0002_account_entitlements", "0001_account_tables")
ENV = {FLAG: "true", "KALILLAC_ACCOUNTS_ENABLED": "true", "KALILLAC_DB_ENABLED": "true"}
NEW_USER_COLUMNS = {"deletion_requested_at", "password_reset_required"}
TOKEN = "synthetic-v1-02a-session-only"
LOGGER = "kalillac.accounts.v1"


@pytest.fixture(autouse=True)
def off(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)


def forbidden():
    pytest.fail("database or schema access before refusal")


# --- migration graph and DDL -------------------------------------------------------------

def test_0012_is_the_sole_head_directly_after_the_exact_0011_chain():
    script = ScriptDirectory.from_config(_config())
    assert script.get_heads() == [HEAD]
    assert script.get_revision(HEAD).down_revision == "0011_registration_intents"
    assert tuple(r.revision for r in script.walk_revisions("base", "heads")) == CHAIN


def _migration_module():
    spec = importlib.util.spec_from_file_location("v1_02a_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_source_grants_nothing_and_installs_no_writer():
    tree = ast.parse(MIGRATION.read_text(encoding="utf-8"))
    docstring = ast.get_docstring(tree, clean=False)
    # The guard template and its renderer are checked separately below.
    guard = {id(n) for top in tree.body if (isinstance(top, ast.Assign)
             and [t.id for t in top.targets] == ["PRIVILEGE_GUARD"]) or (isinstance(top, ast.FunctionDef)
             and top.name == "privilege_guard") for n in ast.walk(top)}
    sql = " ".join(n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                   and n.value != docstring and id(n) not in guard).upper()
    for word in ("GRANT", "REVOKE", "FUNCTION", "TRIGGER", "RULE", "DEFAULT PRIVILEGES", "OWNER",
                 "SECURITY DEFINER", "SET ROLE", "COMMENT ON", "UPDATE ", "DELETE "):
        assert not re.search(r"\b" + word, sql), word
    # Role names come only from the validated migration-role configuration.
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not names & {"getenv", "environ"}
    executes = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "execute" and getattr(n.func.value, "id", None) == "op"]
    # The only data statement is the prepared latch row; the other is the guard.
    assert len(executes) == 2 and getattr(executes[1].args[0].func, "id", None) == "privilege_guard"
    latch = " ".join(n.value for n in ast.walk(executes[0]) if isinstance(n, ast.Constant))
    assert latch.startswith("INSERT INTO kalillac.account_security_activation") and "'prepared'" in latch
    assert "'active'" not in latch


def test_privilege_guard_only_reads_catalogs_and_raises():
    migration = _migration_module()
    guard = migration.privilege_guard("kalillac_runtime_x", "kalillac_owner_x")
    assert guard.startswith("DO $v1_guard$") and guard.endswith("END $v1_guard$")
    # Configured names are embedded only as quoted literals, resolved server-side.
    assert "quote_ident('kalillac_runtime_x')" in guard and "quote_ident('kalillac_owner_x')" in guard
    # Quoted privilege names aside, the block has no statement that changes anything.
    code = re.sub(r"'[^']*'", "''", guard).upper()
    for word in ("GRANT", "REVOKE", "CREATE", "ALTER", "DROP", "INSERT", "UPDATE", "DELETE", "TRUNCATE",
                 "COMMENT", "EXECUTE", "PERFORM", "SET", "RESET", "LOCK", "COPY", "CALL", "NOTIFY"):
        assert not re.search(r"\b" + word + r"\b", code), word
    assert code.count("RAISE EXCEPTION") == 2 and code.count("MESSAGE = ''") == 2
    assert guard.count("'V1_SECURITY_PRIVILEGES_UNSAFE'") == 2
    for table in v1_schema.TABLES:
        assert guard.count("'" + table + "'") == 2
    # R/A membership is read from pg_auth_members and pg_has_role, never excused by the owner group.
    assert "pg_catalog.pg_auth_members" in guard and "'MEMBER'" in guard
    for unsafe in ("x'; DROP SCHEMA kalillac; --", "Kalillac_App", "", "a" * 64):
        with pytest.raises(ValueError):
            migration.privilege_guard(unsafe, "kalillac_owner")
        with pytest.raises(ValueError):
            migration.privilege_guard("kalillac_app", unsafe)


def _render(revisions):
    config = _config()
    config.set_main_option("sqlalchemy.url", "postgresql+psycopg://offline@localhost/kalillac")
    config.output_buffer = buffer = io.StringIO()
    if revisions.startswith(HEAD):
        command.downgrade(config, revisions, sql=True)
    else:
        command.upgrade(config, revisions, sql=True)
    return buffer.getvalue()


def _statements(sql):
    # Alembic ends every offline statement with ";" and a blank line; the guard's
    # DO block contains inner semicolons but no blank line.
    body = sql.split("BEGIN;", 1)[1].rsplit("COMMIT;", 1)[0]
    body = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("--"))
    return [s.strip() for s in body.split(";\n\n") if s.strip() and "alembic_version" not in s]


def test_offline_postgresql_upgrade_is_owner_only_additive_ddl():
    statements = _statements(_render("0011_registration_intents:" + HEAD))
    kinds = [re.match(r"(\w+ \S+)", s).group(1).upper() for s in statements]
    assert kinds == ["ALTER TABLE", "ALTER TABLE"] + ["CREATE TABLE"] * 2 + ["CREATE INDEX"] \
        + ["CREATE TABLE"] * 3 + ["INSERT INTO", "DO $V1_GUARD$"]
    assert all(re.search(r"ADD COLUMN (deletion_requested_at|password_reset_required)", s) for s in statements[:2])
    assert "password_reset_required BOOLEAN DEFAULT false NOT NULL" in statements[1]
    created = [re.search(r"CREATE TABLE kalillac\.(\w+)", s).group(1) for s in statements if s.startswith("CREATE TABLE")]
    assert sorted(created) == sorted(v1_schema.TABLES)
    assert "SERIAL" not in " ".join(statements).upper() and "DEFAULT" not in " ".join(statements[2:-2]).upper()
    assert statements[-2].startswith("INSERT INTO kalillac.account_security_activation")
    # The privilege refusal is server-side, so a reviewed offline script carries it too.
    from kalillac_db.migration_roles import migration_app_role, migration_owner_role
    assert statements[-1] == _migration_module().privilege_guard(migration_app_role(), migration_owner_role())


@pytest.mark.parametrize("url,target", [
    ("postgresql+psycopg://offline@localhost/kalillac", "0011_registration_intents"),
    ("postgresql+psycopg://offline@localhost/kalillac", "base"),
    ("sqlite://", "0011_registration_intents"),
])
def test_offline_downgrade_through_0012_is_refused_before_destructive_sql(url, target):
    config = _config()
    config.set_main_option("sqlalchemy.url", url)
    config.output_buffer = buffer = io.StringIO()
    with pytest.raises(RuntimeError, match="^V1_SECURITY_OFFLINE_DOWNGRADE_UNSUPPORTED$"):
        command.downgrade(config, HEAD + ":" + target, sql=True)
    emitted = [line for line in buffer.getvalue().splitlines() if line.strip() and not line.startswith("--")]
    # At most the transaction opener: no DROP, ALTER, DELETE or version change.
    assert emitted in ([], ["BEGIN;"]), emitted


def test_sqlite_upgrade_adds_only_the_user_columns_with_exact_defaults():
    engine = make_sqlite_engine()
    with engine.begin() as connection:
        command.upgrade(_config(connection=connection), "0011_registration_intents")
        connection.execute(text("INSERT INTO kalillac.users (id,email,password_hash,created_at,updated_at,is_active)"
                                " VALUES ('00000000000000000000000000000001','pre@example.test','h',:t,:t,1)"),
                           {"t": "2026-01-01 00:00:00"})
        before = set(inspect(connection).get_table_names(schema="kalillac"))
        command.upgrade(_config(connection=connection), HEAD)
        assert set(inspect(connection).get_table_names(schema="kalillac")) == before
        columns = {c["name"]: c for c in inspect(connection).get_columns("users", schema="kalillac")}
        assert columns["deletion_requested_at"]["nullable"] is True
        assert columns["password_reset_required"]["nullable"] is False
        assert connection.execute(text("SELECT deletion_requested_at, password_reset_required FROM kalillac.users"
                                       )).one() == (None, 0)
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(text("UPDATE kalillac.users SET password_reset_required = NULL"))
        command.downgrade(_config(connection=connection), "0011_registration_intents")
        assert not NEW_USER_COLUMNS & {c["name"] for c in inspect(connection).get_columns("users", schema="kalillac")}
        assert connection.execute(text("SELECT email FROM kalillac.users")).scalar_one() == "pre@example.test"


def test_orm_never_names_the_new_user_columns_and_works_before_0012():
    engine = make_sqlite_engine()
    with engine.begin() as connection:
        command.upgrade(_config(connection=connection), "0011_registration_intents")
    statements = []
    event.listen(engine, "before_cursor_execute", lambda *a: statements.append(a[2]))
    factory = make_session_factory(engine)
    with factory() as session, session.begin():
        session.add(User(email="legacy@example.test", password_hash="synthetic"))
    with factory() as session:
        assert session.query(User).one().email == "legacy@example.test"
    assert statements and not any(column in s for s in statements for column in NEW_USER_COLUMNS)


# --- startup gate ------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", " FALSE "])
def test_off_startup_touches_no_database_or_schema(value):
    assert load_v1_startup(forbidden, {FLAG: value}) is None


@pytest.mark.parametrize("missing", ["KALILLAC_ACCOUNTS_ENABLED", "KALILLAC_DB_ENABLED"])
def test_on_startup_checks_prerequisites_before_the_database(missing):
    env = dict(ENV)
    env.pop(missing)
    with pytest.raises(V1SecurityConfigError, match="requires accounts and database"):
        load_v1_startup(forbidden, env)
    with pytest.raises(V1SecurityConfigError, match="must be a boolean"):
        load_v1_startup(forbidden, {FLAG: "tru"})


def _boom():
    raise RuntimeError("postgresql://synthetic-user:synthetic-secret@db.invalid/kalillac")


@pytest.mark.parametrize("factory,reason", [
    (lambda: None, "database_disabled"),
    (lambda: make_session_factory(make_sqlite_engine()), "unsupported_database"),
    (_boom, "state_unreadable"),
])
def test_on_startup_refuses_without_postgresql_and_sanitizes(factory, reason):
    with pytest.raises(V1SecurityConfigError) as error:
        load_v1_startup(factory, ENV)
    assert str(error.value) == FLAG + " requires a qualified V1 private-authority catalog (" + reason + ")."
    assert error.value.__suppress_context__ and "secret" not in str(error.value)


class FakeSession:
    def __init__(self, log):
        self.log = log

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def execute(self, statement, params=None):
        self.log.append(str(statement))

    @contextmanager
    def begin(self):
        yield self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.mark.parametrize("state,reason", [(v1_schema.LEGACY, "schema_not_prepared"),
                                          (v1_schema.PREPARED, "prepared_inactive"),
                                          (v1_schema.ACTIVE, "authority_catalog_unqualified")])
def test_every_schema_state_refuses_on_startup_in_a_read_only_transaction(monkeypatch, state, reason):
    log = []
    monkeypatch.setattr(v1_schema, "classify", lambda session: log.append("classify") or state)
    monkeypatch.setattr(v1_security, "load_v1_security", lambda *a, **k: pytest.fail("V1 loaded without authority"))
    with pytest.raises(V1SecurityConfigError, match=re.escape("(" + reason + ")")):
        load_v1_startup(lambda: (lambda: FakeSession(log)), ENV)
    assert log == ["SET TRANSACTION READ ONLY", "SET LOCAL lock_timeout = '3s'",
                   "SET LOCAL statement_timeout = '3s'", "classify"]


def test_application_startup_uses_the_gate_and_never_migrates():
    app_source = (CANDIDATE / "app_fastapi_candidate.py").read_text(encoding="utf-8")
    assert "_v1_account_security = _load_v1_startup()" in app_source
    assert "load_v1_security as" not in app_source
    runtime = [CANDIDATE / "app_fastapi_candidate.py", *CANDIDATE.glob("kalillac_accounts/*.py"),
               *CANDIDATE.glob("kalillac_db/**/*.py")]
    for path in runtime:
        source = path.read_text(encoding="utf-8")
        assert not re.search(r"^\s*(from|import)\s+alembic", source, re.M), path.name
        assert "command.upgrade" not in source, path.name


def test_off_mode_application_import_loads_no_v1_schema_or_migration_machinery():
    env = {k: v for k, v in os.environ.items() if not k.startswith("KALILLAC_")}
    env.update({"OPENAI_API_KEY": "", "TAVILY_API_KEY": "", "BRAVE_API_KEY": "", "DEBUG_MODE": "false",
                "GROQ_API_KEY": "synthetic-never-real", "PYTHONDONTWRITEBYTECODE": "1"})
    probe = ("import json, socket, sys\n"
             "real = socket.socket.connect\n"
             "def guard(self, address):\n"
             "    if (address[0] if isinstance(address, tuple) else address) not in ('127.0.0.1', '::1', 'localhost'):\n"
             "        raise ConnectionRefusedError('blocked')\n"
             "    return real(self, address)\n"
             "socket.socket.connect = guard\n"
             "import app_fastapi_candidate as app\n"
             "print(json.dumps({'v1': app._v1_account_security is None,"
             " 'schema': 'kalillac_accounts.v1_schema' in sys.modules, 'alembic': 'alembic' in sys.modules}))")
    result = subprocess.run([sys.executable, "-B", "-c", probe], cwd=str(CANDIDATE), env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"v1": True, "schema": False, "alembic": False}


def test_no_runtime_module_writes_or_names_the_security_tables():
    allowed = {CANDIDATE / "kalillac_accounts" / "v1_schema.py"}
    for path in CANDIDATE.rglob("*.py"):
        parts = path.relative_to(CANDIDATE).parts
        if parts[0] in ("tests", "migrations") or path in allowed or "__pycache__" in parts:
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        assert not any(table in source for table in v1_schema.TABLES), path
    # Every SQL statement in the classifier reads (SELECT) or sets a transaction-
    # local limit; privilege names such as 'TRUNCATE' are only checked, never run.
    tree = ast.parse((CANDIDATE / "kalillac_accounts" / "v1_schema.py").read_text(encoding="utf-8"))
    docstrings = {ast.get_docstring(n, clean=False) for n in ast.walk(tree)
                  if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef))}
    statements = [part.split() for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                  and n.value not in docstrings for part in n.value.upper().split(";") if len(part.split()) > 1]
    for words in statements:
        assert words[0] not in ("INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE", "GRANT", "REVOKE", "CREATE",
                                "DROP", "ALTER", "COMMENT", "DO", "CALL", "COPY", "LOCK", "RESET", "SECURITY"), words
        assert words[0] != "SET" or words[1] in ("LOCAL", "TRANSACTION"), words
    assert any(words[0] == "SELECT" for words in statements)


# --- sanitized operational diagnostic -------------------------------------------------

# Carried by every secret-bearing value below; it must surface nowhere.
MARKER = "v1-02a-marker-" + uuid.uuid4().hex
LINE = "V1 account authorization unavailable: {} sqlstate={}\n"


class SyntheticDatabaseError(Exception):
    def __init__(self, sqlstate):
        super().__init__("SELECT kalillac.users WHERE token_hash = '" + hash_session_token(TOKEN)
                         + "' password=synthetic-secret synthetic@example.test " + MARKER)
        self.orig = SimpleNamespace(sqlstate=sqlstate)


@pytest.fixture
def failing(monkeypatch):
    def configure(error):
        def prepare(session):
            raise error
        monkeypatch.setattr(v1_security, "_prepare", prepare)
        return V1AccountSecurity(lambda: make_session_factory(make_sqlite_engine()))
    return configure


def _texts(caplog):
    return " ".join(r.getMessage() + repr(r.args) + str(r.exc_info) + str(r.exc_text) for r in caplog.records)


def assert_undisclosed(caplog, captured, *errors):
    """No secret in any log record, stdout, stderr or exception chain."""
    chains = "".join("".join(traceback.format_exception(e)) + repr(e.__context__) + repr(e.__cause__)
                     for e in errors)
    exposed = _texts(caplog) + captured.out + captured.err + chains
    for secret in (MARKER, "synthetic-secret", "synthetic@example.test", TOKEN, hash_session_token(TOKEN)):
        assert secret not in exposed


@pytest.mark.parametrize("sqlstate,shown", [("55P03", "55P03"), ("57014", "57014"), ("x'; --", "-"), (None, "-")])
def test_database_failure_writes_only_the_fixed_diagnostic(caplog, capfd, failing, sqlstate, shown):
    security = failing(SyntheticDatabaseError(sqlstate))
    with caplog.at_level("DEBUG", logger=LOGGER), pytest.raises(V1SecurityUnavailable) as error:
        security.read(hash_session_token(TOKEN), lambda *a: None)
    assert error.value.__suppress_context__ and error.value.__cause__ is None and error.value.__context__ is None
    captured = capfd.readouterr()
    assert (captured.out, captured.err) == ("", LINE.format("SyntheticDatabaseError", shown))
    assert not caplog.records
    assert_undisclosed(caplog, captured, error.value)


@pytest.mark.parametrize("case", ["rejected-401", "rejected-403", "cancelled"])
def test_rejection_and_cancellation_are_unchanged_and_write_nothing(capfd, monkeypatch, failing, case):
    error = {"rejected-401": RequestRejected(401, "not_authenticated"),
             "rejected-403": RequestRejected(403, "account_unavailable"),
             "cancelled": SyntheticDatabaseError("57014")}[case]
    emitted = []
    monkeypatch.setattr(v1_security, "_emit", emitted.append)
    security = failing(error)
    stopped = SimpleNamespace(is_set=lambda: case == "cancelled")
    with pytest.raises((RequestRejected, V1SecurityUnavailable)) as raised:
        security.read(hash_session_token(TOKEN), lambda *a: None, stopped)
    if case == "cancelled":
        assert type(raised.value) is V1SecurityUnavailable
    else:
        assert raised.value is error
    assert emitted == [] and capfd.readouterr() == ("", "")


class HostileSqlstate(Exception):
    """A driver error whose SQLSTATE accessor throws."""
    @property
    def sqlstate(self):
        raise RuntimeError(MARKER)


class HostileOrig(Exception):
    """A wrapper whose original-error accessor throws."""
    @property
    def orig(self):
        raise RuntimeError(MARKER)


class _HostileName(type):
    @property
    def __name__(cls):
        raise RuntimeError(MARKER)


class HostileClassName(Exception, metaclass=_HostileName):
    """Even the class-name metadata accessor throws."""
    sqlstate = "55P03"


class SlyState(str):
    def isascii(self):
        raise RuntimeError(MARKER)


class SlySqlstate(Exception):
    """A SQLSTATE that is a str subclass with a throwing method."""
    sqlstate = SlyState("55P03")


@contextmanager
def hostile_environment(kind, monkeypatch):
    """A hostile logging graph or diagnostic sink; yields the hostile calls made."""
    invoked = []

    def hostile(*args, **kwargs):
        invoked.append(kind)
        raise RuntimeError(MARKER)

    class Formatter(logging.Formatter):
        format = hostile

    class Handler(logging.Handler):
        emit = hostile

    attached, cleanup = [], []
    if kind in SINKS:
        fd, cleanup = SINKS[kind](monkeypatch)
        monkeypatch.setattr(v1_security, "DIAGNOSTIC_FD", fd)
    elif kind == "formatter":
        # A standard stderr StreamHandler whose formatter fails with a secret:
        # logging would report that failure, traceback included, on stderr.
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(Formatter())
        attached = [handler]
    elif kind == "handler":
        attached = [Handler()]
    elif kind == "filter":
        for name in (LOGGER, ""):
            monkeypatch.setattr(logging.getLogger(name), "filters", [hostile])
    elif kind == "logger":
        monkeypatch.setattr(logging.Logger, "_log", hostile)
    elif kind == "sink":
        monkeypatch.setattr(v1_security, "_emit", hostile)
    loggers = [logging.getLogger(LOGGER), logging.getLogger()]
    for logger in loggers:
        for handler in attached:
            logger.addHandler(handler)
    try:
        yield invoked
    finally:
        for logger in loggers:
            for handler in attached:
                logger.removeHandler(handler)
        for close in cleanup:
            close()


def _fill(sock_or_fd):
    """Fill a pipe or socket to capacity through an independent nonblocking path."""
    if isinstance(sock_or_fd, socket.socket):
        while True:
            try:
                sock_or_fd.send(b"#" * 65536, socket.MSG_DONTWAIT)
            except BlockingIOError:
                return
    fd = os.open("/proc/self/fd/%d" % sock_or_fd, os.O_WRONLY | os.O_NONBLOCK)
    try:
        while True:
            try:
                os.write(fd, b"#" * 65536)
            except BlockingIOError:
                return
    finally:
        os.close(fd)


def _pipe(monkeypatch, *, reader=True, full=False, unopenable=False):
    read_end, write_end = os.pipe()
    if not reader:
        os.close(read_end)
    if full:
        _fill(write_end)
    if unopenable:
        monkeypatch.setattr(v1_security, "_REOPEN", "/nonexistent-v1-02a-sink/{}")
    return write_end, [lambda: os.close(write_end)] + ([lambda: os.close(read_end)] if reader else [])


def _socket(monkeypatch, *, full=False, peer=True):
    ours, theirs = socket.socketpair()
    if full:
        _fill(ours)
    if not peer:
        theirs.close()
    return ours.fileno(), [ours.close, theirs.close]


def _closed_fd(monkeypatch):
    # Descriptors are allocated lowest first, so this one stays unused.
    with pytest.raises(OSError):
        os.fstat(1023)
    return 1023, []


def _read_only_fd(monkeypatch):
    fd = os.open(__file__, os.O_RDONLY)
    return fd, [lambda: os.close(fd)]


# Diagnostic endpoints that cannot take the line now: each must drop it at once,
# without blocking and without touching the shared descriptor's flags.
SINKS = {
    "no-reader": lambda mp: _pipe(mp, reader=False),
    "full-pipe": lambda mp: _pipe(mp, full=True),
    "sink-open-failure": lambda mp: _pipe(mp, unopenable=True),
    "full-socket": lambda mp: _socket(mp, full=True),
    "socket-write-failure": lambda mp: _socket(mp, peer=False),
    "closed-fd": _closed_fd,
    "read-only-fd": _read_only_fd,
}


# (error, hostile environment, the fixed diagnostic's tokens when the sink works)
HOSTILE = {
    "formatter": (SyntheticDatabaseError("55P03"), "formatter", ("SyntheticDatabaseError", "55P03")),
    "filter": (SyntheticDatabaseError("55P03"), "filter", ("SyntheticDatabaseError", "55P03")),
    "handler": (SyntheticDatabaseError("55P03"), "handler", ("SyntheticDatabaseError", "55P03")),
    "logger-method": (SyntheticDatabaseError("55P03"), "logger", ("SyntheticDatabaseError", "55P03")),
    "sqlstate-accessor": (HostileSqlstate(MARKER), None, ("HostileSqlstate", "-")),
    "orig-accessor": (HostileOrig(MARKER), None, ("HostileOrig", "-")),
    "class-name-accessor": (HostileClassName(MARKER), None, ("-", "55P03")),
    "sqlstate-str-subclass": (SlySqlstate(MARKER), None, ("SlySqlstate", "-")),
    "sink-failure": (SyntheticDatabaseError("55P03"), "sink", None),
    "extraction-and-sink-failure": (HostileSqlstate(MARKER), "sink", None),
    **{name: (SyntheticDatabaseError("55P03"), name, None) for name in SINKS},
    "extraction-and-full-pipe": (HostileSqlstate(MARKER), "full-pipe", None),
}


@pytest.mark.parametrize("case", list(HOSTILE))
def test_hostile_diagnostics_or_logging_never_disclose_or_replace_the_503(caplog, capfd, monkeypatch, failing, case):
    error, kind, tokens = HOSTILE[case]
    security = failing(error)
    api = FastAPI()
    api.include_router(build_account_router(forbidden, v1_security=security))
    # DEBUG on the V1 logger only: other libraries' own records (httpx) stay out
    # of the hostile root graph, which the V1 path must never enter itself.
    with caplog.at_level("DEBUG", logger=LOGGER), hostile_environment(kind, monkeypatch) as invoked:
        blocking = os.get_blocking(v1_security.DIAGNOSTIC_FD) if kind in SINKS and kind != "closed-fd" else None
        started = time.monotonic()
        with pytest.raises(V1SecurityUnavailable) as raised:
            security.read(hash_session_token(TOKEN), lambda *a: None)
        with TestClient(api, base_url="https://testserver") as client:
            response = client.get("/api/account/me", headers={"Cookie": "__Host-kalillac_account=" + TOKEN})
        elapsed = time.monotonic() - started
        if blocking is not None:
            # The shared descriptor's flags are untouched.
            assert os.get_blocking(v1_security.DIAGNOSTIC_FD) is blocking is True
    assert elapsed < 2.0
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    assert (response.status_code, response.json(), response.headers["cache-control"]) == (
        503, {"error": "account_unavailable"}, "no-store")
    # The logging graph is never entered; only the controlled sink may be.
    assert invoked == (["sink"] * 2 if kind == "sink" else [])
    assert not [r for r in caplog.records if r.name == LOGGER]
    captured = capfd.readouterr()
    assert (captured.out, captured.err) == ("", "" if tokens is None else LINE.format(*tokens) * 2)
    assert_undisclosed(caplog, captured, raised.value)


@pytest.mark.parametrize("path", ["me", "entitlements"])
def test_reported_failure_keeps_the_exact_unavailable_response(caplog, capfd, failing, path):
    security = failing(SyntheticDatabaseError("55P03"))
    api = FastAPI()
    api.include_router(build_account_router(forbidden, v1_security=security))
    with TestClient(api, base_url="https://testserver") as client:
        response = client.get("/api/account/" + path, headers={"Cookie": "__Host-kalillac_account=" + TOKEN})
    assert (response.status_code, response.json(), response.headers["cache-control"]) == (
        503, {"error": "account_unavailable"}, "no-store")
    captured = capfd.readouterr()
    assert (captured.out, captured.err) == ("", LINE.format("SyntheticDatabaseError", "55P03"))
    assert_undisclosed(caplog, captured)


@pytest.mark.parametrize("endpoint", ["pipe", "socket"])
def test_controlled_sink_delivers_the_fixed_line_without_touching_the_shared_descriptor(monkeypatch, failing,
                                                                                          endpoint):
    if endpoint == "pipe":
        reader, fd = os.pipe()
        receive, cleanup = (lambda: os.read(reader, 4096)), [lambda: os.close(reader), lambda: os.close(fd)]
    else:
        ours, theirs = socket.socketpair()
        fd, receive, cleanup = ours.fileno(), (lambda: theirs.recv(4096)), [ours.close, theirs.close]
    monkeypatch.setattr(v1_security, "DIAGNOSTIC_FD", fd)
    try:
        with pytest.raises(V1SecurityUnavailable):
            failing(SyntheticDatabaseError("55P03")).read(hash_session_token(TOKEN), lambda *a: None)
        assert os.get_blocking(fd) is True and os.fstat(fd)
        assert receive() == LINE.format("SyntheticDatabaseError", "55P03").encode("ascii")
    finally:
        for close in cleanup:
            close()


# Runs the real HTTP route in a child whose stderr is a full, closed or
# read-only descriptor. Results go to a file: stderr is never read until exit.
PROBE = r'''
import asyncio, gc, json, logging, os, sys, threading, time, tracemalloc, traceback
from types import SimpleNamespace
scenario, result_path, marker = sys.argv[1:4]
sys.path.insert(0, "tests")
out = {}
try:
    invoked = []

    class Recording(logging.StreamHandler):
        def emit(self, record):
            invoked.append(record.name)
            super().emit(record)
    for name in ("", "kalillac.accounts.v1"):
        logging.getLogger(name).addHandler(Recording(sys.stderr))
    logging.getLogger("kalillac.accounts.v1").setLevel(logging.DEBUG)
    if scenario == "full-stderr-pipe":
        # Filled through an independent description; fd 2 itself stays blocking.
        fd, filled = os.open("/proc/self/fd/2", os.O_WRONLY | os.O_NONBLOCK), 0
        try:
            while True:
                try:
                    filled += os.write(fd, b"#" * 4096)
                except BlockingIOError:
                    break
            try:
                os.write(fd, b"#")
                out["full"] = False
            except BlockingIOError:
                out["full"] = True
        finally:
            os.close(fd)
        out["filled"] = filled
    elif scenario == "closed-stderr":
        os.close(2)
    elif scenario == "read-only-stderr":
        fd = os.open(os.path.abspath("tests/test_v1_schema.py"), os.O_RDONLY)
        os.dup2(fd, 2)
        os.close(fd)
    out["blocking"] = None if scenario == "closed-stderr" else os.get_blocking(2)

    import kalillac_accounts.v1_security as v1
    from kalillac_accounts.router import build_account_router
    from kalillac_accounts.tokens import hash_session_token
    from account_test_db import make_session_factory, make_sqlite_engine
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    class Failure(Exception):
        def __init__(self):
            super().__init__("SELECT kalillac.users password=" + marker)
            self.orig = SimpleNamespace(sqlstate="55P03")
    gate = {"event": None}

    def prepare(session):
        if gate["event"] is not None:
            gate["event"].wait(10)
        raise Failure()
    v1._prepare = prepare
    factory = make_session_factory(make_sqlite_engine())
    security = v1.V1AccountSecurity(lambda: factory)

    def forbidden():
        raise AssertionError("database access")
    api = FastAPI()
    api.include_router(build_account_router(forbidden, v1_security=security))
    token = "synthetic-v1-02a-probe-session"
    digest, responses = hash_session_token(token), set()
    # Counters only: the probe itself must retain nothing per failure.
    refusals = {"count": 0, "disclosed": False, "slowest": 0.0}

    def direct():
        started = time.monotonic()
        try:
            security.read(digest, lambda *a: None)
        except v1.V1SecurityUnavailable as error:
            chain = "".join(traceback.format_exception(error)) + repr(error.__context__) + repr(error.__cause__)
            refusals["count"] += 1
            refusals["disclosed"] |= marker in chain
        return time.monotonic() - started

    def descriptors():
        return len(os.listdir("/proc/self/fd"))
    with TestClient(api, base_url="https://testserver") as client:
        def http():
            started = time.monotonic()
            response = client.get("/api/account/me", headers={"Cookie": "__Host-kalillac_account=" + token})
            responses.add((response.status_code, json.dumps(response.json()), response.headers["cache-control"],
                           marker in response.text))
            return time.monotonic() - started
        for _ in range(5):
            direct(), http()
        gc.collect()
        before = (descriptors(), threading.active_count())
        tracemalloc.start()
        baseline = tracemalloc.get_traced_memory()[0]
        for _ in range(300):
            refusals["slowest"] = max(refusals["slowest"], direct())
        for _ in range(30):
            refusals["slowest"] = max(refusals["slowest"], http())
        gc.collect()
        growth = tracemalloc.get_traced_memory()[0] - baseline
        tracemalloc.stop()
        after = (descriptors(), threading.active_count())

    async def cancelled():
        gate["event"] = threading.Event()
        task = asyncio.create_task(security.read_async(digest, lambda *a: None))
        await asyncio.sleep(0.1)
        task.cancel()
        asyncio.get_running_loop().call_later(0.1, gate["event"].set)
        started = time.monotonic()
        try:
            await task
        except asyncio.CancelledError:
            return "cancelled", time.monotonic() - started
        return "completed", time.monotonic() - started
    cancel = asyncio.run(cancelled())
    out.update(max_latency=refusals["slowest"], responses=sorted(map(list, responses)), before=before, after=after,
               memory_growth=growth, cancel=cancel[0], cancel_seconds=cancel[1], invoked=invoked,
               chains_disclose=refusals["disclosed"], refusals=refusals["count"],
               blocking_after=None if scenario == "closed-stderr" else os.get_blocking(2))
except BaseException:
    out["error"] = traceback.format_exc()
with open(result_path, "w") as handle:
    json.dump(out, handle)
'''


@pytest.mark.parametrize("scenario", ["full-stderr-pipe", "closed-stderr", "read-only-stderr"])
def test_http_route_never_waits_for_stderr_in_a_bounded_subprocess(tmp_path, scenario):
    result = tmp_path / "result.json"
    env = {k: v for k, v in os.environ.items() if not k.startswith("KALILLAC_")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    child = subprocess.Popen([sys.executable, "-B", "-W", "ignore", "-c", PROBE, scenario, str(result), MARKER],
                             cwd=str(CANDIDATE), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE)
    try:
        # A hard deadline; the full pipe is never drained before the child exits.
        child.wait(timeout=90)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()
        child.stderr.close()
        pytest.fail("the authorization route waited on stderr")
    leftover = child.stderr.read()
    child.stderr.close()
    data = json.loads(result.read_text())
    assert child.returncode == 0 and "error" not in data, data.get("error")
    # Every refusal completed at once, with the exact 503, while stderr stayed unusable.
    assert data["max_latency"] < 1.0 and data["refusals"] >= 305
    assert data["responses"] == [[503, json.dumps({"error": "account_unavailable"}), "no-store", False]]
    # Repeated failures retain nothing: no descriptor, thread or memory growth.
    assert data["after"] == data["before"] and data["memory_growth"] < 64 * 1024
    assert (data["cancel"], data["cancel_seconds"] < 2.0) == ("cancelled", True)
    assert data["invoked"] == [] and not data["chains_disclose"]
    if scenario == "full-stderr-pipe":
        assert data["full"] is True and data["blocking"] is data["blocking_after"] is True
        # The pipe stayed full: it holds only the filler, nothing written later.
        assert leftover == b"#" * data["filled"]
    else:
        assert leftover == b""
    assert MARKER.encode() not in leftover
