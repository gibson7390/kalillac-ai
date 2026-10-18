"""Daily chat-limit configuration (configuration only; no enforcement).

Pinned rules: enforcement is off by default; when on it requires accounts,
database support and usage metering; both tier limits must be explicit
positive integers with paid strictly above free; there are no built-in
allowances; errors name the setting, never its value; and while off, limit
settings are never read and cannot change application behavior.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys

import pytest

os.environ.setdefault("GROQ_API_KEY", "test-not-real")

from kalillac_accounts import usage_limits
from kalillac_accounts.usage_limits import (
    ENFORCEMENT_FLAG,
    FREE_DAILY_LIMIT,
    PAID_DAILY_LIMIT,
    PREREQUISITE_FLAGS,
    UsageLimitConfigError,
    UsageLimits,
    load_usage_limits,
    usage_enforcement_enabled,
)


CANDIDATE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PREREQUISITES_ON = {flag: "true" for flag in PREREQUISITE_FLAGS}

VALID = {
    ENFORCEMENT_FLAG: "true",
    **PREREQUISITES_ON,
    FREE_DAILY_LIMIT: "20",
    PAID_DAILY_LIMIT: "200",
}

# Distinctive values so a leak into an error message is unambiguous.
MALFORMED_VALUES = [
    "abc-leak-1",
    "12.5",
    "1e3",
    "+7",
    "1_000",
    "0x10",
    "٣",  # Arabic-Indic digit three: int() accepts it, the setting must not
    "7 days",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Start every test with no KALILLAC_* settings at all."""

    for key in list(os.environ):
        if key.startswith("KALILLAC_"):
            monkeypatch.delenv(key)


def _set(monkeypatch, values):
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def _load_error(monkeypatch, values):
    _set(monkeypatch, values)

    with pytest.raises(UsageLimitConfigError) as caught:
        load_usage_limits()

    return str(caught.value)


# --- defaults --------------------------------------------------------------------------


def test_enforcement_defaults_off():
    assert usage_enforcement_enabled() is False
    assert load_usage_limits() is None


@pytest.mark.parametrize("value", ["", "false", "0", "no", "off", "enabled", "2"])
def test_non_true_flag_values_leave_enforcement_off(monkeypatch, value):
    _set(monkeypatch, {**VALID, ENFORCEMENT_FLAG: value})

    assert usage_enforcement_enabled() is False
    assert load_usage_limits() is None


def test_no_builtin_allowances():
    defaults = [
        field.default
        for field in dataclasses.fields(UsageLimits)
        if field.default is not dataclasses.MISSING
        or field.default_factory is not dataclasses.MISSING
    ]

    assert defaults == []


# --- valid configuration ------------------------------------------------------------------


@pytest.mark.parametrize("flag_value", ["1", "true", "TRUE", " yes ", "on"])
def test_valid_enabled_configuration(monkeypatch, flag_value):
    _set(monkeypatch, {**VALID, ENFORCEMENT_FLAG: flag_value})

    assert usage_enforcement_enabled() is True
    assert load_usage_limits() == UsageLimits(
        free_daily_chats=20,
        paid_daily_chats=200,
    )


def test_limit_values_are_trimmed_ints(monkeypatch):
    _set(monkeypatch, {**VALID, FREE_DAILY_LIMIT: " 1 ", PAID_DAILY_LIMIT: "02"})

    limits = load_usage_limits()

    assert limits == UsageLimits(free_daily_chats=1, paid_daily_chats=2)
    assert type(limits.free_daily_chats) is int
    assert type(limits.paid_daily_chats) is int


def test_settings_object_is_immutable(monkeypatch):
    _set(monkeypatch, VALID)
    limits = load_usage_limits()

    with pytest.raises(dataclasses.FrozenInstanceError):
        limits.free_daily_chats = 10_000

    with pytest.raises(dataclasses.FrozenInstanceError):
        limits.paid_daily_chats = 10_000


# --- prerequisites -----------------------------------------------------------------------


@pytest.mark.parametrize("missing", PREREQUISITE_FLAGS)
def test_each_missing_prerequisite_is_named(monkeypatch, missing):
    values = {key: value for key, value in VALID.items() if key != missing}

    message = _load_error(monkeypatch, values)

    assert message == f"{ENFORCEMENT_FLAG} requires {missing}."


