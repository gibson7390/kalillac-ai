"""Email-verification verifier: destination, redaction, cleanup and exact check-count contracts."""
from argparse import Namespace
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

PATH = Path(__file__).resolve().parents[1] / "tools/verify_email_verification_postgres.py"
spec = importlib.util.spec_from_file_location("verify_email_verification_postgres", PATH)
tool = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = tool
spec.loader.exec_module(tool)

ARGS = Namespace(expect_db="kalillac_staging", expect_user="kalillac_staging_app")
ENV = {
    "KALILLAC_DB_NAME": "kalillac_staging",
    "KALILLAC_DB_USER": "kalillac_staging_app",
    "KALILLAC_DB_HOST": "127.0.0.1",
    "KALILLAC_DB_PORT": "5432",
    "KALILLAC_DB_PASSWORD": "disposable-test-password",
}
CHECK_NAMES = (
    "check_schema_and_grants", "check_storage_and_reissue", "check_success_changes_only_verification",
    "check_rejections_change_nothing", "check_same_token_race", "check_sibling_token_race",
    "check_concurrent_issuance_leaves_one_usable_token", "check_expiry_after_lock",
    "check_account_lock_isolation", "check_transaction_limits_precede_account_lock",
    "check_lock_timeout_rolls_back", "check_rollback_after_mutation",
    "check_delivery_failure_rolls_back_issuance", "check_verified_and_inactive_accounts_get_nothing",
    "check_constraints_and_cascade",
)


def test_fixed_destination_and_exact_fifteen_checks():
    tool.validate_destination(ARGS, ENV)
    assert tool.REQUIRED_DATABASE == "kalillac_staging"
    assert tool.REQUIRED_ROLE == "kalillac_staging_app"
    assert tuple(check.__name__ for check in tool.CHECKS) == CHECK_NAMES
    assert len(set(tool.CHECKS)) == 15


def test_workflow_requires_the_exact_result():
    workflow = (Path(__file__).resolve().parents[2] / ".github/workflows/subscription-foundation-verification.yml").read_text()
    assert "tools/verify_email_verification_postgres.py" in workflow
    assert '"RESULT: 15/15 checks passed"' in workflow
    assert '"RESULT: 14/14 checks passed"' in workflow and "RESULT: 17/17 checks passed" in workflow


@pytest.mark.parametrize("setting,value", [
    ("KALILLAC_DB_NAME", "production"), ("KALILLAC_DB_USER", "kalillac_app"),
    ("KALILLAC_DATABASE_URL", "postgresql://private:password@remote/production"),
    ("KALILLAC_DB_HOST", "remote.example.com"), ("KALILLAC_DB_HOST", "::1"),
    ("KALILLAC_DB_PORT", "0"), ("KALILLAC_DB_PORT", "65536"), ("KALILLAC_DB_PORT", "54x"),
    ("PGHOSTADDR", "203.0.113.1"), ("PGSERVICE", "private-service"),
    ("PGOPTIONS", "-c search_path=public"), ("PGPASSFILE", "private-file"),
    ("KALILLAC_DB_PASSWORD", ""),
])
def test_refuses_unsafe_environment_before_building_any_engine(setting, value, monkeypatch, capsys):
    monkeypatch.setattr(tool, "build_engine", lambda env: pytest.fail("engine built before validation"))
    result = tool.main(["--expect-db", ARGS.expect_db, "--expect-user", ARGS.expect_user], {**ENV, setting: value})
    assert result == 2
    output = capsys.readouterr()
    assert output.out == "VERIFICATION_VERIFY_REFUSED\n" and output.err == ""


@pytest.mark.parametrize("argv", [
    [], ["--expect-db", "production", "--expect-user", "kalillac_staging_app"],
    ["--expect-db", "kalillac_staging", "--expect-user", "postgres"],
    ["--secret=not-for-output"],
])
def test_refuses_bad_arguments_without_echo(argv, monkeypatch, capsys):
    monkeypatch.setattr(tool, "build_engine", lambda env: pytest.fail("engine built"))
    assert tool.main(argv, ENV) == 2
    assert capsys.readouterr().out == "VERIFICATION_VERIFY_REFUSED\n"


def test_engine_forces_loopback_and_safe_connection_parameters(monkeypatch):
    captured = {}
    def create(url, **kwargs):
        captured.update(url=url, **kwargs)
        return object()
    monkeypatch.setattr(tool, "create_engine", create)
    tool.build_engine(ENV)
    assert captured["url"].database == "kalillac_staging"
    assert captured["url"].username == "kalillac_staging_app"
    assert captured["url"].host == "127.0.0.1"
    assert captured["hide_parameters"] and not captured["echo"]
    assert captured["pool_size"] == 8 and captured["max_overflow"] == 0
    args = captured["connect_args"]
    assert args["hostaddr"] == "127.0.0.1" and args["connect_timeout"] == 3
    assert args["options"] == "-c lock_timeout=10s -c statement_timeout=45s"
    assert args["passfile"] == os.devnull and args["sslmode"] == "disable"
    assert args["application_name"] == "kalillac-verification-verify"


