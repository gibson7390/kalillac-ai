"""Local staging: the complete Kalillac application on this machine (Linux or WSL), for the owner to evaluate.

Run as root (in WSL: `wsl -u root`), with the repository's Python environment, from candidate/:

    python3 -B tools/local_staging.py setup        once: database, provisioning, transition, web root, demo accounts
    python3 -B tools/local_staging.py start        start PostgreSQL, the application and the front server
    python3 -B tools/local_staging.py stop
    python3 -B tools/local_staging.py status       what runs, the URL, the demo accounts
    python3 -B tools/local_staging.py spaces-allow EMAIL   let a local account use Spaces (restarts the application)
    python3 -B tools/local_staging.py refresh-web  after `npm run build` in site/: rebuild the web root (front server only)
    python3 -B tools/local_staging.py reset --yes  delete local staging entirely (its database and files only)

The root (default /var/lib/kalillac-local-staging, or KALILLAC_LOCAL_ROOT) is checked before anything else:
an absolute path with no symlink in it, named kalillac-local-staging (optionally -suffix), every directory
above it root's and writable by no one else (a sticky directory such as /tmp excepted above the parent), so no
other user can rename or exchange any part of the path. Setup creates it with an ownership marker
(.kalillac-local-staging: this deployment, this path and this directory's device and inode); every other
command, and above all `reset`, refuses a root without that exact marker. `reset` deletes through directory
descriptors opened from / without following a link and checked as they are opened, so what it deletes is the
directory it checked, wherever its path points by then.

`stop` signals only the processes `start` launched: each launch records its process's start time, the boot,
a random token in the process's environment, this checkout and the script's absolute path, and the process
(the service user's, in this checkout) is signalled only after all of them match: a reused PID or another
checkout's process never does. Every signal goes through a pidfd, never a numeric PID or group id: the group
with PIDFD_SIGNAL_PROCESS_GROUP (Linux 6.9 and later), or, where the kernel refuses that flag, each member
through its own pidfd while the leader is alive. A stop is confirmed (the process and its group have exited,
by SIGKILL after a grace period if need be) or it fails: the records are kept and `reset` deletes nothing.

Then open http://localhost:8010 (the local panel: http://localhost:8010/__local__/).

What it is: the real application on a real PostgreSQL 16 cluster of its own (port 55433, data under
/var/lib/kalillac-local-staging), provisioned by tools/provision_deployment.py, activated by the V1 operator
transition, started by the controlled launcher, with the staging settings template, exactly as the staging
runbook does on a host (the same sequence tools/verify_provisioned_boot.py proves in CI). Differences, all
deliberate and visible:
* providers without real test credentials are simulated (tools/local_staging_sim.py; the panel and every
  simulated reply say so). Real keys go in /var/lib/kalillac-local-staging/secrets/ (see `status`);
* the commercial values (allowances, budgets, prices, attempt ceilings) are the boot verifier's synthetic
  test inputs, labelled in the settings file, never the owner's decisions;
* the site is served by tools/local_staging_web.py on http://localhost:8010 instead of Nginx and TLS.
It never touches production, any other worktree's data, or the test suites' disposable cluster (port 55432).
"""
from __future__ import annotations

import argparse
import errno
import grp
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import select
import shutil
import signal
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request

CANDIDATE = Path(__file__).resolve().parents[1]
REPO = CANDIDATE.parent
sys.path[:0] = [str(CANDIDATE), str(Path(__file__).resolve().parent)]

DEFAULT_ROOT = Path("/var/lib/kalillac-local-staging")
ROOT = DEFAULT_ROOT                       # Replaced in main() by the checked root (owned_root).
ROOT_OWNER = 0                            # The uid that owns the root and its marker (tests use their own).
MARKER = ".kalillac-local-staging"
ROOT_NAME = re.compile(r"kalillac-local-staging(-[a-z0-9]{1,24})?")
WEB_PORT, APP_PORT, PG_PORT = 8010, 8011, 55433
ORIGIN = f"http://localhost:{WEB_PORT}"
DEPLOYMENT = "kalillac-local-staging"
DATABASE = "kalillac_local"
ROLES = {"owner": "kalillac_local_owner", "runtime": "kalillac_local_runtime", "registration": "kalillac_local_registration",
         "service": "kalillac_local_v1_service", "executor": "kalillac_local_v1_executor",
         "operator": "kalillac_local_v1_operator", "migrate": "kalillac_local_migrate"}
DEMO = {"free": "demo.free@kalillac-local.test", "premium": "demo.premium@kalillac-local.test"}
HEADER = """# KALILLAC LOCAL STAGING: settings for local evaluation on this machine only.
# Every allowance, budget, price and attempt ceiling below is a SYNTHETIC TEST INPUT (the CI boot
# verifier's), not an owner decision and not a proposal. Generated by tools/local_staging.py.
"""


def say(text):
    print(text, flush=True)


def fail(text):
    print("local staging: " + text, file=sys.stderr, flush=True)
    sys.exit(2)


# --- the root: checked before any command touches anything --------------------------------------------

class UnsafeRoot(Exception):
    """The configured root is not local staging's own directory."""


def _trusted_owners():
    return {0, ROOT_OWNER}


