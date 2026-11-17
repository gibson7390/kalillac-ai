"""tools/provision_deployment.py on real disposable PostgreSQL 16: a new, empty deployment from nothing.

What is proved with the real code, not a description of it: the deployment the tool creates is accepted
by the registration bootstrap (every production check except the file-permission loader, which needs
root and is proved by tools/verify_provisioned_boot.py), registers a real account, takes the operator
transition with the credential files the tool wrote, and then qualifies the V1 startup gate and the
service connection. And the refusals: it only creates, it never touches a role or database it did not
mark, it is repeatable only while the deployment is unused, and no password reaches SQL or its output.
Every credential is synthetic and exists only in the disposable cluster.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, make_url, text
from sqlalchemy.orm import sessionmaker

from kalillac_accounts.v1_security import FLAG, load_v1_startup
from registration_test_support import FakeEmail

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import provision_deployment as provision  # noqa: E402
import v1_security_transition as transition  # noqa: E402

DEPLOYMENT = "staging-test"
RELEASE = "b" * 40
DIRECTORY = "/etc/kalillac-staging-test"
ORIGIN = "https://staging.invalid"


class Files:
    """What RootFiles would have written, kept in memory."""

    def __init__(self):
        self.protected_files, self.private_files, self.settings_files, self.prepared = {}, {}, {}, 0
        self.fail_on = None

    def prepare(self):
        self.prepared += 1

    def protected(self, name, raw):
        if self.fail_on == name:
            raise OSError("synthetic write failure")
        self.protected_files[name] = raw

    def private(self, name, raw):
        self.private_files[name] = raw

    def settings(self, name, raw):
        self.settings_files[name] = raw

    def read(self, path, ceiling=None):
        prefix = DIRECTORY + "/protected/"
        if not path.startswith(prefix):
            raise OSError("not a protected file")
        return self.protected_files[path[len(prefix):]]

    def passwords(self):
        found = [json.loads(self.protected_files[provision.PROTECTED[k]])["password"]
                 for k in ("registration", "service", "operator")]
        found.append(self.private_files[provision.PRIVATE["runtime_password"]].decode().strip())
        found.append(json.loads(self.private_files[provision.PRIVATE["migration"]])["password"])
        return found

    def environment(self):
        return dict(line.split("=", 1) for line in self.settings_files[provision.SETTINGS_FILE].decode().splitlines())


class Fresh:
    pass


@pytest.fixture
def fresh(monkeypatch):
    raw = os.getenv("KALILLAC_REGISTRATION_TEST_POSTGRES_URL")
    if not raw:
        if os.getenv("KALILLAC_REGISTRATION_REQUIRE_POSTGRES") == "true":
            pytest.fail("required disposable PostgreSQL is missing")
        pytest.skip("explicit disposable PostgreSQL required")
    url = make_url(raw)
    if url.host not in ("127.0.0.1", "localhost") or url.database != "postgres":
        raise RuntimeError("NONLOCAL_POSTGRES_TEST_REFUSED")
    w = Fresh()
    tag = uuid.uuid4().hex[:12]
    w.names = {key: f"kalillac_test_pv_{key}_{tag}" for key in (*provision.ROLE_SETTINGS, "migrate")}
    w.database = "kalillac_test_pv_" + tag
    w.url, w.files, w.statements, w.engines = url, Files(), [], []
    for key, setting in provision.ROLE_SETTINGS.items():
        monkeypatch.setenv(setting, w.names[key])
    monkeypatch.delenv(FLAG, raising=False)

    def engine(target, **kwargs):
        made = create_engine(target, hide_parameters=True, pool_size=2, max_overflow=0, **kwargs)
        w.engines.append(made)

        @event.listens_for(made, "before_cursor_execute")
        def record(connection, cursor, statement, parameters, context, executemany):
            w.statements.append(statement + " " + repr(parameters))
        return made
    w.engine = engine
    w.admin = create_engine(url, isolation_level="AUTOCOMMIT", hide_parameters=True)
    try:
        yield w
    finally:
        for made in w.engines:
            made.dispose()
        with w.admin.connect() as c:
            c.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :d"
                           " AND pid <> pg_backend_pid()"), {"d": w.database})
            c.exec_driver_sql(f"DROP DATABASE IF EXISTS {w.database}")
            for key in ("migrate", "registration", "runtime", "service", "executor", "operator", "owner"):
                c.exec_driver_sql(f"DROP ROLE IF EXISTS {w.names[key]}")
        w.admin.dispose()


def run(w, command, *extra, capsys, **changes):
    options = {"--deployment-id": DEPLOYMENT, "--release-sha": RELEASE, "--database": w.database,
               "--db-host": w.url.host, "--db-port": str(w.url.port), "--migration-login": w.names["migrate"],
               "--admin-url-file": "/synthetic/admin-url", "--directory": DIRECTORY, "--service-group": "synthetic",
               **changes}
    argv = [command, *[part for pair in options.items() for part in pair], *extra]
    capsys.readouterr()
    code = provision.run(
        argv, environ=os.environ, admin_engine_factory=lambda path: w.engine(w.url),
        connect_admin_to=lambda admin, database: w.engine(w.url.set(database=database)),
        connect=lambda options, database, user, password: w.engine(
            w.url.set(database=database, username=user, password=password)),
        files_factory=lambda directory, group: w.files)
    return code, capsys.readouterr().out


def existing(w):
    with w.admin.connect() as c:
        roles = set(c.execute(text("SELECT rolname FROM pg_roles WHERE rolname = ANY(:n)"),
                              {"n": list(w.names.values())}).scalars())
        database = c.execute(text("SELECT count(*) FROM pg_database WHERE datname = :d"), {"d": w.database}).scalar_one()
    return roles, database


def verifiers(w):
    with w.admin.connect() as c:
        return dict(c.execute(text("SELECT rolname, rolpassword FROM pg_authid WHERE rolname = ANY(:n)"),
                              {"n": list(w.names.values())}).all())


def runtime_factory(w):
    password = w.files.private_files[provision.PRIVATE["runtime_password"]].decode().strip()
    return sessionmaker(bind=w.engine(w.url.set(database=w.database, username=w.names["runtime"], password=password)),
                        expire_on_commit=False)


def test_plan_and_a_dry_run_report_and_change_nothing(fresh, capsys):
    w = fresh
    for command in ("plan", "provision"):
        code, out = run(w, command, capsys=capsys)
        summary = json.loads(out)
        assert code == 0 and summary["applied"] is False
        assert set(summary["roles"].values()) == {"absent"} and summary["database"] == {w.database: "absent"}
    assert existing(w) == (set(), 0) and w.files.prepared == 0 and not w.files.protected_files


def test_a_provisioned_deployment_registers_an_account_and_takes_the_v1_transition(fresh, capsys, monkeypatch, tmp_path):
    w = fresh
    code, out = run(w, "provision", "--apply", capsys=capsys)
    assert code == 0, out
    summary = json.loads(out)
    assert summary["applied"] is True and summary["head"] == transition.HEAD
    assert sorted(w.files.protected_files) == sorted(provision.PROTECTED.values())
    assert sorted(w.files.private_files) == sorted(provision.PRIVATE.values()) and w.files.prepared == 1

    # No password reaches SQL, a server log or the tool's output: only SCRAM verifiers do.
    passwords = w.files.passwords()
    assert len(set(passwords)) == 5 and all(len(p) == 43 for p in passwords)
    assert not any(p in out for p in passwords)
    assert not any(p in statement for p in passwords for statement in w.statements)
    assert all(v is None or v.startswith("SCRAM-SHA-256$") for v in verifiers(w).values())

    # Exactly the roles the application's checks expect, each marked as this deployment's.
    with w.admin.connect() as c:
        rows = {r[0]: tuple(r[1:]) for r in c.execute(text(
            "SELECT rolname, rolcanlogin, rolinherit, rolsuper, rolcreatedb, rolcreaterole, rolreplication,"
            " rolbypassrls, shobj_description(oid, 'pg_authid') FROM pg_roles WHERE rolname = ANY(:n)"),
            {"n": list(w.names.values())})}
    for key, name in w.names.items():
        assert rows[name] == (key in provision.LOGINS, False, False, False, False, False, False,
                              provision.MARK + DEPLOYMENT)

    # The registration bootstrap, with every production check but the file-permission loader.
    import kalillac_accounts.registration_startup as startup
    monkeypatch.setattr(startup, "read_protected", w.files.read)
    env = {**w.files.environment(), "KALILLAC_ACCOUNT_REGISTRATION_V2_ENABLED": "true",
           "KALILLAC_SITE_ORIGIN": ORIGIN, "KALILLAC_ACCOUNT_REGISTRATION_URL": ORIGIN + "/account/create/confirm"}
    assert env["KALILLAC_DEPLOYMENT_ID"] == DEPLOYMENT and env["KALILLAC_DB_USER"] == w.names["runtime"]
    factory = runtime_factory(w)
    gateway = FakeEmail()
    authority = startup.bootstrap(factory, gateway, env)
    assert authority is not None and "REGISTRATION_V2_READY" in capsys.readouterr().out
    try:
        authority.issue_after_response("first@example.test")
        token = gateway.links[-1].split("#token=", 1)[1]
        assert gateway.links[-1].startswith(ORIGIN + "/account/create/confirm")
        authority.confirm_after_hash(authority.prevalidate(token), "synthetic-hash")
    finally:
        authority._dispose()
    with factory() as s:
        assert s.execute(text("SELECT count(*) FROM kalillac.users WHERE email_verified_at IS NOT NULL")).scalar_one() == 1
    factory.kw["bind"].dispose()                       # Ingress closed for the transition.

    # The operator transition, with the S and P credential files the tool wrote.
    record = tmp_path / "record.jsonl"
    environ = {**env, transition.RECORD_FILE_ENV: str(record), "KALILLAC_RELEASE_SHA": RELEASE, FLAG: "true",
               "KALILLAC_ACCOUNTS_ENABLED": "true"}
    code = transition.run(["activate", "--release-sha", RELEASE, "--apply"], environ=environ, read_file=w.files.read,
                          admin_engine_factory=lambda: w.engine(w.url.set(database=w.database)))
    assert code == 0, capsys.readouterr().out

    # The V1 startup gate qualifies, and S logs in through the application's own path.
    from kalillac_accounts.v1_authority import load_v1_authority
    factory = runtime_factory(w)
    assert load_v1_startup(lambda: factory, environ) is not None
    load_v1_authority(lambda: factory, environ, read_file=w.files.read).dispose()

    # A deployment in use is never provisioned again, and nothing of it is touched.
    before, files = verifiers(w), dict(w.files.protected_files)
    code, out = run(w, "provision", "--apply", capsys=capsys)
    assert code == 2 and "in use" in out
    assert verifiers(w) == before and w.files.protected_files == files


def test_a_provisioned_but_unused_deployment_is_not_provisioned_twice(fresh, capsys):
    w = fresh
    assert run(w, "provision", "--apply", capsys=capsys)[0] == 0
    before, files = verifiers(w), dict(w.files.protected_files)
    code, out = run(w, "provision", "--apply", capsys=capsys)
    assert code == 2 and "already recorded" in out
    assert verifiers(w) == before and w.files.protected_files == files and w.files.prepared == 1


def test_a_run_that_failed_before_the_activation_is_safe_to_repeat(fresh, capsys):
    w = fresh
    w.files.fail_on = provision.PROTECTED["service"]
    code, out = run(w, "provision", "--apply", capsys=capsys)
    assert code == 1 and out.startswith("failed:")
    with w.admin.connect() as c:
        assert c.execute(text("SELECT count(*) FROM pg_database WHERE datname = :d"), {"d": w.database}).scalar_one() == 1
    first = verifiers(w)
    w.files.fail_on = None
    code, out = run(w, "plan", capsys=capsys)
    assert code == 0 and set(json.loads(out)["roles"].values()) == {"ours"}
    code, out = run(w, "provision", "--apply", capsys=capsys)
    assert code == 0 and json.loads(out)["applied"] is True
    second = verifiers(w)
    assert all(first[w.names[k]] != second[w.names[k]] for k in provision.LOGINS)   # Fresh passwords, matching the files.
    with runtime_factory(w)() as s:
        assert s.execute(text("SELECT state FROM kalillac.account_registration_activation")).scalar_one() == "active_zero_legacy"


@pytest.mark.parametrize("foreign", ["role", "database", "database_owned_by_another_role"])
def test_a_role_or_database_it_did_not_create_is_refused_and_nothing_changes(fresh, capsys, foreign):
    w = fresh
    with w.admin.connect() as c:
        if foreign == "role":
            c.exec_driver_sql(f"CREATE ROLE {w.names['runtime']} LOGIN")
        else:
            c.exec_driver_sql(f"CREATE DATABASE {w.database}")
            if foreign == "database_owned_by_another_role":   # Even carrying the mark.
                c.exec_driver_sql(f"COMMENT ON DATABASE {w.database} IS '{provision.MARK + DEPLOYMENT}'")
    before = existing(w), verifiers(w)
    code, out = run(w, "provision", "--apply", capsys=capsys)
    assert code == 2 and "was not created for this deployment" in out
    assert (existing(w), verifiers(w)) == before and w.files.prepared == 0
    with w.admin.connect() as c:                                   # The foreign role keeps what it had.
        if foreign == "role":
            assert c.execute(text("SELECT shobj_description(oid, 'pg_authid') FROM pg_roles WHERE rolname = :n"),
                             {"n": w.names["runtime"]}).scalar_one() is None


def test_a_marked_role_with_other_attributes_is_refused(fresh, capsys):
    w = fresh
    with w.admin.connect() as c:
        c.exec_driver_sql(f"CREATE ROLE {w.names['owner']} NOLOGIN CREATEROLE")
        c.exec_driver_sql(f"COMMENT ON ROLE {w.names['owner']} IS '{provision.MARK + DEPLOYMENT}'")
    code, out = run(w, "provision", "--apply", capsys=capsys)
    assert code == 2 and "attributes" in out and existing(w) == ({w.names["owner"]}, 0)


@pytest.mark.parametrize("change, message", [
    ({"--release-sha": "abc"}, "--release-sha"),
    ({"--deployment-id": "bad id"}, "--deployment-id"),
    ({"--database": "postgres"}, "--database"),
    ({"--database": "Robert'); DROP"}, "--database"),
    ({"--migration-login": "x; DROP ROLE y"}, "--migration-login"),
    ({"--directory": "relative/path"}, "--directory"),
    ({"--directory": "/etc/../tmp"}, "--directory"),
    ({"--db-port": "0"}, "--db-host and --db-port"),
])
def test_invalid_options_are_refused_before_any_connection(fresh, capsys, change, message):
    w = fresh
    code, out = run(w, "provision", "--apply", capsys=capsys, **change)
    assert code == 2 and message in out and not w.engines and existing(w) == (set(), 0)


def test_a_role_setting_is_never_defaulted_and_names_are_distinct(fresh, capsys, monkeypatch):
    w = fresh
    monkeypatch.delenv("KALILLAC_MIGRATION_APP_ROLE")
    code, out = run(w, "provision", "--apply", capsys=capsys)
    assert code == 2 and "KALILLAC_MIGRATION_APP_ROLE" in out and not w.engines
    monkeypatch.setenv("KALILLAC_MIGRATION_APP_ROLE", w.names["owner"])
    code, out = run(w, "provision", "--apply", capsys=capsys)
    assert code == 2 and "different names" in out and existing(w) == (set(), 0)


def test_writing_the_files_needs_root(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("this refusal is for an unprivileged caller")
    with pytest.raises(provision.Refused):
        provision.RootFiles(str(tmp_path / "deployment"), os.getegid()).prepare()
    assert not (tmp_path / "deployment").exists()
