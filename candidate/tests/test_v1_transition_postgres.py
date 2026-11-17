"""E1 and E2 on real disposable PostgreSQL 16: the row-lock anchor (0025), the runtime login's final grants
(E1a), and the operator transition with its rollback (tools/v1_security_transition.py).

The disposable superuser stands in for the operator's administrative login. S's and P's credentials are
generated here and handed to the tool as file bytes; nothing touches a real deployment."""
from __future__ import annotations

import base64
import json
import os
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

import kalillac_accounts.v1_schema as v1_schema
from kalillac_accounts.registration_startup import effective_privileges
from kalillac_accounts.v1_authority import V1Operator, build_v1_authority
from kalillac_accounts.v1_final_grants import FINAL, LEGACY, runtime_account_state
from kalillac_accounts.v1_security import FLAG, load_v1_startup
from test_v1_issuance_postgres import account, issue, key
from test_v1_revocation_postgres import admin_sql
from test_v1_schema_postgres import classify, migrate, sqlstate, version
from test_v1_session_end_postgres import head_world

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import v1_security_transition as transition  # noqa: E402

HEAD = "0025_v1_row_lock_anchor"
PREVIOUS = "0024_v1_recovery_credential"
DEPLOYMENT = "e2-test"
RELEASE = "a" * 40


def secret():
    return base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")


@pytest.fixture
def world(monkeypatch, tmp_path):
    with head_world(monkeypatch) as w:
        with w.admin_engine.connect() as c:
            w.instance = c.scalar(text("SELECT database_instance_id FROM kalillac.account_registration_activation"))
            w.registration_role = json.loads(c.scalar(text(
                "SELECT obj_description('kalillac.account_registration_activation'::regclass,'pg_class')")))[
                "registration_role"]
        w.passwords = {"service": secret(), "operator": secret()}
        w.record = tmp_path / "record.jsonl"
        w.environ = {"KALILLAC_DEPLOYMENT_ID": DEPLOYMENT, transition.RECORD_FILE_ENV: str(w.record),
                     transition.SERVICE_FILE_ENV: "service", transition.OPERATOR_FILE_ENV: "operator",
                     "KALILLAC_RELEASE_SHA": RELEASE, FLAG: "true", "KALILLAC_ACCOUNTS_ENABLED": "true",
                     "KALILLAC_DB_ENABLED": "true"}
        w.engines = []
        yield w
        for engine in w.engines:
            engine.dispose()
        # Leave the shared cluster's roles as the migrations expect them.
        admin_sql(w, *[f"ALTER ROLE {w.names[r]} NOLOGIN PASSWORD NULL" for r in ("service", "operator")])


def credential(w, which, **changes):
    body = {"format_version": 1, "role": w.names[which], "database_instance_id": str(w.instance),
            "deployment_id": DEPLOYMENT, "password": w.passwords[which], **changes}
    return json.dumps(body).encode()


def run(w, *argv, verify_service=None, overrides=None):
    files = {"service": credential(w, "service", **(overrides or {}).get("service", {})),
             "operator": credential(w, "operator", **(overrides or {}).get("operator", {}))}

    def admin():
        engine = create_engine(w.admin_engine.url, hide_parameters=True, pool_size=1, max_overflow=0)
        return engine
    return transition.run(list(argv), admin_engine_factory=admin, environ=w.environ,
                          read_file=lambda path, ceiling: files[path], verify_service=verify_service)


def latch(w):
    with w.admin_engine.connect() as c:
        return tuple(c.execute(text("SELECT state, authority_version, deployment_id, release_sha"
                                    " FROM kalillac.account_security_activation")).one())


def runtime_state(w):
    with w.admin_engine.connect() as c:
        return (effective_privileges(c, w.registration_role, w.names["app"], w.names["owner"]),
                runtime_account_state(c, w.names["app"]))


def logins(w):
    with w.admin_engine.connect() as c:
        return {r[0]: r[1] for r in c.execute(text("SELECT rolname, rolcanlogin FROM pg_roles WHERE rolname = ANY(:r)"),
                                              {"r": [w.names["service"], w.names["operator"], w.names["executor"]]})}


