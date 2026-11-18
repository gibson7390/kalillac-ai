"""tools/staging_preflight.py: a filled-in staging configuration, checked before the first start.

Each case starts from the staging template filled with the boot verifier's synthetic values (test inputs,
not proposals) and breaks one thing; the preflight must name exactly that check, and never print a value.
"""
from __future__ import annotations

import io
import os
from pathlib import Path
import sys

import pytest

# The boot verifier (whose synthetic values these tests share) imports POSIX-only modules; staging and
# CI are Linux.
pytest.importorskip("grp")
TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
import staging_preflight as preflight  # noqa: E402
from provision_deployment import GENERATED_SETTINGS  # noqa: E402
from verify_provisioned_boot import SYNTHETIC, TEMPLATE, template_settings  # noqa: E402

UNIT_TEMPLATE = TEMPLATE.parent / "kalillac-staging.service.template"
FILES_ROOT = "/var/lib/kalillac-staging-v1/files"
STRIPE_KEY = "sk_test_" + "S1nthet1cKey" * 3
WEBHOOK = "whsec_" + "S1nthet1cHook" * 3
PROVIDERS = {"OPENAI_API_KEY": "sk-synthetic-openai-0001", "BRAVE_API_KEY": "synthetic-brave-0001",
             "TAVILY_API_KEY": "tvly-synthetic-0001", "KALILLAC_RESEND_API_KEY": "re_synthetic_0001"}


def write_env(path, settings):
    path.write_text("".join(f"{k}={v}\n" for k, v in settings.items()), encoding="utf-8")


class Config:
    def __init__(self, root):
        self.dir = root / "etc"
        self.private = self.dir / "private"
        self.private.mkdir(parents=True)
        self.settings = template_settings(FILES_ROOT)
        # Every setting provision_deployment.py writes (synthetic values).
        self.deployment = {name: "synthetic" for name in GENERATED_SETTINGS}
        self.deployment.update(KALILLAC_DEPLOYMENT_ID="kalillac-staging-v1", KALILLAC_DB_ENABLED="true",
                               KALILLAC_DB_PORT="5433")
        self.keys = dict(PROVIDERS)
        self.secrets = {"kalillac_db_password": "synthetic-db-password-0001", "stripe_secret_key": STRIPE_KEY,
                        "stripe_webhook_secret": WEBHOOK}
        self.unit = root / "kalillac-staging-v1.service"
        self.unit_values = {"UNIT_NAME": "kalillac-staging-v1", "DEPLOYMENT_ID": "kalillac-staging-v1",
                            "SERVICE_USER": "kalillac-staging", "SERVICE_GROUP": "kalillac-staging",
                            "RELEASE_DIR": "/opt/kalillac-staging-v1/release", "DIR": str(self.dir),
                            "VENV": "/opt/kalillac-staging-v1/venv", "PORT": "8002", "MEMORY_MAX": "2G",
                            "CPU_QUOTA": "100%"}

    def write(self):
        write_env(self.dir / "settings.env", self.settings)
        write_env(self.dir / "deployment.env", self.deployment)
        write_env(self.private / "provider-keys.env", self.keys)
        for name, value in self.secrets.items():
            (self.private / name).write_text(value + "\n", encoding="utf-8")
        text = UNIT_TEMPLATE.read_text(encoding="utf-8")
        for name, value in self.unit_values.items():
            text = text.replace(f"<{name}>", value)
        self.unit.write_text(text, encoding="utf-8")
        if os.name == "posix":
            os.chmod(self.private, 0o700)
            for path in self.private.iterdir():
                os.chmod(path, 0o600)
        return self

    def run(self):
        out = io.StringIO()
        code = preflight.run(self.dir, self.unit, TEMPLATE, out=out,
                             owner_uid=os.getuid() if os.name == "posix" else 0)
        return code, out.getvalue()


@pytest.fixture
def config(tmp_path):
    return Config(tmp_path)


def problems(output):
    return {line[9:].split(":", 1)[0] for line in output.splitlines() if line.startswith("PROBLEM")}


