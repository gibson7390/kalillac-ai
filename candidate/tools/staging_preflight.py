"""Staging preflight: check a filled-in staging configuration before the first start (read-only).

For the operator, after the runbook's steps 5 and 6 (settings, secrets and the unit written) and before
step 8 (the first start). It changes nothing, opens no network connection and never prints a value from
any file it reads: every line names a check and, at most, setting names.

    cd REL/candidate && sudo VENV/bin/python3 -B tools/staging_preflight.py --dir DIR \
        --unit /etc/systemd/system/UNIT.service --template REL/deploy/staging/settings.env.template

Run it from REL/candidate with staging's own Python environment (it runs the application's own settings
loaders, so it needs the release's code and dependencies). Exit status: 0 when every check passed, 1 when
any check found a problem, 2 for a usage error (a missing file).

What it checks, beyond "the application would start":
* every template setting is present, every placeholder replaced, every line valid EnvironmentFile syntax;
* the product's fixed identity is unchanged (GPT-6 Luna only, Brave then Tavily, 10 anonymous responses,
  V1 account security, Financial Safeguards, enforcement, Stripe TEST mode);
* it is not production: not production's hostname, port 8001 or unit name; every address is the staging
  host's, and Stripe returns customers to the account page;
* Stripe holds test-mode secrets only (an sk_live_ or rk_live_ key is refused);
* the owner's numbers are coherent with each other (no ceiling above the ceiling it sits under, no
  per-request ceiling above its tier's day, no anonymous share above the total). It never judges whether
  a number is right: that is the owner's decision;
* the secret files are root-only (POSIX hosts);
* and the application's own loaders accept the settings (Financial Safeguards, reasoning modes, Spaces,
  billing, usage limits), so a start would not stop on any of them.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
import os
from pathlib import Path
import re
import stat
import sys
from urllib.parse import urlsplit

CANDIDATE = Path(__file__).resolve().parents[1]
for _path in (CANDIDATE, Path(__file__).resolve().parent):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

PRODUCTION_HOSTS = frozenset({"kalillac.ai", "www.kalillac.ai"})
PRODUCTION_PORT = 8001
PRODUCTION_UNIT = "kalillac-ai-fastapi-staging.service"     # a historical name: it IS production's unit
KEY = re.compile(r"[A-Z_][A-Z0-9_]*")
PLACEHOLDER = re.compile(r"<[^>\n]*>")
PROVIDER_KEYS = ("OPENAI_API_KEY", "BRAVE_API_KEY", "TAVILY_API_KEY", "KALILLAC_RESEND_API_KEY")
SECRET_FILES = ("kalillac_db_password", "stripe_secret_key", "stripe_webhook_secret", "provider-keys.env")

# The product's fixed identity: changed only by the owner, never by a staging operator.
FIXED = {
    "OPENAI_MODEL": "gpt-6-luna", "KALILLAC_PRICE_OPENAI_MODEL": "gpt-6-luna",
    "KALILLAC_SEARCH_PRIMARY": "brave", "KALILLAC_SEARCH_FALLBACK": "tavily",
    "KALILLAC_ANONYMOUS_DAILY_LIMIT_ENABLED": "true", "KALILLAC_ANONYMOUS_DAILY_LIMIT": "10",
    "KALILLAC_V1_ACCOUNT_SECURITY_ENABLED": "true", "KALILLAC_ACCOUNT_COOKIE_SECURE": "true",
    "KALILLAC_FINANCIAL_SAFEGUARDS_ENABLED": "true", "KALILLAC_PROVIDER_CEILINGS_ENABLED": "true",
    "KALILLAC_USAGE_ENFORCEMENT_ENABLED": "true", "KALILLAC_STRIPE_MODE": "test",
}
# Where each address must lead on the staging host (path and query, exactly).
ADDRESSES = {
    "KALILLAC_ACCOUNT_REGISTRATION_URL": "/account/create/confirm",
    "KALILLAC_ACCOUNT_RECOVERY_URL": "/account/recovery/confirm",
    "KALILLAC_BILLING_SUCCESS_URL": "/account?checkout=success",
    "KALILLAC_BILLING_CANCEL_URL": "/account?checkout=cancelled",
    "KALILLAC_BILLING_PORTAL_RETURN_URL": "/account",
}


class Report:
    def __init__(self, out):
        self.out, self.problems = out, 0

    def check(self, name, problems=()):
        problems = sorted(set(problems))
        if problems:
            self.problems += 1
            self.out.write(f"PROBLEM  {name}: {', '.join(problems)}\n")
        else:
            self.out.write(f"ok       {name}\n")

    def note(self, text):
        self.out.write(f"note     {text}\n")


def read_env_file(path):
    """(settings, malformed line numbers, duplicate keys). Comments and blank lines are skipped. Values
    are kept in memory only, for the checks; nothing here prints one."""
    settings, malformed, duplicates = {}, [], []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition("=")
        # systemd EnvironmentFile: KEY=VALUE, no spaces around the key, no quotes, no inline comment.
        if (not sep or not KEY.fullmatch(key) or value != value.strip() or value[:1] in ("'", '"')
                or " #" in value):
            malformed.append(f"line {number}")
            continue
        if key in settings:
            duplicates.append(key)
        settings[key] = value
    return settings, malformed, duplicates


def _decimal(value):
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError):
        return None
    return number if number.is_finite() else None


def coherence_problems(settings):
    """Relations between the owner's numbers that can only be mistakes, whatever the values."""
    problems = []
    usd = lambda name: _decimal(settings.get(name))  # noqa: E731
    for period in ("DAY", "MONTH"):
        total = usd(f"KALILLAC_FINANCE_{period}_GLOBAL_LIMIT_USD")
        for part in ("FREE", "PAID", "OPENAI", "BRAVE", "TAVILY"):
            name = f"KALILLAC_FINANCE_{period}_{part}_LIMIT_USD"
            if total is not None and usd(name) is not None and usd(name) > total:
                problems.append(f"{name} above KALILLAC_FINANCE_{period}_GLOBAL_LIMIT_USD")
    for part in ("GLOBAL", "FREE", "PAID", "OPENAI", "BRAVE", "TAVILY"):
        day, month = usd(f"KALILLAC_FINANCE_DAY_{part}_LIMIT_USD"), usd(f"KALILLAC_FINANCE_MONTH_{part}_LIMIT_USD")
        if day is not None and month is not None and day > month:
            problems.append(f"KALILLAC_FINANCE_DAY_{part}_LIMIT_USD above its month")
    for tier in ("FREE", "PAID"):
        request, day = usd(f"KALILLAC_FINANCE_MAX_REQUEST_{tier}_USD"), usd(f"KALILLAC_FINANCE_DAY_{tier}_LIMIT_USD")
        if request is not None and day is not None and request > day:
            problems.append(f"KALILLAC_FINANCE_MAX_REQUEST_{tier}_USD above KALILLAC_FINANCE_DAY_{tier}_LIMIT_USD")
    for provider in ("OPENAI", "BRAVE", "TAVILY"):
        total, anonymous = (_decimal(settings.get(f"KALILLAC_{provider}_{k}_ATTEMPTS")) for k in ("TOTAL", "ANONYMOUS"))
        if total is not None and anonymous is not None and anonymous > total:
            problems.append(f"KALILLAC_{provider}_ANONYMOUS_ATTEMPTS above KALILLAC_{provider}_TOTAL_ATTEMPTS")
    headroom = usd("KALILLAC_FINANCE_MIN_HEADROOM_PERCENT")
    if headroom is not None and not Decimal(0) <= headroom < Decimal(100):
        problems.append("KALILLAC_FINANCE_MIN_HEADROOM_PERCENT outside 0-100")
    return problems


