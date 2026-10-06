"""Provider-neutral bounded transport adapter and its process-local holder.

Mapping and holder tests use fake transports, fake clocks and barriers.
Loopback tests drive the real BoundedTransport against a local socket and
synchronize on events; the only waits are the bounded transport's own
deadlines.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
import socket
import sys
import threading
import time

import pytest


CANDIDATE_DIR = Path(__file__).resolve().parents[1]

if str(CANDIDATE_DIR) not in sys.path:
    sys.path.insert(0, str(CANDIDATE_DIR))


from kalillac_routing import bounded_transport
from kalillac_routing.bounded_transport import (
    InvalidJSONResponse,
    ResponseTooLarge,
    TransportCancelled,
    TransportCleanupUnconfirmed,
    TransportClosed,
    TransportConnectionError,
    TransportDeadlineExceeded,
    TransportError,
    TransportHTTPError,
    TransportOverloaded,
    TransportQuarantined,
    UnsupportedContentEncoding,
)
from kalillac_routing.provider_transport import (
    TransportHolder,
    TransportUnavailable,
    post_json_within_budget,
)
from kalillac_routing.request_budget import (
    RequestBudget,
    RequestCancelled,
    RequestDeadlineExceeded,
)
from kalillac_routing.request_limits import TransportLimits


SETTINGS = TransportLimits(
    max_outstanding=4,
    dns_threads=1,
    max_pending_dns=4,
    cancel_poll_interval_seconds=0.02,
    backstop_grace_seconds=0.5,
    cleanup_grace_seconds=1.0,
    close_timeout_seconds=5.0,
)
URL = "https://provider.invalid/v1/endpoint"
HEADERS = {"Authorization": "Bearer test-not-a-key", "Content-Type": "application/json"}
PAYLOAD = {"model": "m", "input": [{"role": "user", "content": "hi"}]}
MAX_BYTES = 2097152


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now


def make_budget(clock=None, duration=30.0):
    return RequestBudget(
        duration_seconds=duration,
        max_model_attempts=6,
        max_search_attempts=6,
        clock=clock or FakeClock(),
    )


class FakeTransport:
    """Stands in for BoundedTransport: records construction, posts, closes."""

    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.posts = []
        self.closes = []
        self.behavior = lambda call: {"ok": True}
        FakeTransport.instances.append(self)

    def post_json(self, url, payload, *, headers, timeout, max_bytes, cancelled=None):
        call = {
            "url": url,
            "payload": payload,
            "headers": headers,
            "timeout": timeout,
            "max_bytes": max_bytes,
            "cancelled": cancelled,
        }
        self.posts.append(call)
        return self.behavior(call)

    def close(self, timeout):
        self.closes.append(timeout)
        return "closed-report"


@pytest.fixture
def fake():
    FakeTransport.instances = []
    holder = TransportHolder(factory=FakeTransport)
    return holder


def post(holder, budget, *, timeout=5.0, selected=False):
    return post_json_within_budget(
        holder,
        SETTINGS,
        URL,
        PAYLOAD,
        headers=HEADERS,
        budget=budget,
        timeout=timeout,
        request_deadline_selected=selected,
        max_bytes=MAX_BYTES,
    )


def failing_with(error):
    def behave(call):
        raise error

    return behave


# --- holder ----------------------------------------------------------------------------


def test_holder_constructs_nothing_until_first_use(fake):
    assert fake.existing() is None
    assert FakeTransport.instances == []


def test_holder_creates_once_under_concurrent_first_use():
    created = []
    gate = threading.Barrier(8)

    def factory(**kwargs):
        created.append(kwargs)
        return FakeTransport(**kwargs)

    holder = TransportHolder(factory=factory)
    results = []

    def worker():
        gate.wait()
        results.append(holder.get_or_create(SETTINGS))

    threads = [threading.Thread(target=worker) for _ in range(8)]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join(10)

    assert len(created) == 1
    assert len(results) == 8
    assert all(result is results[0] for result in results)
    assert holder.existing() is results[0]


def test_holder_passes_validated_settings_to_the_transport(fake):
    transport = fake.get_or_create(SETTINGS)

    assert transport.kwargs == {
        "max_outstanding": 4,
        "dns_threads": 1,
        "max_pending_dns": 4,
        "cancel_poll_interval": 0.02,
        "backstop_grace": 0.5,
        "cleanup_grace": 1.0,
    }


def test_holder_rejects_incompatible_later_settings(fake):
    fake.get_or_create(SETTINGS)
    other = TransportLimits(**{**SETTINGS.__dict__, "max_outstanding": 5})

    with pytest.raises(ValueError) as caught:
        fake.get_or_create(other)

    assert len(FakeTransport.instances) == 1
    assert "5" not in str(caught.value)


def test_holder_rejects_settings_of_the_wrong_type(fake):
    with pytest.raises(TypeError):
        fake.get_or_create({"max_outstanding": 4})

    assert FakeTransport.instances == []


def test_close_without_a_transport_constructs_nothing(fake):
    assert fake.close() is None
    assert fake.closed is True
    assert fake.existing() is None
    assert FakeTransport.instances == []

    with pytest.raises(TransportUnavailable):
        fake.get_or_create(SETTINGS)

    assert FakeTransport.instances == []


def test_close_closes_an_existing_transport_with_the_configured_timeout(fake):
    transport = fake.get_or_create(SETTINGS)

    assert fake.close() == "closed-report"
    assert fake.close() == "closed-report"        # repeated shutdown is safe
    assert transport.closes == [5.0, 5.0]
    assert len(FakeTransport.instances) == 1

    with pytest.raises(TransportUnavailable):
        fake.get_or_create(SETTINGS)


def test_quarantine_is_permanent_and_stops_later_posts(fake):
    budget = make_budget()
    fake.get_or_create(SETTINGS).behavior = failing_with(
        TransportCleanupUnconfirmed("connection")
    )

    with pytest.raises(TransportUnavailable):
        post(fake, budget)

    assert fake.quarantined is True

    for _ in range(3):
        with pytest.raises(TransportUnavailable) as caught:
            post(fake, budget)

        assert caught.value.reason == "quarantined"

    assert len(FakeTransport.instances[0].posts) == 1
    assert len(FakeTransport.instances) == 1


def test_holder_and_adapter_contain_no_provider_policy():
    from kalillac_routing import provider_transport

    source = inspect.getsource(provider_transport).lower()

    for word in ("openai", "groq", "cloudflare", "tavily", "gpt", "luna", "http://", "https://"):
        assert word not in source


# --- adapter: pass-through ----------------------------------------------------------------


def test_adapter_forwards_the_request_unchanged_and_polls_the_budget(fake):
    budget = make_budget()
    seen = []
    transport = fake.get_or_create(SETTINGS)

    def behave(call):
        seen.append(call["cancelled"]())
        budget.cancel()
        seen.append(call["cancelled"]())
        return {"answer": 42}

    transport.behavior = behave

    assert post(fake, budget, timeout=7.5) == {"answer": 42}

    call = transport.posts[0]
    assert call["url"] == URL
    assert call["payload"] is PAYLOAD
    assert call["headers"] == HEADERS
    assert call["timeout"] == 7.5
    assert call["max_bytes"] == MAX_BYTES
    assert seen == [False, True]
    assert len(transport.posts) == 1


@pytest.mark.parametrize("error", [TypeError("bad"), ValueError("bad")])
def test_programming_defects_propagate_unmapped(fake, error):
    fake.get_or_create(SETTINGS).behavior = failing_with(error)

    with pytest.raises(type(error)):
        post(fake, make_budget())

    assert fake.quarantined is False


# --- adapter: the mapping table -----------------------------------------------------------


REMOTE = "remote"
UNAVAILABLE = "unavailable"
DEADLINE = "deadline"

# error factory -> (outcome when cap-selected, outcome when request-selected,
#                   quarantines). The budget is open and not cancelled.
MAPPING = {
    "TransportError": (lambda: TransportError(), UNAVAILABLE, UNAVAILABLE, False),
    "TransportClosed": (lambda: TransportClosed(), UNAVAILABLE, UNAVAILABLE, False),
    "TransportOverloaded": (lambda: TransportOverloaded(), UNAVAILABLE, UNAVAILABLE, False),
    "TransportQuarantined": (lambda: TransportQuarantined(), UNAVAILABLE, UNAVAILABLE, False),
    "TransportCancelled": (lambda: TransportCancelled(), UNAVAILABLE, UNAVAILABLE, False),
    "TransportDeadlineExceeded": (lambda: TransportDeadlineExceeded(), REMOTE, DEADLINE, False),
    "TransportCleanupUnconfirmed-deadline": (
        lambda: TransportCleanupUnconfirmed("deadline"), UNAVAILABLE, DEADLINE, True,
    ),
    "TransportCleanupUnconfirmed-cancelled": (
        lambda: TransportCleanupUnconfirmed("cancelled"), UNAVAILABLE, UNAVAILABLE, True,
    ),
    "TransportCleanupUnconfirmed-connection": (
        lambda: TransportCleanupUnconfirmed("connection"), UNAVAILABLE, UNAVAILABLE, True,
    ),
    "TransportConnectionError": (lambda: TransportConnectionError(), REMOTE, REMOTE, False),
    "TransportHTTPError": (lambda: TransportHTTPError(500), REMOTE, REMOTE, False),
    "InvalidJSONResponse": (lambda: InvalidJSONResponse(), REMOTE, REMOTE, False),
    "UnsupportedContentEncoding": (lambda: UnsupportedContentEncoding(), REMOTE, REMOTE, False),
    "ResponseTooLarge": (lambda: ResponseTooLarge(), REMOTE, REMOTE, False),
}


def test_mapping_covers_every_public_transport_error():
    public = {
        cls for _name, cls in inspect.getmembers(bounded_transport, inspect.isclass)
        if issubclass(cls, TransportError) and cls.__module__ == bounded_transport.__name__
    }
    mapped = {make().__class__ for make, *_ in MAPPING.values()}

    assert mapped == public


def _expect(outcome, error, raised):
    if outcome == REMOTE:
        assert raised is error
    elif outcome == DEADLINE:
        assert type(raised) is RequestDeadlineExceeded
    else:
        assert type(raised) is TransportUnavailable
        assert raised.reason == type(error).__name__

    if raised is not error:
        assert raised.__cause__ is None
        assert raised.__suppress_context__ is True


@pytest.mark.parametrize("name", list(MAPPING))
@pytest.mark.parametrize("selected", [False, True], ids=["cap_selected", "request_selected"])
def test_every_transport_error_follows_the_mapping(fake, name, selected):
    make, cap_outcome, request_outcome, quarantines = MAPPING[name]
    error = make()
    fake.get_or_create(SETTINGS).behavior = failing_with(error)

    with pytest.raises(BaseException) as caught:
        post(fake, make_budget(), selected=selected)

    _expect(request_outcome if selected else cap_outcome, error, caught.value)
    assert fake.quarantined is quarantines
    assert len(FakeTransport.instances[0].posts) == 1     # never retried


@pytest.mark.parametrize("name", list(MAPPING))
@pytest.mark.parametrize("selected", [False, True], ids=["cap_selected", "request_selected"])
def test_cancellation_overrides_every_transport_error(fake, name, selected):
    make, *_rest, quarantines = MAPPING[name]
    budget = make_budget()
    error = make()

    def behave(call):
        budget.cancel()
        raise error

    fake.get_or_create(SETTINGS).behavior = behave

    with pytest.raises(RequestCancelled) as caught:
        post(fake, budget, selected=selected)

    assert caught.value.__cause__ is None
    assert fake.quarantined is quarantines


@pytest.mark.parametrize("name", list(MAPPING))
def test_actual_deadline_overrides_every_transport_error(fake, name):
    make, *_rest, quarantines = MAPPING[name]
    clock = FakeClock()
    budget = make_budget(clock, duration=30.0)
    error = make()

    def behave(call):
        clock.now += 30.0                      # the request deadline has passed
        raise error

    fake.get_or_create(SETTINGS).behavior = behave

    with pytest.raises(RequestDeadlineExceeded):
        post(fake, budget, selected=False)

    assert fake.quarantined is quarantines


def test_cancellation_wins_over_an_expired_deadline(fake):
    clock = FakeClock()
    budget = make_budget(clock, duration=30.0)

    def behave(call):
        budget.cancel()
        clock.now += 60.0
        raise TransportCleanupUnconfirmed("deadline")

    fake.get_or_create(SETTINGS).behavior = behave

    with pytest.raises(RequestCancelled):
        post(fake, budget, selected=True)

    assert fake.quarantined is True


def test_unavailable_error_is_fixed_and_content_free(fake):
    fake.get_or_create(SETTINGS).behavior = failing_with(TransportOverloaded())

    with pytest.raises(TransportUnavailable) as caught:
        post(fake, make_budget())

    text = str(caught.value)
    assert text == TransportUnavailable.MESSAGE
    assert URL not in text and "Bearer" not in text and "hi" not in text


# --- loopback: the real BoundedTransport ---------------------------------------------------


class LoopbackServer:
    """One-connection HTTP server on 127.0.0.1.

    mode "json": answers 200 with a JSON body.
    mode "hold": never answers; records when the client closes.
    mode "trickle": sends headers, then one body byte per interval forever.
    """

    def __init__(self, mode, body=b'{"status": "completed"}'):
        self.mode = mode
        self.body = body
        self.request = b""
        self.received = threading.Event()
        self.peer_closed = threading.Event()
        self.stop = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.sock.settimeout(10)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/v1/endpoint"

    def _read_request(self, conn):
        data = b""

        while b"\r\n\r\n" not in data:
            chunk = conn.recv(65536)

            if not chunk:
                return data

            data += chunk

        head, _, body = data.partition(b"\r\n\r\n")
        length = 0

        for line in head.split(b"\r\n")[1:]:
            key, _, value = line.partition(b":")

            if key.strip().lower() == b"content-length":
                length = int(value.strip())

        while len(body) < length:
            chunk = conn.recv(65536)

            if not chunk:
                break

            body += chunk

        return head + b"\r\n\r\n" + body

    def _serve(self):
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return

        conn.settimeout(10)

        try:
            self.request = self._read_request(conn)
            self.received.set()

            if self.mode == "json":
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    + b"Content-Length: " + str(len(self.body)).encode()
                    + b"\r\nConnection: close\r\n\r\n" + self.body
                )
            elif self.mode == "hold":
                if conn.recv(1) == b"":
                    self.peer_closed.set()
            elif self.mode == "trickle":
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    b"Content-Length: 100000\r\n\r\n"
                )

                while not self.stop.is_set():
                    conn.sendall(b" ")

                    if self.stop.wait(0.05):
                        break
        except OSError:
            self.peer_closed.set()
        finally:
            conn.close()

    def close(self):
        self.stop.set()
        self.sock.close()
        self.thread.join(10)


@pytest.fixture
def real_holder():
    holder = TransportHolder()
    yield holder
    report = holder.close()
    assert report is None or report.clean


def _headers_of(request):
    head = request.partition(b"\r\n\r\n")[0].decode("latin-1")
    return {
        key.strip().lower(): value.strip()
        for key, _, value in (line.partition(":") for line in head.split("\r\n")[1:])
    }


def test_loopback_round_trip_sends_identical_json_and_headers(real_holder):
    server = LoopbackServer("json", body=b'{"status": "completed", "output": []}')

    try:
        result = post_json_within_budget(
            real_holder, SETTINGS, server.url, PAYLOAD,
            headers=HEADERS, budget=make_budget(clock=time.monotonic),
            timeout=5.0, request_deadline_selected=False, max_bytes=MAX_BYTES,
        )
    finally:
        server.close()

    assert result == {"status": "completed", "output": []}
    body = server.request.partition(b"\r\n\r\n")[2]
    assert body == json.dumps(PAYLOAD).encode()
    headers = _headers_of(server.request)
    assert headers["authorization"] == HEADERS["Authorization"]
    assert headers["content-type"] == "application/json"
    assert headers["accept-encoding"] == "identity"


def test_loopback_cancellation_closes_the_connection_promptly(real_holder):
    server = LoopbackServer("hold")
    budget = make_budget(clock=time.monotonic, duration=30.0)
    outcome = []

    def call():
        try:
            post_json_within_budget(
                real_holder, SETTINGS, server.url, PAYLOAD,
                headers=HEADERS, budget=budget, timeout=20.0,
                request_deadline_selected=False, max_bytes=MAX_BYTES,
            )
        except BaseException as error:
            outcome.append(error)

    worker = threading.Thread(target=call)
    worker.start()

    try:
        assert server.received.wait(10)
        cancelled_at = time.monotonic()
        budget.cancel()
        worker.join(10)
        assert server.peer_closed.wait(10)
        closed_after = time.monotonic() - cancelled_at
    finally:
        server.close()

    assert [type(error) for error in outcome] == [RequestCancelled]
    # Poll interval plus cleanup grace, far below the 20 s call timeout.
    assert closed_after < SETTINGS.cancel_poll_interval_seconds + SETTINGS.cleanup_grace_seconds + 1.0
    assert real_holder.quarantined is False


def test_loopback_trickle_cannot_extend_the_request_deadline(real_holder):
    server = LoopbackServer("trickle")
    budget = make_budget(clock=time.monotonic, duration=1.0)
    timeout, selected = budget.select_call_timeout(90.0)
    started = time.monotonic()

    try:
        with pytest.raises(RequestDeadlineExceeded):
            post_json_within_budget(
                real_holder, SETTINGS, server.url, PAYLOAD,
                headers=HEADERS, budget=budget, timeout=timeout,
                request_deadline_selected=selected, max_bytes=MAX_BYTES,
            )

        elapsed = time.monotonic() - started
    finally:
        server.close()

    assert selected is True
    # The body never completes; one total deadline ends the call anyway.
    assert elapsed < 1.0 + SETTINGS.cleanup_grace_seconds + 1.0
