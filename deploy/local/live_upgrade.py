"""Explicitly authorized local backup/migration. Never starts the application or changes Git."""
from __future__ import annotations
import argparse
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

from upgrade_support import (NEW, RELEASE, Cluster, Refused, copy_tree, database_identity, database_snapshot,
                             digest, engines, env_file, file_hash, migrate, require, runtime_access, same_contents,
                             save_json, settings_identity, tree_manifest, validate_backup)
from rehearse_upgrade import stored_files

INSTALLED = Path("/var/lib/kalillac-local-staging")
SYNTHETIC = {"KALILLAC_ACCOUNT_DAILY_PROVIDER_ATTEMPTS_FREE": "100",
             "KALILLAC_ACCOUNT_DAILY_PROVIDER_ATTEMPTS_PAID": "1000",
             "KALILLAC_FILES_DELETE_AFTER_EXPIRY_DAYS": "3650"}


WINDOWS_GIT = ("/mnt/c/Program Files/Git/cmd/git.exe", "/mnt/c/Program Files/Git/mingw64/bin/git.exe")
# Only these reach Git: GIT_DIR, GIT_WORK_TREE, GIT_INDEX_FILE and the like could point it elsewhere.
GIT_ENVIRONMENT_KEEP = ("PATH", "LANG", "LC_ALL", "HOME", "SYSTEMROOT", "WINDIR")


def _windows_path(path):
    """/mnt/<drive>/rest as <DRIVE>:/rest, for Git for Windows; refuses anything else."""
    match = re.fullmatch(r"/mnt/([a-z])(/.*)?", str(path))
    require(match is not None, "a Windows-created worktree must be under /mnt/<drive>")
    return f"{match.group(1).upper()}:{match.group(2) or '/'}"


def _git_layout(checkout):
    """"linux" for a repository or linked worktree Linux Git can open; "windows" for a linked worktree that
    Git for Windows created (its .git file names a drive path, which Linux Git cannot follow)."""
    dotgit = checkout / ".git"
    require(not dotgit.is_symlink() and (dotgit.is_dir() or dotgit.is_file()), "not a Git checkout")
    if dotgit.is_dir():
        return "linux"
    line = dotgit.read_text().strip()
    require(line.startswith("gitdir: ") and "\n" not in line, "unrecognized .git file")
    target = line[len("gitdir: "):]
    if re.fullmatch(r"[A-Za-z]:[/\\].*", target):
        return "windows"
    require(target.startswith("/"), "relative or unrecognized linked worktree")
    return "linux"


def _git(checkout, layout):
    """The command for read-only Git queries bound to this checkout: no optional locks (status never rewrites
    the index), a cleaned environment, and for a Windows-created worktree Git for Windows itself: the same Git,
    configuration (core.autocrlf) and index cache that created it, so its CRLF checkout reads as clean."""
    env = {k: os.environ[k] for k in GIT_ENVIRONMENT_KEEP if k in os.environ}
    if layout == "windows":
        executable = next((g for g in WINDOWS_GIT if os.access(g, os.X_OK)), None)
        require(executable is not None, "Git for Windows is required to check a Windows-created worktree")
        location = _windows_path(checkout)
        return [executable, "--no-optional-locks", "-C", location], env, location
    return ["git", "--no-optional-locks", "-c", f"safe.directory={checkout}", "-C", str(checkout)], env, str(checkout)


