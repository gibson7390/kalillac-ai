"""Unit tests for deploy/release/envcheck.py (EnvironmentFile subset, policy) and bytecheck.py.

    python -m pytest deploy/release/tests/test_verifiers.py -v

The systemd composition itself is exercised against real systemd by disposable-systemd.sh.
"""
import argparse
import importlib._bootstrap_external as bootstrap
import importlib.util
import os
from pathlib import Path
import py_compile
import sys

import pytest

HERE = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location("kdt_" + name, HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


envcheck = load("envcheck")
bytecheck = load("bytecheck")


# ------------------------------------------------------------------ EnvironmentFile subset
def parse(tmp_path, text):
    path = tmp_path / "service.env"
    path.write_bytes(text.encode("utf-8"))
    return envcheck.parse_env_file(str(path))


def test_supported_syntax(tmp_path):
    assert parse(tmp_path, "# c\n; c\n\nA=1\n  B = two words  \nC=\"x y\"\nD='p#q'\nE=\nF=a#b\n") == {
        "A": "1", "B": "two words", "C": "x y", "D": "p#q", "E": "", "F": "a#b"}


@pytest.mark.parametrize("text", [
    "A=1\\\nB=2\n",          # continuation line
    "A=$HOME\n",             # expansion
    "A=`id`\n",              # command substitution
    "A=\"x\\\"y\"\n",        # escape in quotes
    "export A=1\n",          # export prefix
    "A=\"unterminated\n",    # unmatched quote
    "A=x #comment\n",        # inline comment after whitespace
    "A=1\r\n",               # CR
    "1A=1\n",                # invalid name
    "A\n",                   # no assignment
])
def test_unsupported_syntax_fails_closed(tmp_path, text):
    with pytest.raises(envcheck.Refuse):
        parse(tmp_path, text)


def test_refusal_never_contains_the_value(tmp_path):
    with pytest.raises(envcheck.Refuse) as refusal:
        parse(tmp_path, "SECRET=sk-live-abc$def\n")
    assert "sk-live" not in str(refusal.value)


# ------------------------------------------------------------------ next-start policy
ARGS = argparse.Namespace(flags_off="KALILLAC_BILLING_ENABLED KALILLAC_SPACES_ENABLED",
                          flags_on="KALILLAC_PROVIDER_CEILINGS_ENABLED",
                          flag_values="KALILLAC_ANONYMOUS_DAILY_LIMIT=10", model="gpt-6-luna")
GOOD = {"KALILLAC_PROVIDER_CEILINGS_ENABLED": "true", "KALILLAC_ANONYMOUS_DAILY_LIMIT": "10",
        **{"KALILLAC_%s_%s" % (p, k): "5" for p in envcheck.CEILING_PROVIDERS for k in envcheck.CEILING_KINDS}}


def test_policy_accepts_the_approved_state():
    envcheck.policy(dict(GOOD), ARGS)


@pytest.mark.parametrize("change, text", [
    ({"KALILLAC_BILLING_ENABLED": "yes"}, "KALILLAC_BILLING_ENABLED would be ON"),
    ({"KALILLAC_PROVIDER_CEILINGS_ENABLED": "false"}, "provider ceilings would be disabled"),
    ({"KALILLAC_BRAVE_TOTAL_ATTEMPTS": ""}, "KALILLAC_BRAVE_TOTAL_ATTEMPTS would not be an explicit integer"),
    ({"KALILLAC_ANONYMOUS_DAILY_LIMIT": "50"}, "KALILLAC_ANONYMOUS_DAILY_LIMIT would not be 10"),
    ({"OPENAI_MODEL": "other"}, "OPENAI_MODEL would not be gpt-6-luna"),
])
def test_policy_refuses(change, text):
    with pytest.raises(envcheck.Refuse) as refusal:
        envcheck.policy({**GOOD, **change}, ARGS)
    assert text in str(refusal.value)


def test_snapshot_holds_no_value():
    snap = envcheck.snapshot({"OPENAI_API_KEY": "sk-secret"})
    assert "sk-secret" not in repr(snap) and len(snap["OPENAI_API_KEY"]) == 64


# ------------------------------------------------------------------ bytecode
def release(tmp_path):
    root = tmp_path / "release"
    (root / "pkg").mkdir(parents=True)
    (root / ".git").mkdir()
    source = root / "pkg" / "mod.py"
    source.write_text("VALUE = 1\n\ndef f():\n    return VALUE + 1\n")
    return root, source


def test_genuine_bytecode_passes(tmp_path):
    root, source = release(tmp_path)
    py_compile.compile(str(source), doraise=True)
    py_compile.compile(str(source), doraise=True, optimize=1)
    assert bytecheck.check(str(root)) == "BYTECODE_OK 2"


def write_pyc(source, text, name=None):
    stat = os.stat(source)
    code = compile(text, str(source), "exec", dont_inherit=True)
    target = Path(importlib.util.cache_from_source(str(source)))
    if name:
        target = target.with_name(name)
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(bootstrap._code_to_timestamp_pyc(code, int(stat.st_mtime), stat.st_size))
    return target


def test_metadata_valid_altered_bytecode_fails(tmp_path):
    root, source = release(tmp_path)
    write_pyc(source, "VALUE = 2\n\ndef f():\n    return VALUE + 1\n")
    assert "code differs from its source" in bytecheck.check(str(root))


@pytest.mark.parametrize("name, text", [
    ("mod.cpython-299.pyc", "not bytecode of this interpreter"),
    ("mod.%s.opt-9.pyc" % sys.implementation.cache_tag, "unknown optimization tag"),
    ("gone.%s.pyc" % sys.implementation.cache_tag, "no regular source file"),
])
def test_unverifiable_bytecode_fails(tmp_path, name, text):
    root, source = release(tmp_path)
    write_pyc(source, source.read_text(), name=name)
    assert text in bytecheck.check(str(root))


def test_sourceless_bytecode_fails(tmp_path):
    root, source = release(tmp_path)
    write_pyc(source, source.read_text()).rename(root / "pkg" / "mod.pyc")
    assert "bytecode outside __pycache__" in bytecheck.check(str(root))


def test_git_directory_is_not_scanned(tmp_path):
    root, _ = release(tmp_path)
    (root / ".git" / "x.pyc").write_bytes(b"\0" * 32)
    assert bytecheck.check(str(root)) == "BYTECODE_OK 0"


# ------------------------------------------------------------------ launcher-supplied variables
# systemd's next-start configuration (no BRAVE_API_KEY) and a process whose launcher added one.
DECLARED = {"PATH": "/usr/bin", "OPENAI_API_KEY": "sk-declared", "KALILLAC_BILLING_ENABLED": "false",
            "KALILLAC_PROVIDER_CEILINGS_ENABLED": "true"}
RUNNING = {**DECLARED, "BRAVE_API_KEY": "brave-synthetic-secret", "INVOCATION_ID": "x", "JOURNAL_STREAM": "1:2"}


def refusal(env, running, launcher):
    with pytest.raises(envcheck.Refuse) as refused:
        envcheck.compare(env, running, set(launcher))
    message = str(refused.value)
    assert "secret" not in message and "sk-" not in message and "synthetic" not in message
    return message


def test_launcher_brave_key_passes_when_exactly_approved():
    assert envcheck.compare(DECLARED, RUNNING, {"BRAVE_API_KEY"}) == ["BRAVE_API_KEY"]


@pytest.mark.parametrize("approved", [set(), {"TAVILY_API_KEY"}, {"brave_api_key"}, {"BRAVE_API"}, {"BRAVE_API_KEY_2"}])
def test_launcher_brave_key_refused_unless_exactly_approved(approved):
    assert refusal(DECLARED, RUNNING, approved).endswith("without approval): BRAVE_API_KEY")


@pytest.mark.parametrize("change, text", [
    ({"TAVILY_API_KEY": "tvly-synthetic-secret"}, "without approval): TAVILY_API_KEY"),    # another credential
    ({"OPENAI_API_KEY": "sk-override"}, "pending change) for: OPENAI_API_KEY"),            # override of a declared one
    ({"KALILLAC_BILLING_ENABLED": "true"}, "pending change) for: KALILLAC_BILLING_ENABLED"),
    ({"PYTHONPATH": "/opt/kalillac-release-f126a245/candidate"}, "without approval): PYTHONPATH"),
])
def test_other_launcher_additions_and_overrides_still_refused(change, text):
    assert text in refusal(DECLARED, {**RUNNING, **change}, {"BRAVE_API_KEY"})


def test_launcher_removing_a_declared_variable_is_refused():
    running = {k: v for k, v in RUNNING.items() if k != "KALILLAC_PROVIDER_CEILINGS_ENABLED"}
    assert "pending change) for: KALILLAC_PROVIDER_CEILINGS_ENABLED" in refusal(DECLARED, running, {"BRAVE_API_KEY"})


def test_pending_systemd_change_still_detected_with_brave_approved():
    pending = {**DECLARED, "KALILLAC_BILLING_ENABLED": "true"}   # reloaded drop-in, not yet in the process
    assert "pending change) for: KALILLAC_BILLING_ENABLED" in refusal(pending, RUNNING, {"BRAVE_API_KEY"})
