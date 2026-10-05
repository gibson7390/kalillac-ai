"""Bounded JSON transport: loopback servers and controlled mocks only.

No external network access. Timing assertions use generous tolerances;
measured values are collected in MEASUREMENTS and printed at module end.
They describe this machine and run, not a universal guarantee.

Connection-closure evidence is strict: the loopback server records a
client close only when it reads end-of-stream or the peer resets the
connection. A receive timeout is recorded as a failure, and the server
fixture fails the test if any client connection was still open at
teardown or any handler thread did not stop.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import tracemalloc
import zlib

import httpx
import pytest


CANDIDATE_DIR = Path(__file__).resolve().parents[1]

if str(CANDIDATE_DIR) not in sys.path:
    sys.path.insert(0, str(CANDIDATE_DIR))


from kalillac_routing import bounded_transport
from kalillac_routing.bounded_transport import (
    BoundedTransport,
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


DEADLINE = 0.5
TOLERANCE = 1.5
BOUNDED_WAIT = 0.5       # allowed lateness for startup/lock-bounded waits
JOIN = 10.0
SECRET = "sk-test-secret-0123456789"
MEASUREMENTS = []
HTTP_LOGGERS = (
    "httpx", "httpcore.connection", "httpcore.http11",
    "httpcore.http2", "httpcore.proxy", "httpcore.socks",
)


# --- loopback server -------------------------------------------------------------------


GZIP_BOMB = gzip.compress(b"0" * 5_000_000)
GZIP_OK = gzip.compress(json.dumps({"compressed": True}).encode())
INCOMPRESSIBLE = gzip.compress(os.urandom(1_500_000))
CLIENT_GONE = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)
# On Linux, closing a listening socket from another thread does not wake a
# thread blocked in accept(), so the accept loop polls with this timeout.
ACCEPT_POLL = 0.05


class LoopbackServer:
    """Raw-socket HTTP/1.1 server; behavior chosen by request path."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(64)
        self.sock.settimeout(ACCEPT_POLL)
        self.port = self.sock.getsockname()[1]
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.serve_errors = []      # accept failures not caused by teardown
        self.requests = {}          # path -> (headers dict, body bytes)
        self.closed = {}            # path -> Event: client closed (EOF/reset)
        self.timeouts = []          # paths whose client never closed in time
        self.waiting = set()        # paths still waiting for the client
        self.expected_open = set()  # paths a test expects to stay open
        self.conns = set()
        self.threads = []
        self.accept_thread = threading.Thread(target=self._serve, daemon=True)
        self.accept_thread.start()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def received(self, path):
        with self.lock:
            return path in self.requests

    def closed_event(self, path):
        with self.lock:
            return self.closed.setdefault(path, threading.Event())

    def _serve(self):
        while not self.stopping.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue  # re-check stopping
            except OSError:
                if not self.stopping.is_set():
                    # Not caused by teardown: recorded so the fixture fails.
                    self.serve_errors.append("accept failed")
                return

            # Accepted sockets must not inherit the accept poll timeout.
            conn.settimeout(None)
            thread = threading.Thread(target=self._handle, args=(conn,), daemon=True)

            # Registered under the lock teardown uses to set `stopping`, so a
            # connection accepted while teardown starts is closed here and
            # never gets a handler teardown would not know about.
            with self.lock:
                if self.stopping.is_set():
                    conn.close()
                    return

                self.conns.add(conn)
                self.threads.append(thread)

            thread.start()

    def _read_request(self, conn):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(65536)
            if not chunk:
                return None, None, None
            data += chunk
        head, rest = data.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        path = lines[0].split(" ")[1]
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        length = int(headers.get("content-length", "0"))
        while len(rest) < length:
            rest += conn.recv(65536)
        return path, headers, rest

    def _client_closed(self, path):
        if not self.stopping.is_set():
            self.closed_event(path).set()

    def _timed_out(self, path):
        if not self.stopping.is_set():
            with self.lock:
                self.timeouts.append(path)

    def _wait_for_close(self, conn, path):
        """Only end-of-stream or a peer reset counts as the client closing.
        A timeout is a failure; an error caused by teardown is neither."""

        with self.lock:
            self.waiting.add(path)

        conn.settimeout(JOIN)

        try:
            while True:
                if not conn.recv(65536):
                    self._client_closed(path)
                    return
        except socket.timeout:
            self._timed_out(path)
        except CLIENT_GONE:
            self._client_closed(path)
        except OSError:
            pass  # teardown closed the socket
        finally:
            with self.lock:
                self.waiting.discard(path)

    def _respond(self, conn, status, body=b"", extra=b""):
        conn.sendall(
            b"HTTP/1.1 " + status + b"\r\nContent-Length: "
            + str(len(body)).encode() + b"\r\n" + extra + b"\r\n" + body
        )

    def _trickle(self, conn, path):
        conn.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
        conn.settimeout(0.02)
        give_up_at = time.monotonic() + JOIN

        with self.lock:
            self.waiting.add(path)

        try:
            while time.monotonic() < give_up_at:
                try:
                    conn.sendall(b"1\r\n \r\n")
                    if conn.recv(1) == b"":
                        self._client_closed(path)
                        return
                except socket.timeout:
                    continue

            self._timed_out(path)
        except CLIENT_GONE:
            self._client_closed(path)
        except OSError:
            pass
        finally:
            with self.lock:
                self.waiting.discard(path)

    def _handle(self, conn):
        try:
            path, headers, body = self._read_request(conn)
            if path is None:
                return
            with self.lock:
                self.requests[path] = (headers, body)
            self.closed_event(path)
            route = path.split("?")[0]

            if route == "/ok":
                payload = json.dumps({"ok": True, "echo": json.loads(body)}).encode()
                self._respond(conn, b"200 OK", payload,
                              b"Content-Type: application/json\r\n")
            elif route == "/malformed":
                self._respond(conn, b"200 OK", b'{"secret": "' + SECRET.encode() + b'"')
            elif route in ("/status429", "/status500"):
                code = route[-3:].encode()
                self._respond(conn, code + b" Error", b'{"error":"' + SECRET.encode() + b'"}')
            elif route == "/redirect":
                self._respond(conn, b"302 Found", b"", b"Location: /ok\r\n")
            elif route == "/stall-headers":
                pass
            elif route in ("/stall-body", "/cancel", "/shutdown"):
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n{\"a\":")
            elif route == "/trickle":
                self._trickle(conn, path)
                return
            elif route == "/huge-declared":
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 5000000\r\n\r\n")
                self._stream_blocks(conn, chunked=False)
            elif route == "/huge-chunked":
                conn.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
                self._stream_blocks(conn, chunked=True)
            elif route == "/gzip-bomb":
                self._respond(conn, b"200 OK", GZIP_BOMB, b"Content-Encoding: gzip\r\n")
            elif route == "/gzip-ok":
                self._respond(conn, b"200 OK", GZIP_OK, b"Content-Encoding: gzip\r\n")
            elif route == "/gzip-raw-large":
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\n"
                             b"Transfer-Encoding: chunked\r\n\r\n")
                conn.settimeout(JOIN)
                try:
                    for start in range(0, len(INCOMPRESSIBLE), 65536):
                        block = INCOMPRESSIBLE[start:start + 65536]
                        conn.sendall(b"%x\r\n" % len(block) + block + b"\r\n")
                except OSError:
                    pass
            elif route == "/deflate-ok":
                self._respond(conn, b"200 OK", zlib.compress(b'{"deflated": 1}'),
                              b"Content-Encoding: deflate\r\n")
            elif route == "/brotli":
                self._respond(conn, b"200 OK", b"\x00\x01", b"Content-Encoding: br\r\n")

            self._wait_for_close(conn, path)
        except OSError:
            pass
        finally:
            conn.close()
            with self.lock:
                self.conns.discard(conn)

    @staticmethod
    def _stream_blocks(conn, chunked):
        conn.settimeout(JOIN)
        block = b"x" * 65536
        try:
            for _ in range(80):
                if chunked:
                    conn.sendall(b"%x\r\n" % len(block) + block + b"\r\n")
                else:
                    conn.sendall(block)
        except OSError:
            pass

    def close(self, timeout=JOIN):
        """Bounded teardown. Returns (unclosed client paths, live threads)."""

        # Clients should all be gone by now; give in-flight closes a moment
        # to be observed before treating an open connection as a leak.
        settle_until = time.monotonic() + 2.0
        while time.monotonic() < settle_until:
            with self.lock:
                if not (self.waiting - self.expected_open):
                    break
            time.sleep(0.01)

        with self.lock:
            unclosed = sorted(self.waiting - self.expected_open)
            # Set under the lock: no connection can be registered after
            # this snapshot (see _serve).
            self.stopping.set()
            conns = list(self.conns)

        self.sock.close()

        for conn in conns:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass

        give_up_at = time.monotonic() + timeout
        self.accept_thread.join(max(0.0, give_up_at - time.monotonic()))

        with self.lock:
            threads = list(self.threads)

        for thread in threads:
            thread.join(max(0.0, give_up_at - time.monotonic()))

        alive = [t for t in threads + [self.accept_thread] if t.is_alive()]
        return unclosed, alive