def checkout_identity(checkout, expected, branch=None):
    """The checkout is exactly this repository at exactly this commit, with nothing uncommitted or untracked.

    Works for a plain repository, a Linux linked worktree, and a worktree Git for Windows created, checked
    from WSL (the owner's layout; Codex U2). Read-only: nothing is stashed, reset, cleaned or checked out."""
    checkout = Path(checkout)
    require(checkout.is_absolute() and checkout.is_dir() and re.fullmatch(r"[0-9a-f]{40}", expected),
            "invalid checkout selection")
    layout = _git_layout(checkout)
    command, env, location = _git(checkout, layout)

    def git(*args, detached_ok=False):
        result = subprocess.run([*command, *args], capture_output=True, text=True, env=env,
                                cwd=str(checkout), timeout=120)
        if detached_ok and result.returncode == 1 and not result.stdout:
            return ""                              # symbolic-ref on a detached HEAD.
        require(result.returncode == 0, "Git could not inspect the checkout")
        return result.stdout
    top = git("rev-parse", "--show-toplevel").strip()
    same = top.lower() == location.lower() if layout == "windows" else top == location
    require(same, "the selected path is not the top of its own checkout")
    require(git("rev-parse", "HEAD").strip() == expected, "incorrect checkout release")  # The SHA pins the content.
    if branch is not None:
        require(git("symbolic-ref", "-q", "--short", "HEAD", detached_ok=True).strip() == branch, "the checkout is on another branch")
    require(git("status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none") == "",
            "checkout has uncommitted work; preserve it first")


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
    staged = stage_settings(root / "settings.env", SETTINGS_ADDITION)   # Before anything changes.
    admin = migration = None
    migrated = False
    try:
        cluster.start(root / "pgsock", port)
        admin, migration = engines(root, port)
        database_identity(admin, root / "pg", deployment["KALILLAC_DB_NAME"], activation)
        require(runtime_access(root, root, uid, gid, live=True) == result["runtime"],
                "runtime files differ from rehearsal")
        before = database_snapshot(admin)
        same_contents(before, result["database"])
        require(stored_files(root, admin, uid, gid) == result["files"], "files differ from rehearsal")
        migrate(migration, deployment)
        migrated = True
        same_contents(before, database_snapshot(admin, project_new=True))
        require(stored_files(root, admin, uid, gid) == result["files"], "stored files changed")
    except BaseException:
        if not migrated:
            discard_staged(staged)          # Nothing changed: the original settings stay exactly as they were.
        else:
            print(f"migrated, settings not yet replaced; staged settings kept for review: {staged.path}",
                  file=sys.stderr)
        raise
    finally:
        if admin is not None:
            admin.dispose()
        if migration is not None:
            migration.dispose()
        cluster.stop()  # Also confirm shutdown after a partial/failed start. Never restore automatically.
    try:
        commit_settings(staged)
    except Refused:
        print("migrated to 0026; settings NOT replaced and kept as they are; add the three settings by hand after "
              f"review, from the staged file {staged.path}", file=sys.stderr)
        raise
    require(env_file(root / "settings.env")["KALILLAC_RELEASE_SHA"] == activation, "activation setting changed")


# --- recovery: restore a verified cold backup into the same root, and resume an interrupted restore -----------

JOURNAL = ".restore.json"             # Beside the failed-state folder: the recovery transaction's evidence.
EVIDENCE = ".manifest.json"           # Beside it too: the failed state's own manifest.
RESERVE = 256 * 1024 * 1024           # Free space kept beyond the backup's size when copying it back.


def _evidence_paths(failed):
    return failed, failed.with_name(failed.name + EVIDENCE), failed.with_name(failed.name + JOURNAL)


def _identity(path):
    st = os.lstat(path)
    return st.st_dev, st.st_ino


def _within(path, other):
    return path == other or other in path.parents


def _mount_id(path):
    """The id of the mount holding path (the longest mount point that contains it): a rename works only within
    one mount, even between bind mounts of the same device."""
    real, best = os.path.realpath(path), (-1, -1)
    with open("/proc/self/mountinfo") as mounts:
        for line in mounts:
            fields = line.split()
            point = fields[4].encode().decode("unicode_escape")
            if real == point or real.startswith(point.rstrip("/") + "/"):
                best = max(best, (len(point), int(fields[0])))
    require(best[1] >= 0, "the filesystem mount could not be determined")
    return best[1]


