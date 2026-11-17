"""Provision a NEW, EMPTY Kalillac deployment (staging): its PostgreSQL roles and database, the schema at
this release's head, the registration activation of a database that holds no account, and the protected
files the application reads at startup. Operator command only.

Usage (as root on the deployment's host, before its service has ever started, from candidate/, with the six
role-name settings exported):
    python tools/provision_deployment.py plan      <options>
    python tools/provision_deployment.py provision <options> [--apply]

Options: --deployment-id ID  --release-sha <40 hex>  --database NAME  --db-host HOST  --db-port PORT
         --migration-login ROLE  --admin-url-file PATH  --directory DIR  --service-group GROUP

What `provision --apply` does, in this order:
  1. roles, in one transaction on the administrative connection: owner, executor, service and operator
     (NOLOGIN) and the migration, runtime and registration logins, each with no attribute and no
     inheritance; the migration login may assume the owner, the owner may SET to the executor. Passwords
     are 32 random bytes and reach the server only as SCRAM-SHA-256 verifiers;
  2. the database, owned by the owner role, with CONNECT taken from PUBLIC and given to the migration and
     runtime logins;
  3. the kalillac schema and every migration to this release's head, as the migration login;
  4. the files, under DIR: protected/ (root:GROUP 0440, read by the service through the strict loader):
     the registration keyring, activation receipt and its evidence, and the registration, V1 service and
     V1 operator credentials, each bound to this database instance and deployment; private/ (root 0600):
     the runtime login's password (for systemd LoadCredential as kalillac_db_password) and the migration
     login; and deployment.env, the non-secret settings that must match what was created;
  5. the registration activation (unactivated -> active_zero_legacy). The database itself counts: the
     0021 trigger refuses while any account lacks a verified email;
  6. verification, as startup will see it: the V1 classifier accepts the catalog as `prepared`, the
     registration privilege check passes in the legacy state, and each login authenticates.
It does NOT run the V1 transition (tools/v1_security_transition.py, the operator's next step), start a
service, or touch Nginx, DNS or any other database.

It only ever creates. Every role and the database are marked with this deployment's id when created; a
role or database that exists without that mark belongs to something else and is refused before anything
changes (on a cluster shared with another deployment, the role settings must therefore name roles of
their own). It is safe to run again only while the deployment is still unused: once the registration
activation is recorded, or any account or an active V1 latch exists, it refuses. Without --apply (and
for `plan`) it connects, reports what exists and what it would create, and changes nothing.

For a database created here, the evidence's writer_exclusion names the release that provisioned it and
a fresh lease id: no application has ever connected, which the tool checks (no session of any of its
logins) before recording the activation.

Writing or testing this tool is not authorization to run it against production. Exit codes: 0 done or
plan printed; 2 refused, nothing changed; 1 failed part-way. A failure before the activation was recorded
is safe to retry: run it again. One after it (the result did not verify) is not retried: the deployment
must not be started, and the cause is the operator's to find.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import uuid

CANDIDATE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(CANDIDATE), str(Path(__file__).resolve().parent)]

from v1_security_transition import HEAD, scram_verifier  # noqa: E402

MARK = "kalillac-provision:"
ROLE_SETTINGS = {
    "owner": "KALILLAC_MIGRATION_OWNER_ROLE", "runtime": "KALILLAC_MIGRATION_APP_ROLE",
    "registration": "KALILLAC_MIGRATION_REGISTRATION_ROLE", "service": "KALILLAC_V1_SECURITY_SERVICE_ROLE",
    "executor": "KALILLAC_V1_SECURITY_EXECUTOR_ROLE", "operator": "KALILLAC_V1_SECURITY_OPERATOR_ROLE"}
LOGINS = ("migrate", "runtime", "registration")
NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")
DEPLOYMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
SHA = re.compile(r"[0-9a-f]{40}")
HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}")
ATTRIBUTES = "NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
INVENTORY_QUERY = "SELECT count(*) FROM kalillac.users WHERE email_verified_at IS NULL"
PROTECTED = {"keyring": "registration-keyring.json", "activation": "registration-activation.json",
             "evidence": "registration-activation.json.evidence.json",
             "registration": "registration-credential.json", "service": "v1-service-credential.json",
             "operator": "v1-operator-credential.json"}
PRIVATE = {"runtime_password": "kalillac_db_password", "migration": "migration-login.json"}
SETTINGS_FILE = "deployment.env"
# The settings deployment.env carries: the ones that must match what was created. No template repeats them.
GENERATED_SETTINGS = (
    "KALILLAC_DEPLOYMENT_ID", "KALILLAC_DB_ENABLED", "KALILLAC_DB_HOST", "KALILLAC_DB_PORT", "KALILLAC_DB_NAME",
    "KALILLAC_DB_USER", *ROLE_SETTINGS.values(), "KALILLAC_ACCOUNT_REGISTRATION_KEYRING_FILE",
    "KALILLAC_ACCOUNT_REGISTRATION_ACTIVATION_FILE", "KALILLAC_ACCOUNT_REGISTRATION_DATABASE_CREDENTIAL_FILE",
    "KALILLAC_V1_SERVICE_CREDENTIALS_FILE", "KALILLAC_V1_OPERATOR_CREDENTIALS_FILE")


class Refused(Exception):
    pass


class Failed(Exception):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def new_password():
    """32 random bytes in canonical unpadded base64url: the form every credential file requires."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


