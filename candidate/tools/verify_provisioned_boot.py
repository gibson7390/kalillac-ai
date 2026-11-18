"""Prove that a deployment made by tools/provision_deployment.py starts the real application.

Run as root only in an isolated test environment (CI, or a development machine's own disposable
PostgreSQL). Everything is synthetic and removed afterwards: one uniquely named /run fixture, one
database and seven roles with unique test names on the loopback cluster named by
KALILLAC_REGISTRATION_TEST_POSTGRES_URL. It never reads deployment files and contacts no provider.

The sequence is the staging runbook's, with the real commands:
  1. provision_deployment.py provision --apply, as root, writing real root-owned files;
  2. v1_security_transition.py activate --apply, as the unprivileged service identity, reading its
     credentials through the strict protected-file loader;
  3. the controlled launcher (kalillac_server.py) as the service identity, with its environment built
     from deploy/staging/settings.env.template (every placeholder filled with a synthetic value) and
     the generated deployment.env, its secrets in a credentials directory as systemd would provide;
  4. real HTTP against the running process.
The last line printed is a JSON summary: {"tests", "failures", "synthetic_only", "results"}.
"""
from __future__ import annotations

import grp
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

CANDIDATE = Path(__file__).resolve().parents[1]
TEMPLATE = CANDIDATE.parent / "deploy" / "staging" / "settings.env.template"
sys.path[:0] = [str(CANDIDATE), str(Path(__file__).resolve().parent)]

import provision_deployment as provision  # noqa: E402

DEPLOYMENT = "staging-boot-test"
RELEASE = "c" * 40
HOSTNAME = "staging.invalid"
# Synthetic values for every placeholder of the template. They are test inputs, not proposals: the
# allowances, budgets and prices of a real staging environment are the owner's and the providers'.
SYNTHETIC = {
    "HOST_STAGING_HOSTNAME": HOSTNAME, "HOST_RELEASE_SHA": RELEASE, "HOST_EMAIL_SENDER": "accounts@staging.invalid",
    "OWNER_FREE_DAILY_RESPONSES": "20", "OWNER_PAID_DAILY_RESPONSES": "200",
    "OWNER_FILES_DELETE_AFTER_EXPIRY_DAYS": "0",
    **{f"OWNER_{p}_{k}": v for p in ("OPENAI", "BRAVE", "TAVILY")
       for k, v in (("TOTAL_ATTEMPTS", "1000"), ("ANONYMOUS_ATTEMPTS", "100"), ("PERIOD_SECONDS", "86400"))},
    "PRICE_OPENAI_INPUT": "1.25", "PRICE_OPENAI_CACHED_INPUT": "0.125", "PRICE_OPENAI_CACHE_WRITE": "1.25",
    "PRICE_OPENAI_OUTPUT": "10", "PRICE_OPENAI_LONG_CONTEXT_THRESHOLD_OR_NONE": "none",
    "PRICE_OPENAI_INPUT_OVERHEAD_TOKENS": "8192", "PRICE_OPENAI_IMAGE_INPUT_TOKENS": "3000",
    "PRICE_BRAVE_REQUEST": "0.005", "PRICE_TAVILY_CREDIT": "0.008",
    "PRICE_TAVILY_CREDITS_SEARCH_BASIC": "1", "PRICE_TAVILY_CREDITS_SEARCH_ADVANCED": "2",
    "PRICE_TAVILY_CREDITS_EXTRACT_BASIC": "1", "PRICE_TAVILY_CREDITS_EXTRACT_ADVANCED": "2",
    "OWNER_DAY_GLOBAL_USD": "100", "OWNER_DAY_FREE_USD": "20", "OWNER_DAY_PAID_USD": "70",
    "OWNER_MONTH_GLOBAL_USD": "1000", "OWNER_MONTH_FREE_USD": "200", "OWNER_MONTH_PAID_USD": "700",
    "OWNER_MIN_HEADROOM_PERCENT": "10",
    "OWNER_DAY_OPENAI_USD": "100", "OWNER_DAY_BRAVE_USD": "100", "OWNER_DAY_TAVILY_USD": "100",
    "OWNER_MONTH_OPENAI_USD": "1000", "OWNER_MONTH_BRAVE_USD": "1000", "OWNER_MONTH_TAVILY_USD": "1000",
    "OWNER_MAX_REQUEST_FREE_USD": "1", "OWNER_MAX_REQUEST_PAID_USD": "5",
    "OWNER_BALANCED_EXTRA_OUTPUT_TOKENS": "0", "OWNER_DEEP_EXTRA_OUTPUT_TOKENS": "8000",
    "OWNER_FREE_REASONING_MODES": "quick,balanced", "OWNER_PAID_REASONING_MODES": "quick,balanced,deep",
    "OWNER_STRIPE_TEST_PRICE_ID": "price_synthetic0000000001",
    "OWNER_SPACES_BETA_ACCOUNT_IDS": "00000000-0000-4000-8000-000000000001",
    "PROD_REQUEST_DEADLINE_SECONDS": "60",
    "PROD_REQUEST_QUEUE_WAIT_SECONDS": "5",
    "PROD_REQUEST_MAX_MODEL_ATTEMPTS": "6",
    "PROD_REQUEST_MAX_SEARCH_ATTEMPTS": "6",
    "PROD_TRANSPORT_MAX_OUTSTANDING": "4",
    "PROD_TRANSPORT_DNS_THREADS": "1",
    "PROD_TRANSPORT_MAX_PENDING_DNS": "4",
    "PROD_TRANSPORT_CANCEL_POLL_INTERVAL_SECONDS": "0.02",
    "PROD_TRANSPORT_BACKSTOP_GRACE_SECONDS": "0.5",
    "PROD_TRANSPORT_CLEANUP_GRACE_SECONDS": "1",
    "PROD_TRANSPORT_CLOSE_TIMEOUT_SECONDS": "3",
    "PROD_OPENAI_MAX_BYTES": "2097152",
    "PROD_TAVILY_TRANSPORT_MAX_OUTSTANDING": "3",
    "PROD_TAVILY_TRANSPORT_DNS_THREADS": "1",
    "PROD_TAVILY_TRANSPORT_MAX_PENDING_DNS": "3",
    "PROD_TAVILY_TRANSPORT_CANCEL_POLL_INTERVAL_SECONDS": "0.02",
    "PROD_TAVILY_TRANSPORT_BACKSTOP_GRACE_SECONDS": "0.5",
    "PROD_TAVILY_TRANSPORT_CLEANUP_GRACE_SECONDS": "1",
    "PROD_TAVILY_TRANSPORT_CLOSE_TIMEOUT_SECONDS": "2",
    "PROD_TAVILY_SEARCH_MAX_BYTES": "262144",
    "PROD_TAVILY_EXTRACT_MAX_BYTES": "524288",
}
PLACEHOLDER = re.compile(r"<([A-Z0-9_]+)>")