def test_connection_failure_is_contained_and_disposed(monkeypatch, capsys):
    disposed = []
    monkeypatch.setattr(tool, "build_engine", lambda env: SimpleNamespace(dispose=lambda: disposed.append(True)))
    def fail(engine):
        raise RuntimeError("private SQL parameters database URL password token email")
    monkeypatch.setattr(tool, "guard", fail)
    assert tool.main(["--expect-db", ARGS.expect_db, "--expect-user", ARGS.expect_user], ENV) == 1
    output = capsys.readouterr()
    assert output.out == "VERIFICATION_VERIFY_FAILED\n" and output.err == ""
    assert disposed == [True]


def test_runner_failure_prints_no_exception_or_context(monkeypatch, capsys):
    cleaned = []
    def bad(ctx):
        raise RuntimeError("private-verification-link private-email private-token")
    ctx = SimpleNamespace(settle=lambda: True)
    monkeypatch.setattr(tool, "cleanup", lambda context: cleaned.append(context))
    assert tool.execute(ctx, [bad]) == 1
    assert capsys.readouterr().out == "FAIL bad\nRESULT: 0/1 checks passed\n"
    assert cleaned == [ctx]


def test_active_worker_refuses_cleanup(monkeypatch, capsys):
    monkeypatch.setattr(tool, "cleanup", lambda context: pytest.fail("unsafe cleanup"))
    ctx = SimpleNamespace(settle=lambda: False)
    assert tool.execute(ctx, [lambda ctx: None]) == 3
    assert capsys.readouterr().out == "PASS <lambda>\nVERIFICATION_VERIFY_WORKERS_ACTIVE\n"


def test_refuses_optimized_python_before_connection():
    result = subprocess.run(
        [sys.executable, "-O", "-B", str(PATH), "--expect-db", ARGS.expect_db, "--expect-user", ARGS.expect_user],
        env={**os.environ, **ENV}, capture_output=True, text=True,
    )
    assert result.returncode == 2 and result.stdout == "VERIFICATION_VERIFY_REFUSED\n"


def test_no_base_exception_is_treated_as_ordinary_failure(monkeypatch):
    monkeypatch.setattr(tool, "cleanup", lambda ctx: None)
    def stop(ctx):
        raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        tool.execute(SimpleNamespace(settle=lambda: True), [stop])


def test_tool_has_no_ddl_or_real_provider_calls():
    source = PATH.read_text()
    for forbidden in ("CREATE TABLE", "ALTER TABLE", "DROP ", "GRANT ", "alembic", "import stripe",
                      "StripeClient", "StripeGateway", "smtplib", "requests", "httpx", "urllib.request",
                      "KALILLAC_DATABASE_URL\"]"):
        assert forbidden not in source, forbidden


def test_guard_checks_client_loopback_without_assuming_container_server_address():
    class Result:
        def __init__(self, value):
            self.value = value
        def one(self):
            return self.value
        def scalar(self):
            return self.value
    class Connection:
        connection = SimpleNamespace(driver_connection=SimpleNamespace(
            info=SimpleNamespace(hostaddr="127.0.0.1", port=5432)))
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def execute(self, statement):
            statement = str(statement)
            if "current_database()" in statement:
                return Result(("kalillac_staging", "kalillac_staging_app", "kalillac_staging_app", "172.17.0.2", 5432))
            if "to_regclass" in statement:
                assert "account_email_verification_tokens" in statement
                return Result(True)
            return Result(("10s", "45s"))
    tool.guard(SimpleNamespace(connect=lambda: Connection(), url=SimpleNamespace(port=5432)))
    class WrongPeer(Connection):
        connection = SimpleNamespace(driver_connection=SimpleNamespace(
            info=SimpleNamespace(hostaddr="203.0.113.9", port=5432)))
    with pytest.raises(tool.VerificationRefused):
        tool.guard(SimpleNamespace(connect=lambda: WrongPeer(), url=SimpleNamespace(port=5432)))


@pytest.mark.parametrize("name", [
    "check_storage_and_reissue", "check_success_changes_only_verification",
    "check_rejections_change_nothing", "check_same_token_race", "check_sibling_token_race",
    "check_rollback_after_mutation", "check_delivery_failure_rolls_back_issuance",
    "check_verified_and_inactive_accounts_get_nothing", "check_constraints_and_cascade",
])
def test_database_independent_verifier_checks_on_separate_sqlite_connections(tmp_path, name, capsys):
    from account_test_db import make_session_factory, make_sqlite_file_engine
    from kalillac_db.models import Base
    engine = make_sqlite_file_engine(str(tmp_path))
    Base.metadata.create_all(engine)
    ctx = tool.Context(engine, make_session_factory(engine))
    try:
        getattr(tool, name)(ctx)
        assert ctx.settle()
        tool.cleanup(ctx)
    finally:
        assert ctx.settle()
        engine.dispose()
    # Service event codes raised inside a check never reach the tool's output.
    assert capsys.readouterr().out == ""
