"""Resend adapter: exact request, messages, bounds and redaction. Injected fake transports only."""
from __future__ import annotations

from html.parser import HTMLParser
import json
import re
import threading
import time
import traceback
import uuid

import httpx
import pytest

from kalillac_accounts import email_messages, resend_gateway
from kalillac_accounts.email_gateway import (
    EmailDeliveryError, EmailGateway, EmailVerificationGateway,
)
from kalillac_accounts.resend_gateway import RESEND_ENDPOINT, ResendGateway

# Obviously fake; matches the key format so configuration accepts it.
KEY = "re_FAKE_TEST_KEY_NOT_REAL_0000"
SENDER = "help@kalillac.ai"
EMAIL = "person@example.com"
TOKEN = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_abcd"
RESET_LINK = "https://kalillac.ai/reset#token=" + TOKEN
VERIFY_LINK = "https://kalillac.ai/verify-email#token=" + TOKEN
MESSAGE_ID = "4ef9a417-02e9-4d39-ad75-9611e0fcc33c"


class Recorder:
    def __init__(self, respond=None):
        self.requests = []
        self.respond = respond or (lambda request: httpx.Response(200, json={"id": MESSAGE_ID}))

    def __call__(self, request):
        self.requests.append(request)
        return self.respond(request)


def gateway_for(recorder, **kwargs):
    return ResendGateway(KEY, SENDER, transport_factory=lambda: httpx.MockTransport(recorder), **kwargs)


def sent_json(request):
    return json.loads(request.content)


SENDS = {
    "password_reset": lambda gateway: gateway.send_password_reset(EMAIL, RESET_LINK),
    "password_changed": lambda gateway: gateway.send_password_changed(EMAIL),
    "email_verification": lambda gateway: gateway.send_email_verification(EMAIL, VERIFY_LINK),
}


# --- request contract ----------------------------------------------------------------


@pytest.mark.parametrize("operation", list(SENDS))
def test_exact_endpoint_method_headers_and_single_request(operation):
    recorder = Recorder()
    SENDS[operation](gateway_for(recorder))
    [request] = recorder.requests
    assert request.method == "POST"
    assert str(request.url) == RESEND_ENDPOINT == "https://api.resend.com/emails"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert request.headers["content-type"] == "application/json"
    assert request.headers["accept-encoding"] == "identity"
    key = request.headers["idempotency-key"]
    purpose = operation.replace("_", "-")
    assert re.fullmatch(rf"kalillac-email-v1/{purpose}/[0-9a-f]{{32}}(?:[0-9a-f]{{32}})?", key)
    assert len(key) <= 256
    for private in (EMAIL, TOKEN, RESET_LINK, VERIFY_LINK, KEY, "example.com"):
        assert private not in key


@pytest.mark.parametrize("operation", list(SENDS))
def test_json_body_contains_only_the_allowed_fields(operation):
    recorder = Recorder()
    SENDS[operation](gateway_for(recorder))
    body = sent_json(recorder.requests[0])
    assert set(body) == {"from", "to", "subject", "text", "html"}
    assert body["from"] == "Kalillac AI <help@kalillac.ai>"
    assert body["to"] == [EMAIL]
    assert all(isinstance(body[name], str) and body[name] for name in ("subject", "text", "html"))


def test_link_messages_have_stable_keys_and_notifications_have_unique_keys():
    recorder = Recorder()
    gateway = gateway_for(recorder)
    gateway.send_password_reset(EMAIL, RESET_LINK)
    gateway.send_password_reset(EMAIL, RESET_LINK)
    gateway.send_password_reset(EMAIL, RESET_LINK + "x")
    gateway.send_email_verification(EMAIL, RESET_LINK)
    gateway.send_password_changed(EMAIL)
    gateway.send_password_changed(EMAIL)
    keys = [request.headers["idempotency-key"] for request in recorder.requests]
    assert keys[0] == keys[1]
    assert len({keys[1], keys[2], keys[3], keys[4], keys[5]}) == 5