def _open_parent(path: Path) -> int:
    """A descriptor of the root's parent, reached from / one component at a time without following a link,
    each directory checked as it is opened: root's (or ROOT_OWNER's) and writable by no one else, so no other
    user can rename, replace or exchange any directory on the way. Above the parent a sticky directory (/tmp)
    is accepted: there only an entry's owner, here a trusted one, can move it. The caller closes it."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        components = path.parent.parts[1:]
        where = Path("/")
        for index in range(len(components) + 1):
            st = os.fstat(fd)
            parent = index == len(components)
            sticky = bool(st.st_mode & stat.S_ISVTX) and not parent
            if st.st_uid not in _trusted_owners() or (st.st_mode & 0o022 and not sticky):
                owners = "root" if ROOT_OWNER == 0 else f"root or uid {ROOT_OWNER}"
                raise UnsafeRoot(f"every directory above the root must belong to {owners} and not be writable by "
                                 f"others: {where}")
            if parent:
                return fd
            where = where / components[index]
            try:
                child = os.open(components[index], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError as error:
                if error.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise UnsafeRoot(f"the root must not be inside a symlink: {where}") from None
                if error.errno == errno.ENOENT:
                    raise UnsafeRoot(f"the root's parent must exist: {path.parent}") from None
                raise
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise


def checked_path(raw) -> Path:
    """The root's path alone: absolute, already normal, named for local staging, no symlink anywhere in it,
    and every directory above it trusted (so nothing can be swapped in anywhere along it)."""
    raw = str(raw or "")
    if not raw.startswith("/") or os.path.normpath(raw) != raw or "\0" in raw:
        raise UnsafeRoot(f"the root must be an absolute, normal path: {raw!r}")
    path = Path(raw)
    if not ROOT_NAME.fullmatch(path.name):
        raise UnsafeRoot(f"the root must be a directory named kalillac-local-staging, not {path}")
    if os.path.realpath(path) != raw:
        raise UnsafeRoot(f"the root must not be, or be inside, a symlink: {path}")
    os.close(_open_parent(path))
    return path


def _expected_marker(path: Path, st):
    return {"deployment": DEPLOYMENT, "root": str(path), "device": st.st_dev, "inode": st.st_ino}


def _directory(path: Path, parent_fd=None):
    try:
        st = os.lstat(path) if parent_fd is None else os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(st.st_mode):
        raise UnsafeRoot(f"the root is not a directory: {path}")
    if st.st_uid != ROOT_OWNER or st.st_mode & 0o022:
        raise UnsafeRoot(f"the root must belong to uid {ROOT_OWNER} and not be writable by others: {path}")
    return st


def read_marker(path: Path, parent_fd=None):
    """The marker's contents, read without following a symlink, or None if there is none."""
    if parent_fd is None:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    else:
        fd = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        try:
            marker = os.open(MARKER, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
        except FileNotFoundError:
            return None
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise UnsafeRoot(f"the ownership marker is a symlink: {path / MARKER}") from None
            raise
        try:
            st = os.fstat(marker)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != ROOT_OWNER or st.st_mode & 0o022 or st.st_size > 4096:
                raise UnsafeRoot(f"the ownership marker is not a file of uid {ROOT_OWNER}: {path / MARKER}")
            data = os.read(marker, 4097)
        finally:
            os.close(marker)
    finally:
        os.close(fd)
    try:
        return json.loads(data)
    except ValueError:
        raise UnsafeRoot(f"the ownership marker is unreadable: {path / MARKER}") from None


def write_marker(path: Path):
    st = _directory(path)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        marker = os.open(MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=fd)
        try:
            os.write(marker, json.dumps(_expected_marker(path, st)).encode())
        finally:
            os.close(marker)
    finally:
        os.close(fd)


def _legacy(path: Path):
    """A root set up before the marker existed: only the default path, holding this deployment's record."""
    if path != DEFAULT_ROOT:
        return False
    try:
        fd = os.open(path / "local.json", os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as record:
            return json.load(record).get("deployment") == DEPLOYMENT
    except (OSError, ValueError, AttributeError):
        return False


def owned_root(raw, *, create=False, adopt=False):
    """The checked root, proven to be local staging's own directory by its marker.

    create: setup may make the directory, or take an empty one, and mark it.
    adopt: a non-destructive command may mark a root set up before markers existed (the default path only).
    Returns the path, or None when it does not exist (and create is False)."""
    path = checked_path(raw)
    st = _directory(path)
    if st is None:
        if not create:
            return None
        os.mkdir(path, 0o755)
        if os.geteuid() == 0:
            os.chown(path, ROOT_OWNER, 0)
        os.chmod(path, 0o755)
        write_marker(path)
        return owned_root(raw)
    marker = read_marker(path)
    if marker is None:
        if (create and not os.listdir(path)) or (adopt and _legacy(path)):
            write_marker(path)
            return owned_root(raw)
        raise UnsafeRoot(f"{path} has no local staging ownership marker ({MARKER}); refusing to use it")
    if marker != _expected_marker(path, st):
        raise UnsafeRoot(f"{path}'s ownership marker belongs to another directory or deployment; refusing to use it")
    return path


def pinned_root(path: Path) -> int:
    """The parent's descriptor, with the root in it proven by its marker through that same descriptor: what
    a destructive operation then does inside it happens to the checked directory, even if a directory on the
    path is renamed or exchanged afterwards. The caller closes it."""
    checked_path(str(path))
    parent_fd = _open_parent(path)
    try:
        st = _directory(path, parent_fd)
        if st is None:
            raise UnsafeRoot(f"{path} no longer exists")
        if read_marker(path, parent_fd) != _expected_marker(path, st):
            raise UnsafeRoot(f"{path} is no longer the marked local staging root")
        return parent_fd
    except BaseException:
        os.close(parent_fd)
        raise


# --- the service identity and paths -------------------------------------------------------------------

def identity():
    owner = os.stat(CANDIDATE)
    if owner.st_uid == 0:
        fail("the checkout must belong to an ordinary user (the service identity), not root")
    return owner.st_uid, owner.st_gid, grp.getgrgid(owner.st_gid).gr_name, pwd.getpwuid(owner.st_uid).pw_dir


def paths():
    return {name: ROOT / name for name in ("pg", "pgsock", "admin", "deployment", "credentials", "state", "secrets",
                                           "run", "webroot")}


def pg_bin(home):
    for candidate in (os.environ.get("KALILLAC_LOCAL_PG_BIN", ""),
                      f"{home}/pg16tools/lib/python3.12/site-packages/pgserver/pginstall/bin", "/usr/lib/postgresql/16/bin"):
        if candidate and Path(candidate, "pg_ctl").is_file():
            return candidate
    fail("PostgreSQL 16 binaries not found (set KALILLAC_LOCAL_PG_BIN)")


def as_user(argv, uid, gid, env=None, **kwargs):
    return subprocess.run(argv, user=uid, group=gid, extra_groups=[], env=env, **kwargs)


def write_secret(path: Path, data: str, mode=0o600, uid=0, gid=0):
    if path.exists():
        path.unlink()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    os.write(fd, data.encode())
    os.fchown(fd, uid, gid)
    os.close(fd)


def read_env(path: Path):
    if not path.is_file():
        return {}
    pairs = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            pairs[key.strip()] = value.strip()
    return pairs


# --- PostgreSQL ---------------------------------------------------------------------------------------

class PgStatusUnknown(Exception):
    """pg_ctl status answered neither "running" (0) nor "not running" (3)."""


def pg_running(p, bindir, uid, gid):
    """True if PostgreSQL runs (pg_ctl status 0), False if it does not (3). Any other answer (1, 4: no
    accessible data directory, an error) establishes nothing and raises PgStatusUnknown."""
    result = as_user([f"{bindir}/pg_ctl", "-D", str(p["pg"]), "status"], uid, gid, capture_output=True)
    if result.returncode == 0:
        return True
    if result.returncode == 3:
        return False
    raise PgStatusUnknown(f"pg_ctl status exited {result.returncode}")


def pg_start(p, bindir, uid, gid):
    try:
        if pg_running(p, bindir, uid, gid):
            return
    except PgStatusUnknown as error:
        fail(f"PostgreSQL's state could not be established ({error}); see {p['state'] / 'postgres.log'}")
    result = as_user([f"{bindir}/pg_ctl", "-D", str(p["pg"]), "-l", str(p["state"] / "postgres.log"), "-w", "-o",
                      f"-p {PG_PORT} -k {p['pgsock']} -c listen_addresses=127.0.0.1 -c max_connections=100",
                      "start"], uid, gid, capture_output=True, text=True)
    if result.returncode:
        fail("PostgreSQL did not start; see " + str(p["state"] / "postgres.log"))


def admin_url(p):
    return (p["admin"] / "admin-url").read_text(encoding="utf-8").strip()


# --- setup --------------------------------------------------------------------------------------------

def setup(args):
    uid, gid, group, home = identity()
    p = paths()
    if (ROOT / "local.json").exists():
        fail(f"already set up ({ROOT}); `reset --yes` first to start again")
    bindir = pg_bin(home)
    for name, owner, mode in (("pg", uid, 0o700), ("pgsock", uid, 0o700), ("admin", 0, 0o700), ("credentials", uid, 0o500),
                              ("state", uid, 0o700), ("secrets", 0, 0o700), ("run", 0, 0o755)):
        p[name].mkdir(mode=mode, exist_ok=True)
        os.chown(p[name], owner, gid if owner else 0)
        os.chmod(p[name], mode)
    for sub in ("files", "locks", "outbox"):
        (p["state"] / sub).mkdir(mode=0o700, exist_ok=True)
        os.chown(p["state"] / sub, uid, gid)

    # Resumable: each completed step is detected and skipped, so a failure late in setup needs no reset.
    say("1/7 PostgreSQL 16: a local cluster of its own on 127.0.0.1:%d" % PG_PORT)
    if not ((p["admin"] / "admin-url").is_file() and (p["pg"] / "PG_VERSION").is_file()):
        password = secrets.token_urlsafe(24)
        pwfile = p["state"] / ".initdb-pw"
        write_secret(pwfile, password, 0o600, uid, gid)
        result = as_user([f"{bindir}/initdb", "-D", str(p["pg"]), "-U", "postgres", f"--pwfile={pwfile}",
                          "--auth-host=scram-sha-256", "--auth-local=trust", "-E", "UTF8", "--locale=C.UTF-8"],
                         uid, gid, capture_output=True, text=True)
        pwfile.unlink()
        if result.returncode:
            fail("initdb failed: " + result.stderr[-500:])
        write_secret(p["admin"] / "admin-url", f"postgresql+psycopg://postgres:{password}@127.0.0.1:{PG_PORT}/postgres")
    pg_start(p, bindir, uid, gid)
    if (p["state"] / "transition.done").is_file():
        say("2-4/7 already provisioned and activated")
        build_and_finish(args, p, (p["state"] / "release.sha").read_text().strip())
        return

    say("2/7 provisioning the deployment (roles, database, migrations, protected files, registration activation)")
    import provision_deployment as provision
    for key, setting in provision.ROLE_SETTINGS.items():
        os.environ[setting] = ROLES[key]
    sha = release_sha()
    code = 0 if (p["deployment"] / "deployment.env").is_file() else provision.main(["provision", "--deployment-id", DEPLOYMENT, "--release-sha", sha, "--database", DATABASE,
                           "--db-host", "127.0.0.1", "--db-port", str(PG_PORT), "--migration-login", ROLES["migrate"],
                           "--admin-url-file", str(p["admin"] / "admin-url"), "--directory", str(p["deployment"]),
                           "--service-group", group, "--apply"])
    if code != 0:
        fail("provisioning refused (exit %d)" % code)

    say("3/7 credentials and local settings (synthetic commercial values, labelled)")
    stripe = read_env(p["secrets"] / "stripe.env")
    real_stripe = stripe.get("STRIPE_SECRET_KEY", "").startswith(("sk_test_", "rk_test_"))
    secret_key = stripe["STRIPE_SECRET_KEY"] if real_stripe else "sk_test_" + secrets.token_hex(16)
    webhook = stripe.get("STRIPE_WEBHOOK_SECRET") if real_stripe else "whsec_" + secrets.token_hex(16)
    if real_stripe and not (webhook or "").startswith("whsec_"):
        fail("secrets/stripe.env has a test key but no STRIPE_WEBHOOK_SECRET (from `stripe listen`)")
    os.chmod(p["credentials"], 0o700)
    for name, value in (("kalillac_db_password", (p["deployment"] / "private" / "kalillac_db_password").read_text()),
                        ("stripe_secret_key", secret_key), ("stripe_webhook_secret", webhook)):
        write_secret(p["credentials"] / name, value, 0o400, uid, gid)
    os.chmod(p["credentials"], 0o500)
    write_settings(p, sha, stripe if real_stripe else {})

    say("4/7 the V1 account-security transition, as the service identity")
    import provision_deployment as provision
    from sqlalchemy import make_url
    provision.RootFiles(str(p["deployment"]), gid).protected(
        "transition-admin-url", make_url(admin_url(p)).set(database=DATABASE).render_as_string(hide_password=False).encode())
    env = service_env(p, home)
    env.update({"KALILLAC_V1_TRANSITION_ADMIN_URL_FILE": str(p["deployment"] / "protected" / "transition-admin-url"),
                "KALILLAC_V1_OPERATOR_RECORD_FILE": str(p["state"] / "operator-record.jsonl")})
    result = as_user([sys.executable, "-B", "tools/v1_security_transition.py", "activate", "--release-sha", sha, "--apply"],
                     uid, gid, env=env, cwd=CANDIDATE, capture_output=True, text=True, timeout=300)
    (p["deployment"] / "protected" / "transition-admin-url").unlink()
    if result.returncode != 0 or '"to": "active"' not in result.stdout:
        fail("the operator transition did not activate:\n" + (result.stdout + result.stderr)[-1500:])
    (p["state"] / "release.sha").write_text(sha)
    (p["state"] / "transition.done").write_text("active\n")
    build_and_finish(args, p, sha)


def build_and_finish(args, p, sha):
    say("5/7 the web root (the built site and the chat, marked for accounts, files, images, reasoning and Spaces)")
    build_webroot(p)
    real_stripe = read_env(p["secrets"] / "stripe.env").get("STRIPE_SECRET_KEY", "").startswith(("sk_test_", "rk_test_"))
    (ROOT / "local.json").write_text(json.dumps({"deployment": DEPLOYMENT, "release_sha": sha, "web": ORIGIN,
                                                 "stripe": "real test mode" if real_stripe else "simulated"}, indent=1))
    say("6/7 starting")
    start(args)
    say("7/7 synthetic demo accounts, through the real registration, sign-in and billing flows")
    seed(p)
    status(args)


def release_sha():
    """The checkout's commit. A Windows worktree's .git file names a Windows path that Linux git cannot
    follow, so the HEAD and refs are read directly when git itself cannot."""
    result = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(REPO), "rev-parse", "HEAD"],
                            capture_output=True, text=True)
    if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", result.stdout.strip()):
        return result.stdout.strip()

    def linux(path):
        match = re.match(r"([A-Za-z]):[/\\](.*)", path.strip())
        return Path(f"/mnt/{match.group(1).lower()}/{match.group(2)}".replace("\\", "/")) if match else Path(path.strip())
    dotgit = REPO / ".git"
    gitdir = linux(dotgit.read_text().split("gitdir:", 1)[1]) if dotgit.is_file() else dotgit
    common = gitdir / (gitdir / "commondir").read_text().strip() if (gitdir / "commondir").is_file() else gitdir
    head = (gitdir / "HEAD").read_text().strip()
    if re.fullmatch(r"[0-9a-f]{40}", head):
        return head
    ref = head.split("ref:", 1)[1].strip()
    for base in (gitdir, common):
        if (base / ref).is_file():
            return (base / ref).read_text().strip()
    for line in (common / "packed-refs").read_text().splitlines():
        if line.endswith(" " + ref):
            return line.split()[0]
    fail("cannot determine the checkout's commit")