@pytest.fixture
def server():
    s = LoopbackServer()
    yield s
    unclosed, alive = s.close()
    assert unclosed == [], f"client connections left open: {unclosed}"
    assert alive == [], "loopback server threads did not stop"
    assert s.timeouts == [], f"client never closed: {s.timeouts}"
    assert s.serve_errors == [], "loopback server accept failed"


# --- loopback server teardown ----------------------------------------------------------


def test_idle_server_close_stops_its_waiting_accept_thread():
    s = LoopbackServer()
    time.sleep(3 * ACCEPT_POLL)  # the accept thread is now waiting in accept()
    assert s.accept_thread.is_alive()

    start = time.perf_counter()
    unclosed, alive = s.close(timeout=JOIN)
    elapsed = time.perf_counter() - start

    MEASUREMENTS.append(("idle server close", "close_return_s", elapsed))
    assert alive == []
    assert unclosed == []
    assert s.serve_errors == []
    assert not s.accept_thread.is_alive()
    assert elapsed < 1.0  # bounded by the accept poll, not by JOIN


def test_accept_loop_exits_on_stop_even_if_socket_close_never_wakes_it():
    """Linux does not wake a thread blocked in accept() when another thread
    closes the listening socket. Model that on any platform: signal stop
    without closing the socket; the poll alone must end the loop."""

    s = LoopbackServer()
    time.sleep(3 * ACCEPT_POLL)

    try:
        with s.lock:
            s.stopping.set()

        start = time.perf_counter()
        s.accept_thread.join(1.0)
        elapsed = time.perf_counter() - start

        MEASUREMENTS.append(("accept loop stop, socket open", "exit_s", elapsed))
        assert not s.accept_thread.is_alive()
        assert elapsed < 1.0
        assert s.serve_errors == []
    finally:
        s.sock.close()


# --- transport helpers -----------------------------------------------------------------


def make_transport(**overrides):
    settings = {
        "max_outstanding": 8,
        "dns_threads": 2,
        "max_pending_dns": 2,
        "cancel_poll_interval": 0.02,
        "backstop_grace": 0.25,
        "cleanup_grace": 0.5,
    }
    settings.update(overrides)
    return BoundedTransport(**settings)


@pytest.fixture
def transport():
    t = make_transport()
    yield t
    report = t.close(JOIN)
    assert report.clean, report


# Functional calls get a roomy timeout: a fresh transport's first call
# includes one-time startup (SSL setup ~0.3-0.5 s measured here), which
# counts against the deadline by design. Deadline tests start() first.
def post(transport, url, timeout=5.0, max_bytes=1_000_000, cancelled=None,
         payload=None, headers=None):
    return transport.post_json(
        url,
        {"probe": True} if payload is None else payload,
        headers={"Authorization": f"Bearer {SECRET}"} if headers is None else headers,
        timeout=timeout,
        max_bytes=max_bytes,
        cancelled=cancelled,
    )


