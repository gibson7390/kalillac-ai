"""Offline local-upgrade primitives. No installation is accessed at import time."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile

from sqlalchemy import URL, create_engine, make_url, text

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO / "candidate"), str(REPO / "candidate/tools")]
OLD = "0025_v1_row_lock_anchor"
NEW = "0026_account_provider_attempts"
RELEASE = "d8f45f71a8d6d4ec80512e7c770b9625293e34bb"
DEPLOYMENT = "kalillac-local-staging"


class Refused(RuntimeError):
    pass


def require(condition, reason):
    if not condition:
        raise Refused(reason)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def env_file(path):
    values = {}
    for line in Path(path).read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            require(key not in values, "duplicate environment setting")
            values[key] = value
    return values


def file_hash(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode), "non-regular file")
        h = hashlib.sha256()
        with os.fdopen(fd, "rb", closefd=False) as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                h.update(block)
        after = os.fstat(fd)
        require((before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns), "file changed while hashing")
        return h.hexdigest()
    finally:
        os.close(fd)


def tree_manifest(root):
    """Content, ownership and modes, including dotfiles. Refuse links/sockets/external tablespaces."""
    root = Path(root)
    require(root.is_dir() and not root.is_symlink(), "missing or linked directory")
    result = {}
    for folder, dirs, files in os.walk(root, followlinks=False):
        for path in [Path(folder), *(Path(folder) / n for n in sorted(dirs + files))]:
            info = path.lstat()
            key = str(path.relative_to(root))
            require(stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode), "unsupported filesystem entry")
            item = {"mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid, "gid": info.st_gid}
            if stat.S_ISREG(info.st_mode):
                item.update(size=info.st_size, sha256=file_hash(path))
            result[key] = item
    return result


def copy_tree(source, destination):
    require(not Path(destination).exists(), "copy destination already exists")
    tree_manifest(source)
    subprocess.run(["cp", "-a", "--", str(source), str(destination)], check=True, capture_output=True)
    require(tree_manifest(source) == tree_manifest(destination), "copied filesystem differs")


def save_json(path, value):
    """Exclusive, durable creation; receipts/backups are never overwritten."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def quote(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def database_snapshot(engine, *, project_new=False):
    """One repeatable, read-only snapshot. Hash sorted complete rows, preserving duplicates.

    Only the exact Alembic version and 0026's new counter may differ. Existing columns,
    sequence positions and every other application's row remain part of the comparison.
    """
    tables, sequences = {}, {}
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as c, c.begin():
        c.exec_driver_sql("SET TRANSACTION READ ONLY")
        c.exec_driver_sql("SET LOCAL timezone = 'UTC'")
        c.exec_driver_sql("SET LOCAL bytea_output = 'hex'")
        versions = list(c.scalars(text("SELECT version_num FROM kalillac.alembic_version")))
        require(versions == [NEW if project_new else OLD], "unexpected database migration version")
        names = c.execute(text("""SELECT n.nspname,c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE c.relkind IN ('r','p') AND n.nspname <> 'information_schema' AND n.nspname !~ '^pg_'
            ORDER BY 1,2""")).all()
        for schema, table in names:
            if (schema, table) == ("kalillac", "alembic_version"):
                continue
            fq = quote(schema) + "." + quote(table)
            columns = [tuple(r) for r in c.execute(text("""SELECT a.attname,format_type(a.atttypid,a.atttypmod),
                a.attnotnull,pg_get_expr(d.adbin,d.adrelid) FROM pg_attribute a
                LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
                WHERE a.attrelid=to_regclass(:fq) AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum"""),
                {"fq": fq})]
            expression = "to_jsonb(t)"
            if project_new and (schema, table) == ("kalillac", "account_usage_daily"):
                require(columns[-1] == ("provider_attempts", "bigint", True, "0"), "unexpected counter definition")
                require(c.scalar(text(f"SELECT count(*) FROM {fq} WHERE provider_attempts IS DISTINCT FROM 0")) == 0,
                        "new provider attempts were not initialized to zero")
                expression += " - 'provider_attempts'"
                columns = columns[:-1]
            h, count = hashlib.sha256(), 0
            for row in c.execution_options(stream_results=True).execute(
                    text(f'SELECT ({expression})::text FROM {fq} t ORDER BY ({expression})::text COLLATE "C"')):
                h.update(row[0].encode() + b"\n")
                count += 1
            tables[f"{schema}.{table}"] = {"columns": columns, "rows": count, "sha256": h.hexdigest()}
        for schema, name in c.execute(text("""SELECT schemaname,sequencename FROM pg_sequences
                WHERE schemaname <> 'information_schema' AND schemaname !~ '^pg_' ORDER BY 1,2""")):
            sequences[f"{schema}.{name}"] = tuple(c.execute(
                text(f"SELECT last_value,is_called FROM {quote(schema)}.{quote(name)}")).one())
        activation = dict(c.execute(text("""SELECT state,authority_version,deployment_id,release_sha,
                    activation_id::text FROM kalillac.account_security_activation""")).mappings().one())
    return {"tables": tables, "sequences": sequences, "activation": activation}