def build_webroot(p):
    site = REPO / "site" / "dist" / "public"
    if not (site / "index.html").is_file() or not (site / "account.html").is_file():
        fail("the site is not built: run `npm run build` in site/ first")
    newest = max(f.stat().st_mtime for f in (REPO / "site" / "src").rglob("*") if f.is_file())
    if (site / "index.html").stat().st_mtime < newest:
        fail("site/dist is older than site/src: run `npm run build` in site/ first")
    if p["webroot"].exists():
        shutil.rmtree(p["webroot"])
    sys.path.insert(0, str(REPO / "deploy" / "staging"))
    import build_webroot as builder
    builder.build(site, REPO / "frontend", p["webroot"], ["accounts", "files", "images", "reasoning", "spaces"])
    # Copied from a Windows drive every mode is 0777: the web root is readable, and writable only by its owner.
    for folder, _dirs, files in os.walk(p["webroot"]):
        os.chmod(folder, 0o755)
        for name in files:
            os.chmod(os.path.join(folder, name), 0o644, follow_symlinks=False)


def write_settings(p, sha, stripe):
    from verify_provisioned_boot import template_settings
    settings = template_settings(str(p["state"] / "files"))
    settings.update({
        "KALILLAC_SITE_ORIGIN": ORIGIN, "KALILLAC_RELEASE_SHA": sha,
        "KALILLAC_ACCOUNT_REGISTRATION_URL": ORIGIN + "/account/create/confirm",
        "KALILLAC_ACCOUNT_RECOVERY_URL": ORIGIN + "/account/recovery/confirm",
        "KALILLAC_BILLING_SUCCESS_URL": ORIGIN + "/account?checkout=success",
        "KALILLAC_BILLING_CANCEL_URL": ORIGIN + "/account?checkout=cancelled",
        "KALILLAC_BILLING_PORTAL_RETURN_URL": ORIGIN + "/account",
        "KALILLAC_EMAIL_FROM": "accounts@kalillac-local.test",
    })
    if stripe.get("STRIPE_PRICE_ID"):
        settings["KALILLAC_STRIPE_PRICE_ID"] = settings["KALILLAC_STRIPE_ALLOWED_PRICE_IDS"] = stripe["STRIPE_PRICE_ID"]
    beta = read_env(ROOT / "settings.env").get("KALILLAC_SPACES_BETA_ACCOUNT_IDS")
    if beta:
        settings["KALILLAC_SPACES_BETA_ACCOUNT_IDS"] = beta
    text = HEADER + "".join(f"{key}={value}\n" for key, value in settings.items())
    (ROOT / "settings.env").write_text(text, encoding="utf-8")
    os.chmod(ROOT / "settings.env", 0o644)


