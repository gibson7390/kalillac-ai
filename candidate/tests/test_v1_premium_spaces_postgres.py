"""Space sends of a Premium and a Free customer through the real application, on real disposable PostgreSQL
16 after the operator transition (the Premium journey's fixture: V1 on, E1a's final grants, the V1 daily
quota, Stripe faked at its gateway, the model scripted).

What it proves that the SQLite Spaces suites cannot: under V1, a Space send of a customer whose Premium
subscription Stripe's webhook verified is classified in the PAID scope and may use DEEP, for that very
account; the same customer's Space send is FREE again after cancellation, and DEEP is then refused before
any model work with nothing saved. Synthetic credentials only; nothing leaves the disposable database.
"""
# ruff: noqa: F811  (fixtures imported from other test modules are used as parameters)
from __future__ import annotations

import uuid

import app_fastapi_candidate as app
from kalillac_finance.scopes import TrafficClass
from kalillac_spaces.config import SpacesConfig
from kalillac_spaces.router import build_spaces_router
from test_billing import _subscription
from test_v1_lifecycle_postgres import COOKIE, EMAIL, ORIGIN, PASSWORDS, account_row, live  # noqa: F401
from test_v1_premium_journey_postgres import journey, post, webhook  # noqa: F401
from test_v1_transition_postgres import world  # noqa: F401


def space_send(w, cookie, path, mode=None):
    body = {"message": "Plan the launch week.", "client_request_id": str(uuid.uuid4())}
    if mode:
        body["reasoning_mode"] = mode
    before, calls = len(w.classified), len(w.model_calls)
    w.client.cookies.clear()
    response = w.client.post(path, json=body, headers={"Cookie": f"{COOKIE}={cookie}", "Origin": ORIGIN})
    classified = w.classified[before:]
    return response, (classified[-1] if classified else None), len(w.model_calls) - calls


def saved_messages(w):
    from sqlalchemy import text
    with w.admin_engine.connect() as c:
        return c.scalar(text("SELECT count(*) FROM kalillac.messages"))


def test_a_premium_customers_space_send_is_paid_scope_and_free_again_after_cancellation(journey):
    w = journey
    assert post(w, "/api/account/register", {"email": EMAIL}).status_code == 202
    assert post(w, "/api/account/register/confirm", {"token": w.token(), "password": PASSWORDS[0]}).status_code == 200
    signed_in = post(w, "/api/account/login", {"email": EMAIL, "password": PASSWORDS[0]})
    cookie = signed_in.cookies.get(COOKIE)
    user = account_row(w)[0]
    app.api.include_router(build_spaces_router(
        config=SpacesConfig(beta_account_ids=frozenset({user}), site_origin=ORIGIN), cookie_name=COOKIE,
        generate=app._space_generate, lease_seconds=60, session_factory=lambda: w.runtime_factory,
        v1_security=w.security))
    w.client.cookies.clear()
    headers = {"Cookie": f"{COOKIE}={cookie}", "Origin": ORIGIN}
    space = w.client.post("/api/spaces", json={"name": "Launch", "instructions": ""}, headers=headers).json()["space"]
    conversation = w.client.post(f"/api/spaces/{space['id']}/conversations", json={}, headers=headers).json()
    path = f"/api/spaces/{space['id']}/conversations/{conversation['conversation']['id']}/messages"

    # Free: a Space send is FREE, and DEEP is refused before any model work, with nothing saved.
    response, scope, _ = space_send(w, cookie, path)
    assert response.status_code == 200, response.text
    assert scope == TrafficClass.FREE_ACCOUNT and saved_messages(w) == 2
    response, scope, model_calls = space_send(w, cookie, path, "deep")
    assert (response.status_code, response.json()["error"]) == (403, "reasoning_mode_not_allowed")
    assert model_calls == 0 and saved_messages(w) == 2

    # Premium, verified by Stripe's webhook: the next Space send is PAID and may use DEEP.
    assert post(w, "/api/account/billing/checkout", cookie=cookie).status_code == 200
    w.stripe.subscriptions["sub_1"] = _subscription(user)
    assert webhook(w, "evt_paid", "checkout.session.completed", user).json()["result"] == "processed"
    response, scope, _ = space_send(w, cookie, path, "deep")
    assert response.status_code == 200, response.text
    assert scope == TrafficClass.PAID_ACCOUNT and saved_messages(w) == 4

    # Cancelled, as Stripe reports it: FREE again on the very next Space send.
    w.stripe.subscriptions["sub_1"] = _subscription(user, status="canceled")
    assert webhook(w, "evt_canceled", "customer.subscription.deleted", user).json()["result"] == "processed"
    response, scope, _ = space_send(w, cookie, path)
    assert response.status_code == 200 and scope == TrafficClass.FREE_ACCOUNT
    assert space_send(w, cookie, path, "deep")[0].status_code == 403