def same_contents(before, after):
    require(digest(before) == digest(after), "existing database contents, schema or activation changed")


def database_identity(engine, pg, database, activation):
    with engine.connect() as c:
        row = c.execute(text("SELECT current_setting('data_directory'),current_database(),"
                             " current_setting('server_version_num')::integer")).one()
        require(Path(row[0]).resolve() == Path(pg).resolve() and row[1] == database and 160000 <= row[2] < 170000,
                "database endpoint is not the expected PostgreSQL 16 destination")
        latch = c.execute(text("SELECT state,deployment_id,release_sha FROM kalillac.account_security_activation")).one()
        require(tuple(latch) == ("active", DEPLOYMENT, activation), "activation or deployment binding mismatch")


def migrate(engine, deployment, target=NEW, *, after_sql=None):
    """0025->0026 in one transaction, with the deployment's own migration login/owner."""
    from alembic import command
    from alembic.config import Config
    keys = ("KALILLAC_MIGRATION_OWNER_ROLE", "KALILLAC_MIGRATION_APP_ROLE",
            "KALILLAC_MIGRATION_REGISTRATION_ROLE")
    original = {k: os.environ.get(k) for k in keys}
    try:
        for key in keys:
            os.environ[key] = deployment[key]
        config = Config(str(REPO / "candidate/alembic.ini"))
        config.set_main_option("script_location", str(REPO / "candidate/migrations"))
        with engine.begin() as c:
            config.attributes["connection"] = c
            (command.upgrade if target == NEW else command.downgrade)(config, target)
            if after_sql:
                after_sql(c)  # Test injection inside the same transaction, never a CLI argument.
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


SOCKET_PATH_MAX = 107                   # sun_path holds 108 bytes on Linux, including the terminating NUL.