def provider_keys(p):
    keys = {"OPENAI_API_KEY": "synthetic-not-a-key", "BRAVE_API_KEY": "synthetic-not-a-key",
            "TAVILY_API_KEY": "synthetic-not-a-key", "KALILLAC_RESEND_API_KEY": "re_synthetic_local"}
    real = read_env(p["secrets"] / "provider-keys.env")
    simulated = {"openai", "brave", "tavily", "email"}
    for key, name in (("OPENAI_API_KEY", "openai"), ("BRAVE_API_KEY", "brave"), ("TAVILY_API_KEY", "tavily"),
                      ("KALILLAC_RESEND_API_KEY", "email")):
        if real.get(key):
            keys[key] = real[key]
            simulated.discard(name)
    stripe = json.loads((ROOT / "local.json").read_text())["stripe"] if (ROOT / "local.json").exists() else "simulated"
    if stripe == "simulated" or not read_env(p["secrets"] / "stripe.env"):
        simulated.add("stripe")
    order = ["openai", "brave", "tavily", "stripe", "email"]
    return keys, ",".join(name for name in order if name in simulated)


def service_env(p, home):
    deployment = read_env(p["deployment"] / "deployment.env")
    keys, simulated = provider_keys(p)
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(p["state"]), "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1", "CREDENTIALS_DIRECTORY": str(p["credentials"]),
            **deployment, **read_env(ROOT / "settings.env"), **keys,
            "KALILLAC_SINGLETON_LOCK_DIRECTORY": str(p["state"] / "locks"),
            "KALILLAC_LOCAL_STATE": str(p["state"]), "KALILLAC_LOCAL_SIMULATE": simulated,
            "KALILLAC_LOCAL_WEBHOOK_SECRET_FILE": str(p["credentials"] / "stripe_webhook_secret")}


