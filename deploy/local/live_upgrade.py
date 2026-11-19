"""Explicitly authorized local backup/migration. Never starts the application or changes Git."""
from __future__ import annotations
import argparse
from datetime import timedelta
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from upgrade_support import (NEW, RELEASE, Cluster, Refused, copy_tree, database_identity, database_snapshot,
                             digest, engines, env_file, file_hash, migrate, require, same_contents,
                             save_json, settings_identity, tree_manifest, validate_backup)
from rehearse_upgrade import stored_files

INSTALLED = Path("/var/lib/kalillac-local-staging")
SYNTHETIC = {"KALILLAC_ACCOUNT_DAILY_PROVIDER_ATTEMPTS_FREE": "100",
             "KALILLAC_ACCOUNT_DAILY_PROVIDER_ATTEMPTS_PAID": "1000",
             "KALILLAC_FILES_DELETE_AFTER_EXPIRY_DAYS": "3650"}


def checkout_identity(checkout, expected):
    checkout = Path(checkout)
    require(checkout.is_absolute() and re.fullmatch(r"[0-9a-f]{40}", expected), "invalid checkout selection")
    command = ["git", "-c", f"safe.directory={checkout}", "-C", str(checkout)]
    result = subprocess.run([*command, "rev-parse", "HEAD"], capture_output=True, text=True)
    require(result.returncode == 0 and result.stdout.strip() == expected, "incorrect checkout release")
    result = subprocess.run([*command, "status", "--porcelain"], capture_output=True, text=True)
    require(result.returncode == 0 and not result.stdout, "checkout has uncommitted work; preserve it first")


def no_service_processes(uid):
    """Conservative offline requirement, including processes with lost legacy PID records."""
    live = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid == uid and entry.joinpath("stat").read_text().rsplit(")", 1)[1].split()[0] != "Z":
                live.append(entry.name)
        except FileNotFoundError:
            continue
    require(not live, "service identity still has processes; no backup or migration performed")


def destination(root, device, inode, activation, checkout, code_sha):
    import local_staging
    require(Path(root) == INSTALLED and os.geteuid() == 0, "only the explicitly selected local installation is allowed")
    require(local_staging.owned_root(str(root), create=False, adopt=False) == root, "unverified local root")
    require((root.stat().st_dev, root.stat().st_ino) == (device, inode), "destination filesystem identity mismatch")
    checkout_identity(checkout, code_sha)
    settings, deployment = settings_identity(root, activation)
    require(settings.get("KALILLAC_FILES_ROOT") == str(root / "state/files"), "file storage is outside the backup")
    pg_owner = root.joinpath("pg").stat()
    return settings, deployment, pg_owner.st_uid, pg_owner.st_gid


def offline(root, bindir, uid, gid):
    no_service_processes(uid)
    cluster = Cluster(root / "pg", bindir, uid, gid)
    cluster.stopped()
    no_service_processes(uid)
    return cluster


def stop_install(root, checkout, bindir):
    """Bind the F1 command to verified arguments, overriding ambient local-tool settings."""
    import local_staging
    prior = (local_staging.CANDIDATE, local_staging.REPO, local_staging.ROOT)
    values = {"KALILLAC_LOCAL_ROOT": str(root), "KALILLAC_LOCAL_PG_BIN": str(bindir)}
    ambient = {key: os.environ.get(key) for key in values}
    try:
        os.environ.update(values)
        local_staging.CANDIDATE = checkout / "candidate"
        local_staging.REPO = checkout
        require(local_staging.main(["stop"]) == 0, "shutdown unconfirmed")
    finally:
        local_staging.CANDIDATE, local_staging.REPO, local_staging.ROOT = prior
        for key, value in ambient.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def backup_install(root, output, activation, bindir, uid, gid):
    """A failed or incomplete backup never gets a usable manifest."""
    offline(root, bindir, uid, gid)
    output = Path(output)
    require(output.is_absolute() and not output.exists() and root not in output.parents,
            "backup must be a new absolute destination outside the installation")
    import local_staging
    parent = local_staging._open_parent(output)
    os.close(parent)
    output.mkdir(mode=0o700)
    source = tree_manifest(root)
    copy_tree(root, output / "payload")
    offline(root, bindir, uid, gid)
    require(tree_manifest(root) == source and tree_manifest(output / "payload") == source, "backup changed during copy")
    manifest = {"format": 1, "activation": activation, "root": str(root),
                "device": root.stat().st_dev, "inode": root.stat().st_ino, "files": source}
    save_json(output / "manifest.json", manifest)
    checksum = file_hash(output / "manifest.json")
    validate_backup(output, checksum)
    return checksum