def restore_preflight(root, backup, failed, manifest, *, earlier=()):
    """Everything about the destinations is checked before anything changes (Codex R1): the installation, the
    backup and the new failed state with its two evidence files are separate, by path and by filesystem
    identity (no alias, no symlink anywhere on any path); the failed state is new, under a trusted parent, on the
    installation's own mount (it is moved there by rename, never copied); and the filesystem has room for the
    backup to be copied back. `earlier` failed states (a resumed restore) must not contain the new one."""
    import local_staging
    root, backup, failed = Path(root), Path(backup), Path(failed)
    for path in (root, backup, failed):
        require(path.is_absolute() and os.path.normpath(str(path)) == str(path), "paths must be absolute and normal")
    for path in (root, backup, failed.parent):
        require(path.is_dir() and not path.is_symlink() and os.path.realpath(path) == str(path),
                "the installation, the backup and the failed state's parent must be real folders, with no symlink")
    for path in _evidence_paths(failed):
        require(not os.path.lexists(path), "a failed-state or evidence destination already exists; choose a new one")
    try:
        os.close(local_staging._open_parent(failed))     # Trusted: no other user can rename or swap a folder on it.
    except local_staging.UnsafeRoot as error:
        raise Refused(f"the failed state's parent is not trusted: {error}") from None
    require(not _within(failed, root), "the failed state needs a new folder outside the installation")
    require(not _within(failed, backup), "the failed state must be outside the backup (it would change it)")
    require(not _within(backup, root) and not _within(root, backup), "the backup and the installation overlap")
    require(not any(_within(failed, Path(e)) for e in earlier), "the failed state is inside an earlier failed state")
    guarded = {_identity(root): "installation", _identity(backup): "backup",
               _identity(backup / "payload"): "backup", **{_identity(e): "failed state" for e in earlier}}
    for ancestor in (failed.parent, *failed.parent.parents):
        require(_identity(ancestor) not in guarded, "the failed state would be inside the installation or the backup")
    for ancestor in backup.parents:
        require(_identity(ancestor) != _identity(root), "the backup is inside the installation")
    for ancestor in root.parents:
        require(_identity(ancestor) not in (_identity(backup), _identity(backup / "payload")),
                "the installation is inside the backup")
    require(_mount_id(failed.parent) == _mount_id(root),
            "the failed state must be on the installation's own mount (it is moved there, not copied)")
    needed = sum(item.get("size", 0) for item in manifest["files"].values())
    vfs = os.statvfs(root)
    require(vfs.f_bavail * vfs.f_frsize >= needed + RESERVE and vfs.f_favail >= len(manifest["files"]) + 1000,
            "not enough free space or inodes on the installation's filesystem to copy the backup back")


def _write_journal(path, value, *, create=False):
    """The recovery journal: created exclusively, then replaced atomically after each phase (root's, 0600)."""
    if create:
        save_json(path, value)
        return
    temporary = path.with_name(path.name + ".new")
    save_json(temporary, value)
    os.replace(temporary, path)


def _restore_into(root, backup, manifest, failed, uid, gid, journal):
    """Move the root's entries into `failed` (rename only), copy the backup's contents into the same root inode,
    verify. The journal records each phase; an interruption leaves it saying where, for resume-restore."""
    identity = (manifest["device"], manifest["inode"])
    journal_path = failed.with_name(failed.name + JOURNAL)
    failed.mkdir(mode=0o700)
    journal.update(failed_state=str(failed), failed_identity=list(_identity(failed)), phase="moving")
    _write_journal(journal_path, journal, create=True)
    try:
        for name in sorted(os.listdir(root)):
            os.rename(root / name, failed / name)
        require(not os.listdir(root), "the installation root is not empty after preserving its failed state")
        save_json(failed.with_name(failed.name + EVIDENCE), {"format": 1, "files": tree_manifest(failed)})
        journal["phase"] = "copying"
        _write_journal(journal_path, journal)
        subprocess.run(["cp", "-a", "--", f"{Path(backup) / 'payload'}/.", f"{root}/"], check=True,
                       capture_output=True)
        journal["phase"] = "verifying"
        _write_journal(journal_path, journal)
        require(_identity(root) == identity, "installation root identity changed")
        require(tree_manifest(root) == manifest["files"], "restored contents differ from the backup manifest")
        runtime = runtime_access(root, root, uid, gid, live=True)
    except BaseException:
        phase = journal["phase"]
        try:
            journal["phase"] = "interrupted-" + phase
            _write_journal(journal_path, journal)
        except BaseException:
            pass
        print(f"restore interrupted while {phase}: the root may be empty or partial; nothing was deleted; the "
              f"failed state is {failed}; the backup was not written to. Complete it with resume-restore "
              f"--journal {journal_path} and a NEW --failed-state (UPGRADE.md).", file=sys.stderr)
        raise
    journal["phase"] = "complete"
    _write_journal(journal_path, journal)
    return {"restored": True, "failed_state": str(failed), "journal": str(journal_path), "runtime": runtime}