def template_settings(files_root):
    """The template's settings with every placeholder filled; raises on one this file does not know."""
    values = {**SYNTHETIC, "HOST_FILES_ROOT": files_root}
    settings = {}
    for line in TEMPLATE.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        settings[key] = PLACEHOLDER.sub(lambda match: values[match.group(1)], value)
        assert "<" not in settings[key] and key not in ("OPENAI_API_KEY", "BRAVE_API_KEY", "TAVILY_API_KEY")
    return settings


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def http(port, method, path, body=None):
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Origin": "https://" + HOSTNAME, "Content-Type": "application/json",
                                              "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw)
        except ValueError:
            return error.code, None


def main():
    assert sys.platform == "linux" and os.geteuid() == 0, "isolated root fixture required"
    from sqlalchemy import create_engine, make_url, text
    admin_url = make_url(os.environ["KALILLAC_REGISTRATION_TEST_POSTGRES_URL"])
    assert admin_url.host in ("127.0.0.1", "localhost") and admin_url.database == "postgres", "loopback fixture only"
    owner = os.stat(CANDIDATE)
    # The service identity: whoever owns the checkout (it must be able to import the application), never root.
    uid, gid = (owner.st_uid, owner.st_gid) if owner.st_uid else (65534, 65534)
    group = grp.getgrgid(gid).gr_name
    base = Path("/run") / ("kalillac-provision-test-" + uuid.uuid4().hex)
    assert base.parent == Path("/run") and not base.exists()
    tag = uuid.uuid4().hex[:12]
    names = {key: f"kalillac_test_pb_{key}_{tag}" for key in (*provision.ROLE_SETTINGS, "migrate")}
    database = "kalillac_test_pb_" + tag
    for key, setting in provision.ROLE_SETTINGS.items():
        os.environ[setting] = names[key]
    results, server = [], None

    def record(case, passed):
        results.append({"case": case, "passed": bool(passed)})
        return bool(passed)

    def owned(path, mode):
        path.mkdir(mode=mode)
        os.chown(path, uid, gid)

    def as_service(argv, env, **kwargs):
        return subprocess.Popen([sys.executable, "-B", *argv], cwd=CANDIDATE, env=env, user=uid, group=gid,
                                extra_groups=[], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **kwargs)
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT", hide_parameters=True)
    try:
        base.mkdir(mode=0o755)
        deployment = base / "deployment"
        secret = base / "admin-url"
        fd = os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, admin_url.render_as_string(hide_password=False).encode())
        os.close(fd)
        argv = ["provision", "--deployment-id", DEPLOYMENT, "--release-sha", RELEASE, "--database", database,
                "--db-host", admin_url.host, "--db-port", str(admin_url.port or 5432),
                "--migration-login", names["migrate"], "--admin-url-file", str(secret),
                "--directory", str(deployment), "--service-group", group, "--apply"]
        if not record("provision a new deployment as root", provision.main(argv) == 0):
            return results
        modes = {(p.stat().st_uid, p.stat().st_gid, p.stat().st_mode & 0o7777) for p in (deployment / "protected").iterdir()}
        private = {(p.stat().st_uid, p.stat().st_mode & 0o7777) for p in (deployment / "private").iterdir()}
        record("protected files are root:service 0440 and private files root 0600",
               modes == {(0, gid, 0o440)} and private == {(0, 0o600)})
        record("a provisioned deployment is not provisioned again", provision.main(argv) == 2)

        # What systemd provides: the credentials directory, the state directories, the environment.
        files = provision.RootFiles(str(deployment), gid)
        files.protected("transition-admin-url",
                        admin_url.set(database=database).render_as_string(hide_password=False).encode())
        credentials, state = base / "credentials", base / "state"
        owned(credentials, 0o500)
        owned(state, 0o700)
        owned(state / "files", 0o700)
        for name, raw in (("kalillac_db_password", (deployment / "private" / "kalillac_db_password").read_bytes()),
                          ("stripe_secret_key", b"sk_test_" + b"s" * 32), ("stripe_webhook_secret", b"whsec_" + b"s" * 32)):
            fd = os.open(credentials / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
            os.write(fd, raw)
            os.fchown(fd, uid, gid)
            os.close(fd)
        generated = dict(line.split("=", 1) for line in (deployment / provision.SETTINGS_FILE).read_text().splitlines())
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(state), "PYTHONDONTWRITEBYTECODE": "1",
               "CREDENTIALS_DIRECTORY": str(credentials), **generated, **template_settings(str(state / "files")),
               "OPENAI_API_KEY": "synthetic-not-a-key", "BRAVE_API_KEY": "synthetic-not-a-key",
               "TAVILY_API_KEY": "synthetic-not-a-key", "KALILLAC_RESEND_API_KEY": "re_synthetic",
               "KALILLAC_SINGLETON_LOCK_DIRECTORY": str(state / "locks")}

        operator = {**env, "KALILLAC_V1_TRANSITION_ADMIN_URL_FILE": str(deployment / "protected" / "transition-admin-url"),
                    "KALILLAC_V1_OPERATOR_RECORD_FILE": str(state / "operator-record.jsonl")}
        transition = as_service(["tools/v1_security_transition.py", "activate", "--release-sha", RELEASE, "--apply"], operator)
        output = transition.communicate(timeout=180)[0]
        record("the operator transition activates as the service identity",
               transition.returncode == 0 and '"to": "active"' in output)

        port = free_port()
        server = as_service(["kalillac_server.py", "--host", "127.0.0.1", "--port", str(port)], env)
        deadline, healthy = time.monotonic() + 120, False
        while time.monotonic() < deadline and server.poll() is None and not healthy:
            try:
                healthy = http(port, "GET", "/api/health") == (200, {"status": "ok"})
            except OSError:
                time.sleep(0.5)
        if not record("the application starts from the staging template and answers its health check", healthy):
            if server.poll() is None:
                server.kill()
            # Fixed labels and setting names only reach this output (the application names settings, never values).
            print(server.communicate(timeout=30)[0][-2000:], file=sys.stderr)
            # The launcher reports no cause. Import the application the same way and show the last line
            # of what stopped it: the application's configuration errors name settings, never values.
            probe = as_service(["-c", "import app_fastapi_candidate"], env)
            print("startup stopped at:", [line for line in probe.communicate(timeout=120)[0].splitlines() if "Error" in line][-1:], file=sys.stderr)
            if os.environ.get("KALILLAC_VERIFY_BOOT_TRACE") == "1":   # Synthetic fixture only.
                trace = ("import builtins, sys, traceback, kalillac_server\n_p = builtins.print\n"
                         "def p(*a, **k):\n    if a and a[0] == 'SINGLETON_START_FAILED': traceback.print_exc()\n"
                         "    _p(*a, **k)\nbuiltins.print = p\n"
                         f"sys.exit(kalillac_server.main(['--host', '127.0.0.1', '--port', '{port}']))\n")
                probe = as_service(["-c", trace], env)
                try:
                    print(probe.communicate(timeout=60)[0][-3000:], file=sys.stderr)
                except subprocess.TimeoutExpired:
                    probe.kill()
            server = None
            return results
        record("an anonymous visitor is not signed in", http(port, "GET", "/api/account/me") == (401, {"error": "not_authenticated"}))
        status, body = http(port, "POST", "/api/account/login", {"email": "nobody@example.test", "password": "wrong-password-0000"})
        record("a wrong sign-in is refused through the V1 credential authority", status == 401 and isinstance(body, dict))
        for case, method, path in (("the account security route needs a session", "GET", "/api/account/security"),
                                   ("Checkout needs a session", "POST", "/api/account/billing/checkout"),
                                   ("the entitlement route needs a session", "GET", "/api/account/entitlements"),
                                   ("the usage route needs a session", "GET", "/api/account/usage"),
                                   ("Spaces need a session", "GET", "/api/spaces")):
            record(case, http(port, method, path, {} if method == "POST" else None)[0] == 401)
        status, body = http(port, "POST", "/api/billing/stripe/webhook", {"id": "evt_unsigned"})
        record("an unsigned webhook is refused", status == 400)
        server.send_signal(signal.SIGTERM)
        output = server.communicate(timeout=60)[0]
        # Uvicorn finishes its shutdown and then re-raises the signal it was stopped with: systemd's clean stop.
        record("the launcher took its singleton and shuts down gracefully on SIGTERM",
               "SINGLETON_LOCK_ACQUIRED" in output and "Application shutdown complete" in output
               and server.returncode in (0, -signal.SIGTERM))
        record("registration reached readiness under the protected files", "REGISTRATION_V2_READY" in output)
        server = None

        legacy = {k: v for k, v in env.items() if k != "KALILLAC_V1_ACCOUNT_SECURITY_ENABLED"}
        refused = as_service(["kalillac_server.py", "--host", "127.0.0.1", "--port", str(port)], legacy)
        try:
            refused.communicate(timeout=120)
            record("a start without V1 mode is refused against the activated deployment", refused.returncode != 0)
        except subprocess.TimeoutExpired:
            refused.kill()
            record("a start without V1 mode is refused against the activated deployment", False)
        return results
    finally:
        if server is not None and server.poll() is None:
            server.kill()
        with admin.connect() as connection:
            connection.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :d"
                                    " AND pid <> pg_backend_pid()"), {"d": database})
            connection.exec_driver_sql(f"DROP DATABASE IF EXISTS {database}")
            for key in ("migrate", "registration", "runtime", "service", "executor", "operator", "owner"):
                connection.exec_driver_sql(f"DROP ROLE IF EXISTS {names[key]}")
        admin.dispose()
        if base.exists():
            assert base.parent == Path("/run") and base.name.startswith("kalillac-provision-test-")
            shutil.rmtree(base)
        print(json.dumps({"tests": len(results), "failures": sum(not r["passed"] for r in results),
                          "synthetic_only": True, "results": results}))


if __name__ == "__main__":
    outcome = main()
    sys.exit(0 if outcome and all(r["passed"] for r in outcome) else 1)
