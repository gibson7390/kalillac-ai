"""B2 at the application: the real /api/chat classifies a request as PAID only for a V1-verified paid
account matching the request's resolved account; the same entitlement decides paid reasoning modes; the
file assistant's control request carries the same evidence (test_files_chat_handoff.py). V1 off, a free tier, a mismatch or any
refusal leaves everything exactly as it was (FREE)."""
# ruff: noqa: F811  (fixtures imported from other test modules are used as parameters)
from __future__ import annotations

from types import SimpleNamespace
import uuid

import pytest
from fastapi.testclient import TestClient

import app_fastapi_candidate as app
from kalillac_accounts.auth import RequestRejected
from kalillac_accounts.entitlements import FREE, PAID
from kalillac_accounts.paid_access import VerifiedAccount
from kalillac_finance.scopes import TrafficClass
from kalillac_routing import provider_controls
from kalillac_routing import reasoning_modes as modes
from test_reasoning_modes import boundary, enabled  # noqa: F401  (fixtures)
from test_usage_enforcement import MESSAGE, REPLY, SETTINGS, TOKEN, foundation, install_budget  # noqa: F401


class Security:
    """V1-01's read, answering with a fixed verified account (or raising a refusal)."""

    def __init__(self, account_id=None, entitlement=PAID, raises=None):
        self.account_id, self.entitlement, self.raises, self.calls = account_id, entitlement, raises, 0

    async def read_async(self, token_hash, work, with_identity=False, authorize=True):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return VerifiedAccount(self.account_id, self.entitlement)


@pytest.fixture
def classified(monkeypatch):
    """Provider controls on (enough for the control request) with request budgets, as they always run
    together; records each request's classification."""
    install_budget(monkeypatch, True)
    monkeypatch.setattr(provider_controls, "SERVICE", SimpleNamespace(config=SimpleNamespace(abuse=None)))
    seen = []
    original = provider_controls.account_status

    def spy(authenticated, paid_entitlement=None):
        original(authenticated, paid_entitlement)
        seen.append((authenticated, provider_controls.current_request().traffic_class))
    monkeypatch.setattr(provider_controls, "account_status", spy)
    return seen


def v1(monkeypatch, security):
    monkeypatch.setattr(app, "_v1_account_security", security)
    monkeypatch.setattr(app, "_account_settings", SETTINGS, raising=False)


def chat(token=TOKEN, **body):
    with TestClient(app.api) as client:
        headers = {"Cookie": SETTINGS.cookie_name + "=" + token} if token else {}
        return client.post("/api/chat", json={"message": MESSAGE, **body}, headers=headers)


# --- the financial scope at /api/chat -----------------------------------------------------------------

def test_a_v1_verified_paid_account_is_classified_paid(foundation, classified, monkeypatch):
    security = Security(foundation["user_id"], PAID)
    v1(monkeypatch, security)
    response = chat()
    assert response.status_code == 200 and response.json()["reply"] == REPLY
    assert classified == [(True, TrafficClass.PAID_ACCOUNT)] and security.calls == 1


@pytest.mark.parametrize("case", ["free_tier", "v1_off", "another_account", "refused", "unavailable"])
def test_everything_else_stays_free(foundation, classified, monkeypatch, case):
    security = {
        "free_tier": Security(foundation["user_id"], FREE),
        "v1_off": None,
        "another_account": Security(uuid.uuid4(), PAID),          # Not the request's resolved account.
        "refused": Security(raises=RequestRejected(401, "not_authenticated")),
        "unavailable": Security(raises=RuntimeError("synthetic outage")),
    }[case]
    v1(monkeypatch, security)
    assert chat().status_code == 200
    assert classified == [(True, TrafficClass.FREE_ACCOUNT)]


def test_an_anonymous_request_never_asks_v1_and_stays_anonymous(foundation, classified, monkeypatch):
    security = Security(foundation["user_id"], PAID)
    v1(monkeypatch, security)
    monkeypatch.setattr(app, "_usage_meter", None)
    monkeypatch.setattr(app, "_usage_quota", None)
    assert chat(token=None).status_code == 200
    assert classified == [(False, TrafficClass.ANONYMOUS)] and security.calls == 0


def test_without_a_meter_the_v1_account_is_the_requests_account(foundation, classified, monkeypatch):
    v1(monkeypatch, Security(foundation["user_id"], PAID))
    monkeypatch.setattr(app, "_usage_meter", None)
    monkeypatch.setattr(app, "_usage_quota", None)
    assert chat().status_code == 200
    assert classified == [(True, TrafficClass.PAID_ACCOUNT)]


def test_the_evidence_never_outlives_or_leaves_its_request(foundation, classified, monkeypatch):
    """Two requests, paid then free: each is classified on its own control request."""
    security = Security(foundation["user_id"], PAID)
    v1(monkeypatch, security)
    assert chat().status_code == 200
    security.entitlement = FREE                                    # The subscription ended in between.
    foundation["now"] = foundation["now"]                          # Same day; the meter still resolves.
    monkeypatch.setattr(app, "_usage_quota", None)
    chat()
    assert [c[1] for c in classified][:1] == [TrafficClass.PAID_ACCOUNT]
    assert classified[-1][1] is not TrafficClass.PAID_ACCOUNT
    assert provider_controls.current_request() is None


# --- reasoning modes at the chat boundary -------------------------------------------------------------

def test_a_paid_only_mode_needs_the_verified_paid_entitlement(boundary, enabled, monkeypatch):  # noqa: F811
    client, calls = boundary
    account = uuid.uuid4()
    cookie = {"Cookie": SETTINGS.cookie_name + "=" + TOKEN}
    # V1 off: refused for everyone, as before.
    v1(monkeypatch, None)
    refused = client.post("/api/chat", json={"message": "a", "reasoning_mode": "deep"}, headers=cookie)
    assert (refused.status_code, refused.json()) == (403, {"error": "reasoning_mode_not_allowed"})
    # V1 on, free tier: refused. Paid: selected with its effort.
    v1(monkeypatch, Security(account, FREE))
    assert client.post("/api/chat", json={"message": "b", "reasoning_mode": "deep"}, headers=cookie).status_code == 403
    v1(monkeypatch, Security(account, PAID))
    allowed = client.post("/api/chat", json={"message": "c", "reasoning_mode": "deep"}, headers=cookie)
    assert allowed.status_code == 200
    assert calls == [("c", modes.Selection("deep", "high", 8000), "high")]
    # Without the cookie the paid account's permission is not there.
    assert client.post("/api/chat", json={"message": "d", "reasoning_mode": "deep"}).status_code == 403
    assert modes._SELECTED.get() is None