def _offline_for_recovery(root, bindir, uid, gid):
    """No process of the service identity (PostgreSQL runs as it), and no running PostgreSQL in the root."""
    no_service_processes(uid)
    if (root / "pg").is_dir():
        result = Cluster(root / "pg", bindir, uid, gid).command("-D", str(root / "pg"), "status")
        require(result.returncode != 0, "PostgreSQL is running; stop it first")
    no_service_processes(uid)


def restore_install(root, backup, backup_hash, failed_state, bindir, uid, gid):
    """Manual recovery from a verified cold backup, into the SAME root folder (its inode is what the F1
    ownership marker names). Nothing is deleted: whatever the root holds is first moved, by rename on the same
    filesystem, into a new failed-state folder (the failed upgrade's evidence, with its own manifest). Then the
    backup's contents are copied in and verified. A journal beside the failed state records the transaction.
    Nothing is started.

    Restoring a pre-upgrade backup DISCARDS EVERY WRITE MADE AFTER THAT BACKUP (they remain only in the failed
    state). Run it only offline, after a separately authorized decision."""
    root, backup, failed = Path(root), Path(backup), Path(failed_state)
    manifest = validate_backup(backup, backup_hash)
    require((manifest["root"], manifest["device"], manifest["inode"]) == (str(root), *_identity(root)),
            "backup belongs to another destination")
    _offline_for_recovery(root, bindir, uid, gid)
    restore_preflight(root, backup, failed, manifest)
    journal = {"format": 1, "operation": "restore", "root": str(root), "device": manifest["device"],
               "inode": manifest["inode"], "activation": manifest["activation"], "backup": str(backup),
               "backup_sha256": backup_hash, "resumes": None}
    return _restore_into(root, backup, manifest, failed, uid, gid, journal)


def _journal_chain(journal_path, root, backup, backup_hash, manifest):
    """The recovery journals from the given one back to the first restore, each root's own (0600, no link), each
    naming this root, backup and checksum, each failed state still the folder it recorded. Returns them."""
    chain, path = [], Path(journal_path)
    while path is not None:
        require(len(chain) < 64 and path.name.endswith(JOURNAL) and path.is_absolute(), "invalid recovery journal")
        st = os.lstat(path)
        require(stat.S_ISREG(st.st_mode) and st.st_uid == 0 and stat.S_IMODE(st.st_mode) == 0o600
                and st.st_nlink == 1, "the recovery journal is not root's own file")
        journal = json.loads(path.read_text())
        require(journal.get("format") == 1 and journal.get("operation") in ("restore", "resume-restore")
                and (journal.get("root"), journal.get("device"), journal.get("inode"), journal.get("backup"),
                     journal.get("backup_sha256"), journal.get("activation"))
                == (str(root), manifest["device"], manifest["inode"], str(backup), backup_hash, manifest["activation"]),
                "the recovery journal belongs to another installation or backup")
        failed = Path(journal["failed_state"])
        require(path == failed.with_name(failed.name + JOURNAL) and failed.is_dir() and not failed.is_symlink()
                and list(_identity(failed)) == journal["failed_identity"], "a recorded failed state has changed")
        chain.append((path, journal))
        path = Path(journal["resumes"]) if journal.get("resumes") else None
    require(chain[0][1]["phase"] != "complete", "that restore completed; nothing to resume")
    return chain


