"""D4, the account support tool without a database server: arguments, dry runs, the operator record,
refusals before any call, and what show and export never contain. Real PostgreSQL and the operator role
P: test_v1_account_support_postgres.py."""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import v1_account_support as tool  # noqa: E402
from kalillac_accounts.v1_authority import (OperatorResult, V1AuthorityUnavailable, V1Operator,  # noqa: E402
                                            _operator_functions, _reviewed_functions)

from account_test_db import make_session_factory, make_sqlite_engine  # noqa: E402


class Operator:
    def __init__(self, *answers):
        self.answers, self.calls, self.disposed = list(answers), [], False

    def operate(self, action, **arguments):
        self.calls.append((action, arguments))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def dispose(self):
        self.disposed = True


@pytest.fixture
def db():
    from kalillac_db.models import Base
    engine = make_sqlite_engine()
    Base.metadata.create_all(engine)
    yield make_session_factory(engine)
    engine.dispose()


def an_account(db):
    from kalillac_accounts.passwords import hash_password
    from kalillac_db.models import User
    with db() as session, session.begin():
        user = User(id=uuid.uuid4(), email="person@example.com", password_hash=hash_password("a password here"),
                    is_active=True)
        session.add(user)
        return user.id


def call(db, argv, operator=None, environ=None, record=None):
    environ = dict(environ or {})
    if record is not None:
        environ[tool.RECORD_FILE_ENV] = str(record)
    built = []

    def factory():
        built.append(operator)
        if isinstance(operator, BaseException):
            raise operator
        return operator
    code = tool.run(argv, reader_factory=db, operator_factory=factory, environ=environ)
    return code, built


@pytest.mark.parametrize("argv", [
    [], ["suspend"], ["suspend", str(uuid.uuid4())], ["suspend", str(uuid.uuid4()), "--reason", "because"],
    ["delete", str(uuid.uuid4())], ["suspend", "not-a-uuid", "--reason", "support_review"],
    ["suspend", str(uuid.uuid4()), "--reason", "support_review", "--operation-id", "x"],
])
def test_arguments_outside_the_contract_are_refused_before_any_connection(db, argv, capsys):
    operator = Operator()
    code, built = call(db, argv, operator)
    assert code == 2 and operator.calls == []
    assert built == [] or argv[-1] == "x"


def test_a_dry_run_verifies_p_shows_the_state_and_calls_nothing(db, tmp_path, capsys):
    account = an_account(db)
    operator, record = Operator(), tmp_path / "record.jsonl"
    code, built = call(db, ["suspend", str(account), "--reason", "support_review"], operator, record=record)
    out = capsys.readouterr().out
    assert code == 0 and built == [operator] and operator.calls == [] and operator.disposed
    assert '"live_sessions": 0' in out and "dry run: would call quarantine" in out
    assert not record.exists()


def test_apply_records_intent_then_outcome_with_the_reason_and_one_operation_id(db, tmp_path, capsys):
    account = an_account(db)
    operation = uuid.uuid4()
    operator, record = Operator(OperatorResult("quarantined", False)), tmp_path / "record.jsonl"
    code, _ = call(db, ["suspend", str(account), "--reason", "abuse_report", "--operation-id", str(operation),
                        "--apply"], operator, record=record)
    assert code == 0
    assert operator.calls == [("quarantine", {"operation_id": operation, "account_id": account})]
    intent, outcome = (json.loads(line) for line in record.read_text().splitlines())
    assert intent["phase"] == "intent" and outcome["phase"] == "outcome"
    for line in (intent, outcome):
        assert (line["operation_id"], line["action"], line["account_id"], line["reason"]) == (
            str(operation), "quarantine", str(account), "abuse_report")
    assert (outcome["outcome"], outcome["replayed"]) == ("quarantined", False)
    if os.name == "posix":
        assert oct(record.stat().st_mode & 0o777) == "0o600"
    assert '"outcome": "quarantined"' in capsys.readouterr().out


@pytest.mark.parametrize("command, action", [("reinstate", "reinstate"), ("request-deletion", "request_deletion")])
def test_each_command_calls_exactly_its_operator_function(db, tmp_path, command, action):
    account = an_account(db)
    operator = Operator(OperatorResult("not_found", False))
    code, _ = call(db, [command, str(account), "--reason", "customer_request", "--apply"], operator,
                   record=tmp_path / "r.jsonl")
    assert code == 0 and [c[0] for c in operator.calls] == [action]


def test_apply_without_a_record_file_or_with_an_unwritable_one_calls_nothing(db, tmp_path, capsys):
    account = an_account(db)
    operator = Operator()
    assert call(db, ["suspend", str(account), "--reason", "support_review", "--apply"], operator)[0] == 2
    assert call(db, ["suspend", str(account), "--reason", "support_review", "--apply"], operator,
                record=tmp_path)[0] == 2                                # A directory cannot be appended to.
    assert operator.calls == []


def test_an_unverifiable_operator_connection_is_refused_and_nothing_is_recorded(db, tmp_path, capsys):
    account = an_account(db)
    record = tmp_path / "record.jsonl"
    code, _ = call(db, ["suspend", str(account), "--reason", "support_review", "--apply"],
                   V1AuthorityUnavailable(), record=record)
    assert code == 2 and not record.exists()
    assert "could not be verified" in capsys.readouterr().out


def test_an_unavailable_call_is_recorded_as_such_and_exits_1(db, tmp_path):
    account = an_account(db)
    record = tmp_path / "record.jsonl"
    operator = Operator(V1AuthorityUnavailable())
    code, _ = call(db, ["suspend", str(account), "--reason", "support_review", "--apply"], operator, record=record)
    assert code == 1 and json.loads(record.read_text().splitlines()[-1])["outcome"] == "unavailable"


def test_show_and_export_never_contain_a_credential(db, capsys):
    from kalillac_db.models import User
    account = an_account(db)
    with db() as session:
        stored = session.get(User, account).password_hash
    assert tool.run(["show", str(account)], reader_factory=db, operator_factory=None, environ={}) == 0
    assert tool.run(["export", str(account)], reader_factory=db, operator_factory=None, environ={}) == 0
    out = capsys.readouterr().out
    assert stored not in out and "argon2" not in out and "password" not in out and "token" not in out
    export = json.loads(out[out.index("{\n"):])
    assert export["format"] == "kalillac-account-export-v1" and export["account"]["email"] == "person@example.com"
    assert tool.run(["export", str(uuid.uuid4())], reader_factory=db, operator_factory=None, environ={}) == 2


def test_the_operator_connection_cannot_make_the_applications_calls():
    operator = V1Operator(lambda: None, role="kalillac_p", executor="kalillac_e")
    for name in ("issue_session", "end_session", "reset_password", "change_password", "request_own_deletion",
                 "revoke_sessions", "issue_reset_token", "credential"):
        with pytest.raises(V1AuthorityUnavailable):
            getattr(operator, name)()
    with pytest.raises(V1AuthorityUnavailable):
        operator.operate("drop_everything", operation_id=uuid.uuid4(), account_id=uuid.uuid4())
    with pytest.raises(V1AuthorityUnavailable):
        operator.operate("quarantine", operation_id=str(uuid.uuid4()), account_id=uuid.uuid4())
    # P's reviewed set is exactly the operator functions, disjoint from S's.
    assert len(_operator_functions()) == 3 and not set(_operator_functions()) & set(_reviewed_functions())