def test_a_correct_configuration_passes_and_prints_no_value(config):
    code, output = config.write().run()
    assert problems(output) == set() and code == 0, output
    assert output.splitlines()[-1] == "PASSED"
    values = [STRIPE_KEY, WEBHOOK, *PROVIDERS.values(), *config.secrets.values(), SYNTHETIC["HOST_STAGING_HOSTNAME"],
              SYNTHETIC["OWNER_STRIPE_TEST_PRICE_ID"], SYNTHETIC["HOST_RELEASE_SHA"],
              SYNTHETIC["OWNER_SPACES_BETA_ACCOUNT_IDS"]]
    for value in values:
        assert value not in output


def settings_change(**changes):
    return lambda c: c.settings.update(changes)


def settings_drop(name):
    return lambda c: c.settings.pop(name)


HOST = "https://" + SYNTHETIC["HOST_STAGING_HOSTNAME"]
BROKEN = {
    "live Stripe key": (lambda c: c.secrets.update(stripe_secret_key="sk_live_" + "x" * 30), {"Stripe test mode only", "the application accepts its billing settings"}),
    "restricted live key": (lambda c: c.secrets.update(stripe_secret_key="rk_live_" + "x" * 30), {"Stripe test mode only", "the application accepts its billing settings"}),
    "not a webhook secret": (lambda c: c.secrets.update(stripe_webhook_secret="sk_test_" + "x" * 30),
                             {"Stripe test mode only", "the application accepts its billing settings"}),
    "Stripe live mode": (settings_change(KALILLAC_STRIPE_MODE="live"),
                         {"the product's fixed identity unchanged", "the application accepts its billing settings"}),
    "another model": (settings_change(OPENAI_MODEL="gpt-5"),
                      {"the product's fixed identity unchanged", "the application accepts its reasoning modes settings"}),
    "anonymous allowance changed": (settings_change(KALILLAC_ANONYMOUS_DAILY_LIMIT="25"),
                                    {"the product's fixed identity unchanged",
                                     "the application accepts its anonymous allowance settings"}),
    "safeguards off": (settings_change(KALILLAC_FINANCIAL_SAFEGUARDS_ENABLED="false"),
                       {"the product's fixed identity unchanged",
                        "the application accepts its Financial Safeguards settings"}),
    "placeholder left": (settings_change(KALILLAC_FINANCE_DAY_PAID_LIMIT_USD="<OWNER_DAY_PAID_USD>"),
                         {"no placeholder left", "the application accepts its Financial Safeguards settings"}),
    "setting missing": (settings_drop("KALILLAC_USAGE_LIMIT_PAID_DAILY"),
                        {"every template setting present", "the application accepts its usage limits settings"}),
    "production hostname": (lambda c: c.settings.update({k: v.replace(SYNTHETIC["HOST_STAGING_HOSTNAME"], "kalillac.ai")
                                                         for k, v in c.settings.items()}),
                            {"staging addresses, not production's"}),
    "another host in one address": (settings_change(KALILLAC_BILLING_SUCCESS_URL="https://example.com/account?checkout=success"),
                                    {"staging addresses, not production's"}),
    "the earlier billing return path": (settings_change(KALILLAC_BILLING_SUCCESS_URL=HOST + "/account/billing?checkout=success"),
                                        {"staging addresses, not production's"}),
    "plain http": (settings_change(KALILLAC_SITE_ORIGIN="http://" + SYNTHETIC["HOST_STAGING_HOSTNAME"]),
                   {"staging addresses, not production's", "the application accepts its Spaces settings"}),
    "short release": (settings_change(KALILLAC_RELEASE_SHA="abc1234"), {"release commit"}),
    "files root elsewhere": (settings_change(KALILLAC_FILES_ROOT="/tmp/files"), {"files root"}),
    "free above global": (settings_change(KALILLAC_FINANCE_DAY_FREE_LIMIT_USD="150"),
                          {"the owner's numbers are coherent", "the application accepts its Financial Safeguards settings"}),
    "day above month": (settings_change(KALILLAC_FINANCE_DAY_OPENAI_LIMIT_USD="100", KALILLAC_FINANCE_MONTH_OPENAI_LIMIT_USD="50"),
                        {"the owner's numbers are coherent", "the application accepts its Financial Safeguards settings"}),
    "request above its day": (settings_change(KALILLAC_FINANCE_MAX_REQUEST_FREE_USD="25"),
                              {"the owner's numbers are coherent", "the application accepts its Financial Safeguards settings"}),
    "anonymous above total": (settings_change(KALILLAC_BRAVE_ANONYMOUS_ATTEMPTS="5000"),
                              {"the owner's numbers are coherent"}),
    "unknown reasoning mode": (settings_change(KALILLAC_REASONING_MODES_FREE="quick,turbo"),
                               {"the application accepts its reasoning modes settings"}),
    "provider key missing": (lambda c: c.keys.pop("TAVILY_API_KEY"), {"provider keys"}),
    "a provisioned setting repeated": (settings_change(KALILLAC_DB_NAME="another_database"),
                                       {"settings.env repeats no provisioned setting"}),
    "deployment.env edited": (lambda c: c.deployment.pop("KALILLAC_V1_SERVICE_CREDENTIALS_FILE"),
                              {"deployment.env is exactly what provisioning wrote"}),
    "provider key outside the private file": (lambda c: c.settings.update(OPENAI_API_KEY="sk-elsewhere"),
                                              {"secrets only in the private file"}),
    # A malformed line is not read at all, so its setting is also missing.
    "quoted value": (settings_change(KALILLAC_EMAIL_FROM='"accounts@staging.invalid"'),
                     {"settings.env syntax", "every template setting present"}),
    "production port": (lambda c: c.unit_values.update(PORT="8001"), {"the unit"}),
    "listening publicly": (lambda c: c.unit_values.update(PORT="8002 --host 0.0.0.0"), {"the unit"}),
    "production's unit name": (lambda c: setattr(c, "unit", c.unit.with_name(preflight.PRODUCTION_UNIT)), {"the unit"}),
    "running as root": (lambda c: c.unit_values.update(SERVICE_USER="root"), {"the unit"}),
}