def resume_restore(root, backup, backup_hash, journal_path, failed_state, bindir):
    """Complete an interrupted restore (Codex R2), without the ownership marker in the root: the root may be
    empty or half-copied. Its own evidence instead:

    * the backup, verified against the checksum the operator recorded when making it, names this root's path,
      device and inode (the marker's own identity);
    * the journal chain names this root, backup and checksum, and every recorded failed state is still the
      folder it recorded;
    * the original marker exists (in the root or a recorded failed state) and equals the backup's, byte for
      byte, and no different marker is anywhere among them.

    The partial state is moved into another NEW failed-state folder (earlier ones are never touched), the
    backup's contents are copied into the same root inode and verified. Nothing is started."""
    import local_staging
    root, backup, failed = Path(root), Path(backup), Path(failed_state)
    manifest = validate_backup(backup, backup_hash)
    require(root.is_dir() and not root.is_symlink() and
            (manifest["root"], manifest["device"], manifest["inode"]) == (str(root), *_identity(root)),
            "the backup does not name this installation root")
    try:
        local_staging.checked_path(str(root))           # The path and every folder above it, as F1 checks them.
    except local_staging.UnsafeRoot as error:
        raise Refused(f"unsafe installation path: {error}") from None
    chain = _journal_chain(journal_path, root, backup, backup_hash, manifest)
    earlier = [Path(j["failed_state"]) for _p, j in chain]
    expected = (Path(backup) / "payload" / local_staging.MARKER).read_bytes()
    markers = [where / local_staging.MARKER for where in (root, *earlier)]
    found = [m.read_bytes() for m in markers if os.path.lexists(m) and not m.is_symlink() and m.is_file()]
    require(found and all(m == expected for m in found) and not any(m.is_symlink() for m in markers),
            "the installation's ownership marker is missing or differs; this is not the backed-up installation")
    pg = manifest["files"].get("pg")
    require(pg is not None, "the backup has no database cluster")
    uid, gid = pg["uid"], pg["gid"]                     # The service identity, from the verified backup.
    _offline_for_recovery(root, bindir, uid, gid)
    restore_preflight(root, backup, failed, manifest, earlier=earlier)
    journal = {"format": 1, "operation": "resume-restore", "root": str(root), "device": manifest["device"],
               "inode": manifest["inode"], "activation": manifest["activation"], "backup": str(backup),
               "backup_sha256": backup_hash, "resumes": str(chain[0][0])}
    return _restore_into(root, backup, manifest, failed, uid, gid, journal)


def recovery_destination(root, device, inode, checkout, code_sha):
    """resume-restore's own gate: the fixed root, as root, at the given device and inode, a clean checkout. It
    does NOT require the ownership marker or settings (an interrupted restore may have moved them); the backup,
    journal and marker evidence are checked by resume_restore itself."""
    require(Path(root) == INSTALLED and os.geteuid() == 0, "only the explicitly selected local installation is allowed")
    require(Path(root).is_dir() and not Path(root).is_symlink() and _identity(root) == (device, inode),
            "destination filesystem identity mismatch")
    checkout_identity(checkout, code_sha)


SETTINGS_ADDITION = ("\n# SYNTHETIC LOCAL TEST INPUTS ONLY; not approved commercial limits.\n"
                     + "".join(f"{k}={v}\n" for k, v in SYNTHETIC.items()))


class Staged:
    __slots__ = ("path", "target", "identity", "content", "original")

    def __init__(self, path, target, identity, content, original):
        self.path, self.target, self.identity, self.content = path, target, identity, content
        self.original = original           # The original's fingerprint: (sha256, size, mtime_ns, ctime_ns, nlink).


def _fingerprint(fd):
    """(sha256, size, mtime_ns, ctime_ns, nlink) of an open regular file, its bytes read through the descriptor."""
    st = os.fstat(fd)
    digest_ = hashlib.sha256()
    with os.fdopen(os.dup(fd), "rb") as stream:
        stream.seek(0)
        for block in iter(lambda: stream.read(1 << 16), b""):
            digest_.update(block)
    return digest_.hexdigest(), st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_nlink