@pytest.mark.parametrize("missing", PREREQUISITE_FLAGS)
def test_disabled_prerequisite_is_named(monkeypatch, missing):
    message = _load_error(monkeypatch, {**VALID, missing: "false"})

    assert message == f"{ENFORCEMENT_FLAG} requires {missing}."


def test_prerequisites_cover_accounts_database_and_metering():
    assert set(PREREQUISITE_FLAGS) == {
        "KALILLAC_ACCOUNTS_ENABLED",
        "KALILLAC_DB_ENABLED",
        "KALILLAC_USAGE_METERING_ENABLED",
    }


# --- limit values ------------------------------------------------------------------------


@pytest.mark.parametrize("name", [FREE_DAILY_LIMIT, PAID_DAILY_LIMIT])
def test_missing_limit_is_named(monkeypatch, name):
    values = {key: value for key, value in VALID.items() if key != name}

    message = _load_error(monkeypatch, values)

    assert message == f"{ENFORCEMENT_FLAG} is enabled but {name} is not set."


@pytest.mark.parametrize("name", [FREE_DAILY_LIMIT, PAID_DAILY_LIMIT])
@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_blank_limit_is_not_set(monkeypatch, name, blank):
    message = _load_error(monkeypatch, {**VALID, name: blank})

    assert message == f"{ENFORCEMENT_FLAG} is enabled but {name} is not set."


@pytest.mark.parametrize("name", [FREE_DAILY_LIMIT, PAID_DAILY_LIMIT])
@pytest.mark.parametrize("value", MALFORMED_VALUES)
def test_malformed_limit_is_rejected(monkeypatch, name, value):
    message = _load_error(monkeypatch, {**VALID, name: value})

    assert message == f"{name} must be a positive integer."


@pytest.mark.parametrize("name", [FREE_DAILY_LIMIT, PAID_DAILY_LIMIT])
@pytest.mark.parametrize("value", ["0", "000", "-1", "-25"])
def test_zero_and_negative_limits_are_rejected(monkeypatch, name, value):
    message = _load_error(monkeypatch, {**VALID, name: value})

    assert message == f"{name} must be a positive integer."


@pytest.mark.parametrize("free, paid", [("20", "20"), ("20", "19"), ("5", "1")])
def test_paid_limit_must_exceed_free(monkeypatch, free, paid):
    message = _load_error(
        monkeypatch,
        {**VALID, FREE_DAILY_LIMIT: free, PAID_DAILY_LIMIT: paid},
    )

    assert message == f"{PAID_DAILY_LIMIT} must be greater than {FREE_DAILY_LIMIT}."


def test_paid_one_above_free_is_valid(monkeypatch):
    _set(monkeypatch, {**VALID, FREE_DAILY_LIMIT: "20", PAID_DAILY_LIMIT: "21"})

    assert load_usage_limits() == UsageLimits(
        free_daily_chats=20,
        paid_daily_chats=21,
    )


# --- safe messages -----------------------------------------------------------------------


@pytest.mark.parametrize("name", [FREE_DAILY_LIMIT, PAID_DAILY_LIMIT])
@pytest.mark.parametrize("value", MALFORMED_VALUES + ["-987654"])
def test_error_messages_never_contain_the_value(monkeypatch, name, value):
    message = _load_error(monkeypatch, {**VALID, name: value})

    assert value.strip() not in message
    assert name in message


def test_ordering_error_does_not_contain_values(monkeypatch):
    message = _load_error(
        monkeypatch,
        {**VALID, FREE_DAILY_LIMIT: "987651", PAID_DAILY_LIMIT: "987650"},
    )

    assert "987651" not in message
    assert "987650" not in message


@pytest.mark.parametrize("name", [FREE_DAILY_LIMIT, PAID_DAILY_LIMIT])
def test_oversized_digit_string_is_a_typed_safe_error(monkeypatch, name):
    # 5,000 digits exceeds Python's int string-conversion limit, so int()
    # raises ValueError; it must surface as the typed configuration error.
    value = "9" * 5000
    _set(monkeypatch, {**VALID, name: value})

    with pytest.raises(UsageLimitConfigError) as caught:
        load_usage_limits()

    error = caught.value
    assert type(error) is UsageLimitConfigError
    assert str(error) == f"{name} must be a positive integer."
    assert "9999" not in str(error)
    assert error.__cause__ is None
    assert error.__suppress_context__ is True