def address_problems(settings):
    problems = []
    origin = urlsplit(settings.get("KALILLAC_SITE_ORIGIN", ""))
    host = (origin.hostname or "").lower()
    if origin.scheme != "https" or not host or origin.path not in ("", "/") or origin.query or origin.port:
        problems.append("KALILLAC_SITE_ORIGIN is not https://<staging hostname>")
    if host in PRODUCTION_HOSTS:
        problems.append("KALILLAC_SITE_ORIGIN is production's hostname")
    for name, target in ADDRESSES.items():
        url = urlsplit(settings.get(name, ""))
        where = url.path + (f"?{url.query}" if url.query else "")
        if url.scheme != "https" or (url.hostname or "").lower() != host or url.port or where != target:
            problems.append(f"{name} is not https://<staging hostname>{target}")
    return problems


def stripe_problems(private):
    problems = []
    try:
        key = (private / "stripe_secret_key").read_text(encoding="utf-8").strip()
    except OSError:
        return ["stripe_secret_key unreadable"]
    if key.startswith(("sk_live_", "rk_live_")):
        problems.append("stripe_secret_key is a LIVE key")
    elif not re.fullmatch(r"(sk|rk)_test_[A-Za-z0-9]{10,}", key):
        problems.append("stripe_secret_key is not a test-mode secret key")
    try:
        webhook = (private / "stripe_webhook_secret").read_text(encoding="utf-8").strip()
    except OSError:
        return problems + ["stripe_webhook_secret unreadable"]
    if not re.fullmatch(r"whsec_[A-Za-z0-9]{10,}", webhook):
        problems.append("stripe_webhook_secret is not a webhook signing secret")
    return problems


