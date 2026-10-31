"""Spaces configuration and startup: default-off, fail-closed allowlist and
origin parsing, prerequisites, and that a disabled Spaces feature leaves no
route, import, database access or public mention behind.

Startup cases import the real application in a fresh interpreter with
networking blocked (an unreachable database URL proves the engine stays
lazy). No database, model, search or network is contacted."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

from kalillac_spaces import config as spaces_config
from kalillac_spaces.config import (
    SpacesConfig,
    SpacesConfigError,
    load_spaces_config,
    parse_beta_account_ids,
    parse_site_origin,
    spaces_enabled,
)

CANDIDATE = Path(__file__).resolve().parents[1]
REPO = CANDIDATE.parent
ID_1 = "00000000-0000-4000-8000-000000000001"
ID_2 = "7d9f3c1e-2b4a-4c8d-9e6f-0a1b2c3d4e5f"

# Every prerequisite of Spaces, switched on with test values only.
REQUEST_BUDGET = {
    "KALILLAC_REQUEST_BUDGET_ENABLED": "true",
    "KALILLAC_REQUEST_DEADLINE_SECONDS": "5", "KALILLAC_REQUEST_QUEUE_WAIT_SECONDS": "2",
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
}
ACCOUNTS = {
    "KALILLAC_ACCOUNTS_ENABLED": "true", "KALILLAC_DB_ENABLED": "true",
    "KALILLAC_DATABASE_URL": "postgresql+psycopg://user:pw@unreachable.invalid/kalillac",
    "KALILLAC_USAGE_METERING_ENABLED": "true", "KALILLAC_USAGE_ENFORCEMENT_ENABLED": "true",
    "KALILLAC_USAGE_LIMIT_FREE_DAILY": "2", "KALILLAC_USAGE_LIMIT_PAID_DAILY": "5",
}
SPACES = {
    "KALILLAC_SPACES_ENABLED": "true",
    "KALILLAC_SPACES_BETA_ACCOUNT_IDS": ID_1,
    "KALILLAC_SITE_ORIGIN": "https://kalillac.test",
}
FULL = {**REQUEST_BUDGET, **ACCOUNTS, **SPACES}

PROBE = """
import json, socket, sys
attempts = []
real_connect = socket.socket.connect
def connect(sock, address):
    # The event loop's own loopback self-pipe is the only connection allowed.
    if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
        return real_connect(sock, address)
    attempts.append(1)
    raise AssertionError("network blocked")
def lookup(*args, **kwargs):
    attempts.append(1)
    raise AssertionError("network blocked")
socket.socket.connect = connect
socket.socket.connect_ex = socket.getaddrinfo = socket.create_connection = lookup
import app_fastapi_candidate as app
from kalillac_db import engine
from fastapi.testclient import TestClient
def walk(routes):
    # FastAPI wraps an included router lazily; walk into it.
    for r in routes:
        if hasattr(r, "original_router"):
            yield from walk(r.original_router.routes)
        else:
            yield getattr(r, "path", "")
paths = sorted(walk(app.api.routes))
with TestClient(app.api, base_url="https://testserver") as client:
    probe = client.get("/api/spaces")
