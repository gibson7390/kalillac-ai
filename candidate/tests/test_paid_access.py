"""B2, the seam from a verified subscription to the PAID scope, without a database server: only a
V1-authorized account whose effective entitlement is paid, matching the request's resolved account, yields
evidence, and only for its own provider-control request."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import uuid

import pytest

from kalillac_accounts.auth import AuthorizedAccount, RequestRejected
from kalillac_accounts.entitlements import FREE, PAID, EffectiveEntitlement
from kalillac_accounts.paid_access import VerifiedAccount, paid_evidence, resolved_account, verified_account
from kalillac_accounts.v1_security import V1SecurityUnavailable
from kalillac_finance.scopes import TrafficClass
from kalillac_finance.traffic import resolve_traffic_class

COOKIE = "__Host-kalillac_account"
ACCOUNT = uuid.uuid4()


class Request:
    def __init__(self, cookie="a-session-token"):
        self.cookies = {COOKIE: cookie} if cookie else {}


class Security:
    """V1-01's read: the work runs with the authorized account; or the configured refusal is raised."""

    def __init__(self, entitlement=PAID, raises=None):
        self.entitlement, self.raises, self.calls = entitlement, raises, []

    async def read_async(self, token_hash, work, with_identity=False, authorize=True):
        self.calls.append((token_hash, with_identity, authorize))
        if self.raises is not None:
            raise self.raises
        # The real read runs the work inside the authorized transaction (PostgreSQL tests); here the
        # authorized account's verified entitlement stands in for get_entitlement's row.
        now = datetime.now(timezone.utc)
        account = AuthorizedAccount(ACCOUNT, "p@example.com", now, now, now)
        return VerifiedAccount(account.id, self.entitlement)


def run(coroutine):
    return asyncio.run(coroutine)


def test_a_paid_v1_account_is_verified_through_the_authorized_read_only():
    security = Security()
    found = run(verified_account(Request(), security, COOKIE))
    assert found.account_id == ACCOUNT and found.paid
    ((digest, with_identity, authorize),) = security.calls
    assert len(digest) == 64 and "a-session-token" not in digest and authorize is True and not with_identity
    assert repr(found) == "VerifiedAccount()"


@pytest.mark.parametrize("request_, security", [
    (Request(cookie=None), Security()),
    (Request(), None),
    (Request(), Security(raises=RequestRejected(401, "not_authenticated"))),
    (Request(), Security(raises=RequestRejected(403, "account_unavailable"))),
    (Request(), Security(raises=V1SecurityUnavailable())),
    (Request(), Security(raises=RuntimeError("database detail"))),
], ids=["no_cookie", "v1_off", "not_authenticated", "quarantined", "unavailable", "unexpected"])
def test_anything_else_is_no_verified_account_and_never_raises(request_, security, capsys):
    assert run(verified_account(request_, security, COOKIE)) is None
    assert "database detail" not in capsys.readouterr().out


@pytest.mark.parametrize("entitlement, paid", [
    (PAID, True), (FREE, False),
    (EffectiveEntitlement(tier="paid", saved_mode_access=True, higher_usage_access=False), False),
    ("paid", False), (None, False),
])
def test_only_an_effective_paid_entitlement_with_higher_usage_is_paid(entitlement, paid):
    assert VerifiedAccount(ACCOUNT, entitlement).paid is paid


def test_evidence_is_bound_to_the_request_and_the_exact_account():
    request, other = object(), object()
    verified = VerifiedAccount(ACCOUNT, PAID)
    evidence = paid_evidence(request, verified, ACCOUNT)
    assert resolve_traffic_class(authenticated=True, request=request, paid_entitlement=evidence) is TrafficClass.PAID_ACCOUNT
    # Another request cannot use it; another account, a free tier or no request yields nothing.
    assert resolve_traffic_class(authenticated=True, request=other, paid_entitlement=evidence) is TrafficClass.FREE_ACCOUNT
    assert paid_evidence(request, verified, uuid.uuid4()) is None
    assert paid_evidence(request, VerifiedAccount(ACCOUNT, FREE), ACCOUNT) is None
    assert paid_evidence(None, verified, ACCOUNT) is None
    assert paid_evidence(request, None, ACCOUNT) is None
    assert paid_evidence(request, verified, None) is None


def test_the_resolved_account_prefers_the_meters_and_falls_back_to_the_verified_one():
    verified = VerifiedAccount(ACCOUNT, PAID)
    other = uuid.uuid4()
    assert resolved_account(other, verified) == other              # A mismatch is then not paid.
    assert paid_evidence(object(), verified, resolved_account(other, verified)) is None
    assert resolved_account(None, verified) == ACCOUNT
    assert resolved_account(None, None) is None
