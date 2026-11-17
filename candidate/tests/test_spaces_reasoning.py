"""R1 and B2 for Space sends, through the real /api/chat lifecycle (test_spaces_chat's fixtures): a Space
send may name a reasoning mode, permitted exactly as on /api/chat, and a verified Premium account's
Space send is classified in the PAID scope for that very account only. Efforts are synthetic test
mappings; the model transport is replaced at its lowest layer."""
from __future__ import annotations

from types import SimpleNamespace
import uuid

import pytest

import app_fastapi_candidate as app
from kalillac_accounts import paid_access
from kalillac_accounts.entitlements import FREE, PAID
from kalillac_finance.scopes import TrafficClass
from kalillac_routing import provider_controls
from kalillac_routing import reasoning_modes as modes
from spaces_test_support import headers, new_request_id
from test_reasoning_modes import load as load_modes
from test_spaces_chat import REPLY, api, new_conversation, new_space, provider, world  # noqa: F401


@pytest.fixture
def moded(monkeypatch):
    monkeypatch.setattr(modes, "CONFIG", load_modes())
    return modes.CONFIG


def send(api, space_id, conversation_id, mode=None, message="Explain keys."):
    body = {"message": message, "client_request_id": new_request_id()}
    if mode is not None:
        body["reasoning_mode"] = mode
    return api.post(f"/api/spaces/{space_id}/conversations/{conversation_id}/messages", json=body,
                    headers=headers("a"))


def saved(api, space_id, conversation_id):
    page = api.get(f"/api/spaces/{space_id}/conversations/{conversation_id}", headers=headers("a")).json()
    return [m["content"] for m in page["messages"]]


def test_a_space_send_uses_the_mode_it_names(world, api, provider, moded):  # noqa: F811
    space_id = new_space(api)
    conversation_id = new_conversation(api, space_id)
    assert send(api, space_id, conversation_id, "quick").status_code == 200
    assert provider.payloads[-1]["reasoning"]["effort"] == "none"            # QUICK's synthetic effort.
    assert send(api, space_id, conversation_id).status_code == 200
    assert provider.payloads[-1]["reasoning"]["effort"] == "low"             # The default mode (BALANCED).
    assert saved(api, space_id, conversation_id) == ["Explain keys.", REPLY, "Explain keys.", REPLY]


def test_a_mode_outside_the_plan_is_refused_before_any_model_work_and_nothing_is_saved(
        world, api, provider, moded):  # noqa: F811
    space_id = new_space(api)
    conversation_id = new_conversation(api, space_id)
    refused = send(api, space_id, conversation_id, "deep")                   # DEEP is paid only; no V1 here.
    assert (refused.status_code, refused.json()["error"]) == (403, "reasoning_mode_not_allowed")
    unknown = send(api, space_id, conversation_id, "turbo")
    assert unknown.status_code in (400, 422) and provider.payloads == []
    assert saved(api, space_id, conversation_id) == []
    for bad in (7, "", "x" * 33, "dé"):
        malformed = send(api, space_id, conversation_id, bad)
        assert (malformed.status_code, malformed.json()["error"]) == (422, "invalid_request"), bad
    assert send(api, space_id, conversation_id, "quick").status_code == 200   # The lease was released.


@pytest.fixture
def classified(monkeypatch):
    seen = []
    status = provider_controls.account_status

    def spy(authenticated, paid_entitlement=None):
        status(authenticated, paid_entitlement)
        seen.append(provider_controls.current_request().traffic_class)
    # Provider controls on, with attempt leases that always succeed: only the classification is observed.
    monkeypatch.setattr(provider_controls, "SERVICE", SimpleNamespace(
        config=SimpleNamespace(abuse=None), reserve=lambda *args: object(), release=lambda lease: True))
    monkeypatch.setattr(provider_controls, "account_status", spy)
    return seen


def verified_as(monkeypatch, account_id, effective):
    from spaces_test_support import SETTINGS
    monkeypatch.setattr(app, "_v1_account_security", object())             # V1 mode, for _space_generate.
    monkeypatch.setattr(app, "_account_settings", SETTINGS, raising=False)  # V1 requires accounts.

    async def read(request, v1_security, cookie_name):
        return paid_access.VerifiedAccount(account_id, effective)
    monkeypatch.setattr(paid_access, "verified_account", read)


def test_a_verified_premium_space_send_is_paid_scope_and_may_use_a_paid_mode(
        world, api, provider, moded, classified, monkeypatch):  # noqa: F811
    space_id = new_space(api)
    conversation_id = new_conversation(api, space_id)
    verified_as(monkeypatch, world.users["a"], PAID)
    response = send(api, space_id, conversation_id, "deep")
    assert response.status_code == 200, response.text
    assert classified[-1] == TrafficClass.PAID_ACCOUNT
    assert provider.payloads[-1]["reasoning"]["effort"] == "high"


@pytest.mark.parametrize("who, effective", [("other", PAID), ("same", FREE)], ids=["another-account", "free"])
def test_paid_scope_needs_the_same_account_and_a_paid_entitlement(
        world, api, provider, moded, classified, monkeypatch, who, effective):  # noqa: F811
    space_id = new_space(api)
    conversation_id = new_conversation(api, space_id)
    verified_as(monkeypatch, uuid.uuid4() if who == "other" else world.users["a"], effective)
    assert send(api, space_id, conversation_id, "deep").status_code == 403    # Not paid: DEEP refused.
    assert send(api, space_id, conversation_id).status_code == 200
    assert classified[-1] == TrafficClass.FREE_ACCOUNT