def retention_preflight(root, days):
    from kalillac_files.retention import clean
    report = clean(root / "state/files", delete_after=timedelta(days=days), apply=False)
    require(not (report.expired_files or report.orphaned_data or report.stale_temporaries or report.unreadable_records),
            "startup cleanup could remove files or cannot establish their retention state")
    return report


def apply_upgrade(root, backup, backup_hash, receipt, receipt_hash, activation, bindir, uid, gid, code_sha, *, port=55433):
    require(code_sha == RELEASE, "upgrade checkout must be the exact independently tested integrated release")
    cluster = offline(root, bindir, uid, gid)
    manifest = validate_backup(backup, backup_hash)
    require((manifest["root"], manifest["device"], manifest["inode"], manifest["activation"]) ==
            (str(root), root.stat().st_dev, root.stat().st_ino, activation), "backup belongs to another destination")
    result = json.loads(Path(receipt).read_text())
    require(digest(result) == receipt_hash and result["release"] == RELEASE and
            result["backup_sha256"] == backup_hash and result["upgraded_to"] == NEW,
            "missing or incorrect successful rehearsal receipt")
    require(tree_manifest(root) == manifest["files"], "installation changed since backup; make and rehearse a new backup")
    settings, deployment = settings_identity(root, activation)
    require(not any(k in settings for k in SYNTHETIC), "new settings already exist; manual review required")
    retention_preflight(root, 3650)
    admin = migration = None
    try:
        cluster.start(root / "pgsock", port)
        admin, migration = engines(root, port)
        database_identity(admin, root / "pg", deployment["KALILLAC_DB_NAME"], activation)
        before = database_snapshot(admin)
        same_contents(before, result["database"])
        require(stored_files(root, admin) == result["files"], "files differ from rehearsal")
        migrate(migration, deployment)
        same_contents(before, database_snapshot(admin, project_new=True))
        require(stored_files(root, admin) == result["files"], "stored files changed")
    finally:
        if admin is not None:
            admin.dispose()
        if migration is not None:
            migration.dispose()
        cluster.stop()  # Also confirm shutdown after a partial/failed start. Never restore automatically.
    path = root / "settings.env"
    addition = "\n# SYNTHETIC LOCAL TEST INPUTS ONLY; not approved commercial limits.\n"
    addition += "".join(f"{k}={v}\n" for k, v in SYNTHETIC.items())
    temporary = root / "settings.env.upgrade-new"
    require(not temporary.exists(), "configuration staging file already exists")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w") as stream:
        stream.write(path.read_text() + addition)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    require(env_file(path)["KALILLAC_RELEASE_SHA"] == activation, "activation setting changed")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("stop", "backup", "apply"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--device", type=int, required=True)
    parser.add_argument("--inode", type=int, required=True)
    parser.add_argument("--activation-release", required=True)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--checkout-sha", required=True)
    parser.add_argument("--pg-bin", required=True)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--backup-sha256")
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--receipt-sha256")
    parser.add_argument("--authorize-live-operation", action="store_true")
    args = parser.parse_args(argv)
    try:
        require(args.authorize_live_operation, "separate owner authorization is required")
        _settings, _deployment, uid, gid = destination(args.root, args.device, args.inode, args.activation_release,
                                                      args.checkout, args.checkout_sha)
        if args.operation == "stop":
            stop_install(args.root, args.checkout, args.pg_bin)
        elif args.operation == "backup":
            require(args.backup is not None, "new backup destination required")
            checksum = backup_install(args.root, args.backup, args.activation_release, args.pg_bin, uid, gid)
            print(json.dumps({"backup_manifest_sha256": checksum}))
        else:
            require(args.backup and args.backup_sha256 and args.receipt and args.receipt_sha256,
                    "verified backup and receipt required")
            apply_upgrade(args.root, args.backup, args.backup_sha256, args.receipt, args.receipt_sha256,
                          args.activation_release, args.pg_bin, uid, gid, args.checkout_sha)
            print(json.dumps({"migrated": NEW, "application_started": False, "payments": "simulated"}))
        return 0
    except Exception as error:
        reason = str(error) if isinstance(error, Refused) else type(error).__name__
        print(f"live operation refused/failed: {reason}; preserve data and backup; no automatic recovery",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