def new_keyring():
    key = lambda: base64.b64encode(secrets.token_bytes(32)).decode()  # noqa: E731
    return {"format_version": 1, "active_encryption": "reg-enc-v1", "active_lookup": "reg-lookup-v1",
            "encryption_keys": [{"version": "reg-enc-v1", "key_b64": key()}],
            "lookup_keys": [{"version": "reg-lookup-v1", "key_b64": key()}]}


class Options:
    def __init__(self, args, environ):
        roles = {}
        for key, setting in ROLE_SETTINGS.items():
            value = environ.get(setting, "")
            if not NAME.fullmatch(value):
                raise Refused(f"{setting} must be set to this deployment's role name (no default is assumed)")
            roles[key] = value
        if not NAME.fullmatch(args.migration_login or ""):
            raise Refused("--migration-login must be a role name")
        roles["migrate"] = args.migration_login
        if len(set(roles.values())) != len(roles):
            raise Refused("the seven roles must have seven different names")
        if not DEPLOYMENT.fullmatch(args.deployment_id or ""):
            raise Refused("--deployment-id is not a deployment id")
        if not SHA.fullmatch(args.release_sha or ""):
            raise Refused("--release-sha must be the release's full 40-character commit")
        if not NAME.fullmatch(args.database or "") or args.database in ("postgres", "template0", "template1"):
            raise Refused("--database must be a new database's name")
        if not HOST.fullmatch(args.db_host or "") or not 0 < args.db_port < 65536:
            raise Refused("--db-host and --db-port must be the address the service will connect to")
        directory = args.directory or ""
        if not directory.startswith("/") or any(part in ("", ".", "..") for part in directory.split("/")[1:]):
            raise Refused("--directory must be an absolute, normalized path")
        self.roles, self.deployment, self.release = roles, args.deployment_id, args.release_sha
        self.database, self.host, self.port, self.directory = args.database, args.db_host, args.db_port, directory
        self.mark = MARK + self.deployment


# --- what exists ------------------------------------------------------------------------------------------

def _role_state(connection, options):
    """{key: "absent" | "ours"}; a role that exists without this deployment's mark is refused."""
    from sqlalchemy import text
    rows = {r[0]: r for r in connection.execute(text(
        "SELECT rolname, pg_catalog.shobj_description(oid, 'pg_authid'), rolcanlogin, rolinherit, rolsuper,"
        " rolcreatedb, rolcreaterole, rolreplication, rolbypassrls FROM pg_catalog.pg_roles"
        " WHERE rolname = ANY(:n)"), {"n": list(options.roles.values())})}
    state = {}
    for key, name in options.roles.items():
        row = rows.get(name)
        if row is None:
            state[key] = "absent"
        elif row[1] != options.mark:
            raise Refused(f"the role named by {key} already exists and was not created for this deployment")
        elif key in ("service", "operator") and row[2]:
            raise Refused("the V1 transition has given the service roles their logins: this deployment is in use")
        elif tuple(row[2:]) != (key in LOGINS, False, False, False, False, False, False):
            raise Refused(f"the {key} role exists with attributes this tool did not give it")
        else:
            state[key] = "ours"
    return state


def _database_state(connection, options):
    from sqlalchemy import text
    row = connection.execute(text(
        "SELECT pg_catalog.shobj_description(d.oid, 'pg_database'), pg_catalog.pg_get_userbyid(d.datdba)"
        " FROM pg_catalog.pg_database d WHERE d.datname = :n"), {"n": options.database}).one_or_none()
    if row is None:
        return "absent"
    if row[0] != options.mark or row[1] != options.roles["owner"]:
        raise Refused("the database already exists and was not created for this deployment")
    return "ours"