def wait_until(predicate, timeout=JOIN):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def block_loop(t):
    """Block the transport's loop thread until the returned event is set."""

    unblock = threading.Event()
    t._runtime.loop.call_soon_threadsafe(unblock.wait, JOIN)
    return unblock


def assert_clean(transport):
    stats = transport.stats()
    assert stats.outstanding == 0
    assert stats.unconfirmed == 0
    assert stats.open_clients == 0
    assert stats.unresolved_clients == 0
    assert stats.quarantined is False


def assert_resources_released(runtime):
    assert runtime.executor._shutdown is True
    assert runtime.loop.is_closed()
    for name in HTTP_LOGGERS:
        assert runtime.log_filter not in logging.getLogger(name).filters
    assert runtime.thread is None or not runtime.thread.is_alive()


def assert_safe(error):
    text = str(error) + repr(error)
    for leaked in (SECRET, "127.0.0.1", "Bearer", "http://"):
        assert leaked not in text
    assert error.__cause__ is None
    assert error.__context__ is None


# --- success, malformed, HTTP errors -------------------------------------------------


def test_successful_json_round_trip(server, transport):
    start = time.perf_counter()
    result = post(transport, server.url("/ok"), payload={"n": 1, "s": "text"})
    MEASUREMENTS.append(("success (incl. startup)", "elapsed_s", time.perf_counter() - start))

    assert result == {"ok": True, "echo": {"n": 1, "s": "text"}}
    headers, body = server.requests["/ok"]
    assert json.loads(body) == {"n": 1, "s": "text"}
    assert headers["content-type"] == "application/json"
    assert headers["accept-encoding"] == "identity"
    assert headers["authorization"] == f"Bearer {SECRET}"
    assert server.closed_event("/ok").wait(JOIN)
    assert_clean(transport)


def test_caller_cannot_override_accept_encoding(server, transport):
    post(transport, server.url("/ok"), headers={"Accept-Encoding": "gzip, br"})

    assert server.requests["/ok"][0]["accept-encoding"] == "identity"


@pytest.mark.parametrize("path", ["/gzip-ok", "/deflate-ok"])
def test_supported_compression_still_decodes(server, transport, path):
    expected = {"/gzip-ok": {"compressed": True}, "/deflate-ok": {"deflated": 1}}

    assert post(transport, server.url(path)) == expected[path]


def test_malformed_json_is_typed_and_content_free(server, transport):
    with pytest.raises(InvalidJSONResponse) as caught:
        post(transport, server.url("/malformed"))

    assert str(caught.value) == "Response body was not valid JSON."
    assert_safe(caught.value)
    assert_clean(transport)


@pytest.mark.parametrize("path, status", [("/status429", 429), ("/status500", 500)])
def test_http_error_status_is_preserved(server, transport, path, status):
    with pytest.raises(TransportHTTPError) as caught:
        post(transport, server.url(path))

    assert caught.value.status == status
    assert str(caught.value) == "Server returned an HTTP error status."
    assert_safe(caught.value)
    assert server.closed_event(path).wait(JOIN)
    assert_clean(transport)


def test_redirect_is_not_followed(server, transport):
    with pytest.raises(TransportHTTPError) as caught:
        post(transport, server.url("/redirect"))

    assert caught.value.status == 302
    time.sleep(0.2)
    assert not server.received("/ok")


def test_connection_refused_is_typed():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()  # nothing listens on this port now
    t = make_transport()

    try:
        start = time.perf_counter()

        # Windows retries a refused loopback connect for ~2 s, so give it
        # room; the point is the typed error, not the speed.
        with pytest.raises(TransportConnectionError) as caught:
            post(t, f"http://127.0.0.1:{port}/ok", timeout=10.0)

        MEASUREMENTS.append(("refused", "elapsed_s", time.perf_counter() - start))
        assert_safe(caught.value)
        assert_clean(t)
    finally:
        assert t.close(JOIN).clean


# --- deadlines --------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/stall-headers", "/stall-body", "/trickle"])
def test_total_deadline_covers_headers_and_body(server, transport, path):
    transport.start(JOIN)
    start = time.perf_counter()

    with pytest.raises(TransportDeadlineExceeded) as caught:
        post(transport, server.url(path), timeout=DEADLINE)

    overshoot = time.perf_counter() - start - DEADLINE
    MEASUREMENTS.append((path, "deadline_overshoot_s", overshoot))

    assert_safe(caught.value)
    assert overshoot < TOLERANCE
    # Cleanup: the request's client is closed and the server saw it close.
    assert_clean(transport)
    assert server.closed_event(path).wait(JOIN)


def test_trickle_cannot_extend_a_longer_deadline(server, transport):
    transport.start(JOIN)
    start = time.perf_counter()

    with pytest.raises(TransportDeadlineExceeded):
        post(transport, server.url("/trickle"), timeout=1.0)

    elapsed = time.perf_counter() - start
    MEASUREMENTS.append(("/trickle 1.0s", "elapsed_s", elapsed))
    # The loop clock has coarse resolution on some platforms (15.6 ms on
    # Windows), so allow firing slightly early.
    assert 1.0 - 0.05 <= elapsed < 1.0 + TOLERANCE
    assert server.closed_event("/trickle").wait(JOIN)


# --- startup and lock contention ---------------------------------------------------------


def gated_startup(t):
    """Make startup wait for the returned gate; records each attempt."""

    gate = threading.Event()
    attempts = []
    real_initialize = t._initialize

    def slow_initialize():
        attempts.append(threading.current_thread().name)
        gate.wait(JOIN)
        runtime = real_initialize()
        attempts.append(runtime)
        return runtime

    t._initialize = slow_initialize
    return gate, attempts


def test_startup_longer_than_the_deadline_is_bounded(server):
    t = make_transport()
    gate, attempts = gated_startup(t)

    try:
        start = time.perf_counter()

        with pytest.raises(TransportDeadlineExceeded):
            post(t, server.url("/ok"), timeout=0.3)

        elapsed = time.perf_counter() - start
        MEASUREMENTS.append(("startup > deadline", "return_s", elapsed))
        assert elapsed < 0.3 + BOUNDED_WAIT
        assert t.stats().starting is True
        assert not server.received("/ok")

        gate.set()
        assert post(t, server.url("/ok"))["ok"] is True
        assert len([a for a in attempts if isinstance(a, str)]) == 1
        assert t.stats().loops_created == 1
    finally:
        gate.set()
        assert t.close(JOIN).clean