class Cluster:
    """Explicit status and stop confirmation. Never removes a data directory."""
    def __init__(self, pg, bindir, uid, gid):
        self.pg, self.bin, self.uid, self.gid = Path(pg), str(bindir), uid, gid

    def command(self, *args, tool="pg_ctl"):
        drop = {"user": self.uid, "group": self.gid, "extra_groups": []} if os.geteuid() == 0 else {}
        return subprocess.run([f"{self.bin}/{tool}", *args], env={**os.environ, "LC_ALL": "C"},
                              capture_output=True, text=True, **drop)

    def status(self):
        result = self.command("-D", str(self.pg), "status")
        require(result.returncode in (0, 3), "PostgreSQL status unknown; data preserved")
        return result.returncode

    def stopped(self):
        require(self.status() == 3, "PostgreSQL is still running; data preserved")
        result = self.command("-D", str(self.pg), tool="pg_controldata")
        states = [v.strip() for k, _, v in (line.partition(":") for line in result.stdout.splitlines())
                  if k.strip() == "Database cluster state"]
        require(result.returncode == 0 and states == ["shut down"], "PostgreSQL is not cleanly shut down")

    def stop(self):
        if self.status() == 0:
            require(self.command("-D", str(self.pg), "-w", "-m", "fast", "stop").returncode == 0,
                    "PostgreSQL shutdown failed; data preserved")
        self.stopped()

    def start(self, socket, port, *, sandbox=None):
        self.stopped()
        # All paths passed in -o must be shell-word safe (pg_ctl interprets this option).
        for p in (self.pg, socket, sandbox):
            require(p is None or all(ch.isalnum() or ch in "/._-" for ch in str(p)), "unsafe PostgreSQL path")
        require(len(f"{socket}/.s.PGSQL.{int(port)}".encode()) <= SOCKET_PATH_MAX,
                "the PostgreSQL socket path is too long for this system (107 bytes); use a shorter folder")
        options = f"-p {int(port)} -k {socket} -c listen_addresses=127.0.0.1"
        if sandbox:
            options += f" -c config_file={sandbox}"
            # postgresql.auto.conf is read even with a separate config file. Command-line
            # overrides keep its archive, replication, libraries and external paths inert.
            options += (f" -c data_directory={self.pg} -c hba_file={Path(sandbox).with_suffix('.hba')}"
                        f" -c ident_file={Path(sandbox).with_suffix('.ident')}"
                        " -c archive_mode=off -c archive_command= -c restore_command= -c primary_conninfo="
                        " -c shared_preload_libraries= -c session_preload_libraries= -c local_preload_libraries="
                        " -c external_pid_file= -c ssl=off -c max_logical_replication_workers=0")
        result = self.command("-D", str(self.pg), "-l", str(self.pg / "upgrade-postgres.log"),
                              "-w", "-o", options, "start")
        require(result.returncode == 0 and self.status() == 0, "PostgreSQL startup failed; data preserved")


def settings_identity(payload, activation):
    settings = env_file(payload / "settings.env")
    deployment = env_file(payload / "deployment/deployment.env")
    local = json.loads((payload / "local.json").read_text())
    require(deployment.get("KALILLAC_DEPLOYMENT_ID") == DEPLOYMENT and local.get("deployment") == DEPLOYMENT,
            "wrong deployment")
    require(settings.get("KALILLAC_RELEASE_SHA") == activation and local.get("release_sha") == activation,
            "wrong activation release")
    require(settings.get("KALILLAC_SITE_ORIGIN") == "http://localhost:8010" and
            settings.get("KALILLAC_STRIPE_MODE") == "test" and local.get("stripe") == "simulated",
            "only the simulated local installation is supported")
    for name in ("provider-keys.env", "stripe.env"):
        path = payload / "secrets" / name
        require(not path.exists() or not any(env_file(path).values()), "real provider credentials are configured")
    require((payload / "pg/PG_VERSION").read_text().strip() == "16", "PostgreSQL major version must be 16")
    require(not any((payload / "pg/pg_tblspc").iterdir()), "external tablespaces are unsupported")
    require(not any((payload / "pg" / n).exists() for n in ("standby.signal", "recovery.signal")),
            "recovery/replica clusters are unsupported")
    return settings, deployment


def validate_backup(backup, expected_hash):
    backup = Path(backup)
    require(not backup.is_symlink() and backup.is_dir(), "backup path invalid")
    require(file_hash(backup / "manifest.json") == expected_hash, "backup manifest checksum mismatch")
    manifest = json.loads((backup / "manifest.json").read_text())
    require(manifest["format"] == 1, "unknown backup format")
    require(tree_manifest(backup / "payload") == manifest["files"], "backup contents or permissions changed")
    settings_identity(backup / "payload", manifest["activation"])
    return manifest


