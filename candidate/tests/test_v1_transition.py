"""E1 and E2 without a database server: the final-grant statements and their inverse, 0025's statements,
the SCRAM verifier the transition sends instead of a password, and the tool's refusals before any change.
Real PostgreSQL: test_v1_transition_postgres.py and test_v1_lifecycle_postgres.py (E3)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import re
import sys
from pathlib import Path

import pytest

from kalillac_accounts.v1_final_grants import (FINAL, LEGACY, TABLE_RIGHTS, GrantStateInvalid, e1a_statements,
                                               rollback_statements)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import v1_security_transition as transition  # noqa: E402

COLUMNS = ["id", "email", "password_hash", "created_at", "updated_at", "is_active", "email_verified_at",
           "registration_intent_id", "quarantined_at", "sessions_valid_after", "deletion_requested_at",
           "password_reset_required", "row_lock_anchor"]


def test_e1a_takes_every_write_and_the_hash_read_and_keeps_reads_and_the_anchor():
    statements = e1a_statements("kalillac_r", COLUMNS)
    grant = next(s for s in statements if s.startswith("GRANT SELECT ("))
    assert "password_hash" not in grant and "UPDATE (row_lock_anchor)" in grant
    assert all(c in grant for c in COLUMNS if c != "password_hash")
    assert "REVOKE SELECT, INSERT, UPDATE, DELETE ON kalillac.users FROM kalillac_r" in statements
    assert "REVOKE INSERT, UPDATE, DELETE ON kalillac.account_sessions FROM kalillac_r" in statements
    assert "GRANT SELECT ON kalillac.account_security_activation TO kalillac_r" in statements
    # Entitlements and billing (E1b) are untouched here.
    assert not any("entitlement" in s or "billing" in s for s in statements)


def test_rollback_is_the_exact_inverse_table_by_table():
    forward, back = e1a_statements("kalillac_r", COLUMNS), rollback_statements("kalillac_r", COLUMNS)
    touched = lambda statements: {re.search(r"ON (kalillac\.\w+)", s).group(1) for s in statements}  # noqa: E731
    assert touched(forward) == touched(back)
    assert "GRANT INSERT, UPDATE, DELETE ON kalillac.account_sessions TO kalillac_r" in back
    assert "REVOKE SELECT ON kalillac.account_security_activation FROM kalillac_r" in back
    assert set(TABLE_RIGHTS) == {"account_sessions", "account_recovery_tokens", "account_email_verification_tokens",
                                 "account_security_activation"}
    assert {LEGACY, FINAL} == {state for rights in TABLE_RIGHTS.values() for state in rights}


@pytest.mark.parametrize("bad", ["kalillac r", "r; DROP TABLE x", "", "R"])
def test_role_names_are_validated_before_any_statement_is_built(bad):
    with pytest.raises(Exception):
        e1a_statements(bad, COLUMNS)


def test_e1a_needs_the_anchor_column():
    with pytest.raises(GrantStateInvalid):
        e1a_statements("kalillac_r", [c for c in COLUMNS if c != "row_lock_anchor"])


def test_the_scram_verifier_matches_rfc_7677_and_carries_no_password():
    salt = bytes(range(16))
    verifier = transition.scram_verifier("pencil", salt=salt)
    method, rest = verifier.split("$", 1)
    iterations_salt, keys = rest.split("$")
    iterations, salt_b64 = iterations_salt.split(":")
    stored_b64, server_b64 = keys.split(":")
    assert method == "SCRAM-SHA-256" and iterations == "4096" and base64.b64decode(salt_b64) == salt
    salted = hashlib.pbkdf2_hmac("sha256", b"pencil", salt, 4096)
    assert base64.b64decode(stored_b64) == hashlib.sha256(hmac.new(salted, b"Client Key", hashlib.sha256).digest()).digest()
    assert base64.b64decode(server_b64) == hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    assert "pencil" not in verifier and re.fullmatch(r"[A-Za-z0-9+/=$:-]+", verifier)
    assert transition.scram_verifier("pencil") != transition.scram_verifier("pencil")      # Fresh salt.


def test_the_0025_migration_adds_only_the_column_and_its_check():
    path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0025_v1_row_lock_anchor.py"
    spec = importlib.util.spec_from_file_location("v1_0025", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    names = {"service": "kalillac_s", "executor": "kalillac_e", "owner": "kalillac_o", "runtime": "kalillac_r",
             "operator": "kalillac_p"}
    statements = module.upgrade_statements(names)
    changes = [s for s in statements if not s.startswith("DO ")]
    assert changes == ["ALTER TABLE kalillac.users ADD COLUMN row_lock_anchor smallint",
                       "ALTER TABLE kalillac.users ADD CONSTRAINT ck_users_row_lock_anchor_null"
                       " CHECK (row_lock_anchor IS NULL)"]
    assert (module.revision, module.down_revision) == ("0025_v1_row_lock_anchor", "0024_v1_recovery_credential")


@pytest.mark.parametrize("argv, environ, message", [
    (["activate", "--release-sha", "a" * 40, "--apply"], {}, "KALILLAC_V1_OPERATOR_RECORD_FILE"),
    (["rollback", "--apply"], {}, "KALILLAC_V1_OPERATOR_RECORD_FILE"),
    (["activate"], {}, ""),
    (["deactivate"], {}, ""),
])
def test_the_tool_refuses_before_connecting(argv, environ, message, capsys):
    connected = []
    code = transition.run(argv, admin_engine_factory=lambda: connected.append(1), environ=environ,
                          read_file=lambda *a: b"")
    assert code == 2 and connected == [] and message in capsys.readouterr().out


def test_an_unconfigured_administrative_connection_is_refused(tmp_path, capsys):
    def missing():
        raise OSError()
    code = transition.run(["check"], admin_engine_factory=missing, environ={}, read_file=lambda *a: b"")
    assert code == 2 and "administrative connection" in capsys.readouterr().out
