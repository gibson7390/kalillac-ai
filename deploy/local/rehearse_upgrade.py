"""Rehearse ONLY a verified cold backup; never connects to the installed database."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import socket
import sys

from sqlalchemy import text
from upgrade_support import (NEW, OLD, RELEASE, Refused, as_service, copied_cluster, database_identity, database_snapshot,
                             digest, engines, env_file, migrate, require, runtime_access, same_contents, save_json,
                             tree_manifest)


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def stored_files(payload, engine, uid=None, gid=None):
    """Check the owner mapping, each record's id and size, the bytes, and that the application can use them:
    every metadata file and owner folder, and every unexpired document, read (and folders listed and written)
    as the service identity, not as root (Codex U3). A file root can read but the service cannot fails."""
    from kalillac_files.store import FileStore
    if uid is None or gid is None:                # The service identity: the account the data directory belongs to.
        owner = (payload / "pg").stat()
        uid, gid = owner.st_uid, owner.st_gid
    root = payload / "state/files"
    files = tree_manifest(root)
    now = datetime.now(timezone.utc)
    with engine.connect() as c:
        owners = {hashlib.sha256(str(row[0]).encode()).hexdigest()[:32]: str(row[0])
                  for row in c.execute(text("SELECT id FROM kalillac.users"))}
    store = FileStore(root, clock=lambda: now)
    records, unexpired, read, folders = 0, [], [], {root}
    for path in sorted(root.glob("*/*.json")):
        record = json.loads(path.read_text())
        owner = owners.get(path.parent.name)
        require(owner is not None and record["file_id"] == path.stem, "stored file owner or id does not match")
        data = path.with_suffix(".bin")
        require(data.is_file() and data.stat().st_size == record["size"], "stored file data missing or wrong size")
        read.append(path)
        folders.add(path.parent)
        if datetime.fromisoformat(record["expires_at"]).astimezone(timezone.utc) > now:
            _metadata, content = store.get(owner, path.stem)
            require(hashlib.sha256(content).hexdigest() == files[str(data.relative_to(root))]["sha256"],
                    "stored file access returned different bytes")
            unexpired.append(data)
            read.append(data)
        records += 1
    folders = sorted(folders)
    seen = as_service(uid, gid, read=read, listing=folders, write=folders)
    for path in read:
        require(seen["read"][str(path)] == files[str(path.relative_to(root))]["sha256"],
                "a stored file cannot be read by the application service identity")
    require(all(v == "ok" for v in (*seen["list"].values(), *seen["write"].values())),
            "a file folder cannot be used by the application service identity")
    return {"manifest": files, "records": records, "accessible": len(unexpired)}


def rehearse(backup, backup_hash, bindir, receipt):
    port = free_port()
    with copied_cluster(backup, backup_hash, bindir, port=port) as (payload, manifest, cluster):
        uid, gid = cluster.uid, cluster.gid          # The service identity: the data directory's owner.
        admin, migration = engines(payload, port)
        try:
            database_identity(admin, cluster.pg, env_file(payload / "deployment/deployment.env")["KALILLAC_DB_NAME"],
                              manifest["activation"])
            runtime = runtime_access(payload, manifest["root"], uid, gid)
            before = database_snapshot(admin)
            file_before = stored_files(payload, admin, uid, gid)
            migrate(migration, env_file(payload / "deployment/deployment.env"))
            same_contents(before, database_snapshot(admin, project_new=True))
            require(stored_files(payload, admin, uid, gid) == file_before, "files changed during migration")
            migrate(migration, env_file(payload / "deployment/deployment.env"), OLD)
            same_contents(before, database_snapshot(admin))
            require(stored_files(payload, admin, uid, gid) == file_before, "files changed during downgrade")
        finally:
            admin.dispose()
            migration.dispose()
    # Restore a second copy independently of the upgraded/downgraded copy.
    port = free_port()
    with copied_cluster(backup, backup_hash, bindir, port=port) as (payload, manifest, cluster):
        admin, migration = engines(payload, port)
        try:
            database_identity(admin, cluster.pg, env_file(payload / "deployment/deployment.env")["KALILLAC_DB_NAME"],
                              manifest["activation"])
            same_contents(before, database_snapshot(admin))
            require(stored_files(payload, admin, cluster.uid, cluster.gid) == file_before, "restored files differ")
            require(runtime_access(payload, manifest["root"], cluster.uid, cluster.gid) == runtime,
                    "restored runtime files differ")
        finally:
            admin.dispose()
            migration.dispose()
    result = {"format": 1, "release": RELEASE, "backup_sha256": backup_hash, "database": before,
              "files": file_before, "runtime": runtime, "upgraded_to": NEW, "restored_to": OLD}
    save_json(receipt, result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--backup-sha256", required=True)
    parser.add_argument("--pg-bin", required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = rehearse(args.backup, args.backup_sha256, args.pg_bin, args.receipt)
        print(json.dumps({"verified": True, "tables": len(result["database"]["tables"]),
                          "files": result["files"]["records"], "receipt_sha256": digest(result)}))
        return 0
    except Refused as error:
        # A known refusal: its reason is safe to show (paths and checks, never contents or credentials).
        print(f"rehearsal of {args.backup} refused: {error}. No receipt was written; any scratch state kept for "
              "inspection is named above.", file=sys.stderr)
        return 2
    except Exception as error:
        print(f"rehearsal failed ({type(error).__name__}); retained state must be inspected", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