def _unused(connection, options):
    """Refuse unless the existing database is still unused: no account, no recorded activation, no
    active V1 latch, and no open session of any of the deployment's logins."""
    from sqlalchemy import text
    exists = lambda table: connection.execute(  # noqa: E731
        text("SELECT pg_catalog.to_regclass(:t) IS NOT NULL"), {"t": "kalillac." + table}).scalar_one()
    if exists("users") and connection.execute(text("SELECT EXISTS (SELECT 1 FROM kalillac.users)")).scalar_one():
        raise Refused("the database holds accounts: it is not a new deployment")
    if exists("account_registration_activation") and connection.execute(text(
            "SELECT state FROM kalillac.account_registration_activation")).scalar_one() != "unactivated":
        raise Refused("the registration activation is already recorded: this deployment is provisioned")
    if exists("account_security_activation") and connection.execute(text(
            "SELECT state FROM kalillac.account_security_activation")).scalar_one() != "prepared":
        raise Refused("the V1 latch is not `prepared`: this deployment is in use")
    logins = [options.roles[k] for k in ("runtime", "registration", "service", "operator")]
    if connection.execute(text("SELECT count(*) FROM pg_catalog.pg_stat_activity WHERE datname = current_database()"
                               " AND usename = ANY(:l)"), {"l": logins}).scalar_one():
        raise Refused("an application login has an open session: a service is already running against it")


def survey(admin, connect_admin_to, options):
    with admin.connect() as connection:
        roles = _role_state(connection, options)
        database = _database_state(connection, options)
    if database == "ours":
        engine = connect_admin_to(options.database)
        try:
            with engine.connect() as connection:
                _unused(connection, options)
        finally:
            engine.dispose()
    return roles, database


# --- the steps ----------------------------------------------------------------------------------------------

def _create_roles(admin, options, state, passwords):
    r = options.roles
    with admin.begin() as connection:
        for key, name in r.items():
            if state[key] == "absent":
                connection.exec_driver_sql(f"CREATE ROLE {name} {'LOGIN' if key in LOGINS else 'NOLOGIN'} {ATTRIBUTES}")
                # The mark is this tool's fixed prefix and a validated deployment id: it cannot end the literal.
                connection.exec_driver_sql(f"COMMENT ON ROLE {name} IS '{options.mark}'")
        for key in LOGINS:
            # A verifier is base64 with '$' and ':' only. No password appears in SQL or a server log.
            connection.exec_driver_sql(f"ALTER ROLE {r[key]} PASSWORD '{scram_verifier(passwords[key])}'")
        connection.exec_driver_sql(f"GRANT {r['owner']} TO {r['migrate']}")
        # The prerequisite of the V1 migrations: the owner may SET to the executor, and inherits nothing.
        connection.exec_driver_sql(f"GRANT {r['executor']} TO {r['owner']} WITH INHERIT FALSE, SET TRUE")


def _create_database(admin, options, state):
    r = options.roles
    with admin.execution_options(isolation_level="AUTOCOMMIT").connect() as connection:
        if state == "absent":
            connection.exec_driver_sql(f"CREATE DATABASE {options.database} OWNER {r['owner']}")
            connection.exec_driver_sql(f"COMMENT ON DATABASE {options.database} IS '{options.mark}'")
        connection.exec_driver_sql(f"REVOKE CONNECT ON DATABASE {options.database} FROM PUBLIC")
        connection.exec_driver_sql(f"GRANT CONNECT ON DATABASE {options.database} TO {r['migrate']}, {r['runtime']}")


def _migrate(migration, options):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text
    r = options.roles
    with migration.begin() as connection:
        connection.exec_driver_sql("SET LOCAL ROLE " + r["owner"])
        connection.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS kalillac AUTHORIZATION " + r["owner"])
        connection.exec_driver_sql("GRANT USAGE ON SCHEMA kalillac TO " + r["runtime"])
    config = Config(str(CANDIDATE / "alembic.ini"))
    config.set_main_option("script_location", str(CANDIDATE / "migrations"))
    with migration.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    with migration.connect() as connection:
        connection.exec_driver_sql("SET LOCAL ROLE " + r["owner"])
        version = connection.execute(text("SELECT version_num FROM kalillac.alembic_version")).scalar_one()
    if version != HEAD:
        raise Failed(f"the migrations ended at {version}, not this release's head {HEAD}")


