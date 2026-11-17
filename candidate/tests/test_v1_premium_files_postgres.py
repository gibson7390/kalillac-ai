"""Files and images in the Premium journey, on real disposable PostgreSQL 16 after the operator transition
(the journey's fixture: V1 on, E1a's final grants, Stripe faked at its gateway).

Through the real /api/files router with the application's own file request scope and V1 authenticator,
the real isolated worker and a scripted model: a Free customer is refused files; once Stripe's webhook
verifies Premium, the customer uploads a photo, Kalillac stores only its clean re-encoding (no EXIF),
asks about it, and the model receives exactly that copy as an image in a PAID-scope request; after
cancellation files are refused again. Every image and credential is synthetic.
"""
# ruff: noqa: F811  (fixtures imported from other test modules are used as parameters)
from __future__ import annotations

import base64
import io
from urllib.parse import quote

import pytest

import app_fastapi_candidate as app
from kalillac_files.account_access import build_authenticator
from kalillac_files.router import FileService, build_files_router
from kalillac_files.store import FileStore
from kalillac_finance.scopes import TrafficClass
from test_billing import _subscription
from test_v1_lifecycle_postgres import COOKIE, EMAIL, ORIGIN, PASSWORDS, SETTINGS, account_row, live  # noqa: F401
from test_v1_premium_journey_postgres import journey, post, webhook  # noqa: F401
from test_v1_transition_postgres import world  # noqa: F401

Image = pytest.importorskip("PIL.Image")
SECRET = "SYNTHETIC-GPS-51.5"


def photo():
    image = Image.new("RGB", (64, 48), (200, 120, 40))
    exif = Image.Exif()
    exif[0x010E] = SECRET
    out = io.BytesIO()
    image.save(out, format="JPEG", exif=exif.tobytes())
    return out.getvalue()


@pytest.fixture
def files(journey, tmp_path):
    w = journey
    w.sent_to_model = []

    def model(items):
        w.sent_to_model.append(items)
        return {"output": [{"type": "message", "role": "assistant",
                            "content": [{"type": "output_text", "text": "An orange rectangle."}]}]}
    w.store = FileStore(tmp_path / "files")
    service = FileService(store=w.store, call_model=model, required_tier="paid", images=True,
                          authenticate=build_authenticator(settings=SETTINGS, v1_security=w.security,
                                                           session_factory=lambda: w.runtime_factory),
                          request_scope=app._file_request_scope, failure_status=app._file_failure)
    app.api.include_router(build_files_router(service=service))
    return w


def upload(w, cookie, data, name):
    w.client.cookies.clear()
    return w.client.post("/api/files", content=data, headers={
        "Cookie": f"{COOKIE}={cookie}", "Origin": ORIGIN, "Content-Type": "application/octet-stream",
        "X-Kalillac-Filename": quote(name)})


def assist(w, cookie, message, file_ids):
    before = len(w.classified)
    response = post(w, "/api/files/assist", {"message": message, "file_ids": file_ids}, cookie=cookie)
    return response, w.classified[before:]


def test_premium_files_and_images_from_purchase_to_cancellation(files):
    w = files
    assert post(w, "/api/account/register", {"email": EMAIL}).status_code == 202
    assert post(w, "/api/account/register/confirm", {"token": w.token(), "password": PASSWORDS[0]}).status_code == 200
    cookie = post(w, "/api/account/login", {"email": EMAIL, "password": PASSWORDS[0]}).cookies.get(COOKIE)
    user = account_row(w)[0]

    # Free: files are a Premium capability; nothing is stored.
    refused = upload(w, cookie, photo(), "holiday.jpg")
    assert (refused.status_code, refused.json()["error"]) == (403, "files_require_premium")

    # Premium, verified by Stripe's webhook.
    assert post(w, "/api/account/billing/checkout", cookie=cookie).status_code == 200
    w.stripe.subscriptions["sub_1"] = _subscription(user)
    assert webhook(w, "evt_paid", "checkout.session.completed", user).json()["result"] == "processed"

    stored = upload(w, cookie, photo(), "holiday.jpg")
    assert stored.status_code == 201, stored.text
    record = stored.json()["file"]
    assert record["media_type"] == "image/jpeg" and record["filename"] == "holiday.jpg"
    _, kept = w.store.get(str(user), record["id"])
    assert SECRET.encode() not in kept and kept != photo()

    response, classified = assist(w, cookie, "What is in this photo?", [record["id"]])
    assert response.status_code == 200, response.text
    assert response.json()["reply"] == "An orange rectangle."
    assert classified == [TrafficClass.PAID_ACCOUNT]
    parts = w.sent_to_model[-1][-1]["content"]
    assert parts[1] == {"type": "input_image", "detail": "high",
                        "image_url": "data:image/jpeg;base64," + base64.b64encode(kept).decode("ascii")}

    # Cancelled, as Stripe reports it: files are refused again on the very next request.
    w.stripe.subscriptions["sub_1"] = _subscription(user, status="canceled")
    assert webhook(w, "evt_canceled", "customer.subscription.deleted", user).json()["result"] == "processed"
    again, _ = assist(w, cookie, "And now?", [record["id"]])
    assert (again.status_code, again.json()["error"]) == (403, "files_require_premium")