def privileges(w):
    """Every effective right R, A, S and P hold on every Kalillac table and column, for exact comparison."""
    with w.admin_engine.connect() as c:
        return sorted(tuple(r) for r in c.execute(text("""
            SELECT p, c.relname, a.attname::text, r FROM unnest(CAST(:p AS text[])) p
            CROSS JOIN pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
            CROSS JOIN unnest(ARRAY['SELECT','INSERT','UPDATE','REFERENCES']) r
            WHERE n.nspname = 'kalillac' AND c.relkind = 'r' AND has_column_privilege(p, c.oid, a.attnum, r)
            UNION ALL SELECT p, c.relname, '', r FROM unnest(CAST(:p AS text[])) p
            CROSS JOIN pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            CROSS JOIN unnest(ARRAY['DELETE','TRUNCATE','TRIGGER']) r
            WHERE n.nspname = 'kalillac' AND c.relkind = 'r' AND has_table_privilege(p, c.oid, r)"""),
            {"p": [w.names["app"], w.registration_role, w.names["service"], w.names["operator"]]}))


def as_runtime(w, sql, **params):
    with w.runtime_engine.connect() as c:
        return sqlstate(c, sql, params)


def qualifies(w, environ=None):
    return load_v1_startup(lambda: w.runtime_factory, {**w.environ, **(environ or {})})


# --- 0025, the anchor --------------------------------------------------------------------------------

def test_the_anchor_is_inert_and_grants_nothing(world):
    w = world
    assert version(w) == HEAD and classify(w.owner_factory) == v1_schema.PREPARED
    assert runtime_state(w) == (LEGACY, LEGACY)
    user, _ = account(w)
    with w.admin_engine.begin() as c:
        assert sqlstate(c, "UPDATE kalillac.users SET row_lock_anchor = 1 WHERE id = :u", {"u": user}) == "23514"
    for role in ("app", "registration", "service", "operator"):
        with w.admin_engine.connect() as c:
            assert not c.scalar(text("SELECT has_column_privilege(:p,'kalillac.users','row_lock_anchor','UPDATE')"),
                                {"p": w.names[role] if role != "registration" else w.registration_role})


def test_0025_downgrades_to_0024_exactly_and_upgrades_again(world):
    w = world
    before = privileges(w)
    migrate(w, PREVIOUS, down=True)
    with w.admin_engine.connect() as c:
        assert not c.scalar(text("SELECT count(*) FROM pg_attribute WHERE attrelid='kalillac.users'::regclass"
                                 " AND attname='row_lock_anchor' AND NOT attisdropped"))
    assert classify(w.owner_factory) == v1_schema.PREPARED
    migrate(w, HEAD)
    assert privileges(w) == before and classify(w.owner_factory) == v1_schema.PREPARED


@pytest.mark.parametrize("tamper", [
    "ALTER TABLE kalillac.users DROP CONSTRAINT ck_users_row_lock_anchor_null",
    "ALTER TABLE kalillac.users ALTER COLUMN row_lock_anchor SET DEFAULT 1",
    "GRANT UPDATE (row_lock_anchor) ON kalillac.users TO {app}",       # E1a's grant without the latch.
])
def test_a_tampered_anchor_or_an_early_grant_is_refused(world, tamper):
    w = world
    admin_sql(w, tamper.format(**w.names))
    with pytest.raises(v1_schema.V1SchemaRefused):
        classify(w.owner_factory)


# --- E2: activate, verify, roll back ------------------------------------------------------------------

def test_check_and_a_dry_run_change_nothing(world, capsys):
    w = world
    before = (privileges(w), latch(w), logins(w))
    assert run(w, "check") == 0
    assert run(w, "activate", "--release-sha", RELEASE) == 0
    assert '"applied": false' in capsys.readouterr().out
    assert (privileges(w), latch(w), logins(w)) == before and not w.record.exists()