def _documents(migration, options, passwords, now):
    """Every file's content, from the database as it is now. Nothing is written here."""
    from sqlalchemy import text
    r = options.roles
    with migration.connect() as connection:
        connection.exec_driver_sql("SET LOCAL ROLE " + r["owner"])
        instance = str(connection.execute(text(
            "SELECT database_instance_id FROM kalillac.account_registration_activation")).scalar_one())
        unverified = connection.execute(text(INVENTORY_QUERY)).scalar_one()
        total = connection.execute(text("SELECT count(*) FROM kalillac.users")).scalar_one()
    if unverified or total:
        raise Refused("the database holds accounts: it is not a new deployment")
    stamp = now.isoformat()
    activation = {"format_version": 1, "policy_version": 1, "database_instance_id": instance,
                  "activation_id": str(uuid.uuid4()), "cutover_id": str(uuid.uuid4()),
                  "deployment_id": options.deployment, "schema_revision": "0011_registration_intents",
                  "evidence_sha256": "", "inventory_at": stamp, "activated_at": stamp, "legacy_unverified_count": 0}
    evidence = {"query_text": INVENTORY_QUERY, "query_sha256": hashlib.sha256(INVENTORY_QUERY.encode()).hexdigest(),
                "aggregates": {"total_users": total, "legacy_unverified_count": unverified},
                "database_identity": {"database_instance_id": instance, "database_name": options.database,
                                      "endpoint_identity": f"{options.host}:{options.port}"},
                "schema_revision": activation["schema_revision"], "inventory_at": stamp,
                "cutover_id": activation["cutover_id"], "deployment_id": options.deployment,
                # No application ever connected to this database: the release that provisioned it.
                "writer_exclusion": {"stopped_release": options.release, "lease_id": str(uuid.uuid4()),
                                     "cutover_id": activation["cutover_id"]}}
    activation["evidence_sha256"] = hashlib.sha256(canonical(evidence)).hexdigest()
    credential = lambda key: canonical({"format_version": 1, "role": r[key], "database_instance_id": instance,  # noqa: E731
                                        "deployment_id": options.deployment, "password": passwords[key]})
    base = options.directory
    settings = {
        "KALILLAC_DEPLOYMENT_ID": options.deployment, "KALILLAC_DB_ENABLED": "true",
        "KALILLAC_DB_HOST": options.host, "KALILLAC_DB_PORT": str(options.port),
        "KALILLAC_DB_NAME": options.database, "KALILLAC_DB_USER": r["runtime"],
        **{setting: r[key] for key, setting in ROLE_SETTINGS.items()},
        "KALILLAC_ACCOUNT_REGISTRATION_KEYRING_FILE": f"{base}/protected/{PROTECTED['keyring']}",
        "KALILLAC_ACCOUNT_REGISTRATION_ACTIVATION_FILE": f"{base}/protected/{PROTECTED['activation']}",
        "KALILLAC_ACCOUNT_REGISTRATION_DATABASE_CREDENTIAL_FILE": f"{base}/protected/{PROTECTED['registration']}",
        "KALILLAC_V1_SERVICE_CREDENTIALS_FILE": f"{base}/protected/{PROTECTED['service']}",
        "KALILLAC_V1_OPERATOR_CREDENTIALS_FILE": f"{base}/protected/{PROTECTED['operator']}"}
    assert tuple(settings) == GENERATED_SETTINGS
    return {
        "activation": activation,
        "protected": {PROTECTED["keyring"]: canonical(new_keyring()), PROTECTED["activation"]: canonical(activation),
                      PROTECTED["evidence"]: canonical(evidence), PROTECTED["registration"]: credential("registration"),
                      PROTECTED["service"]: credential("service"), PROTECTED["operator"]: credential("operator")},
        "private": {PRIVATE["runtime_password"]: passwords["runtime"].encode() + b"\n",
                    PRIVATE["migration"]: canonical({"role": r["migrate"], "password": passwords["migrate"]})},
        "settings": "".join(f"{k}={v}\n" for k, v in settings.items()).encode()}