print(json.dumps({
    "spaces_paths": [p for p in paths if "spaces" in p],
    "chat": "/api/chat" in paths,
    "imported": sorted(m for m in sys.modules if m.startswith("kalillac_spaces")),
    "engine": engine._ENGINE is not None,
    "outbound": len(attempts),
    "probe": [probe.status_code, probe.headers.get("content-type", "")],
    "runtime_mentions_spaces": "space" in json.dumps(app.current_runtime_configuration()).lower(),
}))
"""


def start(env_overrides):
    env = {key: value for key, value in os.environ.items() if not key.startswith("KALILLAC_")}
    env.update({"OPENAI_API_KEY": "", "TAVILY_API_KEY": "", "BRAVE_API_KEY": "", "DEBUG_MODE": "false",
                "GROQ_API_KEY": "test-not-real"})
    env.update(env_overrides)
    return subprocess.run([sys.executable, "-B", "-c", PROBE], cwd=str(CANDIDATE), env=env,
                          capture_output=True, text=True, timeout=120)


def started(env_overrides):
    result = start(env_overrides)
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def refused(env_overrides):
    result = start(env_overrides)
    assert result.returncode != 0
    return result.stderr


# --- pure configuration ---------------------------------------------------------------


@pytest.mark.parametrize("value,expected", [
    (None, False), ("", False), ("false", False), ("0", False), ("off", False), ("enabled", False),
    ("true", True), ("TRUE", True), (" 1 ", True), ("yes", True), ("on", True),
])
def test_the_flag_is_off_unless_explicitly_on(value, expected):
    environ = {} if value is None else {spaces_config.ENABLED_ENV: value}
    assert spaces_enabled(environ) is expected


@pytest.mark.parametrize("value", [None, "", " ", ",", " , ,"])
def test_an_empty_allowlist_grants_nobody(value):
    ids = parse_beta_account_ids(value)
    assert ids == frozenset()
    config = SpacesConfig(beta_account_ids=ids, site_origin="https://kalillac.test")
    assert not config.has_beta_access(uuid.UUID(ID_1))


def test_a_valid_allowlist_grants_exactly_those_accounts():
    ids = parse_beta_account_ids(f" {ID_1} ,{ID_2},{ID_1}")
    assert ids == {uuid.UUID(ID_1), uuid.UUID(ID_2)}
    config = SpacesConfig(beta_account_ids=ids, site_origin="https://kalillac.test")
    assert config.has_beta_access(uuid.UUID(ID_2))
    assert not config.has_beta_access(uuid.uuid4())
    assert repr(config) == "SpacesConfig(beta_accounts=2)"          # ids never printed


@pytest.mark.parametrize("entry", [
    "person@example.com", ID_2.upper(), ID_1.replace("-", ""), "{" + ID_1 + "}", "urn:uuid:" + ID_1,
    ID_1[:-1], ID_1 + "0", "not-an-id", f"{ID_1};{ID_2}", f"{ID_1} {ID_2}",
])
def test_anything_but_canonical_account_ids_fails_and_never_echoes_the_value(entry):
    with pytest.raises(SpacesConfigError) as error:
        parse_beta_account_ids(f"{ID_2},{entry}")
    assert str(error.value) == "KALILLAC_SPACES_BETA_ACCOUNT_IDS must list account UUIDs only."
    assert entry not in str(error.value)


@pytest.mark.parametrize("origin", [
    "https://kalillac.test", "https://www.kalillac.test:8443", "http://127.0.0.1:8000", "http://localhost:5173",
])
def test_exact_origins_are_accepted(origin):
    assert parse_site_origin(origin) == origin


@pytest.mark.parametrize("origin", [
    None, "", "kalillac.test", "https://kalillac.test/", "https://kalillac.test/app", "https://kalillac.test?x=1",
    "https://kalillac.test#x", "http://kalillac.test", "https://user:pw@kalillac.test", "https://KALILLAC.test",
    "ftp://kalillac.test", "https://", "https://kalillac.test:99999", "*",
])
def test_the_site_origin_is_required_and_exact(origin):
    with pytest.raises(SpacesConfigError) as error:
        parse_site_origin(origin)
    assert str(error.value) == "KALILLAC_SITE_ORIGIN must be an exact origin."


def test_load_reads_only_its_own_settings():
    config = load_spaces_config({"KALILLAC_SPACES_BETA_ACCOUNT_IDS": ID_1, "KALILLAC_SITE_ORIGIN": "https://kalillac.test"})
    assert config.beta_account_ids == {uuid.UUID(ID_1)} and config.site_origin == "https://kalillac.test"
    with pytest.raises(SpacesConfigError, match="KALILLAC_SITE_ORIGIN"):
        load_spaces_config({"KALILLAC_SPACES_BETA_ACCOUNT_IDS": ID_1})


# --- startup --------------------------------------------------------------------------


def test_default_startup_has_no_spaces_at_all():
    facts = started({})
    assert facts == {
        "spaces_paths": [], "chat": True, "imported": [], "engine": False, "outbound": 0,
        "probe": [404, "application/json"], "runtime_mentions_spaces": False,
    }


def test_flag_off_with_every_prerequisite_on_installs_nothing_and_reads_no_spaces_setting():
    # Malformed Spaces settings are never read while the flag is off.
    facts = started({**FULL, "KALILLAC_SPACES_ENABLED": "false",
                     "KALILLAC_SPACES_BETA_ACCOUNT_IDS": "person@example.com", "KALILLAC_SITE_ORIGIN": "nonsense"})
    assert facts["spaces_paths"] == [] and facts["imported"] == []
    assert facts["chat"] is True and facts["engine"] is False and facts["outbound"] == 0
    assert facts["runtime_mentions_spaces"] is False


def test_enabled_with_valid_settings_mounts_the_routes_without_touching_the_database():
    facts = started(FULL)
    assert sorted(set(facts["spaces_paths"])) == sorted([   # one entry per method
        "/api/spaces", "/api/spaces/{space_id}", "/api/spaces/{space_id}/instructions",
        "/api/spaces/{space_id}/conversations", "/api/spaces/{space_id}/conversations/{conversation_id}",
        "/api/spaces/{space_id}/conversations/{conversation_id}/messages",
    ])
    assert facts["chat"] is True and facts["engine"] is False and facts["outbound"] == 0
    # Signed out: refused before any database access.
    assert facts["probe"] == [401, "application/json"]


def test_enabled_with_an_empty_allowlist_starts_and_admits_nobody():
    facts = started({**FULL, "KALILLAC_SPACES_BETA_ACCOUNT_IDS": ""})
    assert facts["spaces_paths"] and facts["outbound"] == 0


@pytest.mark.parametrize("overrides,message", [
    ({"KALILLAC_SPACES_BETA_ACCOUNT_IDS": "person@example.com"},
     "KALILLAC_SPACES_BETA_ACCOUNT_IDS must list account UUIDs only."),
    ({"KALILLAC_SPACES_BETA_ACCOUNT_IDS": f"{ID_1},{ID_2.upper()}"},
     "KALILLAC_SPACES_BETA_ACCOUNT_IDS must list account UUIDs only."),
    ({"KALILLAC_SITE_ORIGIN": ""}, "KALILLAC_SITE_ORIGIN must be an exact origin."),
    ({"KALILLAC_SITE_ORIGIN": "https://kalillac.test/"}, "KALILLAC_SITE_ORIGIN must be an exact origin."),
    ({"KALILLAC_ACCOUNTS_ENABLED": "false", "KALILLAC_USAGE_METERING_ENABLED": "false",
      "KALILLAC_USAGE_ENFORCEMENT_ENABLED": "false"},
     "KALILLAC_SPACES_ENABLED requires KALILLAC_ACCOUNTS_ENABLED and KALILLAC_DB_ENABLED."),
    ({"KALILLAC_REQUEST_BUDGET_ENABLED": "false"},
     "KALILLAC_SPACES_ENABLED requires KALILLAC_REQUEST_BUDGET_ENABLED and account usage limits."),
    ({"KALILLAC_USAGE_ENFORCEMENT_ENABLED": "false"},
     "KALILLAC_SPACES_ENABLED requires KALILLAC_REQUEST_BUDGET_ENABLED and account usage limits."),
])
def test_invalid_or_incomplete_configuration_fails_startup(overrides, message):
    stderr = refused({**FULL, **overrides})
    assert message in stderr
    assert "person@example.com" not in stderr


# --- nothing public advertises Spaces -------------------------------------------------


def test_no_public_page_or_script_mentions_spaces():
    sources = [REPO / "frontend" / "index.html", REPO / "site" / "index.html"]
    sources += [p for p in (REPO / "site" / "src").rglob("*") if p.suffix in {".ts", ".tsx", ".css", ".html"}]
    for path in sources:
        text = path.read_text(encoding="utf-8").lower()
        assert "/api/spaces" not in text, path
        assert "kalillac_spaces" not in text, path