@contextmanager
def copied_cluster(backup, expected_hash, bindir, *, port):
    """A backup is copied into a new scratch root, never started in place.

    Cleanup only follows a confirmed stop and successful body. Every unexpected failure
    keeps the entire scratch directory, even if the database was stopped.
    """
    manifest = validate_backup(backup, expected_hash)
    pg_entry = manifest["files"].get("pg") or {}
    work = Path(tempfile.mkdtemp(prefix="kalillac-upgrade-copy-"))
    os.chmod(work, 0o711)
    identity = (work.stat().st_dev, work.stat().st_ino)
    payload = work / "payload"
    # The copy's PostgreSQL runs as the service identity: it must be able to reach the scratch folder. A TMPDIR
    # root alone can enter (0700) is refused here, before copying anything, with the reason (UPGRADE.md: 0711).
    if os.geteuid() == 0 and pg_entry.get("uid"):
        probe = work / ".traversal-probe"             # Reading it needs exactly traversal of every folder above.
        probe.write_text("probe")
        os.chmod(probe, 0o644)
        try:
            seen = as_service(pg_entry["uid"], pg_entry["gid"], read=[probe])
        except Refused:
            seen = {"read": {}}
        reachable = seen["read"].get(str(probe)) == file_hash(probe)
        probe.unlink()
        if not reachable:
            shutil.rmtree(work)
            raise Refused("the service identity cannot reach the scratch folder: TMPDIR must be traversable "
                          "(for example mode 0711)")
    # The socket in a short folder of its own: a deep TMPDIR would exceed the 107-byte socket path limit.
    sock = Path(tempfile.mkdtemp(prefix="kus-", dir="/tmp"))
    cluster, success = None, False
    try:
        copy_tree(Path(backup) / "payload", payload)
        require(tree_manifest(payload) == manifest["files"], "clone differs from verified backup")
        pg_owner = payload.joinpath("pg").stat()
        cluster = Cluster(payload / "pg", bindir, pg_owner.st_uid, pg_owner.st_gid)
        os.chmod(sock, 0o700)
        if os.geteuid() == 0:
            os.chown(sock, pg_owner.st_uid, pg_owner.st_gid)
        # Do not load the source's configuration, archive commands, replication or preload libraries.
        config, hba, ident = (work / n for n in ("sandbox.conf", "sandbox.hba", "sandbox.ident"))
        hba.write_text("local all all trust\nhost all all 127.0.0.1/32 scram-sha-256\n")
        ident.write_text("")
        config.write_text(f"data_directory='{payload / 'pg'}'\nhba_file='{hba}'\nident_file='{ident}'\n")
        cluster.start(sock, port, sandbox=config)
        yield payload, manifest, cluster
        success = True
    finally:
        if cluster is not None:
            try:
                cluster.stop()  # Any failure here preserves work, irrespective of body success.
            except BaseException:
                print(f"shutdown unconfirmed; rehearsal state preserved: {work}", file=sys.stderr)
                raise
        if success:
            require((work.stat().st_dev, work.stat().st_ino) == identity, "scratch root identity changed")
            shutil.rmtree(work)
            shutil.rmtree(sock, ignore_errors=True)       # Only after a confirmed stop: the socket is gone.
        else:
            print(f"rehearsal state preserved: {work} (socket folder {sock})", file=sys.stderr)


# --- access as the application's service identity (Codex U3, and the runtime credentials) -------------------

# Run in a child process with the service's uid and primary gid and NO supplementary groups: exactly how local
# staging launches the application (local_staging._spawn). Prints only hashes of files the manifest already
# holds, and error class names; never a file's contents.
_SERVICE_CHECK = r"""
import hashlib, json, os, sys
plan = json.load(sys.stdin)
out = {"read": {}, "list": {}, "write": {}}
for path in plan["read"]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as stream:
            for block in iter(lambda: stream.read(1 << 20), b""):
                h.update(block)
        out["read"][path] = h.hexdigest()
    except OSError as error:
        out["read"][path] = "error:" + type(error).__name__
for path in plan["list"]:
    try:
        os.listdir(path)
        out["list"][path] = "ok"
    except OSError as error:
        out["list"][path] = "error:" + type(error).__name__
for path in plan["write"]:
    out["write"][path] = "ok" if os.access(path, os.W_OK | os.X_OK) else "error:denied"
json.dump(out, sys.stdout)
"""