def test_concurrent_callers_share_one_bounded_startup(server):
    t = make_transport(max_outstanding=32)
    gate, attempts = gated_startup(t)
    callers = 8
    barrier = threading.Barrier(callers)
    results = []
    lock = threading.Lock()

    def call():
        barrier.wait(JOIN)
        start = time.perf_counter()
        try:
            post(t, server.url("/ok"), timeout=0.3)
            outcome = "ok"
        except TransportError as error:
            outcome = type(error)
        with lock:
            results.append((outcome, time.perf_counter() - start))

    threads = [threading.Thread(target=call) for _ in range(callers)]

    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(JOIN)

        assert [outcome for outcome, _ in results] == [TransportDeadlineExceeded] * callers
        slowest = max(elapsed for _, elapsed in results)
        MEASUREMENTS.append(("8 callers, startup > deadline", "slowest_return_s", slowest))
        assert slowest < 0.3 + BOUNDED_WAIT

        # One starter thread, one initialization, no matter how many callers.
        starters = [a for a in attempts if isinstance(a, str)]
        assert starters == [f"{t._name}-start"]

        gate.set()
        assert post(t, server.url("/ok"))["ok"] is True
        assert t.stats().loops_created == 1
    finally:
        gate.set()
        assert t.close(JOIN).clean


@pytest.mark.parametrize("started", [False, True])
def test_lock_contention_longer_than_the_deadline_is_bounded(server, started):
    t = make_transport()

    if started:
        t.start(JOIN)

    t._lock.acquire()
    releaser = threading.Timer(1.5, t._lock.release)
    releaser.start()

    try:
        start = time.perf_counter()

        with pytest.raises(TransportDeadlineExceeded):
            post(t, server.url("/ok"), timeout=0.3)

        elapsed = time.perf_counter() - start
        MEASUREMENTS.append((f"lock held 1.5s, started={started}", "return_s", elapsed))
        assert elapsed < 0.3 + BOUNDED_WAIT
        assert not server.received("/ok")

        releaser.join(JOIN)
        assert post(t, server.url("/ok"))["ok"] is True
    finally:
        releaser.join(JOIN)
        assert t.close(JOIN).clean


def test_close_during_slow_startup_reports_then_discards(server):
    t = make_transport()
    gate, attempts = gated_startup(t)
    outcome = []

    def waiter():
        try:
            post(t, server.url("/ok"), timeout=5.0)
        except TransportError as error:
            outcome.append(type(error))

    thread = threading.Thread(target=waiter)
    thread.start()

    try:
        assert wait_until(lambda: t.stats().starting)
        start = time.perf_counter()
        report = t.close(0.3)
        MEASUREMENTS.append(("close during startup", "close_return_s",
                             time.perf_counter() - start))

        assert report.clean is False
        assert report.starting is True

        gate.set()
        thread.join(JOIN)
        assert outcome == [TransportClosed]
        assert wait_until(lambda: not t.stats().starting)

        # The late runtime was never published or run, and was released.
        built = [a for a in attempts if not isinstance(a, str)]
        assert len(built) == 1
        assert built[0].thread is None
        assert_resources_released(built[0])
        assert t.stats().loops_created == 0
        assert not server.received("/ok")
        assert t.close(1.0).clean
    finally:
        gate.set()
        thread.join(JOIN)


def test_close_arriving_just_before_publish_discards_the_runtime(server):
    """close() lands after the starter's first closed-check but before it
    publishes: the runtime must still be discarded, never run."""

    t = make_transport()
    built = []
    real_initialize = t._initialize
    real_check = t._closing_wins

    def initialize():
        runtime = real_initialize()
        built.append(runtime)
        return runtime

    def check_then_close_arrives(runtime):
        decision = real_check(runtime)
        if not decision and runtime is not None:
            # Exactly what close() does first, landing in the window.
            t._closed = True
        return decision

    t._initialize = initialize
    t._closing_wins = check_then_close_arrives

    with pytest.raises(TransportClosed):
        post(t, server.url("/ok"))

    assert len(built) == 1
    assert built[0].thread is None            # never run
    assert_resources_released(built[0])
    assert t.stats().loops_created == 0
    assert not any(
        thread.name == f"{t._name}-loop" for thread in threading.enumerate()
    )
    assert t.close(1.0).clean
    assert not server.received("/ok")


def test_failed_startup_is_typed_and_can_be_retried(server):
    t = make_transport()
    real_initialize = t._initialize
    calls = []

    def failing_once():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError(SECRET)
        return real_initialize()

    t._initialize = failing_once

    try:
        with pytest.raises(TransportError) as caught:
            post(t, server.url("/ok"))

        assert type(caught.value) is TransportError
        assert_safe(caught.value)
        assert post(t, server.url("/ok"))["ok"] is True
        assert len(calls) == 2
    finally:
        assert t.close(JOIN).clean


# --- size limits -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/huge-declared", "/huge-chunked", "/gzip-raw-large"],
)
def test_oversized_responses_are_rejected(server, transport, path):
    with pytest.raises(ResponseTooLarge) as caught:
        post(transport, server.url(path), timeout=10.0)

    assert_safe(caught.value)
    assert_clean(transport)
    assert server.closed_event(path).wait(JOIN)


def test_compressed_bomb_is_bounded_while_decoding(server, transport):
    # ~5 KB of gzip expanding to 5 MB, against a 1 MB limit.
    assert len(GZIP_BOMB) < 100_000
    tracemalloc.start()

    try:
        with pytest.raises(ResponseTooLarge):
            post(transport, server.url("/gzip-bomb"), timeout=10.0)

        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    MEASUREMENTS.append(("/gzip-bomb", "peak_traced_mb", peak / 1e6))
    # Far below the 5 MB the body would decode to.
    assert peak < 4_000_000
    assert_clean(transport)