@pytest.mark.parametrize("recipient", [
    "a@example.com, b@example.com", "Name <a@example.com>", "a b@example.com", '"a"@example.com',
    "a@example", "a@example.com\nBcc: b@example.com", "", None, "a;b@example.com", "<a@example.com>",
])
def test_recipient_must_be_one_plain_address_before_any_request(recipient):
    recorder = Recorder()
    with pytest.raises(EmailDeliveryError):
        gateway_for(recorder).send_password_changed(recipient)
    assert recorder.requests == []


@pytest.mark.parametrize("link", ["javascript:alert(1)", "data:text/html,x", "//kalillac.ai/reset", "", None])
def test_links_must_be_http_urls_before_any_request(link):
    recorder = Recorder()
    with pytest.raises(EmailDeliveryError):
        gateway_for(recorder).send_password_reset(EMAIL, link)
    with pytest.raises(EmailDeliveryError):
        gateway_for(recorder).send_email_verification(EMAIL, link)
    assert recorder.requests == []


def test_endpoint_is_fixed_and_not_read_from_the_environment(monkeypatch):
    for name in ("RESEND_API_URL", "RESEND_BASE_URL", "KALILLAC_RESEND_API_URL", "KALILLAC_RESEND_ENDPOINT",
                 "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "SSL_CERT_FILE"):
        monkeypatch.setenv(name, "https://attacker.invalid/emails")
    recorder = Recorder()
    gateway_for(recorder).send_password_changed(EMAIL)
    assert str(recorder.requests[0].url) == "https://api.resend.com/emails"
    from pathlib import Path
    source = Path(resend_gateway.__file__).read_text()
    assert "getenv" not in source and "environ" not in source


def test_client_and_default_transport_are_bounded(monkeypatch):
    assert resend_gateway.TIMEOUT.connect == 1.5 and resend_gateway.TIMEOUT.read == 2.0
    assert resend_gateway.TIMEOUT.write == 1.0 and resend_gateway.TIMEOUT.pool == 0.5
    assert resend_gateway.TOTAL_SECONDS == 5.0
    transport = resend_gateway._default_transport()
    try:
        assert transport._pool._retries == 0
    finally:
        transport.close()
    captured = {}
    real = httpx.Client
    def client(**kwargs):
        captured.update(kwargs)
        return real(**kwargs)
    monkeypatch.setattr(resend_gateway.httpx, "Client", client)
    gateway_for(Recorder()).send_password_changed(EMAIL)
    assert captured["follow_redirects"] is False and captured["trust_env"] is False
    assert captured["timeout"] is resend_gateway.TIMEOUT


def test_construction_makes_no_request_and_repr_hides_secrets():
    calls = []
    gateway = ResendGateway(KEY, SENDER, transport_factory=lambda: calls.append(1))
    assert calls == [] and repr(gateway) == "ResendGateway()"
    assert isinstance(gateway, EmailGateway) and isinstance(gateway, EmailVerificationGateway)


# --- messages -------------------------------------------------------------------------------


class _Parsed(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags, self.links, self.attributes = [], [], []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attributes.extend(name for name, _ in attrs)
        if tag == "a":
            self.links.append(dict(attrs)["href"])


ALLOWED_TAGS = {"html", "head", "meta", "title", "body", "p", "a"}


@pytest.mark.parametrize("operation,subject,link,lifetime", [
    ("password_reset", "Reset your Kalillac AI password", RESET_LINK, "30 minutes"),
    ("email_verification", "Verify your email for Kalillac AI", VERIFY_LINK, "24 hours"),
])
def test_link_messages(operation, subject, link, lifetime):
    recorder = Recorder()
    SENDS[operation](gateway_for(recorder))
    body = sent_json(recorder.requests[0])
    assert body["subject"] == subject
    assert body["text"].count(link) == 1
    assert f"expires in {lifetime}" in body["text"] and f"expires in {lifetime}" in body["html"]
    assert "ignore this email" in body["text"] and "ignore this email" in body["html"]
    parsed = _Parsed(body["html"])
    assert parsed.links == [link]
    assert set(parsed.tags) <= ALLOWED_TAGS
    assert not {"src", "style", "background", "srcset"} & set(parsed.attributes)
    assert TOKEN not in body["html"].replace(link, "")
    assert TOKEN not in body["text"].replace(link, "")


def test_password_changed_message_has_no_link_or_credential():
    recorder = Recorder()
    gateway_for(recorder).send_password_changed(EMAIL)
    body = sent_json(recorder.requests[0])
    assert body["subject"] == "Your Kalillac AI password was changed"
    assert "was just changed" in body["text"] and "start account recovery" in body["text"]
    assert "http" not in body["text"] and "http" not in body["html"]
    parsed = _Parsed(body["html"])
    assert parsed.links == [] and set(parsed.tags) <= ALLOWED_TAGS


def test_dynamic_values_are_escaped_and_the_href_is_preserved_exactly():
    link = 'https://kalillac.ai/reset?a=1&b="2"<script>alert(1)</script>#token=' + TOKEN
    for render in (email_messages.password_reset, email_messages.email_verification):
        message = render(link)
        assert "<script>" not in message.html and '"2"' not in message.html
        assert "&amp;b=&quot;2&quot;&lt;script&gt;" in message.html
        assert _Parsed(message.html).links == [link]
        assert message.text.count(link) == 1


def test_no_external_resources_or_tracking():
    for message in (email_messages.password_reset(RESET_LINK), email_messages.password_changed(),
                    email_messages.email_verification(VERIFY_LINK)):
        lowered = message.html.lower()
        for forbidden in ("<img", "<script", "<style", "<link", "<iframe", "url(", "@import", "src=",
                          "unsubscribe", "pixel", "utm_"):
            assert forbidden not in lowered
        assert repr(message).startswith("EmailMessage(subject=") and "html" not in repr(message)


# --- responses and failures -----------------------------------------------------------------


def test_success_response_is_validated_and_discarded():
    recorder = Recorder()
    assert gateway_for(recorder).send_password_changed(EMAIL) is None
    assert len(recorder.requests) == 1


@pytest.mark.parametrize("status", [201, 202, 204, 301, 302, 303, 307, 308, 400, 401, 403, 404, 405, 409,
                                    410, 413, 422, 429, 500, 502, 503, 504])
def test_every_non_success_status_fails_once_without_retry_or_redirect(status):
    recorder = Recorder(lambda request: httpx.Response(
        status, headers={"location": "https://attacker.invalid/emails"}, json={"id": MESSAGE_ID}))
    with pytest.raises(EmailDeliveryError):
        gateway_for(recorder).send_password_reset(EMAIL, RESET_LINK)
    assert len(recorder.requests) == 1


@pytest.mark.parametrize("content,headers", [
    (b"not json", {}), (b"[]", {}), (b"{}", {}), (b'{"id": 5}', {}), (b'{"id": ""}', {}),
    (b'{"id": null}', {}), (b'{"id": "bad id!"}', {}), (b'{"id": "' + b"a" * 129 + b'"}', {}),
    (b'{"id": "abc", "extra": 1}', {}), (b'{"id": "abc"', {}),
    (b'{"id": "abc"}', {"content-type": "text/html"}),
    (b'{"id": "abc"}', {"content-encoding": "gzip"}),
])
def test_malformed_successful_responses_fail(content, headers):
    recorder = Recorder(lambda request: httpx.Response(
        200, content=content, headers={"content-type": "application/json", **headers}))
    with pytest.raises(EmailDeliveryError):
        gateway_for(recorder).send_email_verification(EMAIL, VERIFY_LINK)
    assert len(recorder.requests) == 1


def test_oversized_response_is_refused_by_declared_length():
    body = json.dumps({"id": MESSAGE_ID, "pad": "x" * 5000}).encode()
    recorder = Recorder(lambda request: httpx.Response(
        200, content=body, headers={"content-type": "application/json"}))
    with pytest.raises(EmailDeliveryError):
        gateway_for(recorder).send_password_changed(EMAIL)


def test_oversized_streamed_response_stops_reading_at_the_cap():
    produced = []
    class Endless(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(1000):
                produced.append(1)
                yield b" " * 512
    recorder = Recorder(lambda request: httpx.Response(
        200, headers={"content-type": "application/json"}, stream=Endless()))
    with pytest.raises(EmailDeliveryError):
        gateway_for(recorder).send_password_changed(EMAIL)
    assert len(produced) <= 10


@pytest.mark.parametrize("error", [httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout,
                                   httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError])
def test_timeouts_and_transport_failures_fail_once(error):
    def fail(request):
        raise error("transport detail", request=request)
    recorder = Recorder(fail)
    with pytest.raises(EmailDeliveryError):
        gateway_for(recorder).send_password_reset(EMAIL, RESET_LINK)
    assert len(recorder.requests) == 1


def test_total_attempt_is_bounded_and_in_flight_attempts_are_capped():
    release = threading.Event()
    def stall(request):
        release.wait(10)
        return httpx.Response(200, json={"id": MESSAGE_ID})
    recorder = Recorder(stall)
    gateway = gateway_for(recorder, total_seconds=0.2, max_in_flight=1)
    started = time.monotonic()
    with pytest.raises(EmailDeliveryError):
        gateway.send_password_changed(EMAIL)
    assert time.monotonic() - started < 2
    # The abandoned attempt still holds the only slot: the next call fails at once.
    with pytest.raises(EmailDeliveryError):
        gateway.send_password_changed(EMAIL)
    assert len(recorder.requests) == 1
    release.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            gateway.send_password_changed(EMAIL)
            break
        except EmailDeliveryError:
            time.sleep(0.05)
    assert len(recorder.requests) == 2


SECRET_ID = "msg_" + uuid.uuid4().hex
SECRETS = (EMAIL, RESET_LINK, TOKEN, KEY, "SELECT * FROM kalillac.users WHERE email = 'x'",
           "7f1c2e9a-0d4b-4c1e-9a7f-5d2b8c3e1f00", SECRET_ID, "provider-private-detail")


def _assert_nothing_leaks(error, capsys, caplog):
    rendered = "".join(traceback.format_exception(error))
    assert str(error) == "EMAIL_DELIVERY_FAILED" and error.args == ("EMAIL_DELIVERY_FAILED",)
    assert error.__cause__ is None and error.__suppress_context__
    output = capsys.readouterr()
    assert output.out == "" and output.err == "" and caplog.text == ""
    for secret in SECRETS:
        assert secret not in rendered


@pytest.mark.parametrize("status", [200, 400, 500])
def test_responses_containing_secrets_do_not_leak(status, capsys, caplog):
    leak = " ".join(SECRETS)
    recorder = Recorder(lambda request: httpx.Response(
        status, json={"id": "x y", "message": leak, "name": leak}))
    with pytest.raises(EmailDeliveryError) as raised:
        gateway_for(recorder).send_password_reset(EMAIL, RESET_LINK)
    _assert_nothing_leaks(raised.value, capsys, caplog)


def test_provider_exception_details_do_not_leak(capsys, caplog):
    def fail(request):
        raise httpx.ConnectError(" ".join(SECRETS), request=request)
    with pytest.raises(EmailDeliveryError) as raised:
        gateway_for(Recorder(fail)).send_password_reset(EMAIL, RESET_LINK)
    _assert_nothing_leaks(raised.value, capsys, caplog)


@pytest.mark.parametrize("exception", [KeyboardInterrupt, SystemExit])
def test_base_exceptions_propagate_unchanged(exception):
    marker = exception()
    def stop(request):
        raise marker
    recorder = Recorder(stop)
    with pytest.raises(exception) as raised:
        gateway_for(recorder).send_password_changed(EMAIL)
    assert raised.value is marker and len(recorder.requests) == 1