def permission_problems(directory, owner_uid=0):
    if os.name != "posix":
        return None
    problems = []
    private = directory / "private"
    for path in [private, *(private / name for name in SECRET_FILES)]:
        try:
            info = path.stat()
        except OSError:
            problems.append(f"{path.name} missing")
            continue
        if info.st_uid != owner_uid or stat.S_IMODE(info.st_mode) & 0o077:
            problems.append(f"{path.name} not root-only")
    for name in ("settings.env", "deployment.env"):
        try:
            if stat.S_IMODE((directory / name).stat().st_mode) & 0o022:
                problems.append(f"{name} writable by others")
        except OSError:
            problems.append(f"{name} missing")
    return problems


def unit_problems(unit_path, directory):
    text = unit_path.read_text(encoding="utf-8")
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    problems = []
    if unit_path.name == PRODUCTION_UNIT:
        problems.append("the unit has production's name")
    if PLACEHOLDER.search(body):
        problems.append("placeholders remain")
    port = re.search(r"^ExecStart=.*--port\s+(\d+)\b", body, re.M)
    if not port:
        problems.append("ExecStart names no --port")
    elif int(port.group(1)) == PRODUCTION_PORT:
        problems.append("the port is production's (8001)")
    if re.search(r"^ExecStart=.*--host\s+(?!127\.0\.0\.1\b)\S+", body, re.M):
        problems.append("the application listens beyond 127.0.0.1")
    user = re.search(r"^User=(\S+)", body, re.M)
    if not user or user.group(1) in ("root", "0"):
        problems.append("the service would run as root")
    expected = [f"EnvironmentFile={directory}/deployment.env", f"EnvironmentFile={directory}/settings.env",
                f"EnvironmentFile={directory}/private/provider-keys.env"]
    files = re.findall(r"^EnvironmentFile=\S+", body, re.M)
    if files != expected:
        problems.append("EnvironmentFile lines are not this configuration directory's three files, in order")
    for credential in ("kalillac_db_password", "stripe_secret_key", "stripe_webhook_secret"):
        if f"LoadCredential={credential}:{directory}/private/{credential}" not in body.splitlines():
            problems.append(f"LoadCredential {credential} missing")
    return problems


@contextmanager
def environment(values):
    """os.environ is exactly `values` inside (the application's loaders read it), then restored."""
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update(values)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def loader_results(merged, private):
    """The application's own settings loaders, in-process, against the merged settings. Their messages
    name settings, never values."""
    from kalillac_accounts import usage_limits
    from kalillac_billing.config import load_billing_config
    from kalillac_finance.config import load_financial_config
    from kalillac_routing.reasoning_modes import load_reasoning_modes
    from kalillac_spaces.config import load_spaces_config

    checks = {
        "Financial Safeguards": lambda: load_financial_config(dict(os.environ)),
        "reasoning modes": lambda: load_reasoning_modes(dict(os.environ), os.environ.get("OPENAI_MODEL"),
                                                        os.environ.get("OPENAI_REASONING_EFFORT")),
        "Spaces": lambda: load_spaces_config(dict(os.environ)),
        "billing": load_billing_config,
        "usage limits": usage_limits.load_usage_limits,
        "anonymous allowance": usage_limits.load_anonymous_daily_limit,
    }
    results = {}
    with environment({**merged, "CREDENTIALS_DIRECTORY": str(private)}):
        for name, load in checks.items():
            try:
                loaded = load()
            except Exception as exc:                          # noqa: BLE001 - reported, never re-raised
                results[name] = [f"{type(exc).__name__}: {str(exc)[:200]}"]
            else:
                results[name] = [] if loaded is not None else ["disabled"]
    return results