def test_unsupported_encoding_is_refused(server, transport):
    with pytest.raises(UnsupportedContentEncoding):
        post(transport, server.url("/brotli"))

    assert_clean(transport)


# --- cancellation --------------------------------------------------------------------


def test_pre_cancelled_call_sends_nothing(server, transport):
    with pytest.raises(TransportCancelled):
        post(transport, server.url("/ok"), cancelled=lambda: True)

    time.sleep(0.2)
    assert not server.received("/ok")
    assert transport.stats().started is False  # never even started


def test_cancellation_during_request_closes_connection(server, transport):
    flag = threading.Event()
    signalled = {}

    def cancel_after_headers():
        assert wait_until(lambda: server.received("/cancel"))
        time.sleep(0.1)
        signalled["at"] = time.perf_counter()
        flag.set()

    helper = threading.Thread(target=cancel_after_headers)
    helper.start()

    with pytest.raises(TransportCancelled):
        post(transport, server.url("/cancel"), timeout=10.0, cancelled=flag.is_set)

    returned = time.perf_counter()
    helper.join(JOIN)
    latency = returned - signalled["at"]
    MEASUREMENTS.append(("/cancel", "cancel_to_return_s", latency))

    assert latency < TOLERANCE  # ended by cancellation, not the 10 s deadline
    assert server.closed_event("/cancel").wait(JOIN)
    assert_clean(transport)


# --- DNS ------------------------------------------------------------------------------


@pytest.fixture
def slow_dns(monkeypatch):
    release = threading.Event()
    real = socket.getaddrinfo

    def resolver(host, *args, **kwargs):
        if host in ("slow.test", b"slow.test"):
            release.wait(JOIN)
            host = "127.0.0.1"
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    yield release
    release.set()


def test_slow_dns_returns_by_deadline_but_lookup_continues(server, slow_dns):
    t = make_transport(dns_threads=1, max_pending_dns=1)
    t.start(JOIN)

    try:
        start = time.perf_counter()

        with pytest.raises(TransportDeadlineExceeded):
            post(t, f"https://slow.test:{server.port}/ok", timeout=DEADLINE)

        elapsed = time.perf_counter() - start
        MEASUREMENTS.append(("slow DNS", "return_s", elapsed))
        assert elapsed < DEADLINE + TOLERANCE

        stats = t.stats()
        assert stats.outstanding == 0       # the request itself finished
        assert stats.open_clients == 0
        assert stats.dns_pending == 1       # the OS lookup is still running

        slow_dns.set()
        assert wait_until(lambda: t.stats().dns_pending == 0)
    finally:
        slow_dns.set()
        assert t.close(JOIN).clean


def test_repeated_slow_dns_saturation_refuses_new_work(server, slow_dns):
    t = make_transport(dns_threads=1, max_pending_dns=1)
    t.start(JOIN)

    try:
        with pytest.raises(TransportDeadlineExceeded):
            post(t, f"https://slow.test:{server.port}/ok", timeout=0.3)

        start = time.perf_counter()

        with pytest.raises(TransportOverloaded) as caught:
            post(t, server.url("/ok"))

        assert time.perf_counter() - start < 0.5   # refused, not queued
        assert str(caught.value) == "Transport is at capacity."
        assert not server.received("/ok")

        slow_dns.set()
        assert wait_until(lambda: t.stats().dns_pending == 0)
        assert post(t, server.url("/ok"))["ok"] is True
    finally:
        slow_dns.set()
        assert t.close(JOIN).clean


def test_dns_capacity_is_reserved_at_submission(server, slow_dns):
    # More pool threads than the pending limit, so only the reservation
    # can keep queued-plus-running lookups at 1.
    t = make_transport(dns_threads=3, max_pending_dns=1, max_outstanding=16)
    t.start(JOIN)
    callers = 6
    outcomes = []
    lock = threading.Lock()
    samples = []
    sampling = threading.Event()

    def call():
        try:
            post(t, f"https://slow.test:{server.port}/ok", timeout=2.0)
            outcome = "ok"
        except TransportError as error:
            outcome = type(error)
        with lock:
            outcomes.append(outcome)

    def sample():
        while not sampling.is_set():
            samples.append(t._runtime.executor.pending)
            time.sleep(0.001)

    # Hold the loop so every caller passes the admission fast path (the
    # pool is empty then) before any lookup is submitted.
    unblock = block_loop(t)
    threads = [threading.Thread(target=call) for _ in range(callers)]
    sampler = threading.Thread(target=sample)

    try:
        for thread in threads:
            thread.start()

        assert wait_until(lambda: t.stats().outstanding == callers)
        sampler.start()
        unblock.set()

        for thread in threads:
            thread.join(JOIN)

        sampling.set()
        sampler.join(JOIN)

        assert t._runtime.executor.high_water == 1
        assert max(samples) <= 1
        assert outcomes.count(TransportOverloaded) == callers - 1
        assert outcomes.count(TransportDeadlineExceeded) == 1
        assert not server.received("/ok")
    finally:
        unblock.set()
        sampling.set()
        slow_dns.set()
        assert wait_until(lambda: t.stats().dns_pending == 0)
        assert t.close(JOIN).clean


# --- unexpected failures -----------------------------------------------------------------


def test_client_construction_failure_is_contained(server, monkeypatch):
    def broken_client(*args, **kwargs):
        raise RuntimeError(SECRET)

    t = make_transport()
    t.start(JOIN)
    monkeypatch.setattr(bounded_transport.httpx, "AsyncClient", broken_client)

    try:
        with pytest.raises(TransportError) as caught:
            post(t, server.url("/ok"))

        assert type(caught.value) is TransportError
        assert_safe(caught.value)
        assert_clean(t)
        assert not server.received("/ok")
    finally:
        assert t.close(JOIN).clean


def failing_close_client(closes_first, created):
    real_client = httpx.AsyncClient

    class FailingClose(real_client):
        def __init__(self, *args, **kwargs):
            created.append(1)
            super().__init__(*args, **kwargs)

        async def aclose(self):
            if closes_first:
                await super().aclose()
            raise RuntimeError(SECRET)

    return FailingClose