def as_service(uid, gid, *, read=(), listing=(), write=()):
    """What the service identity can actually do: {"read": {path: sha256|error}, "list": ..., "write": ...}.
    Refused if it cannot be established (indeterminate access is never treated as access)."""
    require(type(uid) is int and type(gid) is int and uid > 0 and gid > 0, "invalid service identity")
    require(os.geteuid() == 0, "the service identity's access can only be checked as root")
    plan = json.dumps({"read": [str(p) for p in read], "list": [str(p) for p in listing],
                       "write": [str(p) for p in write]})
    for interpreter in dict.fromkeys((sys.executable, "/usr/bin/python3")):
        try:
            result = subprocess.run([interpreter, "-I", "-S", "-B", "-c", _SERVICE_CHECK], input=plan,
                                    capture_output=True, text=True, user=uid, group=gid, extra_groups=[],
                                    env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}, cwd="/", timeout=600)
        except OSError:
            continue
        if result.returncode == 0:
            try:
                out = json.loads(result.stdout)
            except ValueError:
                continue
            if set(out) == {"read", "list", "write"}:
                return out
    raise Refused("the application service identity's access could not be established")


def _inside(path, prefix, payload):
    """An installed absolute path, as it is inside this payload (the backup or the root itself). The path must be
    canonical (no "//", ".", ".." or trailing "/": a "root//elsewhere" would otherwise map to "/elsewhere"), and
    the mapped path must still lie inside the payload."""
    path, prefix = str(path), str(prefix).rstrip("/")
    require(path.startswith(prefix + "/") and os.path.normpath(path) == path and "\0" not in path,
            "a configured file lies outside the installation (and so outside its backup)")
    relative = path[len(prefix) + 1:]
    mapped = Path(payload) / relative
    require(relative and not relative.startswith("/") and Path(payload) in mapped.parents
            and os.path.normpath(str(mapped)) == str(mapped),
            "a configured file lies outside the installation (and so outside its backup)")
    return mapped


# What the application's startup loaders read, derived from its configuration (Codex U3-R1). Registration
# (kalillac_accounts.registration_startup.bootstrap, when KALILLAC_ACCOUNT_REGISTRATION_V2_ENABLED) reads four
# protected files through registration_files.read_protected, the activation's ".evidence.json" companion among
# them, each with its own size ceiling. The database and billing loaders read CREDENTIALS_DIRECTORY files.
REGISTRATION_FILES = (("KALILLAC_ACCOUNT_REGISTRATION_KEYRING_FILE", "", 16384),
                      ("KALILLAC_ACCOUNT_REGISTRATION_ACTIVATION_FILE", "", 8192),
                      ("KALILLAC_ACCOUNT_REGISTRATION_ACTIVATION_FILE", ".evidence.json", 65536),
                      ("KALILLAC_ACCOUNT_REGISTRATION_DATABASE_CREDENTIAL_FILE", "", 8192))
_TRUE = {"1", "true", "yes", "on"}            # kalillac_db.config, kalillac_billing.config, registration_startup.
_ACL = ("system.posix_acl_access", "system.posix_acl_default")


_SETTING = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def launcher_settings(path):
    """A settings file exactly as the launcher reads it (Codex C2): its values come from local_staging.read_env
    itself (keys and values stripped, comments skipped), so the paths checked are the paths the application
    gets. A file the launcher would read differently from how it is written is refused, never reinterpreted: a
    non-blank line that is not a setting (silently ignored), an invalid name, or one name twice (the last
    silently wins)."""
    import local_staging
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), f"{path.name} is missing")
    seen = set()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        require("=" in line, f"{path.name} line {number} is not a setting (the launcher would ignore it)")
        key = line.partition("=")[0].strip()
        require(_SETTING.fullmatch(key) is not None, f"{path.name} line {number} has an invalid setting name")
        require(key not in seen, f"{path.name} sets {key} more than once (the launcher would use the last)")
        seen.add(key)
    values = local_staging.read_env(path)
    require(set(values) == seen, f"{path.name} is not read as written")
    return values


def startup_environment(payload, installed_root):
    """The settings the application starts with, as local_staging.service_env builds them and with its parser:
    CREDENTIALS_DIRECTORY defaults to the root's credentials/, then deployment.env, then settings.env (either may
    override it; a setting in both takes settings.env's value, as the launcher does)."""
    payload = Path(payload)
    return {"CREDENTIALS_DIRECTORY": f"{str(installed_root).rstrip('/')}/credentials",
            **launcher_settings(payload / "deployment/deployment.env"), **launcher_settings(payload / "settings.env")}