def run(directory, unit, template, out=sys.stdout, owner_uid=0):
    """owner_uid: who must own the secret files (root; a test passes its own uid)."""
    report = Report(out)
    directory, private = Path(directory), Path(directory) / "private"
    files = {name: directory / name for name in ("settings.env", "deployment.env")}
    files["provider-keys.env"] = private / "provider-keys.env"
    parsed = {}
    for name, path in files.items():
        if not path.is_file():
            raise FileNotFoundError(name)
        settings, malformed, duplicates = read_env_file(path)
        report.check(f"{name} syntax", malformed + [f"{key} twice" for key in duplicates])
        parsed[name] = settings
    settings = parsed["settings.env"]
    expected, _, _ = read_env_file(Path(template))

    from provision_deployment import GENERATED_SETTINGS
    generated = parsed["deployment.env"]
    report.check("deployment.env is exactly what provisioning wrote",
                 [f"{k} missing" for k in GENERATED_SETTINGS if k not in generated]
                 + [f"{k} unexpected" for k in generated if k not in GENERATED_SETTINGS])
    # settings.env is read after deployment.env, so a repeated setting would silently replace the value
    # that matches the provisioned database.
    report.check("settings.env repeats no provisioned setting", [k for k in settings if k in GENERATED_SETTINGS])
    report.check("every template setting present", [k for k in expected if k not in settings])
    extra = sorted(k for k in settings if k not in expected)
    if extra:
        report.note(f"settings not in the template (checked by the application, not here): {', '.join(extra)}")
    report.check("no placeholder left", [k for k, v in settings.items() if PLACEHOLDER.search(v) or "<" in v])
    report.check("the product's fixed identity unchanged", [k for k, v in FIXED.items() if settings.get(k) != v])
    report.check("staging addresses, not production's", address_problems(settings))
    sha = settings.get("KALILLAC_RELEASE_SHA", "")
    report.check("release commit", [] if re.fullmatch(r"[0-9a-f]{40}", sha) else ["KALILLAC_RELEASE_SHA is not a full commit"])
    root = settings.get("KALILLAC_FILES_ROOT", "")
    report.check("files root", [] if root.startswith("/var/lib/") else ["KALILLAC_FILES_ROOT is not under /var/lib/<UNIT>"])
    keys = parsed["provider-keys.env"]
    report.check("provider keys", [f"{k} missing or empty" for k in PROVIDER_KEYS if not keys.get(k)]
                 + [f"{k} unexpected" for k in keys if k not in PROVIDER_KEYS])
    overlap = sorted(set(keys) & (set(settings) | set(parsed["deployment.env"])))
    report.check("secrets only in the private file", [f"{k} also outside it" for k in overlap])
    report.check("Stripe test mode only", stripe_problems(private))
    report.check("the owner's numbers are coherent", coherence_problems(settings))
    permissions = permission_problems(directory, owner_uid)
    if permissions is None:
        report.note("file ownership and modes not checked (not a POSIX host)")
    else:
        report.check("secret files root-only", permissions)
    report.check("the unit", unit_problems(Path(unit), directory))
    # Later EnvironmentFile lines win, as in the unit.
    merged = {**parsed["deployment.env"], **settings, **keys}
    for name, problems in loader_results(merged, private).items():
        report.check(f"the application accepts its {name} settings", problems)
    if settings.get("KALILLAC_FILES_IMAGES_ENABLED") == "true":
        report.note("images are on: KALILLAC_FINANCE_OPENAI_IMAGE_INPUT_TOKENS must be the value measured in "
                    "runbook section 9, not an estimate")
    out.write(f"{'PASSED' if not report.problems else f'FAILED: {report.problems} check(s)'}\n")
    return 0 if not report.problems else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--dir", required=True, help="the configuration directory (DIR in the runbook)")
    parser.add_argument("--unit", required=True, help="the installed unit file")
    parser.add_argument("--template", required=True, help="the release's deploy/staging/settings.env.template")
    args = parser.parse_args(argv)
    try:
        return run(args.dir, args.unit, args.template)
    except FileNotFoundError as exc:
        missing = exc.filename or (exc.args[0] if exc.args and isinstance(exc.args[0], str) else "a file")
        sys.stderr.write(f"missing: {Path(missing).name}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