def test_error_has_no_chained_value(monkeypatch):
    _set(monkeypatch, {**VALID, FREE_DAILY_LIMIT: "abc-leak-1"})

    with pytest.raises(UsageLimitConfigError) as caught:
        load_usage_limits()

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


# --- disabled-mode compatibility ---------------------------------------------------------


@pytest.mark.parametrize(
    "leftovers",
    [
        {FREE_DAILY_LIMIT: "abc-leak-1", PAID_DAILY_LIMIT: "-5"},
        {FREE_DAILY_LIMIT: "50", PAID_DAILY_LIMIT: "10"},
        {FREE_DAILY_LIMIT: "", PAID_DAILY_LIMIT: "0"},
    ],
)
def test_disabled_mode_ignores_limit_settings(monkeypatch, leftovers):
    _set(monkeypatch, {**PREREQUISITES_ON, **leftovers})

    assert load_usage_limits() is None


def test_disabled_mode_never_reads_limit_settings(monkeypatch):
    read = []
    real_getenv = os.getenv

    def spy(name, default=None):
        read.append(name)
        return real_getenv(name, default)

    monkeypatch.setattr(usage_limits.os, "getenv", spy)

    assert load_usage_limits() is None
    assert FREE_DAILY_LIMIT not in read
    assert PAID_DAILY_LIMIT not in read


def test_application_loads_validated_default_off_configuration():
    # This phase now wires the existing loader. Keep the guard: no quota
    # implementation is constructed while the loader returns None.
    import ast
    with open(os.path.join(CANDIDATE_DIR, "app_fastapi_candidate.py"), encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    assert any(isinstance(node, ast.ImportFrom) and node.module == "kalillac_accounts.usage_limits" for node in tree.body)
    guarded = [node for node in tree.body if isinstance(node, ast.If) and ast.unparse(node.test) == "_usage_limits is not None"]
    assert len(guarded) == 1
    assert any(isinstance(node, ast.ImportFrom) and node.module == "kalillac_accounts.quota" for node in guarded[0].body)

def _run_app_with(env_overrides):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("KALILLAC_")
    }
    env.update(env_overrides)
    env.setdefault("GROQ_API_KEY", "test-not-real")

    script = (
        "import app_fastapi_candidate as a;"
        "from fastapi.testclient import TestClient;"
        "c = TestClient(a.api, base_url='https://testserver');"
        "print(c.get('/api/health').status_code,"
        " c.get('/api/account/usage').status_code,"
        " a._usage_meter is None)"
    )

    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=CANDIDATE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_unused_limit_settings_do_not_change_startup():
    baseline = _run_app_with({})
    with_leftovers = _run_app_with(
        {FREE_DAILY_LIMIT: "abc-leak-1", PAID_DAILY_LIMIT: "-5"}
    )

    assert baseline.returncode == 0, baseline.stderr
    assert with_leftovers.returncode == 0, with_leftovers.stderr
    assert (
        with_leftovers.stdout.strip().splitlines()[-1]
        == baseline.stdout.strip().splitlines()[-1]
        == "200 404 True"
    )
    assert "abc-leak-1" not in with_leftovers.stdout + with_leftovers.stderr


# --- anonymous daily allowance (configuration only) ---------------------------------------

ANONYMOUS_VALID = {
    usage_limits.ANONYMOUS_FLAG: "true",
    usage_limits.ANONYMOUS_PREREQUISITE_FLAG: "true",
    usage_limits.ANONYMOUS_DAILY_LIMIT: "10",
}

# Distinctive values so a leak into an error message is unambiguous.
ANONYMOUS_REJECTED_VALUES = [
    "11", "9", "0", "-10", "010", "10.0", "1e1", "+10", "1_0", "0x0a", "٣٣",
    "10 per day", "ten", "anon-leak-77", "99999999999999999999999",
]