# --- processes ----------------------------------------------------------------------------------------

SCRIPTS = {"app": "tools/local_staging_app.py", "web": "tools/local_staging_web.py"}
INSTANCE = "KALILLAC_LOCAL_INSTANCE"


def _checkout():
    return os.path.realpath(CANDIDATE)


def _script(name) -> str:
    return os.path.join(_checkout(), SCRIPTS[name])


def _boot():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def _started(pid):
    """The process's start time in clock ticks since boot (/proc/PID/stat field 22): a reused PID has another."""
    return _stat(pid)[3]


def _stat(pid):
    """(state, process group, session, start time) from /proc/PID/stat."""
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return fields[0], int(fields[2]), int(fields[3]), int(fields[19])


def _run_file(p, filename):
    """A file in run/ (root's, not a link), opened, with its status; None if it is absent or not trusted."""
    try:
        fd = os.open(p["run"] / filename, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    handle = os.fdopen(fd)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid not in _trusted_owners() or st.st_mode & 0o022:
        handle.close()
        return None
    return handle, st


def _record(p, name):
    """What `start` recorded for that process (run/NAME.json), or what an older start left (run/NAME.pid: only
    the PID and the time the file was written), or None."""
    opened = _run_file(p, f"{name}.json")
    if opened:
        handle, _st = opened
        with handle:
            try:
                record = json.load(handle)
            except ValueError:
                return None
        return record if isinstance(record, dict) and type(record.get("pid")) is int else None
    opened = _run_file(p, f"{name}.pid")
    if opened:
        handle, st = opened
        with handle:
            try:
                return {"pid": int(handle.read().strip()), "written": st.st_mtime, "legacy": True}
            except ValueError:
                return None
    return None


def _ours(record, name, uid):
    """True only if the process the record names is still the one `start` launched, for this deployment and
    this checkout: the service user's, leading its own group, in this checkout running this checkout's script,
    started at the recorded time on this boot (not a reused PID), with the launch's token in its environment."""
    return _identity(record, name, uid) == "ours"


def _identity(record, name, uid):
    """"ours"; "other" (gone, or its PID now another process's); or "unknown" (it could not be read)."""
    try:
        pid = record["pid"]
        if type(pid) is not int or pid <= 1:
            return "other"
        proc = Path(f"/proc/{pid}")
        if os.stat(proc).st_uid != uid or os.getpgid(pid) != pid or os.readlink(proc / "cwd") != _checkout():
            return "other"
        argv = [os.fsdecode(arg) for arg in (proc / "cmdline").read_bytes().split(b"\0")]
        if record.get("legacy"):
            # Launched as `python -B tools/NAME.py` from this checkout; started no later than its PID file was
            # written and no earlier than two minutes before (a PID reused since then started after it).
            started_at = (time.time() - float(Path("/proc/uptime").read_text().split()[0])
                          + _started(pid) / os.sysconf("SC_CLK_TCK"))
            matches = SCRIPTS[name] in argv[1:3] and record["written"] - 120 <= started_at <= record["written"] + 2
            return "ours" if matches else "other"
        token = record.get("token")
        environ = (proc / "environ").read_bytes().split(b"\0")
        matches = (record.get("script") == _script(name) and _script(name) in argv[1:3]
                   and record.get("boot") == _boot() and record.get("started") == _started(pid)
                   and isinstance(token, str) and re.fullmatch(r"[0-9a-f]{32}", token) is not None
                   and f"{INSTANCE}={token}".encode() in environ)
        return "ours" if matches else "other"
    except (FileNotFoundError, ProcessLookupError, KeyError, ValueError, TypeError, IndexError):
        return "other"
    except OSError:
        return "unknown"


def _running(p, name, uid=None):
    """The PID of local staging's own process of that name, or None."""
    uid = identity()[0] if uid is None else uid
    record = _record(p, name)
    return record["pid"] if record and _ours(record, name, uid) else None


PIDFD_SIGNAL_PROCESS_GROUP = 1 << 2      # include/uapi/linux/pidfd.h, Linux 6.9 and later (EINVAL before).
STOP_GRACE = 30.0                        # Seconds for SIGTERM to end a process and its group...
KILL_WAIT = 10.0                         # ...then for SIGKILL to, before the stop is declared failed.


class StopFailed(Exception):
    """A process of local staging could not be confirmed stopped: its records are kept, nothing is deleted."""


def _members(leader, since):
    """The other live processes of the group a leader started (process group and session both the leader's
    PID, started no earlier than it). Only proof of identity while the leader is alive: its PID, the group's
    and the session's id, cannot be reused until it exits."""
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == leader:
            continue
        try:
            state, group, session, started = _stat(int(entry))
        except (OSError, ValueError, IndexError):
            continue
        if state != "Z" and group == leader and session == leader and started >= since:
            found.append(int(entry))
    return found


def _signal_member(pid, leader_fd, leader, since, sig):
    """Signal one member of the leader's group through its own pidfd, only if, read while that pidfd and the
    leader's both show their processes alive, it is in the leader's group. True if it was signalled."""
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        return False
    try:
        _state, group, session, started = _stat(pid)
        signal.pidfd_send_signal(fd, 0)            # Alive after the read: the read was this process's...
        signal.pidfd_send_signal(leader_fd, 0)     # ...and the leader alive: the group's id was still its own.
        if group != leader or session != leader or started < since:
            return False
        signal.pidfd_send_signal(fd, sig)
        return True
    except (ProcessLookupError, FileNotFoundError):
        return False
    finally:
        os.close(fd)


def _signal_group(leader_fd, leader, since, sig):
    """Signal a live leader's group through pidfds only. With PIDFD_SIGNAL_PROCESS_GROUP the kernel signals
    the group of the process the pidfd names, at once. A kernel that refuses the flag (EINVAL, before 6.9):
    each member through its own pidfd, then the leader. Never a numeric kill() or killpg()."""
    try:
        signal.pidfd_send_signal(leader_fd, sig, None, PIDFD_SIGNAL_PROCESS_GROUP)
        return
    except ProcessLookupError:
        return                                     # The leader has exited: _wait_gone decides the rest.
    except OSError as error:
        if error.errno != errno.EINVAL:
            raise
    for pid in _members(leader, since):
        _signal_member(pid, leader_fd, leader, since, sig)
    try:
        signal.pidfd_send_signal(leader_fd, sig)
    except ProcessLookupError:
        pass


def _wait_gone(leader_fd, leader, since, timeout):
    """True once the leader has exited (its pidfd readable) and its group has no live member left."""
    deadline = time.monotonic() + timeout
    readable, _w, _x = select.select([leader_fd], [], [], timeout)
    if not readable:
        return False
    while _members(leader, since):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def _exited(leader_fd):
    """Whether the process a pidfd names has exited (readable at once), reaped or a zombie."""
    return bool(select.select([leader_fd], [], [], 0)[0])


def _leaderless(record, pid, name):
    """A leader that has exited: "absent" only if no live member of its group is left, else StopFailed (with
    the members' PIDs, which are not signalled: without a live leader the group's id proves nothing)."""
    started = record.get("started")
    since = started if (not record.get("legacy") and type(started) is int and record.get("boot") == _boot()) else 0
    left = _members(pid, since)
    if left:
        raise StopFailed(f"the {name} process {pid} has exited but its group still runs (PIDs "
                         f"{', '.join(map(str, left))}); stop them by hand")
    return "absent"


def _terminate(p, name, uid):
    """Stop local staging's own process of that name and its group, and confirm it. Returns "stopped", or
    "absent" when no process of ours runs under that record; raises StopFailed when that cannot be confirmed
    (no pidfd, an unreadable identity, a process that outlives SIGTERM and SIGKILL, or a group whose leader
    has exited). The pidfd is taken before the identity is checked, so the check and every signal concern the
    same process."""
    record = _record(p, name)
    if not record:
        return "absent"
    pid, since = record["pid"], None
    try:
        leader_fd = os.pidfd_open(pid)
    except ProcessLookupError:
        leader_fd = None
    except (OSError, AttributeError) as error:
        raise StopFailed(f"no process handle (pidfd) for the {name} process {pid}: {error}") from None
    try:
        # The leader has exited, whether reaped (no pidfd) or not yet (a zombie: its pidfd is readable and its
        # identity unreadable). Its group may live on, and is then neither signalled nor declared stopped.
        if leader_fd is None or _exited(leader_fd):
            return _leaderless(record, pid, name)
        state = _identity(record, name, uid)
        if state == "unknown":
            raise StopFailed(f"cannot read the {name} process {pid}'s identity")
        if state != "ours":
            if _exited(leader_fd):                 # It exited while being read.
                return _leaderless(record, pid, name)
            return "absent"                        # A live process that is not ours holds the PID: ours has ended.
        since = record.get("started") if not record.get("legacy") else None
        if type(since) is not int:
            since = _started(pid)
        signal.pidfd_send_signal(leader_fd, 0)     # Still the checked process (the start time read was its own).
        _signal_group(leader_fd, pid, since, signal.SIGTERM)
        if _wait_gone(leader_fd, pid, since, STOP_GRACE):
            return "stopped"
        say(f"the {name} process {pid} did not stop within {STOP_GRACE:g}s of SIGTERM; sending SIGKILL")
        try:
            signal.pidfd_send_signal(leader_fd, 0)
            _signal_group(leader_fd, pid, since, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if _wait_gone(leader_fd, pid, since, KILL_WAIT):
            return "stopped"
        raise StopFailed(f"the {name} process {pid} (or its group) is still running after SIGKILL")
    except (ProcessLookupError, FileNotFoundError):
        # It exited between the check and a signal: confirmed only if nothing of its group is left.
        if _members(pid, since if since is not None else 0):
            raise StopFailed(f"the {name} process {pid} has exited but its group still runs") from None
        return "absent"
    finally:
        if leader_fd is not None:
            os.close(leader_fd)


def _spawn(name, extra, p, uid, gid, env):
    log = open(p["state"] / f"{name}.log", "ab")
    os.chown(p["state"] / f"{name}.log", uid, gid)
    token = secrets.token_hex(16)
    drop = {} if os.geteuid() == uid else {"user": uid, "group": gid, "extra_groups": []}
    process = subprocess.Popen([sys.executable, "-B", _script(name), *extra], cwd=_checkout(),
                               env={**env, INSTANCE: token}, stdout=log, stderr=subprocess.STDOUT,
                               start_new_session=True, **drop)
    try:
        started = _started(process.pid)
    except OSError:
        started = None                                 # Exited at once: a record that matches nothing.
    for stale in (f"{name}.json", f"{name}.pid"):
        (p["run"] / stale).unlink(missing_ok=True)
    fd = os.open(p["run"] / f"{name}.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump({"pid": process.pid, "started": started, "boot": _boot(), "token": token,
                   "script": _script(name), "checkout": _checkout()}, handle)
    return process


def _get(url, timeout=3):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as reply:
            return reply.status, reply.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except OSError:
        return None, b""


def start(args):
    uid, gid, group, home = identity()
    p = paths()
    if not (ROOT / "local.json").exists():
        fail("not set up yet: run `setup` first")
    pg_start(p, pg_bin(home), uid, gid)
    env = service_env(p, home)
    if not _running(p, "app", uid):
        _spawn("app", ["--host", "127.0.0.1", "--port", str(APP_PORT)], p, uid, gid, env)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if _get(f"http://127.0.0.1:{APP_PORT}/api/health")[0] == 200:
                break
            if not _running(p, "app", uid):
                fail("the application stopped while starting; see " + str(p["state"] / "app.log"))
            time.sleep(1)
        else:
            fail("the application did not answer in time; see " + str(p["state"] / "app.log"))
    if not _running(p, "web", uid):
        _spawn("web", ["--webroot", str(p["webroot"]), "--upstream", f"http://127.0.0.1:{APP_PORT}",
                       "--port", str(WEB_PORT)], p, uid, gid, env)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and _get(f"http://127.0.0.1:{WEB_PORT}/")[0] != 200:
            time.sleep(0.5)
    say(f"running: {ORIGIN}  (local panel: {ORIGIN}/__local__/)")


def stop(args):
    uid, gid, group, home = identity()
    p = paths()
    failures = []
    for name in ("web", "app"):
        try:
            _terminate(p, name, uid)
        except StopFailed as error:
            failures.append(str(error))
            continue                                   # Its records stay: the next stop tries again.
        for stale in (f"{name}.json", f"{name}.pid"):
            (p["run"] / stale).unlink(missing_ok=True)
    if p["pg"].exists():
        # Stopped only on pg_ctl's own word: status 3, before or after `stop`. Any other answer fails closed.
        try:
            if pg_running(p, pg_bin(home), uid, gid):
                result = as_user([f"{pg_bin(home)}/pg_ctl", "-D", str(p["pg"]), "stop", "-m", "fast"], uid, gid,
                                 capture_output=True)
                if result.returncode != 0:
                    failures.append(f"PostgreSQL did not stop (pg_ctl stop exited {result.returncode})")
                elif pg_running(p, pg_bin(home), uid, gid):
                    failures.append("PostgreSQL still runs after pg_ctl stop")
        except PgStatusUnknown as error:
            failures.append(f"PostgreSQL's state could not be established ({error})")
    if failures:
        fail("could not confirm that local staging stopped:\n  " + "\n  ".join(failures)
             + "\nIts process records are kept and nothing was deleted.")
    say("stopped")


def status(args):
    p = paths()
    if not (ROOT / "local.json").exists():
        say("not set up")
        return
    info = json.loads((ROOT / "local.json").read_text())
    _keys, simulated = provider_keys(p)
    say(f"Kalillac local staging, release {info['release_sha'][:12]}, deployment {info['deployment']}")
    say(f"  application: {'running' if _running(p, 'app') else 'stopped'};  "
        f"front server: {'running' if _running(p, 'web') else 'stopped'}")
    say(f"  open:  {ORIGIN}      local panel: {ORIGIN}/__local__/")
    say(f"  simulated providers: {simulated or 'none'}  (real keys: {p['secrets']}/provider-keys.env, stripe.env)")
    demo = ROOT / "demo-accounts.txt"
    if demo.is_file():
        say("  synthetic demo accounts:\n" + demo.read_text())


# --- demo accounts, Spaces ----------------------------------------------------------------------------

def _http(method, path, body=None, cookie=None, extra=None, base=ORIGIN):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Origin": ORIGIN, "Accept": "application/json", **(extra or {})}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if cookie:
        headers["Cookie"] = cookie
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as reply:
            return reply.status, reply.headers, reply.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()


def register(email, password):
    import local_staging_sim as sim
    nonce = secrets.token_hex(16)
    status_, _h, body = _http("GET", "/api/account/registration-contract", extra={"X-Kalillac-Registration-Nonce": nonce})
    if status_ != 200:
        fail("registration contract unavailable: %s" % status_)
    before = {m.get("at") for m in sim.outbox()}
    status_, _h, body = _http("POST", "/api/account/register", {"email": email})
    if status_ != 202:
        fail(f"registration request refused: {status_} {body[:200]!r}")
    for _ in range(60):
        mails = [m for m in sim.outbox() if m.get("to") == email and m.get("at") not in before]
        if mails:
            break
        time.sleep(0.5)
    else:
        fail("no registration email arrived in the outbox")
    token = re.search(r"#token=([A-Za-z0-9_-]+)", mails[0]["text"]).group(1)
    status_, _h, body = _http("POST", "/api/account/register/confirm", {"token": token, "password": password})
    if status_ != 200:
        fail(f"registration confirmation refused: {status_} {body[:200]!r}")


def sign_in(email, password):
    status_, headers, body = _http("POST", "/api/account/login", {"email": email, "password": password})
    if status_ != 200:
        fail(f"sign-in refused: {status_}")
    cookie = headers.get("Set-Cookie", "").split(";", 1)[0]
    return cookie


def seed(p):
    import local_staging_sim as sim
    os.environ["KALILLAC_LOCAL_STATE"] = str(p["state"])
    lines = []
    for tier, email in DEMO.items():
        password = "local-" + secrets.token_urlsafe(12)
        register(email, password)
        cookie = sign_in(email, password)
        if tier == "premium":
            status_, _h, body = _http("POST", "/api/account/billing/checkout", cookie=cookie)
            url = json.loads(body).get("checkout_url", "") if status_ == 200 else ""
            if "/c/pay/cs_test_sim_" in url:
                _success, events = sim.complete_checkout(url.rsplit("/", 1)[-1])
                secret = sim.read_secret(p["credentials"] / "stripe_webhook_secret")
                for event in events:
                    raw, signature = sim.signed(event, secret)
                    code, _h, reply = _post_raw(raw, signature)
                    if code != 200:
                        fail(f"the simulated {event['type']} was refused: {code} {reply[:200]!r}")
            else:
                say("  (Stripe is in real test mode: upgrade the premium demo account in the browser)")
        status_, _h, body = _http("GET", "/api/account/entitlements", cookie=cookie)
        tier_now = json.loads(body).get("entitlements", {}).get("tier") if status_ == 200 else "unknown"
        lines.append(f"    {email}   password: {password}   plan after setup: {tier_now}")
    demo = ROOT / "demo-accounts.txt"
    write_secret(demo, "\n".join(lines) + "\n", 0o600)
    allow_spaces(list(DEMO.values()), restart=True)


def _post_raw(raw, signature):
    request = urllib.request.Request(f"http://127.0.0.1:{APP_PORT}/api/billing/stripe/webhook", data=raw, method="POST",
                                     headers={"Stripe-Signature": signature, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as reply:
            return reply.status, reply.headers, reply.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()


def allow_spaces(emails, restart=True):
    from sqlalchemy import create_engine, make_url, text
    p = paths()
    engine = create_engine(make_url(admin_url(p)).set(database=DATABASE), hide_parameters=True)
    with engine.connect() as connection:
        ids = []
        for email in emails:
            row = connection.execute(text("SELECT id FROM kalillac.users WHERE email = :e"), {"e": email.lower()}).first()
            if row is None:
                fail(f"no local account for {email}")
            ids.append(str(row[0]))
    engine.dispose()
    settings = read_env(ROOT / "settings.env")
    current = [i for i in settings.get("KALILLAC_SPACES_BETA_ACCOUNT_IDS", "").split(",")
               if i and i != "00000000-0000-4000-8000-000000000001"]
    merged = ",".join(dict.fromkeys(current + ids))
    lines = (ROOT / "settings.env").read_text().splitlines()
    lines = [f"KALILLAC_SPACES_BETA_ACCOUNT_IDS={merged}" if line.startswith("KALILLAC_SPACES_BETA_ACCOUNT_IDS=") else line
             for line in lines]
    (ROOT / "settings.env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    say(f"Spaces allowed for: {', '.join(emails)}")
    if restart:
        stop(None)
        start(None)


def spaces_allow(args):
    allow_spaces([args.email])


def refresh_web(args):
    """After `npm run build` in site/: the web root again from the current build, the front server restarted.
    The application and its data are not touched."""
    uid, gid, group, home = identity()
    p = paths()
    try:
        _terminate(p, "web", uid)
    except StopFailed as error:
        fail(f"{error}. The front server's record is kept; the web root was not rebuilt.")
    for stale in ("web.json", "web.pid"):
        (p["run"] / stale).unlink(missing_ok=True)
    build_webroot(p)
    say("web root rebuilt from the current site build")
    start(args)


def reset(args):
    if not args.yes:
        fail("this deletes local staging's database and files; repeat with --yes")
    if not os.path.lexists(ROOT):
        say(f"nothing to remove: {ROOT} does not exist")
        return
    if not shutil.rmtree.avoids_symlink_attacks or sys.version_info < (3, 11):
        fail("this platform's recursive delete cannot be bound to a directory descriptor; remove it by hand")
    stop(None)
    try:
        parent_fd = pinned_root(ROOT)     # After stopping: the marked directory, held by descriptor.
    except UnsafeRoot as error:
        fail(f"{error}. Local staging was stopped; nothing was deleted.")
    try:
        # Relative to the held parent: a directory renamed or exchanged anywhere on the path since cannot
        # redirect it, and rmtree itself checks that the entry it opens is the one it examined.
        shutil.rmtree(ROOT.name, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)
    say(f"removed {ROOT}")


def _running_as_root():
    return sys.platform == "linux" and os.geteuid() == 0


def main(argv=None):
    if not _running_as_root():
        fail("run as root on Linux or WSL (wsl -u root ...)")
    parser = argparse.ArgumentParser(prog="local_staging.py")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, handler in (("setup", setup), ("start", start), ("stop", stop), ("status", status),
                          ("seed", lambda args: seed(paths())), ("refresh-web", refresh_web)):
        sub.add_parser(name).set_defaults(handler=handler)
    allow = sub.add_parser("spaces-allow")
    allow.add_argument("email")
    allow.set_defaults(handler=spaces_allow)
    remove = sub.add_parser("reset")
    remove.add_argument("--yes", action="store_true")
    remove.set_defaults(handler=reset)
    args = parser.parse_args(argv)
    global ROOT
    raw = os.environ.get("KALILLAC_LOCAL_ROOT", str(DEFAULT_ROOT))
    try:
        # Setup may create or mark an empty directory; reset never adopts an unmarked one.
        root = owned_root(raw, create=args.command == "setup", adopt=args.command not in ("setup", "reset"))
    except UnsafeRoot as error:
        fail(f"{error}. Nothing was stopped or deleted.")
    ROOT = root or Path(raw)
    args.handler(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