def _protected_policy(path, top, gid, ceiling, *, above_top=False):
    """registration_files.read_protected's rules, checked as root: the file root's, in the service's group,
    exactly 0440, one link, 0 < size <= ceiling, no POSIX ACL; every directory from `top` down to it root's,
    in group 0 or the service's, writable by no one else, no ACL (and above `top` too for the live root; a
    rehearsal copy's scratch ancestors are not the installation's)."""
    st = os.lstat(path)
    require(stat.S_ISREG(st.st_mode) and st.st_uid == 0 and st.st_gid == gid and stat.S_IMODE(st.st_mode) == 0o440
            and st.st_nlink == 1 and 0 < st.st_size <= ceiling and not set(os.listxattr(path, follow_symlinks=False))
            & set(_ACL), "a protected registration file does not meet its loader's requirements")
    top = Path(top)
    directories = [d for d in Path(path).parents if d == top or top in d.parents]
    if above_top:
        directories += list(top.parents)
    for directory in directories:
        d = os.lstat(directory)
        require(stat.S_ISDIR(d.st_mode) and d.st_uid == 0 and d.st_gid in (0, gid) and not stat.S_IMODE(d.st_mode) & 0o7022
                and not set(os.listxattr(directory, follow_symlinks=False)) & set(_ACL),
                "a folder above a protected registration file does not meet its loader's requirements")


def runtime_access(payload, installed_root, uid, gid, *, live=False):
    """Everything the application reads at startup, present, inside the installation (and so its backup),
    meeting its loader's rules, and readable as the service identity. Contents are compared with root's own
    hash, never shown. Returns counts.

    * every *_FILE setting;
    * with registration enabled, all four protected registration files, the activation's evidence companion
      included, under read_protected's rules;
    * with the database enabled, credentials/kalillac_db_password; with billing enabled, the two Stripe
      credentials; and every file in credentials/;
    * the state folders the application writes."""
    payload = Path(payload)
    env = startup_environment(payload, installed_root)
    paths = []
    for key, value in env.items():
        if key.endswith("_FILE") and value:
            paths.append(_inside(value, installed_root, payload))
    registration = []
    if env.get("KALILLAC_ACCOUNT_REGISTRATION_V2_ENABLED", "").strip().lower() in _TRUE:
        for key, suffix, ceiling in REGISTRATION_FILES:
            require(env.get(key), "registration is enabled but a required protected file is not configured")
            path = _inside(env[key] + suffix, installed_root, payload)
            require(os.path.lexists(path), "a required protected registration file is missing")
            _protected_policy(path, payload, gid, ceiling, above_top=live)
            registration.append(path)
    # The directory the database and billing loaders actually read (Codex: a configured CREDENTIALS_DIRECTORY
    # must be the one checked), inside the installation and so inside its backup.
    credentials = _inside(env["CREDENTIALS_DIRECTORY"], installed_root, payload)
    required = []
    if env.get("KALILLAC_DB_ENABLED", "").strip().lower() in _TRUE:
        required.append("kalillac_db_password")
    if env.get("KALILLAC_BILLING_ENABLED", "").strip().lower() in _TRUE:
        required += ["stripe_secret_key", "stripe_webhook_secret"]
    for name in required:
        require((credentials / name).is_file() and (credentials / name).stat().st_size > 0,
                "a credential the application requires at startup is missing")
    listing = [credentials] if credentials.is_dir() else []
    if credentials.is_dir():
        paths += sorted(p for p in credentials.iterdir() if p.is_file())
    paths = list(dict.fromkeys([*paths, *registration]))
    for path in paths:
        require(path.is_file() and not path.is_symlink(), "a configured runtime file is missing")
    writable = [payload / d for d in ("state", "state/files", "state/locks", "state/outbox") if (payload / d).is_dir()]
    if "KALILLAC_FILES_ROOT" in env or env.get("KALILLAC_FILES_ENABLED", "").strip().lower() in _TRUE:
        # Where the application stores files, as configured (it refuses to start without one when enabled).
        files_root = _inside(env.get("KALILLAC_FILES_ROOT", ""), installed_root, payload)
        require(files_root.is_dir() and not files_root.is_symlink(), "the configured file storage folder is missing")
        writable = list(dict.fromkeys([*writable, files_root]))
    seen = as_service(uid, gid, read=paths, listing=listing, write=writable)
    for path in paths:
        require(seen["read"][str(path)] == file_hash(path),
                "the application service identity cannot read a required credential or configuration file")
    require(all(v == "ok" for v in (*seen["list"].values(), *seen["write"].values())),
            "the application service identity cannot use a required folder")
    return {"runtime_files": len(paths), "writable_folders": len(writable)}