def test_activation_applies_e1a_binds_the_latch_and_qualifies_startup(world):
    w = world
    assert run(w, "activate", "--release-sha", RELEASE, "--apply") == 0
    assert latch(w) == ("active", v1_schema.AUTHORITY_VERSION, DEPLOYMENT, RELEASE)
    assert logins(w) == {w.names["service"]: True, w.names["operator"]: True, w.names["executor"]: False}
    assert runtime_state(w) == (FINAL, FINAL)
    assert classify(w.owner_factory) == v1_schema.ACTIVE
    assert qualifies(w) is not None                                  # The startup gate now qualifies.
    for change in ({"KALILLAC_RELEASE_SHA": "b" * 40}, {"KALILLAC_DEPLOYMENT_ID": "elsewhere"},
                   {"KALILLAC_RELEASE_SHA": ""}):
        with pytest.raises(Exception, match="authority_binding_mismatch"):
            qualifies(w, change)
    # S and P log in with their own credentials, each only within its boundary.
    s = build_v1_authority(w.runtime_engine.url, role=w.names["service"], executor=w.names["executor"],
                           credential=credential(w, "service"), database_instance_id=w.instance,
                           deployment_id=DEPLOYMENT)
    p = build_v1_authority(w.runtime_engine.url, role=w.names["operator"], executor=w.names["executor"],
                           credential=credential(w, "operator"), database_instance_id=w.instance,
                           deployment_id=DEPLOYMENT, authority_class=V1Operator)
    try:
        user, stored = account(w)
        assert s.credential(email=f"v102c-{user.hex[:16]}@example.test") == (user, stored)
        assert p.operate("quarantine", operation_id=uuid.uuid4(), account_id=user).outcome == "quarantined"
    finally:
        s.dispose()
        p.dispose()
    records = [json.loads(line) for line in w.record.read_text().splitlines()]
    assert [r["phase"] for r in records] == ["intent", "outcome"] and records[1]["outcome"] == "applied"
    assert w.passwords["service"] not in w.record.read_text() and w.passwords["operator"] not in w.record.read_text()


def test_under_e1a_the_runtime_login_reads_and_locks_but_cannot_write_any_account_state(world):
    w = world
    user, stored = account(w)
    k = key()
    assert issue(w, *k, user, stored)[0] == "issued"
    assert run(w, "activate", "--release-sha", RELEASE, "--apply") == 0
    accepted = {
        "SELECT id, email, created_at, quarantined_at FROM kalillac.users WHERE id = :u": {"u": user},
        "SELECT id FROM kalillac.users WHERE id = :u FOR UPDATE": {"u": user},
        "SELECT id FROM kalillac.users WHERE id = :u FOR SHARE": {"u": user},
        "UPDATE kalillac.users SET row_lock_anchor = NULL WHERE id = :u": {"u": user},
        "SELECT token_hash, revoked_at FROM kalillac.account_sessions WHERE user_id = :u": {"u": user},
        "SELECT token_hash FROM kalillac.account_recovery_tokens WHERE user_id = :u": {"u": user},
    }
    for sql, params in accepted.items():
        assert as_runtime(w, sql, **params) == "accepted", sql
    refused = {
        "SELECT password_hash FROM kalillac.users WHERE id = :u": "42501",
        "SELECT * FROM kalillac.users WHERE id = :u": "42501",
        "UPDATE kalillac.users SET row_lock_anchor = 1 WHERE id = :u": "23514",
        "UPDATE kalillac.users SET password_hash = 'x' WHERE id = :u": "42501",
        "UPDATE kalillac.users SET is_active = false WHERE id = :u": "42501",
        "DELETE FROM kalillac.users WHERE id = :u": "42501",
        "INSERT INTO kalillac.users(id,email,password_hash) VALUES (gen_random_uuid(),'x@y.z','h')": "42501",
        "UPDATE kalillac.account_sessions SET revoked_at = now() WHERE user_id = :u": "42501",
        "INSERT INTO kalillac.account_sessions(id,user_id,token_hash,created_at,expires_at)"
        " VALUES (gen_random_uuid(),:u,'t',now(),now())": "42501",
        "DELETE FROM kalillac.account_sessions WHERE user_id = :u": "42501",
        "UPDATE kalillac.account_recovery_tokens SET consumed_at = now() WHERE user_id = :u": "42501",
        "SELECT 1 FROM kalillac.account_email_verification_tokens": "42501",
    }
    for sql, state in refused.items():
        assert as_runtime(w, sql, u=user) == state, sql