def stage_settings(target, addition):
    """The new settings file, complete and durable beside the original, with the original's owner and mode
    (never broader at any moment: created 0600, then given the original's mode); Codex U4. The original must be
    a plain regular file: no symlink, no second hard link, no extended attributes (ACLs) to carry over."""
    target = Path(target)
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise Refused("settings must be a regular file, not a link") from None
    try:
        st = os.fstat(fd)
        require(stat.S_ISREG(st.st_mode) and st.st_nlink == 1, "settings must be a single regular file")
        require(not os.listxattr(fd), "settings carry extended attributes; review by hand")
        with os.fdopen(os.dup(fd), "rb") as stream:
            original = stream.read()
        fingerprint = _fingerprint(fd)
        require(fingerprint[0] == hashlib.sha256(original).hexdigest(), "settings changed while being read")
    finally:
        os.close(fd)
    require(original.endswith(b"\n"), "settings must end with a newline")
    content = original + addition.encode()
    staging = target.with_name(target.name + ".upgrade-new")
    try:
        out = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise Refused("a staged settings file already exists; review it by hand") from None
    try:
        os.fchown(out, st.st_uid, st.st_gid)
        os.fchmod(out, stat.S_IMODE(st.st_mode))
        view = memoryview(content)
        while view:
            view = view[os.write(out, view):]
        os.fsync(out)
    except BaseException:
        os.close(out)
        os.unlink(staging)
        raise
    os.close(out)
    return Staged(staging, target, (st.st_dev, st.st_ino, st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)), content,
                  fingerprint)


def discard_staged(staged):
    try:
        os.unlink(staged.path)
    except FileNotFoundError:
        pass


def commit_settings(staged):
    """Atomically put the staged file in place, only if the original is still exactly what it was staged from:
    the same file (device, inode), owner, mode and single link, no extended attributes, and the same bytes,
    size and times (an in-place edit changes them: Codex U4-R1). Otherwise nothing is replaced: the operator's
    original stays as it is and the staged file is kept for review. Then the rename is made durable.

    The check is the last step before the rename, not a lock: no settings writer may run during the maintenance
    window (UPGRADE.md), which is what serializes them."""
    def refuse():
        raise Refused(f"settings changed since staging; not replaced (original kept as edited; staged file kept "
                      f"for review: {staged.path})")
    try:
        fd = os.open(staged.target, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        refuse()
    try:
        current = os.fstat(fd)
        if not (stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino, current.st_uid, current.st_gid,
                stat.S_IMODE(current.st_mode)) == staged.identity and not os.listxattr(fd)
                and _fingerprint(fd) == staged.original):
            refuse()
    finally:
        os.close(fd)
    st = os.lstat(staged.path)
    require(stat.S_ISREG(st.st_mode) and (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)) == staged.identity[2:]
            and Path(staged.path).read_bytes() == staged.content, "staged settings changed; not replaced")
    os.replace(staged.path, staged.target)
    directory = os.open(staged.target.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("stop", "backup", "apply", "restore", "resume-restore"))
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
    parser.add_argument("--failed-state", type=Path)
    parser.add_argument("--journal", type=Path)
    parser.add_argument("--authorize-live-operation", action="store_true")
    args = parser.parse_args(argv)
    try:
        require(args.authorize_live_operation, "separate owner authorization is required")
        if args.operation == "resume-restore":
            require(args.backup and args.backup_sha256 and args.journal and args.failed_state,
                    "the backup, its recorded checksum, the recovery journal and a NEW failed-state folder required")
            recovery_destination(args.root, args.device, args.inode, args.checkout, args.checkout_sha)
            resume_restore(args.root, args.backup, args.backup_sha256, args.journal, args.failed_state, args.pg_bin)
            print(json.dumps({"restored": True, "application_started": False, "failed_state": str(args.failed_state)}))
            return 0
        _settings, _deployment, uid, gid = destination(args.root, args.device, args.inode, args.activation_release,
                                                      args.checkout, args.checkout_sha)
        if args.operation == "stop":
            stop_install(args.root, args.checkout, args.pg_bin)
        elif args.operation == "backup":
            require(args.backup is not None, "new backup destination required")
            checksum = backup_install(args.root, args.backup, args.activation_release, args.pg_bin, uid, gid)
            print(json.dumps({"backup_manifest_sha256": checksum}))
        elif args.operation == "restore":
            require(args.backup and args.backup_sha256 and args.failed_state,
                    "verified backup and a new failed-state folder required")
            restore_install(args.root, args.backup, args.backup_sha256, args.failed_state, args.pg_bin, uid, gid)
            print(json.dumps({"restored": True, "application_started": False, "failed_state": str(args.failed_state)}))
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