def _activate(migration, options, activation, now):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    try:
        with migration.begin() as connection:
            connection.exec_driver_sql("SET LOCAL ROLE " + options.roles["owner"])
            done = connection.execute(text(
                "UPDATE kalillac.account_registration_activation SET state = 'active_zero_legacy',"
                " activation_id = :activation_id, cutover_id = :cutover_id, deployment_id = :deployment_id,"
                " evidence_sha256 = :evidence_sha256, inventory_at = :at, activated_at = :at,"
                " legacy_unverified_count = 0 WHERE state = 'unactivated'"),
                {**{k: activation[k] for k in ("activation_id", "cutover_id", "deployment_id", "evidence_sha256")},
                 "at": now}).rowcount
    except DBAPIError:
        raise Failed("the database refused the registration activation") from None
    if done != 1:
        raise Failed("the registration activation was not in the unactivated state")


def _verify(migration, connect, options, passwords):
    from sqlalchemy import text
    from sqlalchemy.orm import Session
    from kalillac_accounts.registration_startup import effective_privileges
    from kalillac_accounts.v1_schema import PREPARED, classify
    r = options.roles
    try:
        with migration.connect() as connection:
            connection.exec_driver_sql("SET LOCAL ROLE " + r["owner"])
            session = Session(bind=connection)
            state = classify(session)
            grants = effective_privileges(session, r["registration"], r["runtime"], r["owner"])
    except Exception as error:
        raise Failed(f"the result did not verify ({type(error).__name__})") from None
    if state != PREPARED or grants != "legacy":
        raise Failed("the result did not verify (not the prepared, legacy-grant state)")
    for key in ("runtime", "registration"):
        engine = connect(options.database, r[key], passwords[key])
        try:
            with engine.connect() as connection:
                row = tuple(connection.execute(text("SELECT session_user, system_user")).one())
        except Exception:
            raise Failed(f"the {key} login could not authenticate") from None
        finally:
            engine.dispose()
        if row != (r[key], "scram-sha-256:" + r[key]):
            raise Failed(f"the {key} login did not authenticate with its own SCRAM password")


def provision(options, *, admin, connect_admin_to, connect, files, apply, clock=None):
    """Returns the summary. Raises Refused (nothing changed) or Failed (part-way: see the module text)."""
    roles, database = survey(admin, connect_admin_to, options)
    summary = {"deployment_id": options.deployment, "database": {options.database: database},
               "roles": {key: roles[key] for key in sorted(roles)}, "applied": False}
    if not apply:
        return summary
    files.prepare()
    passwords = {key: new_password() for key in (*LOGINS, "service", "operator")}
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    _create_roles(admin, options, roles, passwords)
    _create_database(admin, options, database)
    migration = connect(options.database, options.roles["migrate"], passwords["migrate"])
    try:
        _migrate(migration, options)
        engine = connect_admin_to(options.database)
        try:
            with engine.connect() as connection:
                _unused(connection, options)        # Again, immediately before the files and the activation.
        finally:
            engine.dispose()
        documents = _documents(migration, options, passwords, now)
        for name, raw in documents["protected"].items():
            files.protected(name, raw)
        for name, raw in documents["private"].items():
            files.private(name, raw)
        files.settings(SETTINGS_FILE, documents["settings"])
        _activate(migration, options, documents["activation"], now)   # The last change, after every file.
        _verify(migration, connect, options, passwords)
    finally:
        migration.dispose()
    summary.update(applied=True, head=HEAD, directory=options.directory,
                   protected=sorted(PROTECTED.values()), private=sorted(PRIVATE.values()), settings=SETTINGS_FILE,
                   next="run tools/v1_security_transition.py activate as the service group's operator")
    return summary


# --- the host's files (root only) ---------------------------------------------------------------------------