@pytest.mark.parametrize("case", list(BROKEN))
def test_each_mistake_is_named(config, case):
    breaks, expected = BROKEN[case]
    breaks(config)
    code, output = config.write().run()
    assert code == 1 and output.splitlines()[-1].startswith("FAILED"), output
    assert problems(output) == expected, output
    for value in (STRIPE_KEY, WEBHOOK, *PROVIDERS.values()):
        assert value not in output


def test_a_duplicate_setting_is_a_syntax_problem(config):
    config.write()
    with (config.dir / "settings.env").open("a", encoding="utf-8") as fh:
        fh.write("KALILLAC_USAGE_LIMIT_FREE_DAILY=21\n")
    code, output = config.run()
    assert code == 1 and "settings.env syntax" in problems(output)
    assert "KALILLAC_USAGE_LIMIT_FREE_DAILY twice" in output and "=21" not in output


@pytest.mark.skipif(os.name != "posix", reason="file ownership and modes are POSIX")
def test_secret_files_must_be_root_only(config):
    config.write()
    os.chmod(config.private / "stripe_secret_key", 0o644)
    code, output = config.run()
    assert code == 1 and problems(output) == {"secret files root-only"}
    assert "stripe_secret_key not root-only" in output


def test_a_missing_file_is_a_usage_error(config, capsys):
    config.write()
    (config.private / "provider-keys.env").unlink()
    assert preflight.main(["--dir", str(config.dir), "--unit", str(config.unit), "--template", str(TEMPLATE)]) == 2
    assert capsys.readouterr().err == "missing: provider-keys.env\n"


def test_the_preflight_leaves_the_environment_as_it_was(config):
    before = dict(os.environ)
    config.write().run()
    assert dict(os.environ) == before
