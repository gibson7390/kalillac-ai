"""0024 without a database: its place in the graph, the exact bodies and statements, the classifier's
knowledge of both functions, the S connection's call contracts, and V1 recovery requests through the
issuance authority."""
from __future__ import annotations

import hashlib
import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory

import kalillac_accounts.v1_authority as v1_authority
import kalillac_accounts.v1_schema as v1_schema
from kalillac_accounts.recovery_tokens import TOKEN_LIFETIME_SECONDS
from kalillac_accounts.v1_authority import V1AuthorityUnavailable
from test_account_migration import _config
from test_v1_authority import authority

PATH = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0024_v1_recovery_issuance_and_credential.py"
NAMES = {"service": "kalillac_s", "executor": "kalillac_e", "owner": "kalillac_o", "runtime": "kalillac_r",
         "operator": "kalillac_p"}
ISSUE = "kalillac.v1_issue_reset_token(uuid, text, text)"
CREDENTIAL = "kalillac.v1_credential(text)"


def _module():
    spec = importlib.util.spec_from_file_location("v1_0024_recovery_credential", PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_0024_is_the_head_directly_after_0023():
    script = ScriptDirectory.from_config(_config())
    assert script.get_heads() == ["0024_v1_recovery_credential"]
    module = _module()
    assert (module.revision, module.down_revision, module.branch_labels, module.depends_on) == (
        "0024_v1_recovery_credential", "0023_v1_deletion_request", None, None)


def test_the_classifier_knows_both_bodies_and_es_new_rights():
    module = _module()
    assert hashlib.sha256(module.ISSUE_RESET_BODY.encode()).hexdigest() == v1_schema.ISSUE_RESET_SOURCE_SHA256
    assert hashlib.sha256(module.CREDENTIAL_BODY.encode()).hexdigest() == v1_schema.CREDENTIAL_SOURCE_SHA256
    as_sets = lambda grants: {key: set(columns) for key, columns in grants.items()}  # noqa: E731
    assert as_sets(module.EXECUTOR_COLUMN_GRANTS) == as_sets(v1_schema.EXECUTOR_COLUMN_GRANTS_V6)
    # Exactly two new rights: E may read the email and insert a reset token's own columns.
    assert as_sets(v1_schema.EXECUTOR_COLUMN_GRANTS_V6) == {
        **as_sets(v1_schema.EXECUTOR_COLUMN_GRANTS_V5),
        ("users", "SELECT"): set(v1_schema.EXECUTOR_COLUMN_GRANTS_V5[("users", "SELECT")]) | {"email"},
        ("account_recovery_tokens", "INSERT"): {"id", "user_id", "purpose", "token_hash", "created_at", "expires_at"}}
    assert module.EXECUTOR_TABLE_GRANTS == v1_schema.EXECUTOR_TABLE_GRANTS
    reviewed = v1_authority._reviewed_functions()
    assert reviewed[v1_schema.ISSUE_RESET_SIGNATURE] == v1_schema.ISSUE_RESET_SOURCE_SHA256
    assert reviewed[v1_schema.CREDENTIAL_SIGNATURE] == v1_schema.CREDENTIAL_SOURCE_SHA256
    assert len(reviewed) == 8 and v1_schema.DELETION_REQUEST_SIGNATURE not in reviewed


def test_issuance_issues_only_for_an_account_that_may_reset_and_consumes_the_open_tokens():
    body = _module().ISSUE_RESET_BODY
    assert "FOR UPDATE" in body and "WHERE u.id = v_user AND u.email = p_email FOR UPDATE" in body
    assert ("IF NOT FOUND OR NOT v_active OR v_verified IS NULL OR v_quarantined IS NOT NULL"
            " OR v_deletion IS NOT NULL THEN") in body
    consume = body.index("UPDATE kalillac.account_recovery_tokens SET consumed_at = v_at")
    assert consume < body.index("INSERT INTO kalillac.account_recovery_tokens")
    assert "consumed_at IS NULL" in body[consume:consume + 200]
    # The lifetime is the application's, the digest is checked, nothing else is written.
    assert TOKEN_LIFETIME_SECONDS == 30 * 60 and "interval '30 minutes'" in body
    assert "p_token_hash !~ '^[0-9a-f]{64}$'" in body
    assert "kalillac.users SET" not in body and "account_sessions" not in body
    assert "account_security_events" not in body and "DELETE" not in body and "RAISE" not in body


def test_the_credential_read_returns_only_the_id_and_hash_for_one_email_and_writes_nothing():
    body = _module().CREDENTIAL_BODY
    assert "RETURN QUERY SELECT u.id, u.password_hash::text FROM kalillac.users u WHERE u.email = p_email;" in body
    for write in ("INSERT", "UPDATE", "DELETE", "FOR UPDATE", "RAISE"):
        assert write not in body
    assert "length(p_email) > 320" in body


def test_the_upgrade_grants_s_alone_both_functions_and_e_exactly_its_new_rights():
    module = _module()
    statements = module.upgrade_statements(NAMES)
    for signature in (ISSUE, CREDENTIAL):
        assert f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC" in statements
        assert f"GRANT EXECUTE ON FUNCTION {signature} TO kalillac_s" in statements
        assert f"ALTER FUNCTION {signature} OWNER TO kalillac_e" in statements
        assert not any(f"ON FUNCTION {signature} TO kalillac_p" in s for s in statements)
    assert "GRANT SELECT (email) ON kalillac.users TO kalillac_e" in statements
    assert ("GRANT INSERT (id, user_id, purpose, token_hash, created_at, expires_at)"
            " ON kalillac.account_recovery_tokens TO kalillac_e") in statements
    assert not any("ROLE" in s and "LOGIN" in s for s in statements)                # No role changes.
    assert not any(s.startswith("ALTER TABLE") for s in statements)                # No event type, no table change.
    post = statements[-1]
    assert "V1_RECOVERY_CREDENTIAL_POSTCONDITION_INVALID" in post and "<> 22" in post
    assert "deptype = 'o') <> 11" in post
    assert "d.objid IN (fn, issue, ending, resetting, changing, deleting_own, issuing_reset, crediting)))))" in post
    assert "AND p.oid NOT IN (quarantining, reinstating, deleting))" in post          # P's list unchanged.
    assert "('users', 'email', 'SELECT')" in post and "('account_recovery_tokens', 'token_hash', 'INSERT')" in post
    assert v1_schema.ISSUE_RESET_SOURCE_SHA256 in post and v1_schema.CREDENTIAL_SOURCE_SHA256 in post
    assert "V1_RECOVERY_CREDENTIAL_PREREQUISITE_INVALID" in statements[0]
    assert "v1_issue_reset_token" in statements[2] and "v1_credential" in statements[2]


def test_identity_guard_and_event_types_are_unchanged():
    module = _module()
    statements = module.upgrade_statements(NAMES) + module.downgrade_statements(NAMES)
    assert not any("registration_user_identity_guard " in s and "CREATE OR REPLACE" in s for s in statements)
    assert "''account_deletion_request''::character varying])" in module.upgrade_statements(NAMES)[-1]


def test_the_downgrade_restores_0023_exactly():
    statements = _module().downgrade_statements(NAMES)
    assert "V1_RECOVERY_CREDENTIAL_POSTCONDITION_INVALID" in statements[0]
    assert statements[1].startswith("REVOKE INSERT (id, user_id, purpose, token_hash, created_at, expires_at)")
    assert statements[2] == "REVOKE SELECT (email) ON kalillac.users FROM kalillac_e"
    assert f"DROP FUNCTION {ISSUE}" in statements and f"DROP FUNCTION {CREDENTIAL}" in statements
    assert all("V1_RECOVERY_CREDENTIAL_DOWNGRADE_INVALID" in s for s in statements[-3:])


def test_offline_downgrade_is_refused(monkeypatch):
    module = _module()

    class Context:
        as_sql = True

        class dialect:
            name = "postgresql"
    monkeypatch.setattr(module.op, "get_context", lambda: Context())
    with pytest.raises(RuntimeError, match="V1_RECOVERY_CREDENTIAL_OFFLINE_DOWNGRADE_UNSUPPORTED"):
        module.downgrade()


# --- the S connection's calls ------------------------------------------------------------------------

GOOD_ISSUE = dict(token_id=uuid.uuid4(), email="person@example.com", token_hash="f" * 64)


@pytest.mark.parametrize("change", [
    {"token_id": None}, {"token_id": str(uuid.uuid4())}, {"email": ""}, {"email": None},
    {"email": "Person@example.com"}, {"email": " person@example.com"}, {"email": "a" * 321},
    {"token_hash": "F" * 64}, {"token_hash": "f" * 63}, {"token_hash": None},
])
def test_issuance_arguments_outside_the_contract_send_nothing(change):
    log = []
    with pytest.raises(V1AuthorityUnavailable):
        authority(log).issue_reset_token(**{**GOOD_ISSUE, **change})
    assert log == []


@pytest.mark.parametrize("email", ["", None, 5, "Person@example.com", "a" * 321])
def test_credential_arguments_outside_the_contract_send_nothing(email):
    log = []
    with pytest.raises(V1AuthorityUnavailable):
        authority(log).credential(email=email)
    assert log == []


def test_well_formed_calls_reach_the_connection_check():
    for call in (lambda a: a.issue_reset_token(**GOOD_ISSUE), lambda a: a.credential(email="person@example.com")):
        log = []
        with pytest.raises(V1AuthorityUnavailable):
            call(authority(log))
        assert log == ["invalidated", "closed"]


# --- V1 recovery requests -----------------------------------------------------------------------------

class Issuer:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def issue_reset_token(self, **arguments):
        self.calls.append(arguments)
        if isinstance(answer := self.answer, BaseException):
            raise answer
        return answer


def v1_request_world(answer, *, fail_delivery=False):
    import recovery_test_support as support
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from account_test_db import make_session_factory, make_sqlite_engine
    from kalillac_accounts.recovery_router import build_recovery_router
    from kalillac_db.models import Base
    engine = make_sqlite_engine()
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    support.add_user(factory)
    gateway = support.FakeEmail(fail_reset=fail_delivery)
    issuer = Issuer(answer) if answer != "no_authority" else None
    api = FastAPI()
    api.include_router(build_recovery_router(
        session_factory=lambda: factory, gateway=gateway, settings=support.SETTINGS, clock=lambda: support.NOW,
        environ=support.ENV, v1_security=object(), v1_authority=issuer))
    return support, factory, gateway, issuer, TestClient(api, base_url="https://testserver")


def tokens(factory):
    from sqlalchemy import func, select
    from kalillac_db.models import AccountRecoveryToken
    with factory() as session:
        return session.scalar(select(func.count()).select_from(AccountRecoveryToken))


def test_a_v1_request_issues_through_the_authority_and_sends_the_link_for_exactly_that_token(capsys):
    support, factory, gateway, issuer, client = v1_request_world("issued")
    response = client.post("/api/account/recovery/request", json={"email": "Person@Example.com"},
                           headers={"cf-connecting-ip": "192.0.2.10"})
    assert (response.status_code, response.json()) == (202, {"ok": True})
    (call,) = issuer.calls
    assert call["email"] == "person@example.com" and type(call["token_id"]) is uuid.UUID
    ((to, link),) = gateway.reset
    token = link.split("#token=", 1)[1]
    assert to == "person@example.com" and link.startswith("https://kalillac.ai/reset#token=")
    assert call["token_hash"] == hashlib.sha256(token.encode()).hexdigest() and token not in repr(call)
    assert tokens(factory) == 0                                         # Nothing written as R.
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("answer, marker", [
    ("not_issued", ""),
    ("security_unavailable", "ACCOUNT_RECOVERY_REQUEST_FAILED"),
    (V1AuthorityUnavailable(), "ACCOUNT_RECOVERY_REQUEST_FAILED"),
    (RuntimeError("provider-private-detail"), "ACCOUNT_RECOVERY_REQUEST_FAILED"),
    (None, "ACCOUNT_RECOVERY_REQUEST_FAILED"),
    ("ISSUED", "ACCOUNT_RECOVERY_REQUEST_FAILED"),
    ("no_authority", "ACCOUNT_RECOVERY_REQUEST_FAILED"),
])
def test_anything_but_issued_sends_nothing_and_the_public_answer_is_the_same(answer, marker, capsys):
    support, factory, gateway, issuer, client = v1_request_world(answer)
    response = client.post("/api/account/recovery/request", json={"email": "person@example.com"},
                           headers={"cf-connecting-ip": "192.0.2.10"})
    assert (response.status_code, response.json()) == (202, {"ok": True})
    assert gateway.reset == [] and tokens(factory) == 0
    out = capsys.readouterr().out
    assert out.strip() == marker and "provider-private-detail" not in out


def test_a_failed_delivery_is_reported_without_detail(capsys):
    support, factory, gateway, issuer, client = v1_request_world("issued", fail_delivery=True)
    response = client.post("/api/account/recovery/request", json={"email": "person@example.com"},
                           headers={"cf-connecting-ip": "192.0.2.10"})
    assert response.status_code == 202 and len(gateway.reset) == 1
    out = capsys.readouterr().out
    assert out.strip() == "ACCOUNT_RECOVERY_DELIVERY_FAILED" and support.SENSITIVE not in out
