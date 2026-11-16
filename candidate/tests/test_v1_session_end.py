"""0018 without a database: its place in the graph, the exact statements it runs, and the
classifier's knowledge of the function it installs."""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory

import kalillac_accounts.v1_schema as v1_schema
from test_account_migration import _config

PATH = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0018_v1_session_end_authority.py"
NAMES = {"service": "kalillac_s", "executor": "kalillac_e", "owner": "kalillac_o", "runtime": "kalillac_r"}


def _module():
    spec = importlib.util.spec_from_file_location("v1_0018_session_end", PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_0018_sits_directly_after_0017_below_0019():
    script = ScriptDirectory.from_config(_config())
    assert script.get_revision("0019_v1_password_reset").down_revision == "0018_v1_session_end"
    assert script.get_revision("0018_v1_session_end").down_revision == "0017_billing_cases"
    module = _module()
    assert (module.revision, module.down_revision, module.branch_labels, module.depends_on) == (
        "0018_v1_session_end", "0017_billing_cases", None, None)


def test_the_classifier_knows_the_exact_body_it_installs():
    module = _module()
    assert hashlib.sha256(module.BODY.encode()).hexdigest() == v1_schema.END_SESSION_SOURCE_SHA256
    assert v1_schema.END_SESSION_SIGNATURE == module.SIGNATURE.replace(", ", ",")
    assert "SECURITY DEFINER" in module.FUNCTION and "SET search_path = pg_catalog, pg_temp" in module.FUNCTION


def test_the_upgrade_proves_0014_installs_one_function_and_proves_the_result():
    statements = _module().upgrade_statements(NAMES)
    assert "V1_SESSION_END_PREREQUISITE_INVALID" in statements[0] and "v1_issue_session" in statements[0]
    assert "V1_SESSION_END_PREREQUISITE_INVALID" in statements[1] and "v1_end_session" in statements[1]
    assert statements[2:-1] == [
        "GRANT CREATE ON SCHEMA kalillac TO kalillac_e",
        _module().FUNCTION,
        "REVOKE ALL ON FUNCTION kalillac.v1_end_session(uuid, text) FROM PUBLIC",
        "GRANT EXECUTE ON FUNCTION kalillac.v1_end_session(uuid, text) TO kalillac_s",
        "ALTER FUNCTION kalillac.v1_end_session(uuid, text) OWNER TO kalillac_e",
        "REVOKE CREATE ON SCHEMA kalillac FROM kalillac_e",
    ]
    post = statements[-1]
    assert "V1_SESSION_END_POSTCONDITION_INVALID" in post and post.count("(fn, issue, ending)") == 6
    assert "<> 6" in post and "AND d.deptype = 'o') <> 3" in post and "(fn, issue)" not in post
    assert hashlib.sha256(_module().BODY.encode()).hexdigest() in post
    # No grant on data, no role, no event type: only the function and its ACL.
    assert not any(word in " ".join(statements[2:-1]) for word in ("ON kalillac.users", "ROLE ", "event"))


def test_the_downgrade_proves_0018_drops_the_function_as_e_and_proves_0014():
    statements = _module().downgrade_statements(NAMES)
    assert "V1_SESSION_END_POSTCONDITION_INVALID" in statements[0]
    assert statements[1:6] == ["GRANT CREATE ON SCHEMA kalillac TO kalillac_e", "SET ROLE kalillac_e",
                               "DROP FUNCTION kalillac.v1_end_session(uuid, text)", "SET ROLE kalillac_o",
                               "REVOKE CREATE ON SCHEMA kalillac FROM kalillac_e"]
    assert all("V1_SESSION_END_DOWNGRADE_INVALID" in statement for statement in statements[6:])


def test_offline_downgrade_is_refused(monkeypatch):
    module = _module()

    class Context:
        as_sql = True

        class dialect:
            name = "postgresql"
    monkeypatch.setattr(module.op, "get_context", lambda: Context())
    with pytest.raises(RuntimeError, match="V1_SESSION_END_OFFLINE_DOWNGRADE_UNSUPPORTED"):
        module.downgrade()