def test_under_e1a_every_runtime_read_of_an_account_still_works_without_the_hash(world):
    """The usage meter, quota, billing and files read the signed-in account through get_signed_in_user
    as R. Under E1a R cannot read password_hash; the User model defers it, so those reads keep working
    and only an access to the hash itself (legacy password paths, never run in V1) is refused."""
    from datetime import datetime, timezone
    from sqlalchemy.exc import ProgrammingError
    from kalillac_db.models import User
    from kalillac_db.repositories.accounts import get_signed_in_user
    w = world
    user, stored = account(w)
    k = key()
    assert issue(w, *k, user, stored)[0] == "issued"
    assert run(w, "activate", "--release-sha", RELEASE, "--apply") == 0
    with w.runtime_factory() as session, session.begin():
        found = get_signed_in_user(session, k[1], datetime.now(timezone.utc))
        assert found is not None and found.id == user and found.is_active
        assert session.get(User, user) is found
    with w.runtime_factory() as session, session.begin():
        found = get_signed_in_user(session, k[1], datetime.now(timezone.utc))
        with pytest.raises(ProgrammingError) as refused:
            found.password_hash
        assert refused.value.orig.sqlstate == "42501"


def test_rollback_restores_exactly_the_legacy_state(world):
    w = world
    before = (privileges(w), latch(w), logins(w))
    assert run(w, "activate", "--release-sha", RELEASE, "--apply") == 0
    assert run(w, "rollback", "--apply") == 0
    assert (privileges(w), latch(w), logins(w)) == before
    assert classify(w.owner_factory) == v1_schema.PREPARED and runtime_state(w) == (LEGACY, LEGACY)
    # Back to legacy grants, R cannot even read the latch: the gate refuses before classifying it.
    with pytest.raises(Exception, match="authority_state_unreadable"):
        qualifies(w)


def test_a_failed_service_login_after_commit_rolls_back_automatically(world, capsys):
    w = world
    before = (privileges(w), latch(w), logins(w))
    assert run(w, "activate", "--release-sha", RELEASE, "--apply", verify_service=lambda: False) == 1
    assert "rolling back" in capsys.readouterr().out
    assert (privileges(w), latch(w), logins(w)) == before
    assert json.loads(w.record.read_text().splitlines()[-1])["outcome"] == "failed_and_rolled_back"


def test_open_ingress_refuses_and_changes_nothing(world, capsys):
    w = world
    before = (privileges(w), latch(w), logins(w))
    with w.runtime_engine.connect() as held:                       # An application session is still open.
        held.execute(text("SELECT 1"))
        assert run(w, "activate", "--release-sha", RELEASE, "--apply") == 2
    assert "close ingress" in capsys.readouterr().out
    assert (privileges(w), latch(w), logins(w)) == before


@pytest.mark.parametrize("argv, overrides, message", [
    (("activate", "--release-sha", "abc", "--apply"), None, "40-character"),
    (("activate", "--release-sha", RELEASE, "--apply"), {"service": {"deployment_id": "other"}}, "credential"),
    (("activate", "--release-sha", RELEASE, "--apply"), {"operator": {"role": "kalillac_x"}}, "credential"),
    (("rollback", "--apply"), None, "rollback needs active"),
])
def test_refusals_before_any_change(world, argv, overrides, message, capsys):
    w = world
    before = (privileges(w), latch(w), logins(w))
    assert run(w, *argv, overrides=overrides) == 2
    assert message in capsys.readouterr().out
    assert (privileges(w), latch(w), logins(w)) == before


def test_s_and_p_may_not_share_a_password(world, capsys):
    w = world
    w.passwords["operator"] = w.passwords["service"]
    assert run(w, "activate", "--release-sha", RELEASE, "--apply") == 2
    assert latch(w)[0] == "prepared"


def test_activating_twice_is_refused_and_tampering_while_active_stops_startup(world):
    w = world
    assert run(w, "activate", "--release-sha", RELEASE, "--apply") == 0
    assert run(w, "activate", "--release-sha", RELEASE, "--apply") == 2
    admin_sql(w, f"GRANT INSERT (email) ON kalillac.users TO {w.names['app']}")
    with pytest.raises(Exception, match="inconsistent_schema"):
        qualifies(w)
    admin_sql(w, f"REVOKE INSERT (email) ON kalillac.users FROM {w.names['app']}")
    assert qualifies(w) is not None
    admin_sql(w, f"ALTER ROLE {w.names['service']} NOLOGIN")            # A half-undone activation.
    with pytest.raises(Exception, match="inconsistent_schema"):
        qualifies(w)


def test_a_migration_cannot_run_down_past_an_active_latch(world):
    w = world
    assert run(w, "activate", "--release-sha", RELEASE, "--apply") == 0
    with pytest.raises(DBAPIError):
        migrate(w, PREVIOUS, down=True)
    assert version(w) == HEAD and latch(w)[0] == "active"