@pytest.mark.parametrize("closes_first", [True, False])
def test_unconfirmed_client_close_withholds_result_and_quarantines(
    server, monkeypatch, closes_first,
):
    created = []
    t = make_transport()
    t.start(JOIN)
    monkeypatch.setattr(
        bounded_transport.httpx,
        "AsyncClient",
        failing_close_client(closes_first, created),
    )

    if not closes_first:
        # The pooled connection really is left open: expected evidence.
        server.expected_open.add("/ok")

    # The exchange completed, but its connection is not confirmed closed:
    # the result is withheld rather than reported as a success.
    with pytest.raises(TransportCleanupUnconfirmed) as caught:
        post(t, server.url("/ok"))

    assert caught.value.reason == "connection"
    assert_safe(caught.value)
    assert server.received("/ok")

    # The request is finished (not stranded); the resource is unresolved.
    stats = t.stats()
    assert stats.outstanding == 0
    assert stats.open_clients == 0
    assert stats.unresolved_clients == 1
    assert stats.quarantined is True

    if closes_first:
        assert server.closed_event("/ok").wait(JOIN)

    report = t.close(JOIN)
    assert report.closed is True
    assert report.finalized is True
    assert report.loop_thread_alive is False
    assert report.quarantined is True
    assert report.unresolved_clients == 1
    assert report.clean is False          # cleanup could not be confirmed


def test_quarantine_stops_work_accumulating_after_uncertain_cleanup(
    server, monkeypatch,
):
    created = []
    t = make_transport(max_outstanding=8)
    t.start(JOIN)
    monkeypatch.setattr(
        bounded_transport.httpx,
        "AsyncClient",
        failing_close_client(False, created),
    )
    server.expected_open.add("/ok")

    with pytest.raises(TransportCleanupUnconfirmed):
        post(t, server.url("/ok"))

    # Every later attempt is refused immediately, with a fixed typed error,
    # before any client or connection is created.
    for attempt in range(20):
        start = time.perf_counter()

        with pytest.raises(TransportQuarantined) as caught:
            post(t, server.url(f"/ok?attempt={attempt}"))

        assert time.perf_counter() - start < 0.5
        assert str(caught.value) == "Transport is quarantined after unconfirmed cleanup."
        assert_safe(caught.value)

    stats = t.stats()
    assert len(created) == 1
    assert stats.unresolved_clients == 1
    assert stats.outstanding == 0
    assert not any(server.received(f"/ok?attempt={n}") for n in range(20))

    # Quarantine is final: still refused later, and close reports it.
    with pytest.raises(TransportQuarantined):
        post(t, server.url("/ok"))

    report = t.close(JOIN)
    assert report.clean is False
    assert report.quarantined is True
    assert report.unresolved_clients == 1


def test_unexpected_task_failure_is_contained(server):
    t = make_transport()
    t.start(JOIN)

    async def broken_execute(call):
        raise RuntimeError(SECRET)

    t._execute = broken_execute

    try:
        with pytest.raises(TransportError) as caught:
            post(t, server.url("/ok"))

        assert type(caught.value) is TransportError
        assert_safe(caught.value)
        assert_clean(t)
    finally:
        assert t.close(JOIN).clean


def test_unexpected_processing_failure_is_contained(server, transport, monkeypatch):
    def broken_parse(body):
        raise KeyError(SECRET)

    monkeypatch.setattr(bounded_transport, "_parse_json", broken_parse)

    with pytest.raises(TransportError) as caught:
        post(transport, server.url("/ok"))

    assert type(caught.value) is TransportError
    assert_safe(caught.value)
    assert_clean(transport)
    assert server.closed_event("/ok").wait(JOIN)


# --- capacity and unconfirmed cleanup ----------------------------------------------------


def test_concurrent_first_use_creates_one_loop(server):
    t = make_transport(max_outstanding=32)
    workers = 16
    barrier = threading.Barrier(workers)
    results = []

    def use():
        barrier.wait(JOIN)
        results.append(post(t, server.url("/ok"), timeout=5.0)["ok"])

    threads = [threading.Thread(target=use) for _ in range(workers)]

    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(JOIN)

        assert results == [True] * workers
        assert t.stats().loops_created == 1
        loop_threads = [
            thread for thread in threading.enumerate()
            if thread.name == f"{t._name}-loop"
        ]
        assert len(loop_threads) == 1
    finally:
        assert t.close(JOIN).clean


def test_outstanding_cap_refuses_extra_requests(server):
    t = make_transport(max_outstanding=1)
    t.start(JOIN)
    errors = []

    def occupy():
        try:
            post(t, server.url("/stall-headers"), timeout=2.0)
        except TransportError as error:
            errors.append(type(error))

    occupant = threading.Thread(target=occupy)
    occupant.start()

    try:
        assert wait_until(lambda: t.stats().outstanding == 1)

        with pytest.raises(TransportOverloaded):
            post(t, server.url("/ok"))

        occupant.join(JOIN)
        assert errors == [TransportDeadlineExceeded]
        assert server.closed_event("/stall-headers").wait(JOIN)
    finally:
        occupant.join(JOIN)
        assert t.close(JOIN).clean


def test_backstop_reports_unconfirmed_cleanup_and_caps_new_work(server):
    t = make_transport(max_outstanding=1, backstop_grace=0.2, cleanup_grace=0.3)
    t.start(JOIN)
    unblock = block_loop(t)  # the request can neither run nor clean up

    try:
        start = time.perf_counter()

        with pytest.raises(TransportCleanupUnconfirmed) as caught:
            post(t, server.url("/ok"), timeout=0.3)

        MEASUREMENTS.append(("unconfirmed", "return_s", time.perf_counter() - start))
        assert caught.value.reason == "deadline"
        assert str(caught.value) == "Transport cleanup was not confirmed."

        stats = t.stats()
        assert stats.outstanding == 1
        assert stats.unconfirmed == 1

        # Still counted, so no new work is admitted.
        with pytest.raises(TransportOverloaded):
            post(t, server.url("/ok"))

        unblock.set()
        assert wait_until(lambda: t.stats().outstanding == 0)
        assert t.stats().unconfirmed == 0
        # The late request was cancelled before any network work.
        time.sleep(0.2)
        assert not server.received("/ok")
    finally:
        unblock.set()
        assert t.close(JOIN).clean