# libpq reads these when a connection omits the parameter, and some (PGHOSTADDR, PGSERVICE, PGOPTIONS) can
# redirect or reconfigure a connection whose host and port are explicit. None may be set during an upgrade.
DESTINATION_ENVIRONMENT = ("PGHOST", "PGHOSTADDR", "PGPORT", "PGDATABASE", "PGSERVICE", "PGSERVICEFILE",
                           "PGSYSCONFDIR", "PGOPTIONS", "PGTARGETSESSIONATTRS", "PGLOADBALANCEHOSTS")
_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}")


def connection_target(payload, port):
    """The only database connection an upgrade makes: (admin URL, migration URL), both built from validated
    parts, never from a copied DSN. Refused before any engine exists if a query parameter or a libpq
    environment setting could override the destination (Codex U1)."""
    deployment = env_file(payload / "deployment/deployment.env")
    database = deployment.get("KALILLAC_DB_NAME", "")
    require(_IDENTIFIER.fullmatch(database), "invalid database name")
    require(type(port) is int and 0 < port < 65536, "invalid database port")
    require(not [k for k in DESTINATION_ENVIRONMENT if os.environ.get(k)],
            "a libpq environment setting could redirect the connection; unset it")
    admin = make_url((payload / "admin/admin-url").read_text().strip())
    require(admin.drivername == "postgresql+psycopg", "unsupported database driver")
    require(not admin.query, "connection options in the administrative URL are refused")
    require(admin.host in ("localhost", "127.0.0.1") and admin.database in ("postgres", database),
            "non-local database credentials")
    require(isinstance(admin.username, str) and _IDENTIFIER.fullmatch(admin.username) and admin.password,
            "incomplete administrative credentials")
    login = json.loads((payload / "deployment/private/migration-login.json").read_text())
    require(isinstance(login, dict) and set(login) == {"role", "password"} and isinstance(login["role"], str)
            and _IDENTIFIER.fullmatch(login["role"]) and isinstance(login["password"], str) and login["password"],
            "invalid migration login")
    base = dict(drivername="postgresql+psycopg", host="127.0.0.1", port=port, database=database)
    return (URL.create(username=admin.username, password=admin.password, **base),
            URL.create(username=login["role"], password=login["password"], **base))


def _bound_engine(url):
    """An engine whose driver parameters are exactly the validated destination, checked before it connects.
    Explicit hostaddr and an empty options string leave libpq nothing to take from elsewhere."""
    engine = create_engine(url, hide_parameters=True,
                           connect_args={"hostaddr": "127.0.0.1", "options": "", "target_session_attrs": "any"})
    args, params = engine.dialect.create_connect_args(engine.url)
    params = dict(params)
    params.pop("context", None)                       # psycopg's type adapters, not a destination.
    expected = {"host": "127.0.0.1", "port": url.port, "dbname": url.database, "user": url.username,
                "password": url.password}
    if args or params != expected:
        engine.dispose()
        raise Refused("database connection parameters differ from the verified destination")
    return engine


def engines(payload, port):
    admin_url, migration_url = connection_target(payload, port)
    admin = _bound_engine(admin_url)
    try:
        return admin, _bound_engine(migration_url)
    except BaseException:
        admin.dispose()
        raise
