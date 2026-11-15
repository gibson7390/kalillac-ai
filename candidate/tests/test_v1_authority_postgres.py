"""V1-02D step D1 on real disposable PostgreSQL 16: the application's private S connection.

The disposable world stands on 0014 (the shared registration harness; CI's REQUIRE_POSTGRES switch
turns an absent database into a failure). Here, and only here, the test plays the operator's
transition for S: it gives the disposable S role LOGIN, a generated password and CONNECT on the
disposable database, then builds the authority exactly as the application would and calls the
authorities through a real SCRAM login as S. Every password hash is synthetic. Nothing here touches
a non-disposable database.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import threading
import uuid

import pytest
from sqlalchemy import text

import kalillac_accounts.v1_authority as v1_authority
from kalillac_accounts.v1_authority import V1AuthorityUnavailable, build_v1_authority, load_v1_authority
from test_v1_issuance_postgres import _world, account, epoch, key, sessions
from test_v1_revocation_postgres import admin_sql

DEPLOYMENT = "d1-test"


@pytest.fixture
def world(monkeypatch):
    with _world(monkeypatch) as w:
        w.service_password = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
        # The operator transition's part for S, in the disposable database only. Generated
        # base64url passwords cannot contain SQL delimiters; never print SQL.
        admin_sql(w, f"ALTER ROLE {w.names['service']} LOGIN PASSWORD '{w.service_password}'",
                  f"GRANT CONNECT ON DATABASE {w.migration.url.database} TO {w.names['service']}")
        with w.admin_engine.connect() as c:
            w.instance = c.scalar(text("SELECT database_instance_id FROM kalillac.account_registration_activation"))
        w.built = []
        yield w
        for authority in w.built:
            authority.dispose()
        admin_sql(w, f"REVOKE CONNECT ON DATABASE {w.migration.url.database} FROM {w.names['service']}",
                  f"ALTER ROLE {w.names['service']} NOLOGIN PASSWORD NULL")


def credential(w, **changes):
    body = {"format_version": 1, "role": w.names["service"], "database_instance_id": str(w.instance),
            "deployment_id": DEPLOYMENT, "password": w.service_password, **changes}
    return json.dumps(body).encode()


def build(w, **changes):
    authority = build_v1_authority(w.runtime_engine.url, role=w.names["service"], executor=w.names["executor"],
                                   credential=credential(w, **changes), database_instance_id=w.instance,
                                   deployment_id=DEPLOYMENT)
    w.built.append(authority)
    return authority


def refused(w, **changes):
    with pytest.raises(V1AuthorityUnavailable) as error:
        build(w, **changes)
    assert error.value.__cause__ is None and w.service_password not in repr(error.value)


def test_s_logs_in_with_its_own_credential_and_issues_and_revokes_sessions(world):
    w = world
    authority = build(w)
    user, stored = account(w)
    first, second = key(), key()
    issued = authority.issue_session(session_id=first[0], token_hash=first[1], user_id=user,
                                     verified_password_hash=stored, lifetime_days=14)
    assert issued.outcome == "issued" and not issued.replayed
    replay = authority.issue_session(session_id=first[0], token_hash=first[1], user_id=user,
                                     verified_password_hash=stored, lifetime_days=14)
    assert (replay.outcome, replay.replayed, replay.issued_at) == ("issued", True, issued.issued_at)
    authority.issue_session(session_id=second[0], token_hash=second[1], user_id=user,
                            verified_password_hash=stored, lifetime_days=14)
    # A stale credential is a uniform denial, with nothing written.
    stale = authority.issue_session(session_id=uuid.uuid4(), token_hash=secrets.token_hex(32), user_id=user,
                                    verified_password_hash=stored + "x", lifetime_days=14)
    assert (stale.outcome, stale.issued_at, stale.replayed) == ("not_authenticated", None, False)
    assert len(sessions(w, user)) == 2

    others = authority.revoke_sessions(operation_id=uuid.uuid4(), session_id=first[0], token_hash=first[1],
                                       scope="others")
    assert (others.outcome, others.retry_after_seconds) == ("revoked", None)
    live = {row[0] for row in sessions(w, user) if row[4] is None}
    assert live == {first[0]}
    before = epoch(w, user)
    every = authority.revoke_sessions(operation_id=uuid.uuid4(), session_id=first[0], token_hash=first[1],
                                      scope="all")
    assert every.outcome == "revoked" and all(row[4] is not None for row in sessions(w, user))
    assert epoch(w, user) is not None and epoch(w, user) != before
    # The revoked session proves nothing any more.
    gone = authority.revoke_sessions(operation_id=uuid.uuid4(), session_id=first[0], token_hash=first[1],
                                     scope="all")
    assert gone.outcome == "not_authenticated"


def test_the_connection_is_s_itself_and_can_do_nothing_but_call_the_authorities(world):
    w = world
    authority = build(w)
    with authority._transaction() as session:
        assert tuple(session.execute(text("SELECT session_user, current_user")).one()) == (w.names["service"],) * 2
    for statement in ("SELECT count(*) FROM kalillac.users", "SELECT count(*) FROM kalillac.account_sessions",
                      "UPDATE kalillac.users SET is_active = false",
                      "SELECT kalillac.registration_control_lock(false)"):
        with pytest.raises(V1AuthorityUnavailable):
            with authority._transaction() as session:
                session.execute(text(statement))
    # A failed transaction never leaves its connection in the pool in an unknown state.
    authority.verify()


@pytest.mark.parametrize("changes", [
    {"role": "someone_else"}, {"deployment_id": "another"}, {"database_instance_id": str(uuid.uuid4())},
    {"password": base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")},   # Not S's password.
])
def test_a_wrong_credential_is_refused(world, changes):
    refused(world, **changes)


@pytest.mark.parametrize("grant, undo", [
    ("GRANT SELECT ON kalillac.users TO {service}", "REVOKE SELECT ON kalillac.users FROM {service}"),
    ("GRANT SELECT (id) ON kalillac.account_sessions TO {service}",
     "REVOKE SELECT (id) ON kalillac.account_sessions FROM {service}"),
    ("GRANT EXECUTE ON FUNCTION kalillac.registration_control_lock(boolean) TO {service}",
     "REVOKE EXECUTE ON FUNCTION kalillac.registration_control_lock(boolean) FROM {service}"),
    ("GRANT {app} TO {service}", "REVOKE {app} FROM {service}"),
    ("ALTER ROLE {service} INHERIT", "ALTER ROLE {service} NOINHERIT"),
    ("ALTER ROLE {service} CREATEDB", "ALTER ROLE {service} NOCREATEDB"),
    ("REVOKE EXECUTE ON FUNCTION kalillac.v1_issue_session(uuid,text,uuid,text,integer) FROM {service}",
     "GRANT EXECUTE ON FUNCTION kalillac.v1_issue_session(uuid,text,uuid,text,integer) TO {service}"),
])
def test_any_extra_or_missing_right_refuses_the_authority(world, grant, undo):
    w = world
    names = {"service": w.names["service"], "app": w.names["app"]}
    with w.admin_engine.begin() as c:
        c.exec_driver_sql(grant.format(**names))
    try:
        refused(w)
    finally:
        with w.admin_engine.begin() as c:
            c.exec_driver_sql(undo.format(**names))
    build(w)   # Restored, it qualifies again.


def test_an_unreviewed_definer_function_executable_by_s_is_refused(world):
    w = world
    admin_sql(w, "CREATE FUNCTION public.d1_bridge() RETURNS integer LANGUAGE sql SECURITY DEFINER AS 'SELECT 1'")
    try:
        refused(w)
    finally:
        admin_sql(w, "DROP FUNCTION public.d1_bridge()")


def test_a_tampered_authority_body_is_refused(world):
    w = world
    with w.admin_engine.connect() as c:
        source = c.scalar(text("SELECT prosrc FROM pg_proc WHERE oid = 'kalillac.v1_issue_session(uuid,text,uuid,text,integer)'"
                               "::regprocedure"))
    admin_sql(w, "UPDATE pg_catalog.pg_proc SET prosrc = :s WHERE oid = "
                 "'kalillac.v1_issue_session(uuid,text,uuid,text,integer)'::regprocedure", s=source + "\n-- changed\n")
    try:
        refused(w)
    finally:
        admin_sql(w, "UPDATE pg_catalog.pg_proc SET prosrc = :s WHERE oid = "
                     "'kalillac.v1_issue_session(uuid,text,uuid,text,integer)'::regprocedure", s=source)


def test_an_exhausted_pool_fails_closed(world, monkeypatch):
    w = world
    monkeypatch.setattr(v1_authority, "POOL_SIZE", 1)
    monkeypatch.setattr(v1_authority, "POOL_TIMEOUT_SECONDS", 1)
    authority = build(w)
    user, stored = account(w)
    held, release = threading.Event(), threading.Event()

    def hold():
        with authority._transaction():
            held.set()
            release.wait(10)
    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert held.wait(10)
        session_id, token_hash = key()
        with pytest.raises(V1AuthorityUnavailable):
            authority.issue_session(session_id=session_id, token_hash=token_hash, user_id=user,
                                    verified_password_hash=stored, lifetime_days=14)
    finally:
        release.set()
        holder.join(10)
    assert sessions(w, user) == []


def test_loading_reads_the_credential_file_and_the_recorded_database_instance(world):
    w = world
    from sqlalchemy.orm import sessionmaker
    paths = []

    def read_file(path, ceiling):
        paths.append((path, ceiling))
        return credential(w)
    environ = {v1_authority.CREDENTIAL_FILE_ENV: "/etc/kalillac/v1-service.json", "KALILLAC_DEPLOYMENT_ID": DEPLOYMENT}
    authority = load_v1_authority(lambda: sessionmaker(bind=w.runtime_engine), environ, read_file=read_file)
    w.built.append(authority)
    assert paths == [("/etc/kalillac/v1-service.json", v1_authority.CREDENTIAL_MAX_BYTES)]
    with pytest.raises(V1AuthorityUnavailable):
        load_v1_authority(lambda: sessionmaker(bind=w.runtime_engine), {**environ, "KALILLAC_DEPLOYMENT_ID": "x"},
                          read_file=read_file)
