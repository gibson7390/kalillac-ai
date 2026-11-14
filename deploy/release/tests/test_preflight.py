"""deploy/release/preflight.py against the REAL application in this checkout (the release commit).

Runs the same CHILD code stage.sh/activate.sh run in production, in a separate process, with a
production-shaped environment built here (throwaway values; the database URL is unreachable and is
never connected to). Proves the preflight accepts the intended configuration, refuses each forbidden
one, and never prints a configuration value.

    python -m pytest deploy/release/tests/test_preflight.py -v
"""
import importlib.util
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
CANDIDATE = ROOT / "candidate"
spec = importlib.util.spec_from_file_location("kalillac_preflight", ROOT / "deploy" / "release" / "preflight.py")
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)

SENTINEL = "KDT-SENTINEL-SECRET-preflight"
MODEL, HEAD, PREFIXES = "gpt-6-luna", "0015_finance_ledger", "/api/account /api/spaces /api/billing"

PRODUCTION_SHAPE = {
    "KALILLAC_DB_ENABLED": "true",
    "KALILLAC_DATABASE_URL": "postgresql+psycopg://app:" + SENTINEL + "@unreachable.invalid/kalillac",
    "KALILLAC_REQUEST_BUDGET_ENABLED": "true",
    "KALILLAC_REQUEST_DEADLINE_SECONDS": "60", "KALILLAC_REQUEST_QUEUE_WAIT_SECONDS": "5",
    "KALILLAC_REQUEST_MAX_MODEL_ATTEMPTS": "6", "KALILLAC_REQUEST_MAX_SEARCH_ATTEMPTS": "6",
    "KALILLAC_TRANSPORT_MAX_OUTSTANDING": "4", "KALILLAC_TRANSPORT_DNS_THREADS": "1",
    "KALILLAC_TRANSPORT_MAX_PENDING_DNS": "4", "KALILLAC_TRANSPORT_CANCEL_POLL_INTERVAL_SECONDS": "0.02",
    "KALILLAC_TRANSPORT_BACKSTOP_GRACE_SECONDS": "0.5", "KALILLAC_TRANSPORT_CLEANUP_GRACE_SECONDS": "1",
    "KALILLAC_TRANSPORT_CLOSE_TIMEOUT_SECONDS": "3", "KALILLAC_OPENAI_MAX_BYTES": "2097152",
    "KALILLAC_TAVILY_TRANSPORT_MAX_OUTSTANDING": "3", "KALILLAC_TAVILY_TRANSPORT_DNS_THREADS": "1",
    "KALILLAC_TAVILY_TRANSPORT_MAX_PENDING_DNS": "3", "KALILLAC_TAVILY_TRANSPORT_CANCEL_POLL_INTERVAL_SECONDS": "0.02",
    "KALILLAC_TAVILY_TRANSPORT_BACKSTOP_GRACE_SECONDS": "0.5", "KALILLAC_TAVILY_TRANSPORT_CLEANUP_GRACE_SECONDS": "1",
    "KALILLAC_TAVILY_TRANSPORT_CLOSE_TIMEOUT_SECONDS": "2", "KALILLAC_TAVILY_SEARCH_MAX_BYTES": "262144",
    "KALILLAC_TAVILY_EXTRACT_MAX_BYTES": "524288",
    "KALILLAC_PROVIDER_CEILINGS_ENABLED": "true",
    "KALILLAC_OPENAI_TOTAL_ATTEMPTS": "500", "KALILLAC_OPENAI_ANONYMOUS_ATTEMPTS": "200", "KALILLAC_OPENAI_PERIOD_SECONDS": "86400",
    "KALILLAC_BRAVE_TOTAL_ATTEMPTS": "500", "KALILLAC_BRAVE_ANONYMOUS_ATTEMPTS": "200", "KALILLAC_BRAVE_PERIOD_SECONDS": "86400",
    "KALILLAC_TAVILY_TOTAL_ATTEMPTS": "500", "KALILLAC_TAVILY_ANONYMOUS_ATTEMPTS": "200", "KALILLAC_TAVILY_PERIOD_SECONDS": "86400",
    "KALILLAC_ANONYMOUS_DAILY_LIMIT_ENABLED": "true", "KALILLAC_ANONYMOUS_DAILY_LIMIT": "10",
    "KALILLAC_SEARCH_PRIMARY": "brave", "KALILLAC_SEARCH_FALLBACK": "tavily",
    "KALILLAC_FINANCIAL_SAFEGUARDS_ENABLED": "false", "KALILLAC_ACCOUNTS_ENABLED": "false",
    "KALILLAC_BILLING_ENABLED": "false", "KALILLAC_SPACES_ENABLED": "false",
    "OPENAI_API_KEY": SENTINEL, "BRAVE_API_KEY": SENTINEL, "TAVILY_API_KEY": SENTINEL, "STRIPE_SECRET_KEY": SENTINEL,
}


def environment(**changes):
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME") if key in os.environ}
    env.update(PRODUCTION_SHAPE)
    for key, value in changes.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def run(env, head=HEAD, model=MODEL):
    status, line = preflight.run(sys.executable, str(CANDIDATE), [model, head, PREFIXES], env)
    assert SENTINEL not in line
    assert "\n" not in line
    return status, line


def test_production_shape_passes():
    status, line = run(environment())
    assert status == 0, line
    assert line.startswith("APP_PREFLIGHT_OK financial_safeguards=off provider_ceilings=on(openai,brave,tavily)")
    assert "migration_head=0015_finance_ledger" in line


@pytest.mark.parametrize("changes, expected", [
    ({"KALILLAC_PROVIDER_CEILINGS_ENABLED": "false"}, "APP_PREFLIGHT_FAILED provider attempt ceilings are not active"),
    ({"OPENAI_MODEL": "gpt-other"}, "APP_PREFLIGHT_FAILED OpenAI model is not gpt-6-luna"),
    ({"KALILLAC_SEARCH_FALLBACK": None}, "APP_PREFLIGHT_FAILED search policy is not brave primary, tavily fallback"),
    # The application itself rejects a reversed policy at import; the preflight still refuses.
    ({"KALILLAC_SEARCH_PRIMARY": "tavily", "KALILLAC_SEARCH_FALLBACK": "brave"}, "APP_PREFLIGHT_FAILED import: SearchProviderConfigError"),
    # Financial Safeguards switched on: incomplete prices make the application itself refuse to load.
    ({"KALILLAC_FINANCIAL_SAFEGUARDS_ENABLED": "true"}, "APP_PREFLIGHT_FAILED import: "),
    # A missing setting: only the exception type is printed, never its message.
    ({"KALILLAC_REQUEST_DEADLINE_SECONDS": None}, "APP_PREFLIGHT_FAILED import: RequestLimitConfigError"),
])
def test_forbidden_or_broken_configuration_is_refused(changes, expected):
    status, line = run(environment(**changes))
    assert status == 1
    assert line.startswith(expected), line


def test_unexpected_migration_head_is_refused():
    status, line = run(environment(), head="0016_not_this_release")
    assert status == 1
    assert line == "APP_PREFLIGHT_FAILED release migration head is not 0016_not_this_release"


def test_no_bytecode_written_into_the_release():
    before = sorted(str(p) for p in CANDIDATE.rglob("__pycache__"))
    run(environment())
    assert sorted(str(p) for p in CANDIDATE.rglob("__pycache__")) == before
