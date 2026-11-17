"""D4, the account support tool on real disposable PostgreSQL 16, through the operator role P.

In the disposable database only, P is given a login and its own credential, as the operator transition
(E2) will; the tool builds its connection from that credential file's bytes and verifies P's boundary
(exactly the three operator functions, no table right) before anything is called. Seeding and checking
use the disposable superuser; sessions are issued through S's issuance function as the application
does. Every password is synthetic."""
from __future__ import annotations

import base64
import json
import os
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from test_v1_issuance_postgres import account, issue, key
from test_v1_revocation_postgres import admin_sql
from test_v1_session_end_postgres import head_world

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import v1_account_support as tool  # noqa: E402

DEPLOYMENT = "d4-test"


@pytest.fixture
def world(monkeypatch, tmp_path):
    with head_world(monkeypatch) as w:
        w.operator_password = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
        admin_sql(w, f"ALTER ROLE {w.names['operator']} LOGIN PASSWORD '{w.operator_password}'",
                  f"GRANT CONNECT ON DATABASE {w.migration.url.database} TO {w.names['operator']}")
        with w.admin_engine.connect() as c:
            w.instance = c.scalar(text("SELECT database_instance_id FROM kalillac.account_registration_activation"))
        w.record = tmp_path / "operator-record.jsonl"
        w.environ = {"KALILLAC_DEPLOYMENT_ID": DEPLOYMENT, tool.RECORD_FILE_ENV: str(w.record),
                     tool.CREDENTIAL_FILE_ENV: "unused-the-reader-is-injected"}
        w.credential_changes = {}
        yield w
        admin_sql(w, f"REVOKE CONNECT ON DATABASE {w.migration.url.database} FROM {w.names['operator']}",
                  f"ALTER ROLE {w.names['operator']} NOLOGIN PASSWORD NULL")


def credential_bytes(w):
    body = {"format_version": 1, "role": w.names["operator"], "database_instance_id": str(w.instance),
            "deployment_id": DEPLOYMENT, "password": w.operator_password, **w.credential_changes}
    return json.dumps(body).encode()


def run(w, *argv):
    reader = lambda: w.runtime_factory()  # noqa: E731
    return tool.run(list(argv), reader_factory=w.runtime_factory, environ=w.environ,
                    operator_factory=lambda: tool.operator_from_settings(
                        lambda: reader(), w.environ, read_file=lambda path, ceiling: credential_bytes(w)))


def signed_in(w, count=2):
    user, stored = account(w)
    for _ in range(count):
        assert issue(w, *key(), user, stored)[0] == "issued"
    return user, stored


def state(w, user):
    with w.admin_engine.connect() as c:
        row = c.execute(text("SELECT quarantined_at, deletion_requested_at, sessions_valid_after FROM kalillac.users"
                             " WHERE id=:u"), {"u": user}).one()
        live = c.scalar(text("SELECT count(*) FROM kalillac.account_sessions WHERE user_id=:u AND revoked_at IS NULL"),
                        {"u": user})
        events = [r[0] for r in c.execute(text("SELECT event_type FROM kalillac.account_security_events"
                                                " WHERE user_id=:u ORDER BY occurred_at"), {"u": user})]
    return tuple(row) + (live, events)


def records(w):
    return [json.loads(line) for line in w.record.read_text().splitlines()] if w.record.exists() else []


def last_json(out, key):
    return next(json.loads(line) for line in reversed(out.splitlines()) if line.startswith("{") and key in line)


def test_suspend_through_p_ends_every_session_records_the_reason_and_replays_once(world, capsys):
    w = world
    user, _ = signed_in(w)
    operation = uuid.uuid4()
    assert run(w, "suspend", str(user), "--reason", "abuse_report", "--operation-id", str(operation), "--apply") == 0
    quarantined, _, _, live, events = state(w, user)
    assert quarantined is not None and live == 0 and events == ["account_quarantine"]
    assert last_json(capsys.readouterr().out, "outcome") == {"operation_id": str(operation), "outcome": "quarantined",
                                                            "replayed": False}
    assert [(r["phase"], r["reason"], r.get("outcome")) for r in records(w)] == [
        ("intent", "abuse_report", None), ("outcome", "abuse_report", "quarantined")]
    # The same operation again is reported as replayed and applies nothing.
    before = state(w, user)
    assert run(w, "suspend", str(user), "--reason", "abuse_report", "--operation-id", str(operation), "--apply") == 0
    assert last_json(capsys.readouterr().out, "outcome")["replayed"] is True and state(w, user) == before
    # A new operation is refused by the database itself and records that.
    assert run(w, "suspend", str(user), "--reason", "abuse_report", "--apply") == 0
    assert records(w)[-1]["outcome"] == "already_quarantined" and state(w, user) == before