def test_repeated_unconfirmed_timeouts_never_exceed_the_cap(server):
    cap = 3
    t = make_transport(max_outstanding=cap, backstop_grace=0.1, cleanup_grace=0.1)
    t.start(JOIN)
    unblock = block_loop(t)

    try:
        for _ in range(cap):
            with pytest.raises(TransportCleanupUnconfirmed):
                post(t, server.url("/ok"), timeout=0.1)

        assert t.stats().outstanding == cap
        assert t.stats().unconfirmed == cap

        # Every further attempt is refused immediately; nothing accumulates.
        for _ in range(10):
            start = time.perf_counter()

            with pytest.raises(TransportOverloaded):
                post(t, server.url("/ok"), timeout=0.1)

            assert time.perf_counter() - start < 0.5

        assert t.stats().outstanding == cap

        unblock.set()
        assert wait_until(lambda: t.stats().outstanding == 0)
        assert t.stats().unconfirmed == 0
        time.sleep(0.2)
        assert not server.received("/ok")  # none of them reached the network
    finally:
        unblock.set()
        assert t.close(JOIN).clean


# --- shutdown ------------------------------------------------------------------------


def test_shutdown_cancels_in_flight_and_finalizes(server):
    t = make_transport()
    t.start(JOIN)
    runtime = t._runtime
    outcome = []

    def in_flight():
        try:
            post(t, server.url("/shutdown"), timeout=10.0)
        except TransportError as error:
            outcome.append(type(error))

    worker = threading.Thread(target=in_flight)
    worker.start()
    assert wait_until(lambda: server.received("/shutdown"))

    report = t.close(5.0)
    worker.join(JOIN)

    assert report.clean
    assert report.finalized
    assert not report.loop_thread_alive
    assert outcome == [TransportCancelled]
    assert server.closed_event("/shutdown").wait(JOIN)
    assert_resources_released(runtime)

    with pytest.raises(TransportClosed):
        post(t, server.url("/ok"))

    with pytest.raises(TransportClosed):
        t.start(1.0)

    assert t.close(1.0).clean  # idempotent


def test_timed_out_close_finalizes_after_the_last_request_finishes(server):
    t = make_transport(backstop_grace=0.1, cleanup_grace=0.1)
    t.start(JOIN)
    runtime = t._runtime
    unblock = block_loop(t)

    with pytest.raises(TransportCleanupUnconfirmed):
        post(t, server.url("/ok"), timeout=0.1)

    report = t.close(0.3)

    # Not stopped while a request is pending: reported, not abandoned.
    assert report.clean is False
    assert report.outstanding == 1
    assert report.unconfirmed == 1
    assert report.loop_thread_alive is True
    assert report.finalized is False
    assert not runtime.loop.is_closed()

    unblock.set()

    # The last request finishing stops the loop; the loop thread finalizes.
    assert wait_until(lambda: t.stats().finalized)
    assert wait_until(lambda: not runtime.thread.is_alive())
    assert_resources_released(runtime)

    later = t.close(1.0)
    assert later.clean
    assert later.outstanding == 0
    assert not server.received("/ok")


def test_shutdown_report_counts_a_running_dns_lookup(server, slow_dns):
    t = make_transport(dns_threads=1, max_pending_dns=1)
    t.start(JOIN)
    runtime = t._runtime

    with pytest.raises(TransportDeadlineExceeded):
        post(t, f"https://slow.test:{server.port}/ok", timeout=0.3)

    report = t.close(1.0)

    assert report.outstanding == 0
    assert report.loop_thread_alive is False
    assert report.finalized is True
    assert report.dns_pending == 1      # the lookup thread survives close()
    assert report.clean is False
    assert_resources_released(runtime)

    slow_dns.set()
    assert wait_until(lambda: t.stats().dns_pending == 0)
    assert t.close(1.0).clean


def test_close_without_the_lock_is_never_reported_clean():
    """A never-started transport whose lock is held: close() must not
    claim a clean shutdown it could not confirm, and admissions must not be
    reported closed. Once the lock is free, close() succeeds."""

    t = make_transport()
    t._lock.acquire()

    try:
        start = time.perf_counter()
        report = t.close(0.3)
        elapsed = time.perf_counter() - start
    finally:
        t._lock.release()

    MEASUREMENTS.append(("close, lock held", "close_return_s", elapsed))
    assert elapsed < 0.3 + BOUNDED_WAIT
    assert report.clean is False
    assert report.closed is False
    assert t.stats().closed is False

    after = t.close(1.0)
    assert after.clean is True
    assert after.closed is True
    assert t.stats().loops_created == 0


def test_caller_wait_is_bounded_when_finish_contends_after_admission(server):
    """The request finishes on the loop while another thread holds the
    transport lock. The caller must still return within its own bounds
    rather than wait for that lock."""

    t = make_transport(backstop_grace=0.2, cleanup_grace=0.3)
    t.start(JOIN)
    unblock = block_loop(t)
    outcome = {}

    def caller():
        start = time.perf_counter()
        try:
            post(t, server.url("/ok"), timeout=0.3)
        except TransportError as error:
            outcome["error"] = error
        outcome["elapsed"] = time.perf_counter() - start

    thread = threading.Thread(target=caller)
    thread.start()

    # Admitted (holds a slot) while the loop is blocked.
    assert wait_until(lambda: t.stats().outstanding == 1)
    admitted_at = time.perf_counter()

    # Let the deadline pass, then hold the transport lock and release the
    # loop: _begin sees the expired deadline and goes straight to _finish,
    # which now waits for the transport lock.
    t._lock.acquire()
    released = False

    try:
        while time.perf_counter() - admitted_at < 0.35:
            time.sleep(0.01)
        unblock.set()

        thread.join(5.0)
        assert not thread.is_alive(), "caller waited on the contended lock"
    finally:
        t._lock.release()
        released = True
        unblock.set()

    assert released
    MEASUREMENTS.append(("finish contended", "caller_return_s", outcome["elapsed"]))
    # deadline 0.3 + backstop 0.2 + cleanup 0.3, plus generous slack
    assert outcome["elapsed"] < 0.3 + 0.2 + 0.3 + TOLERANCE
    assert isinstance(outcome["error"], TransportCleanupUnconfirmed)
    assert outcome["error"].reason == "deadline"

    # Once the lock is free the call finishes and accounting settles.
    assert wait_until(lambda: t.stats().outstanding == 0)
    assert t.stats().unconfirmed == 0
    assert not server.received("/ok")
    assert t.close(JOIN).clean


