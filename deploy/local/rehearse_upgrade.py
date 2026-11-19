"""Rehearse the local install's database upgrade on a COPY: never writes to the running install.

Run as root in WSL, from the candidate/ of the checkout to upgrade to (it supplies the migrations):
    wsl -u root -- sh -c "cd /mnt/c/Kalillac/<checkout>/candidate && /home/kalillac/kf15/bin/python -B ../deploy/local/rehearse_upgrade.py"
Run on 2026-11-19 from integration/premium-corrections-v1 (1cc3251): 0025 -> 0026, 29 tables, 91 rows, nothing
changed, the original identical before and after, the copy removed. The original cluster (55433) is only read: a base backup
(replication over its local socket) and SELECT count(*) per table. The copy runs on 55434 in a temporary folder
owned by the service user, is migrated with the deployment's own migration login, compared, then removed."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path("/var/lib/kalillac-local-staging")
BIN = "/home/kalillac/pg16tools/lib/python3.12/site-packages/pgserver/pginstall/bin"
COPY_PORT = 55434
CANDIDATE = Path.cwd()
sys.path.insert(0, str(CANDIDATE))

import pwd  # noqa: E402
service = pwd.getpwnam("kalillac")
work = Path(tempfile.mkdtemp(prefix="kalillac-upgrade-rehearsal-", dir="/tmp"))
os.chown(work, service.pw_uid, service.pw_gid)


def as_service(argv, **kwargs):
    return subprocess.run(argv, user=service.pw_uid, group=service.pw_gid, extra_groups=[], check=True,
                          capture_output=True, text=True, **kwargs)


def env_file(path):
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line and not line.startswith("#"))


from sqlalchemy import create_engine, make_url, text  # noqa: E402

admin = make_url((ROOT / "admin" / "admin-url").read_text().strip())
deployment = env_file(ROOT / "deployment" / "deployment.env")
database = deployment["KALILLAC_DB_NAME"]


def counts(port, user=None, password=None):
    url = admin.set(port=port, database=database)
    if user:
        url = url.set(username=user, password=password)
    engine = create_engine(url, hide_parameters=True)
    try:
        with engine.connect() as c:
            tables = [r[0] for r in c.execute(text(
                "SELECT tablename FROM pg_tables WHERE schemaname='kalillac' ORDER BY tablename"))]
            out = {t: c.scalar(text(f'SELECT count(*) FROM kalillac."{t}"')) for t in tables}
            out["__version__"] = c.scalar(text("SELECT version_num FROM kalillac.alembic_version"))
            out["__latch__"] = list(c.execute(text(
                "SELECT state, authority_version, deployment_id, release_sha FROM kalillac.account_security_activation")).one())
            return out
    finally:
        engine.dispose()


try:
    before = counts(55433)                                               # Read-only, the original.
    as_service([f"{BIN}/pg_basebackup", "-h", str(ROOT / "pgsock"), "-p", "55433", "-U", "postgres",
                "-D", str(work / "pg"), "-X", "stream", "-c", "fast"])
    as_service([f"{BIN}/pg_ctl", "-D", str(work / "pg"), "-l", str(work / "copy.log"), "-w", "-o",
                f"-p {COPY_PORT} -k {work} -c listen_addresses=127.0.0.1", "start"])
    copy_before = counts(COPY_PORT)
    assert copy_before == before, "the copy differs from the original"

    # The upgrade itself, as the deployment's migration login, exactly as provision_deployment._migrate runs it.
    login = json.loads((ROOT / "deployment" / "private" / "migration-login.json").read_text())
    for key in ("KALILLAC_MIGRATION_OWNER_ROLE", "KALILLAC_MIGRATION_APP_ROLE", "KALILLAC_MIGRATION_REGISTRATION_ROLE"):
        os.environ[key] = deployment[key]
    from alembic import command
    from alembic.config import Config
    migration = create_engine(admin.set(port=COPY_PORT, database=database, username=login["role"],
                                        password=login["password"]), hide_parameters=True)
    config = Config(str(CANDIDATE / "alembic.ini"))
    config.set_main_option("script_location", str(CANDIDATE / "migrations"))
    with migration.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    migration.dispose()

    after = counts(COPY_PORT)
    attempts = None
    engine = create_engine(admin.set(port=COPY_PORT, database=database), hide_parameters=True)
    with engine.connect() as c:
        attempts = sorted({r[0] for r in c.execute(text("SELECT provider_attempts FROM kalillac.account_usage_daily"))})
    engine.dispose()
    changed = {k: (before[k], after.get(k)) for k in before if k != "__version__" and before[k] != after.get(k)}
    print(json.dumps({"original_version": before["__version__"], "copy_after_upgrade": after["__version__"],
                      "tables": len([k for k in before if not k.startswith("__")]),
                      "rows_total": sum(v for k, v in before.items() if not k.startswith("__")),
                      "changed_tables_or_latch": changed, "provider_attempts_values": attempts,
                      "original_unchanged": counts(55433) == before}, indent=1))
finally:
    subprocess.run([f"{BIN}/pg_ctl", "-D", str(work / "pg"), "-m", "fast", "-w", "stop"],
                   user=service.pw_uid, group=service.pw_gid, extra_groups=[], capture_output=True)
    shutil.rmtree(work)
    print("copy removed:", not work.exists())