class RootFiles:
    """DIR (root:root 0755), DIR/protected (root:GROUP 0750, files 0440) and DIR/private (root 0700, files
    0600). Each file is written whole to a new name, synced and renamed into place."""

    def __init__(self, directory, gid):
        self.base, self.gid = Path(directory), gid

    def _directory(self, path, gid, mode):
        try:
            os.mkdir(path, mode)
        except FileExistsError:
            status = os.lstat(path)                 # Never follow a link someone left in its place.
            if not stat.S_ISDIR(status.st_mode) or status.st_uid != 0:
                raise Refused(f"{path} exists and is not a root-owned directory") from None
        os.chown(path, 0, gid, follow_symlinks=False)
        os.chmod(path, mode)

    def prepare(self):
        if os.geteuid() != 0:
            raise Refused("--apply writes root-owned files: run it as root")
        for ancestor in list(self.base.parents)[::-1]:
            status = os.stat(ancestor)
            # The application's loader refuses a path any ancestor of which someone else could replace.
            if status.st_uid != 0 or status.st_gid not in (0, self.gid) or stat.S_IMODE(status.st_mode) & 0o7022:
                raise Refused(f"{ancestor} must be root-owned and not group- or world-writable")
        self._directory(self.base, 0, 0o755)
        self._directory(self.base / "protected", self.gid, 0o750)
        self._directory(self.base / "private", 0, 0o700)

    def _write(self, directory, name, raw, gid, mode):
        temporary = directory / (name + ".new")
        if temporary.exists():
            temporary.unlink()
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        try:
            os.fchown(fd, 0, gid)
            os.fchmod(fd, mode)
            os.write(fd, raw)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, directory / name)
        handle = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(handle)
        finally:
            os.close(handle)

    def protected(self, name, raw):
        self._write(self.base / "protected", name, raw, self.gid, 0o440)

    def private(self, name, raw):
        self._write(self.base / "private", name, raw, 0, 0o600)

    def settings(self, name, raw):
        self._write(self.base, name, raw, 0, 0o644)


def read_root_only(path, ceiling=4096):
    """The administrative URL: a regular root-owned file nobody else can read."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        status = os.fstat(fd)
        if (not stat.S_ISREG(status.st_mode) or status.st_uid != 0 or stat.S_IMODE(status.st_mode) & 0o077
                or status.st_nlink != 1 or not 0 < status.st_size <= ceiling):
            raise OSError("not a root-only file")
        return os.read(fd, ceiling + 1)
    finally:
        os.close(fd)


# --- command line ---------------------------------------------------------------------------------------------

def run(argv, *, environ, admin_engine_factory, connect_admin_to, connect, files_factory):
    parser = argparse.ArgumentParser(prog="provision_deployment.py", allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "provision"):
        sub = commands.add_parser(name)
        for option in ("--deployment-id", "--release-sha", "--database", "--db-host", "--migration-login",
                       "--admin-url-file", "--directory", "--service-group"):
            sub.add_argument(option, required=True)
        sub.add_argument("--db-port", type=int, required=True)
        if name == "provision":
            sub.add_argument("--apply", action="store_true")
    try:
        args = parser.parse_args(argv)
    except SystemExit as stop:
        return 0 if stop.code == 0 else 2
    apply = bool(getattr(args, "apply", False))
    admin = None
    try:
        options = Options(args, environ)
        try:
            files = files_factory(options.directory, args.service_group)
            admin = admin_engine_factory(args.admin_url_file)
        except Refused:
            raise
        except Exception:
            raise Refused("the service group or the administrative connection file is missing or not"
                          " readable as required") from None
        summary = provision(options, admin=admin, connect_admin_to=lambda database: connect_admin_to(admin, database),
                            connect=lambda database, user, password: connect(options, database, user, password),
                            files=files, apply=apply)
    except Refused as refused:
        print(f"refused: {refused}")
        return 2
    except Failed as failed:
        print(f"failed: {failed}")
        return 1
    except Exception as error:  # A class name only: never a URL, a password or a row.
        print(f"failed: unexpected_error {type(error).__name__}")
        return 1
    finally:
        if admin is not None:
            admin.dispose()
    print(json.dumps(summary, sort_keys=True))
    return 0


def main(argv=None):
    import grp
    from sqlalchemy import create_engine
    from sqlalchemy.engine import URL, make_url

    def admin_engine(path):
        url = make_url(read_root_only(path).decode("utf-8").strip())
        return create_engine(url, hide_parameters=True, pool_size=1, max_overflow=0)

    def connect_admin_to(admin, database):
        return create_engine(admin.url.set(database=database), hide_parameters=True, pool_size=1, max_overflow=0)

    def connect(options, database, user, password):
        return create_engine(URL.create("postgresql+psycopg", username=user, password=password, host=options.host,
                                        port=options.port, database=database),
                             hide_parameters=True, pool_size=1, max_overflow=0)

    def files(directory, group):
        return RootFiles(directory, grp.getgrnam(group).gr_gid)
    try:
        return run(sys.argv[1:] if argv is None else argv, environ=os.environ, admin_engine_factory=admin_engine,
                   connect_admin_to=connect_admin_to, connect=connect, files_factory=files)
    except Exception as error:  # A class name only: never a URL, a password or a row.
        print(f"unexpected_error {type(error).__name__}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
