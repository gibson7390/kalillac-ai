"""V1-02D step D1 without a database: the S credential is parsed strictly, and the authority refuses
anything but PostgreSQL and arguments outside its functions' contracts before sending anything."""
from __future__ import annotations

import base64
import json
import os
import uuid

import pytest

from kalillac_accounts.v1_authority import (V1Authority, V1AuthorityUnavailable, build_v1_authority,
                                            load_v1_authority, parse_service_credential)

INSTANCE = str(uuid.uuid4())
PASSWORD = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
# The last of 43 characters carries two unused bits; setting one decodes to the same bytes but is not
# the canonical encoding.
NONCANONICAL = PASSWORD[:-1] + ALPHABET[ALPHABET.index(PASSWORD[-1]) | 1]


def credential(**changes):
    body = {"format_version": 1, "role": "svc", "database_instance_id": INSTANCE, "deployment_id": "dep-1",
            "password": PASSWORD}
    body.update(changes)
    return json.dumps({k: v for k, v in body.items() if v is not ...}).encode()


def parse(raw, **options):
    return parse_service_credential(raw, role="svc", database_instance_id=INSTANCE, deployment_id="dep-1", **options)


def test_a_well_formed_bound_credential_yields_its_password():
    assert parse(credential()) == PASSWORD
    assert parse_service_credential(credential(), role="svc", database_instance_id=uuid.UUID(INSTANCE),
                                    deployment_id="dep-1") == PASSWORD


@pytest.mark.parametrize("raw", [
    credential(role="other"),                                   # Another role's credential.
    credential(database_instance_id=str(uuid.uuid4())),         # Another database.
    credential(database_instance_id=INSTANCE.upper()),          # Not canonical.
    credential(deployment_id="dep-2"),                          # Another deployment.
    credential(format_version=2), credential(format_version=True), credential(format_version="1"),
    credential(password=PASSWORD[:-1]),                         # Too short.
    credential(password=PASSWORD + "A"),                        # Too long.
    credential(password="+" + PASSWORD[1:]),                    # Not base64url.
    credential(password=NONCANONICAL),
    credential(password=42),
    credential(extra="x"), credential(password=...),            # Extra or missing key.
    b'{"format_version":1,"format_version":1}',                 # Duplicate key.
    b"not json", b"", b"[]",
])
def test_anything_else_is_refused_without_detail(raw):
    with pytest.raises(V1AuthorityUnavailable) as refused:
        parse(raw)
    assert str(refused.value) == "V1 account authority unavailable." and PASSWORD not in repr(refused.value)
    assert refused.value.__cause__ is None


def test_the_runtime_logins_password_is_never_accepted_for_s():
    with pytest.raises(V1AuthorityUnavailable):
        parse(credential(), forbidden_passwords=(None, PASSWORD))
    # An unset deployment never matches, not even an empty one in the file.
    with pytest.raises(V1AuthorityUnavailable):
        parse_service_credential(credential(deployment_id=""), role="svc", database_instance_id=INSTANCE,
                                 deployment_id="")


class Session:
    """A session factory product that is not PostgreSQL: nothing may be sent through it."""

    def __init__(self, log):
        self.log = log

    def begin(self):
        from contextlib import nullcontext
        return nullcontext()

    def connection(self):
        class Connection:
            def invalidate(_self):
                self.log.append("invalidated")
        return Connection()

    def get_bind(self):
        class Bind:
            class dialect:
                name = "sqlite"
        return Bind()

    def execute(self, *args, **kwargs):
        self.log.append("executed")

    def close(self):
        self.log.append("closed")


def authority(log):
    return V1Authority(lambda: Session(log), role="svc", executor="exe")


def test_only_postgresql_is_accepted_and_the_connection_is_discarded():
    log = []
    with pytest.raises(V1AuthorityUnavailable):
        authority(log).issue_session(session_id=uuid.uuid4(), token_hash="a" * 64, user_id=uuid.uuid4(),
                                     verified_password_hash="$argon2id$x", lifetime_days=14)
    assert log == ["invalidated", "closed"]
    assert repr(authority(log)) == "V1Authority()"


GOOD_ISSUE = dict(session_id=uuid.uuid4(), token_hash="a" * 64, user_id=uuid.uuid4(),
                  verified_password_hash="$argon2id$x", lifetime_days=14)
GOOD_REVOKE = dict(operation_id=uuid.uuid4(), session_id=uuid.uuid4(), token_hash="b" * 64, scope="others")


@pytest.mark.parametrize("change", [
    {"session_id": str(uuid.uuid4())}, {"user_id": None}, {"token_hash": "A" * 64}, {"token_hash": "a" * 63},
    {"token_hash": "a" * 64 + "\n"}, {"verified_password_hash": ""}, {"verified_password_hash": None},
    {"lifetime_days": 0}, {"lifetime_days": 367}, {"lifetime_days": True}, {"lifetime_days": 14.0},
])
def test_issue_arguments_outside_the_contract_send_nothing(change):
    log = []
    with pytest.raises(V1AuthorityUnavailable):
        authority(log).issue_session(**{**GOOD_ISSUE, **change})
    assert log == []


@pytest.mark.parametrize("change", [
    {"operation_id": str(uuid.uuid4())}, {"session_id": None}, {"token_hash": "z" * 64},
    {"scope": "everything"}, {"scope": "ALL"}, {"scope": None},
])
def test_revoke_arguments_outside_the_contract_send_nothing(change):
    log = []
    with pytest.raises(V1AuthorityUnavailable):
        authority(log).revoke_sessions(**{**GOOD_REVOKE, **change})
    assert log == []


def test_building_refuses_before_connecting_when_the_roles_or_credential_are_wrong():
    from sqlalchemy import make_url
    url = make_url("postgresql+psycopg://runtime:runtime-pass@127.0.0.1:1/kalillac")
    common = dict(credential=credential(), database_instance_id=INSTANCE, deployment_id="dep-1")
    for role, executor in (("svc", "svc"), ("runtime", "exe"), ("svc", "runtime"), ("bad name", "exe")):
        with pytest.raises(V1AuthorityUnavailable):
            build_v1_authority(url, role=role, executor=executor, **common)
    with pytest.raises(V1AuthorityUnavailable):
        build_v1_authority(url.set(password=PASSWORD), role="svc", executor="exe", **common)
    with pytest.raises(V1AuthorityUnavailable):
        build_v1_authority(url, role="svc", executor="exe", ordinary_password=PASSWORD, **common)


def test_loading_without_a_database_is_refused():
    with pytest.raises(V1AuthorityUnavailable):
        load_v1_authority(lambda: None, {}, read_file=lambda path, ceiling: credential())


def test_the_application_does_not_load_the_authority_yet():
    """D1 is not wired into startup: S stays NOLOGIN until the operator transition (E2)."""
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "app_fastapi_candidate.py").read_text(encoding="utf-8")
    assert "v1_authority" not in source