def test_a_dry_run_verifies_p_and_changes_nothing(world, capsys):
    w = world
    user, _ = signed_in(w)
    before = state(w, user)
    assert run(w, "suspend", str(user), "--reason", "support_review") == 0
    out = capsys.readouterr().out
    assert "dry run" in out and '"live_sessions": 2' in out
    assert state(w, user) == before and records(w) == []


def test_reinstate_after_suspend_leaves_sessions_ended(world):
    w = world
    user, _ = signed_in(w)
    assert run(w, "suspend", str(user), "--reason", "support_review", "--apply") == 0
    assert run(w, "reinstate", str(user), "--reason", "review_cleared", "--apply") == 0
    quarantined, _, _, live, events = state(w, user)
    assert quarantined is None and live == 0 and events == ["account_quarantine", "account_reinstate"]
    assert run(w, "reinstate", str(user), "--reason", "review_cleared", "--apply") == 0
    assert records(w)[-1]["outcome"] == "not_quarantined"


def test_request_deletion_marks_the_account_and_sign_in_is_refused(world):
    w = world
    user, stored = signed_in(w)
    assert run(w, "request-deletion", str(user), "--reason", "customer_request", "--apply") == 0
    _, marked, _, live, events = state(w, user)
    assert marked is not None and live == 0 and events == ["account_deletion_request"]
    assert issue(w, *key(), user, stored)[0] == "not_authenticated"
    assert run(w, "request-deletion", str(user), "--reason", "customer_request", "--apply") == 0
    assert records(w)[-1]["outcome"] == "already_requested"
    assert run(w, "request-deletion", str(uuid.uuid4()), "--reason", "customer_request", "--apply") == 0
    assert records(w)[-1]["outcome"] == "not_found"


@pytest.mark.parametrize("tamper", [
    "GRANT SELECT ON kalillac.users TO {operator}",
    "GRANT EXECUTE ON FUNCTION kalillac.v1_end_session(uuid,text) TO {operator}",
    "GRANT {app} TO {operator}",
])
def test_p_with_anything_beyond_its_three_functions_is_refused_before_any_call(world, tamper, capsys):
    w = world
    user, _ = signed_in(w)
    before = state(w, user)
    admin_sql(w, tamper.format(**w.names))
    assert run(w, "suspend", str(user), "--reason", "support_review", "--apply") == 2
    assert "could not be verified" in capsys.readouterr().out
    assert state(w, user) == before and records(w) == []


@pytest.mark.parametrize("change", [{"deployment_id": "another"}, {"database_instance_id": str(uuid.uuid4())},
                                    {"role": "kalillac_somebody_else"}])
def test_a_credential_bound_to_another_role_instance_or_deployment_is_refused(world, change):
    w = world
    user, _ = signed_in(w)
    w.credential_changes = change
    assert run(w, "suspend", str(user), "--reason", "support_review", "--apply") == 2
    assert records(w) == []


def test_the_applications_credential_cannot_stand_in_for_ps(world):
    """S's credential file names S; built as the operator it is refused (role mismatch), and S itself
    holds none of P's functions."""
    w = world
    user, _ = signed_in(w)
    w.credential_changes = {"role": w.names["service"]}
    assert run(w, "suspend", str(user), "--reason", "support_review", "--apply") == 2


def test_export_is_bounded_and_holds_no_credential_or_digest(world, capsys):
    w = world
    user, stored = signed_in(w, 3)
    assert run(w, "export", str(user)) == 0
    out = capsys.readouterr().out
    export = json.loads(out)
    assert export["account"]["id"] == str(user) and len(export["sessions"]) == 3
    with w.admin_engine.connect() as c:
        digests = [r[0] for r in c.execute(text("SELECT token_hash FROM kalillac.account_sessions WHERE user_id=:u"),
                                           {"u": user})]
    assert stored not in out and "argon2" not in out and not any(d in out for d in digests)
    # The audit table is owner-only: the runtime login cannot read it, and the export says so.
    assert export["security_events"] is None and "owner" in export["security_events_note"]
    assert export["sessions_truncated"] is False
    assert run(w, "show", str(uuid.uuid4())) == 2