def _anonymous_error(monkeypatch, values):
    _set(monkeypatch, values)

    with pytest.raises(UsageLimitConfigError) as caught:
        usage_limits.load_anonymous_daily_limit()

    return str(caught.value)


def test_anonymous_allowance_defaults_off():
    assert usage_limits.anonymous_allowance_enabled() is False
    assert usage_limits.load_anonymous_daily_limit() is None


@pytest.mark.parametrize("value", ["", "false", "0", "no", "off", "enabled", "1x"])
def test_non_true_flag_values_leave_anonymous_allowance_off(monkeypatch, value):
    monkeypatch.setenv(usage_limits.ANONYMOUS_FLAG, value)
    # Leftover or malformed settings are never read while the switch is off.
    monkeypatch.setenv(usage_limits.ANONYMOUS_DAILY_LIMIT, "anon-leak-77")

    assert usage_limits.load_anonymous_daily_limit() is None


@pytest.mark.parametrize("flag_value", ["1", "true", "TRUE", "yes", "on", " On "])
def test_enabled_with_database_and_exactly_ten(monkeypatch, flag_value):
    _set(monkeypatch, {**ANONYMOUS_VALID, usage_limits.ANONYMOUS_FLAG: flag_value})

    assert usage_limits.load_anonymous_daily_limit() == 10
    assert usage_limits.APPROVED_ANONYMOUS_DAILY_LIMIT == 10


def test_surrounding_whitespace_is_trimmed_like_other_limits(monkeypatch):
    _set(monkeypatch, {**ANONYMOUS_VALID, usage_limits.ANONYMOUS_DAILY_LIMIT: " 10 "})

    assert usage_limits.load_anonymous_daily_limit() == 10


def test_anonymous_allowance_requires_database_not_accounts(monkeypatch):
    values = {k: v for k, v in ANONYMOUS_VALID.items() if k != usage_limits.ANONYMOUS_PREREQUISITE_FLAG}

    message = _anonymous_error(monkeypatch, values)
    assert message == (
        "KALILLAC_ANONYMOUS_DAILY_LIMIT_ENABLED requires KALILLAC_DB_ENABLED."
    )

    monkeypatch.setenv(usage_limits.ANONYMOUS_PREREQUISITE_FLAG, "false")
    assert _anonymous_error(monkeypatch, {}) == message

    # Accounts, metering and account enforcement stay off: not required.
    _set(monkeypatch, ANONYMOUS_VALID)
    assert usage_limits.load_anonymous_daily_limit() == 10
    assert load_usage_limits() is None


@pytest.mark.parametrize("value", ["", "   "])
def test_missing_anonymous_limit_is_named(monkeypatch, value):
    message = _anonymous_error(monkeypatch, {**ANONYMOUS_VALID, usage_limits.ANONYMOUS_DAILY_LIMIT: value})

    assert message == (
        "KALILLAC_ANONYMOUS_DAILY_LIMIT_ENABLED is enabled but "
        "KALILLAC_ANONYMOUS_DAILY_LIMIT is not set."
    )


def test_unset_anonymous_limit_is_named(monkeypatch):
    values = {k: v for k, v in ANONYMOUS_VALID.items() if k != usage_limits.ANONYMOUS_DAILY_LIMIT}

    assert "KALILLAC_ANONYMOUS_DAILY_LIMIT is not set" in _anonymous_error(monkeypatch, values)


@pytest.mark.parametrize("value", ANONYMOUS_REJECTED_VALUES)
def test_every_other_anonymous_limit_is_rejected_without_echo(monkeypatch, value):
    message = _anonymous_error(monkeypatch, {**ANONYMOUS_VALID, usage_limits.ANONYMOUS_DAILY_LIMIT: value})

    # A fixed message: the supplied value is never echoed.
    assert message == "KALILLAC_ANONYMOUS_DAILY_LIMIT must be exactly 10."


def test_anonymous_allowance_is_independent_of_account_enforcement(monkeypatch):
    # Account enforcement settings do not enable or configure the anonymous
    # allowance, and the anonymous settings do not enable account enforcement.
    _set(monkeypatch, VALID)
    assert usage_limits.load_anonymous_daily_limit() is None
    assert load_usage_limits() == UsageLimits(free_daily_chats=20, paid_daily_chats=200)