def test_close_before_start_creates_nothing(server):
    t = make_transport()
    before = set(threading.enumerate())

    assert t.close(1.0).clean
    with pytest.raises(TransportClosed):
        post(t, server.url("/ok"))

    assert set(threading.enumerate()) - before == set()
    assert t.stats().loops_created == 0


def test_close_is_bounded_even_if_closing_the_event_loop_blocks(server):
    t = make_transport()
    assert post(t, server.url("/ok"))["ok"] is True
    runtime = t._runtime
    release = threading.Event()
    real_close = runtime.loop.close

    def slow_close():
        release.wait(JOIN)  # e.g. a proactor waiting on cancelled I/O
        real_close()

    runtime.loop.close = slow_close
    start = time.perf_counter()

    report = t.close(0.5)

    elapsed = time.perf_counter() - start
    MEASUREMENTS.append(("blocked loop.close", "close_return_s", elapsed))
    assert elapsed < 0.5 + TOLERANCE
    assert report.loop_thread_alive is True
    assert report.finalized is False
    assert report.clean is False
    # The steps ordered before loop.close() already ran.
    assert runtime.executor._shutdown is True
    for name in HTTP_LOGGERS:
        assert runtime.log_filter not in logging.getLogger(name).filters

    release.set()
    assert wait_until(lambda: t.stats().finalized)
    assert wait_until(lambda: not runtime.thread.is_alive())
    assert_resources_released(runtime)
    assert t.close(1.0).clean


def test_stuck_loop_is_reported_then_finalizes_when_released():
    t = make_transport()
    t.start(JOIN)
    runtime = t._runtime
    unblock = block_loop(t)

    report = t.close(0.3)

    assert report.clean is False
    assert report.loop_thread_alive is True
    assert report.finalized is False

    unblock.set()
    assert wait_until(lambda: t.stats().finalized)
    assert wait_until(lambda: not runtime.thread.is_alive())
    assert_resources_released(runtime)
    assert t.close(1.0).clean


# --- privacy and validation -------------------------------------------------------------


def test_http_library_logs_from_transport_thread_are_dropped(server, transport, caplog):
    with caplog.at_level(logging.DEBUG):
        post(transport, server.url(f"/ok?token={SECRET}"))

    assert server.received(f"/ok?token={SECRET}")
    assert all(SECRET not in record.getMessage() for record in caplog.records)
    assert all(
        not record.name.startswith(("httpx", "httpcore"))
        for record in caplog.records
    )


def test_tls_verification_is_enabled(transport):
    import ssl

    transport.start(JOIN)
    context = transport._runtime.ssl_context
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/x",                 # plain http off loopback
        "ftp://127.0.0.1/x",
        "https://user:pw@example.com/x",       # credentials in URL
        "not a url",
    ],
)
def test_invalid_urls_are_rejected_without_echoing(transport, url):
    with pytest.raises((TypeError, ValueError)) as caught:
        post(transport, url)

    assert "example.com" not in str(caught.value)
    assert "pw" not in str(caught.value)
    assert transport.stats().started is False


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeout": 0}, {"timeout": -1}, {"timeout": float("inf")},
        {"timeout": float("nan")}, {"timeout": True},
        {"max_bytes": 0}, {"max_bytes": 1.5}, {"max_bytes": True},
    ],
)
def test_invalid_limits_are_rejected(server, transport, kwargs):
    arguments = {"timeout": DEADLINE, "max_bytes": 1000, **kwargs}

    with pytest.raises((TypeError, ValueError)):
        post(transport, server.url("/ok"), **arguments)


def test_unserializable_payload_is_rejected_without_echoing(server, transport):
    with pytest.raises(ValueError) as caught:
        post(transport, server.url("/ok"), payload={SECRET: object()})

    assert str(caught.value) == "payload must be JSON-serializable."


@pytest.mark.parametrize(
    "name",
    [
        "max_outstanding", "dns_threads", "max_pending_dns",
        "cancel_poll_interval", "backstop_grace", "cleanup_grace",
    ],
)
def test_constructor_requires_explicit_valid_limits(name):
    with pytest.raises((TypeError, ValueError)):
        make_transport(**{name: 0})

    settings = {
        "max_outstanding": 1, "dns_threads": 1, "max_pending_dns": 1,
        "cancel_poll_interval": 0.1, "backstop_grace": 0.1, "cleanup_grace": 0.1,
    }
    del settings[name]

    with pytest.raises(TypeError):
        BoundedTransport(**settings)


def test_import_and_construction_do_no_network_or_threads():
    script = (
        "import socket, threading\n"
        "def boom(*a, **k): raise AssertionError('network used')\n"
        # Patch methods, not the class: replacing socket.socket breaks ssl.
        "socket.socket.connect = boom; socket.socket.connect_ex = boom\n"
        "socket.getaddrinfo = boom; socket.create_connection = boom\n"
        "before = threading.active_count()\n"
        "from kalillac_routing.bounded_transport import BoundedTransport\n"
        "BoundedTransport(max_outstanding=1, dns_threads=1, max_pending_dns=1,\n"
        "    cancel_poll_interval=0.1, backstop_grace=0.1, cleanup_grace=0.1)\n"
        "print(threading.active_count() - before)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(CANDIDATE_DIR),
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0"


def teardown_module(module):
    print("\nBOUNDED TRANSPORT MEASUREMENTS (this machine, this run)")
    for label, metric, value in MEASUREMENTS:
        print(f"  {label:34} {metric:22} {value:+.4f}")
