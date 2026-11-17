"""deploy/staging: the settings template, the unit template and the web-root builder agree with the code.

No database, no root. The template is proved to START the application by tools/verify_provisioned_boot.py
(root, real PostgreSQL); these tests keep the pieces from drifting apart between such runs: every
placeholder has a synthetic value there, every setting a loader requires is present, nothing the
provisioning tool generates is repeated, no secret has a line, and the page marks the builder writes
are exactly the ones the chat reads.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

CANDIDATE = Path(__file__).resolve().parents[1]
STAGING = CANDIDATE.parent / "deploy" / "staging"
TEMPLATE = STAGING / "settings.env.template"
UNIT = STAGING / "kalillac-staging.service.template"
PLACEHOLDER = re.compile(r"<([A-Z0-9_]+)>")

sys.path.insert(0, str(CANDIDATE / "tools"))
import provision_deployment as provision  # noqa: E402


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


webroot = load(STAGING / "build_webroot.py", "build_webroot")


def settings():
    lines = [line for line in TEMPLATE.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]
    assert all(re.fullmatch(r"[A-Z][A-Z0-9_]*=\S+", line) for line in lines), "KEY=VALUE without spaces or quotes"
    pairs = [line.split("=", 1) for line in lines]
    assert len({key for key, _ in pairs}) == len(pairs), "a setting appears twice"
    return dict(pairs)


def test_every_placeholder_has_a_synthetic_value_in_the_boot_verifier():
    pytest.importorskip("grp")
    import verify_provisioned_boot as boot
    used = {name for value in settings().values() for name in PLACEHOLDER.findall(value)}
    assert used == set(boot.SYNTHETIC) | {"HOST_FILES_ROOT"}
    filled = boot.template_settings("/var/lib/synthetic/files")
    assert set(filled) == set(settings()) and not any("<" in value for value in filled.values())
    # Placeholders say whose value it is: the owner's, a provider's price, production's limit or the host's.
    assert all(name.split("_", 1)[0] in ("OWNER", "PRICE", "PROD", "HOST") for name in used)


def test_every_setting_a_loader_requires_is_in_the_template():
    from kalillac_finance import limits, pricing
    from kalillac_routing import reasoning_modes, request_limits
    have = settings()
    required = {*pricing.REQUIRED, *limits.REQUIRED, *reasoning_modes.REQUIRED, pricing.ENABLE,
                request_limits.DEADLINE_SECONDS, request_limits.QUEUE_WAIT_SECONDS, request_limits.MAX_MODEL_ATTEMPTS,
                request_limits.MAX_SEARCH_ATTEMPTS, "KALILLAC_USAGE_LIMIT_FREE_DAILY", "KALILLAC_USAGE_LIMIT_PAID_DAILY",
                *[f"KALILLAC_{p}_{k}" for p in ("OPENAI", "BRAVE", "TAVILY")
                  for k in ("TOTAL_ATTEMPTS", "ANONYMOUS_ATTEMPTS", "PERIOD_SECONDS")]}
    assert not required - set(have)
    for flag in ("KALILLAC_ACCOUNTS_ENABLED", "KALILLAC_V1_ACCOUNT_SECURITY_ENABLED",
                 "KALILLAC_ACCOUNT_REGISTRATION_V2_ENABLED", "KALILLAC_ACCOUNT_RECOVERY_ENABLED",
                 "KALILLAC_USAGE_METERING_ENABLED", "KALILLAC_USAGE_ENFORCEMENT_ENABLED",
                 "KALILLAC_REQUEST_BUDGET_ENABLED", "KALILLAC_PROVIDER_CEILINGS_ENABLED",
                 "KALILLAC_FINANCIAL_SAFEGUARDS_ENABLED", "KALILLAC_REASONING_MODES_ENABLED",
                 "KALILLAC_BILLING_ENABLED", "KALILLAC_FILES_ENABLED", "KALILLAC_ANONYMOUS_DAILY_LIMIT_ENABLED"):
        assert have[flag] == "true", flag


def test_the_template_keeps_the_product_identity_and_never_live_billing():
    have = settings()
    assert have["OPENAI_MODEL"] == have["KALILLAC_PRICE_OPENAI_MODEL"] == "gpt-6-luna"
    assert (have["KALILLAC_SEARCH_PRIMARY"], have["KALILLAC_SEARCH_FALLBACK"]) == ("brave", "tavily")
    assert have["KALILLAC_STRIPE_MODE"] == "test" and have["KALILLAC_ANONYMOUS_DAILY_LIMIT"] == "10"
    assert have["KALILLAC_ACCOUNT_COOKIE_SECURE"] == "true" and have["KALILLAC_FILES_TIER"] == "paid"
    # The default mode reproduces a request that selects none (REASONING_MODES.md).
    default = have["KALILLAC_REASONING_MODE_DEFAULT"].upper()
    assert have[f"KALILLAC_REASONING_MODE_{default}_EFFORT"] == have["OPENAI_REASONING_EFFORT"]
    assert have[f"KALILLAC_REASONING_MODE_{default}_EXTRA_OUTPUT_TOKENS"] == "0"
    # No allowance, budget, price or Stripe id is decided in the template.
    for key, value in have.items():
        if key.endswith(("_LIMIT_USD", "_USD", "_USD_PER_MTOK", "_USD_PER_REQUEST", "_USD_PER_CREDIT", "_DAILY",
                         "_ATTEMPTS", "_PRICE_ID", "_PRICE_IDS", "MODES_FREE", "MODES_PAID")):
            assert PLACEHOLDER.fullmatch(value), key


def test_no_secret_and_nothing_generated_has_a_line():
    have = settings()
    assert not set(have) & set(provision.GENERATED_SETTINGS)
    assert not [key for key in have if re.search(r"KEY|SECRET|PASSWORD|TOKEN(?!S)|CREDENTIAL", key)]
    assert "NONPRODUCTION" not in TEMPLATE.read_text(encoding="utf-8") and "sk_live" not in TEMPLATE.read_text(encoding="utf-8")


def test_the_unit_loads_the_credentials_the_code_reads_and_the_three_environment_files():
    from kalillac_billing import config as billing
    unit = UNIT.read_text(encoding="utf-8")
    credentials = re.findall(r"^LoadCredential=([a-z_]+):<DIR>/private/([a-z_]+)$", unit, re.M)
    assert credentials == [(name, name) for name in (provision.PRIVATE["runtime_password"],
                                                     billing.SECRET_KEY_CREDENTIAL, billing.WEBHOOK_SECRET_CREDENTIAL)]
    assert re.findall(r"^EnvironmentFile=(.+)$", unit, re.M) == [
        f"<DIR>/{provision.SETTINGS_FILE}", "<DIR>/settings.env", "<DIR>/private/provider-keys.env"]
    assert "ExecStart=<VENV>/bin/python3 -B kalillac_server.py --host 127.0.0.1 --port <PORT>" in unit
    assert (CANDIDATE / "kalillac_server.py").is_file() and "WorkingDirectory=<RELEASE_DIR>/candidate" in unit
    assert "kalillac-ai-fastapi-staging.service" in unit.split("[Unit]")[0]      # Named only as the name to avoid.
    assert "kalillac-ai-fastapi-staging" not in unit.split("[Unit]")[1]


def site(tmp_path):
    built = tmp_path / "site"
    built.mkdir(parents=True)
    (built / "index.html").write_text("<!DOCTYPE html><html lang=\"en\"><body>site</body></html>", encoding="utf-8")
    return built


def test_the_web_root_marks_exactly_what_the_chat_reads(tmp_path):
    frontend = CANDIDATE.parent / "frontend"
    read = set(re.findall(r'getAttribute\("data-kalillac-([a-z]+)"\)', (frontend / "app.js").read_text(encoding="utf-8")))
    assert read == set(webroot.MARKS)
    out = webroot.build(site(tmp_path), frontend, tmp_path / "out", ["reasoning", "accounts", "files"])
    page = (out / "app" / "index.html").read_text(encoding="utf-8")
    assert '<html lang="en" data-kalillac-accounts="on" data-kalillac-files="on" data-kalillac-reasoning="on">' in page
    original = (frontend / "index.html").read_text(encoding="utf-8")
    assert page == original.replace('<html lang="en">', page[page.index("<html"):page.index(">", page.index("<html")) + 1])
    assert (out / "index.html").read_text(encoding="utf-8").count("data-kalillac") == 0
    for name in ("app.js", "app.css"):
        assert (out / "app" / name).read_bytes() == (frontend / name).read_bytes()
    plain = webroot.build(site(tmp_path / "second"), frontend, tmp_path / "plain", [])
    assert (plain / "app" / "index.html").read_text(encoding="utf-8") == original


@pytest.mark.parametrize("marks, message", [(["billing"], "--marks"), (["files", "files"], "--marks")])
def test_the_builder_refuses_unknown_or_repeated_marks(tmp_path, marks, message):
    with pytest.raises(ValueError, match=message):
        webroot.build(site(tmp_path), CANDIDATE.parent / "frontend", tmp_path / "out", marks)
    assert not (tmp_path / "out").exists()


def test_the_builder_refuses_an_existing_output_and_an_unexpected_page(tmp_path, capsys):
    frontend = CANDIDATE.parent / "frontend"
    (tmp_path / "out").mkdir()
    assert webroot.main(["--site", str(site(tmp_path)), "--frontend", str(frontend), "--out", str(tmp_path / "out"),
                         "--marks", "files"]) == 2 and "already exists" in capsys.readouterr().out
    changed = tmp_path / "frontend"
    changed.mkdir()
    (changed / "index.html").write_text("<html lang=\"fr\"></html>", encoding="utf-8")
    with pytest.raises(ValueError, match="not the expected one"):
        webroot.build(tmp_path / "site", changed, tmp_path / "new", ["files"])
    assert not (tmp_path / "new").exists()
