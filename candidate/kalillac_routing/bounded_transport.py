"""Bounded JSON-over-HTTP POST behind a synchronous interface.

Nothing calls this module yet; it is the transport layer future provider
adapters will share. It reads no environment settings and has no
production defaults: every limit is supplied by the caller. Importing it,
or constructing a BoundedTransport, performs no network I/O and starts no
threads. Requires Python 3.11+ (asyncio.timeout).

How a call works:
- post_json() runs in the caller's (worker) thread and starts its deadline
  on entry. Every wait it performs is bounded by that deadline: the
  transport lock is acquired with a timeout, and startup is never run in
  the caller's thread.
- Startup (SSL context, DNS pool, event loop, loop thread) runs once, in a
  single background starter thread. Concurrent first callers share that
  one attempt and wait for it no longer than their own deadlines. If the
  attempt fails, waiting callers get TransportError and a later call may
  start a new attempt; at most one attempt runs at a time.
- On the loop thread, one total deadline (asyncio.timeout) covers
  connection, TLS, sending, response headers and the complete body. A
  server that trickles bytes cannot extend it.
- The caller polls for completion, cancellation and a backstop (deadline
  + backstop_grace). On cancellation or backstop it asks the loop to
  cancel the request and waits up to cleanup_grace for the request's task
  to finish. If it does not, TransportCleanupUnconfirmed is raised and the
  request stays counted as outstanding (and unconfirmed) until it really
  finishes, so it keeps occupying capacity.
- Task completion and resource cleanup are tracked separately. A request
  is finished when its task ends, whatever the outcome. Closing its HTTP
  client is confirmed separately: if aclose() fails or is interrupted,
  the connection may still be open, so the client is counted as
  unresolved (unresolved_clients), the request's result is withheld
  (TransportCleanupUnconfirmed, reason "connection"), and the transport
  is quarantined: every later admission is refused with
  TransportQuarantined. Quarantine is final for this instance; nothing
  retries or replaces the transport automatically. Requests already
  admitted run to completion.

Capacity:
- max_outstanding caps admitted, unfinished requests (including
  unconfirmed ones).
- DNS lookups run in a dedicated pool. Capacity is reserved atomically
  when a lookup is actually submitted: a submission beyond max_pending_dns
  queued-plus-running lookups is refused (the request fails with
  TransportOverloaded), and the reservation is released when the lookup
  finishes, is cancelled before running, or fails to submit. Admission
  also refuses early while the pool is full, but that check is only a fast
  path; the submission check is authoritative.

Requests and responses:
- POST only, JSON body, TLS verification on (system CA bundle via httpx,
  hostname checking), no redirects, no retries, no proxy or certificate
  settings from the environment (trust_env=False).
- http:// is accepted only for loopback hosts. URLs may not carry
  credentials.
- Any 2xx response is parsed as JSON; any other status raises
  TransportHTTPError with only the numeric status. Error bodies are not
  read.
- max_bytes bounds BOTH the bytes received on the wire and the decoded
  body. "Accept-Encoding: identity" is always requested. gzip and deflate
  are still decoded if a server sends them anyway, using zlib with an
  output limit so decompression never produces more than max_bytes + 1
  bytes; any other content encoding is refused. Peak memory per request is
  roughly max_bytes for the body, plus one network chunk and zlib's
  window, plus the parsed JSON objects (which can be several times larger
  than the bytes they came from).

Errors are typed with fixed, content-free messages: no URL, header,
payload, response body or provider error text appears in them, they carry
no chained exceptions, and this module logs nothing. httpx and httpcore
records emitted on this transport's loop thread (which would include
request URLs) are dropped while the transport runs.

Shutdown:
- close(timeout) stops admissions, cancels outstanding requests and waits
  at most `timeout`. The loop is stopped only once nothing is outstanding,
  so pending cleanup is never abandoned. If close() times out it returns
  an unclean report; the last outstanding request to finish then stops the
  loop, and the loop thread finalizes (DNS pool shutdown, logging filters
  removed, loop closed). Later close() calls report the current state.
- Finalization runs on the (daemon) loop thread because closing a loop can
  wait without limit for cancelled I/O (Windows' proactor loop does).
- A report is clean only when admissions are closed, startup is not in
  progress, nothing is outstanding or unconfirmed, the loop thread has
  stopped, finalization is confirmed, no DNS lookup is still running, and
  no client is unresolved. A report taken without the lock (because close
  could not get it in time) is never clean.

Honest limits:
- Cancelling closes the local connection only. A provider may keep
  generating, and may still bill, after the connection closes.
- A running DNS lookup cannot be interrupted; it ends when the operating
  system resolver gives up. The DNS pool's threads are not daemon
  threads, so interpreter exit can wait for a running lookup.
- A loop thread blocked by something outside this module never finalizes;
  it is reported, not hidden.
- There is no connection reuse: each request opens its own connection.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from dataclasses import dataclass
import itertools
import json
import logging
import math
import threading
import time
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit
import zlib

import httpx


# --- errors ------------------------------------------------------------------------


class TransportError(Exception):
    """Base class. Messages are fixed and never include request data."""

    MESSAGE = "Transport request failed."

    def __init__(self) -> None:
        super().__init__(self.MESSAGE)


class TransportClosed(TransportError):
    MESSAGE = "Transport is closed."


class TransportOverloaded(TransportError):
    """Too many outstanding requests or pending DNS lookups."""

    MESSAGE = "Transport is at capacity."


class TransportDeadlineExceeded(TransportError):
    MESSAGE = "Transport deadline exceeded."


class TransportCancelled(TransportError):
    MESSAGE = "Transport request cancelled."


class TransportQuarantined(TransportError):
    """An earlier request's connection could not be confirmed closed, so
    this transport admits no more work."""

    MESSAGE = "Transport is quarantined after unconfirmed cleanup."


class TransportCleanupUnconfirmed(TransportError):
    """Cleanup of this request could not be confirmed.

    reason "deadline" or "cancelled": the caller stopped waiting before the
    request finished; it stays counted as outstanding until it does.
    reason "connection": the request finished but its connection could not
    be confirmed closed; its result is withheld and the transport is
    quarantined."""

    MESSAGE = "Transport cleanup was not confirmed."

    def __init__(self, reason: str) -> None:
        super().__init__()
        # A fixed label; never request content.
        self.reason = reason


class TransportConnectionError(TransportError):
    MESSAGE = "Transport connection failed."


class TransportHTTPError(TransportError):
    MESSAGE = "Server returned an HTTP error status."

    def __init__(self, status: int) -> None:
        super().__init__()
        self.status = status


class ResponseTooLarge(TransportError):
    MESSAGE = "Response exceeded the size limit."


class UnsupportedContentEncoding(TransportError):
    MESSAGE = "Response used an unsupported content encoding."


class InvalidJSONResponse(TransportError):
    MESSAGE = "Response body was not valid JSON."


# --- reports ------------------------------------------------------------------------


@dataclass(frozen=True)
class TransportStats:
    started: bool
    starting: bool
    closed: bool
    quarantined: bool
    loops_created: int
    outstanding: int
    unconfirmed: int
    open_clients: int
    unresolved_clients: int
    dns_pending: int
    dns_high_water: int
    loop_thread_alive: bool
    finalized: bool


@dataclass(frozen=True)
class ShutdownReport:
    clean: bool
    closed: bool
    starting: bool
    quarantined: bool
    outstanding: int
    unconfirmed: int
    loop_thread_alive: bool
    finalized: bool
    dns_pending: int
    unresolved_clients: int


# --- validation ------------------------------------------------------------------------


_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
_SUPPORTED_ENCODINGS = {"gzip", "x-gzip", "deflate"}


def _positive_seconds(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number of seconds.")

    seconds = float(value)

    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"{name} must be a positive finite number.")

    return seconds


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer.")

    if value <= 0:
        raise ValueError(f"{name} must be a positive integer.")

    return value


def _check_url(url: object) -> str:
    if not isinstance(url, str):
        raise TypeError("url must be a string.")

    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        raise ValueError("url is not valid.") from None

    if not host:
        raise ValueError("url is not valid.")

    if parts.username is not None or parts.password is not None:
        raise ValueError("url must not contain credentials.")

    if parts.scheme == "https" or (
        parts.scheme == "http" and host in _LOOPBACK_HOSTS
    ):
        return url

    raise ValueError("url must use https (http only for loopback).")


def _request_headers(headers: object) -> httpx.Headers:
    if not isinstance(headers, Mapping):
        raise TypeError("headers must be a mapping of strings.")

    if not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in headers.items()
    ):
        raise TypeError("headers must be a mapping of strings.")

    try:
        merged = httpx.Headers(dict(headers))
    except Exception:
        raise ValueError("headers are not valid.") from None

    merged["content-type"] = "application/json"
    # Compressed responses are refused or decoded under a size limit; ask
    # for none.
    merged["accept-encoding"] = "identity"
    return merged


def _encode_payload(payload: object) -> bytes:
    try:
        return json.dumps(payload, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        raise ValueError("payload must be JSON-serializable.") from None


# --- internals ------------------------------------------------------------------------


class _DNSCapacityExceeded(RuntimeError):
    """Internal: a lookup submission was refused at capacity."""


class _CountingExecutor(concurrent.futures.ThreadPoolExecutor):
    """The loop's default executor (used for DNS). Reserves capacity
    atomically at submission and releases it when the job finishes, is
    cancelled before running, or fails to submit."""

    def __init__(self, max_workers: int, max_pending: int, name: str) -> None:
        super().__init__(max_workers=max_workers, thread_name_prefix=name)
        self._max_pending = max_pending
        self._pending = 0
        self._high_water = 0
        self._pending_lock = threading.Lock()

    @property
    def pending(self) -> int:
        with self._pending_lock:
            return self._pending

    @property
    def high_water(self) -> int:
        with self._pending_lock:
            return self._high_water

    def submit(self, fn, /, *args, **kwargs):
        with self._pending_lock:
            if self._pending >= self._max_pending:
                raise _DNSCapacityExceeded()

            self._pending += 1
            self._high_water = max(self._high_water, self._pending)

        try:
            future = super().submit(fn, *args, **kwargs)
        except BaseException:
            self._release()
            raise

        # Runs on completion, on failure, and on cancellation before start.
        future.add_done_callback(lambda _future: self._release())
        return future

    def _release(self) -> None:
        with self._pending_lock:
            self._pending -= 1


class _DropThreadRecords(logging.Filter):
    """Drop httpx/httpcore records emitted on one thread (they contain
    request URLs)."""

    def __init__(self, thread_name: str) -> None:
        super().__init__()
        self._thread_name = thread_name

    def filter(self, record: logging.LogRecord) -> bool:
        return record.threadName != self._thread_name


_HTTP_LOGGERS = (
    "httpx",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "httpcore.socks",
)

_instance_ids = itertools.count(1)


@dataclass
class _Runtime:
    ssl_context: Any
    executor: _CountingExecutor
    loop: asyncio.AbstractEventLoop
    log_filter: _DropThreadRecords
    thread: threading.Thread | None = None


class _StartAttempt:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.ok = False


class _Call:
    """One request. Fields marked (loop) are touched only on the loop."""

    def __init__(self, url, body, headers, deadline, max_bytes) -> None:
        self.url = url
        self.body = body
        self.headers = headers
        self.deadline = deadline
        self.max_bytes = max_bytes
        self.lock = threading.Lock()     # guards done/unconfirmed/failure
        self.done = threading.Event()
        self.task = None                 # (loop)
        self.cancel_requested = False    # (loop)
        self.value = None
        self.failure = None              # (exception class, args) or None
        self.unconfirmed = False

    def outcome(self) -> Any:
        if self.failure is None:
            return self.value

        error_class, args = self.failure
        # A fresh instance in the caller's thread: no loop-thread traceback
        # or chained provider exception travels with it.
        raise error_class(*args)


_GENERIC_FAILURE = (TransportError, ())


def _decoder_for(encoding: str | None):
    value = (encoding or "").strip().lower()

    if value in ("", "identity"):
        return None

    if value not in _SUPPORTED_ENCODINGS:
        raise UnsupportedContentEncoding()

    if value == "deflate":
        return zlib.decompressobj()

    return zlib.decompressobj(16 + zlib.MAX_WBITS)


def _append_decoded(decoder, data: bytes, body: bytearray, max_bytes: int) -> None:
    # Never ask zlib for more than one byte past the limit, so a small
    # compressed chunk cannot expand without bound before the check.
    while data:
        body += decoder.decompress(data, max_bytes - len(body) + 1)

        if len(body) > max_bytes:
            raise ResponseTooLarge()

        data = decoder.unconsumed_tail


def _parse_json(body: bytes) -> Any:
    try:
        return json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise InvalidJSONResponse() from None


def _dns_refused(error: BaseException) -> bool:
    """Whether a refused DNS reservation caused this error, however the
    HTTP libraries wrapped it."""

    seen = set()
    pending = [error]

    while pending:
        current = pending.pop()

        if current is None or id(current) in seen:
            continue

        seen.add(id(current))

        if isinstance(current, _DNSCapacityExceeded):
            return True

        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)

        pending.append(current.__cause__)
        pending.append(current.__context__)

    return False


# --- transport ------------------------------------------------------------------------


class BoundedTransport:
    def __init__(
        self,
        *,
        max_outstanding: int,
        dns_threads: int,
        max_pending_dns: int,
        cancel_poll_interval: float,
        backstop_grace: float,
        cleanup_grace: float,
    ) -> None:
        self._max_outstanding = _positive_int(max_outstanding, "max_outstanding")
        self._dns_threads = _positive_int(dns_threads, "dns_threads")
        self._max_pending_dns = _positive_int(max_pending_dns, "max_pending_dns")
        self._poll = _positive_seconds(cancel_poll_interval, "cancel_poll_interval")
        self._backstop_grace = _positive_seconds(backstop_grace, "backstop_grace")
        self._cleanup_grace = _positive_seconds(cleanup_grace, "cleanup_grace")

        # Every critical section under _lock is short and does no I/O.
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        self._name = f"kalillac-transport-{next(_instance_ids)}"
        self._state = "idle"             # idle | starting | started
        self._attempt: _StartAttempt | None = None
        self._runtime: _Runtime | None = None
        self._closed = False
        self._quarantined = False
        self._stop_requested = False
        self._finalized = True           # nothing to finalize yet
        self._loops_created = 0
        self._outstanding = 0
        self._open_clients = 0
        self._unresolved_clients = 0
        self._live: set[_Call] = set()

    # --- startup -----------------------------------------------------------------

    def start(self, timeout: float) -> None:
        """Start now, waiting at most `timeout` seconds (otherwise startup
        happens on first use)."""

        seconds = _positive_seconds(timeout, "timeout")
        self._wait_until_started(time.monotonic() + seconds, None)

    def _acquire(self, deadline: float) -> None:
        if not self._lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise TransportDeadlineExceeded()

    def _wait_until_started(self, deadline: float, cancelled) -> None:
        while True:
            self._acquire(deadline)

            try:
                if self._closed:
                    raise TransportClosed()

                if self._state == "started":
                    return

                if self._state == "idle":
                    self._state = "starting"
                    self._attempt = _StartAttempt()
                    threading.Thread(
                        target=self._start_in_background,
                        args=(self._attempt,),
                        name=f"{self._name}-start",
                        daemon=True,
                    ).start()

                attempt = self._attempt
            finally:
                self._lock.release()

            while not attempt.event.is_set():
                if cancelled is not None and cancelled():
                    raise TransportCancelled()

                left = deadline - time.monotonic()

                if left <= 0:
                    raise TransportDeadlineExceeded()

                attempt.event.wait(min(self._poll, left))

            # A discarded attempt because close() won the race loops back
            # and raises TransportClosed; any other failure is generic.
            if not attempt.ok and not self._closed:
                raise TransportError()

    def _initialize(self) -> _Runtime:
        """Build everything the loop needs. Runs only in the starter."""

        ssl_context = httpx.create_ssl_context(verify=True, trust_env=False)
        executor = _CountingExecutor(
            self._dns_threads,
            self._max_pending_dns,
            f"{self._name}-dns",
        )

        try:
            loop = asyncio.new_event_loop()
        except BaseException:
            executor.shutdown(wait=False)
            raise

        loop.set_default_executor(executor)
        # Never let asyncio log task or callback details.
        loop.set_exception_handler(lambda _loop, _context: None)

        return _Runtime(
            ssl_context=ssl_context,
            executor=executor,
            loop=loop,
            log_filter=_DropThreadRecords(f"{self._name}-loop"),
        )

    def _start_in_background(self, attempt: _StartAttempt) -> None:
        try:
            runtime = self._initialize()
        except Exception:
            runtime = None

        while True:
            with self._lock:
                discard = self._closing_wins(runtime)

            if discard:
                # Released before the attempt is reported finished, so
                # nothing observes "not starting" while these are still
                # open. The loop never ran, so closing it cannot wait on I/O.
                try:
                    runtime.executor.shutdown(wait=False, cancel_futures=True)
                finally:
                    runtime.loop.close()

            with self._lock:
                if runtime is not None and not discard and self._closed:
                    # close() arrived since the check above; _closed never
                    # reverts, so the next pass discards.
                    continue

                self._publish_locked(runtime if not discard else None, attempt)
                return

    def _closing_wins(self, runtime: _Runtime | None) -> bool:
        """close() won the race: never publish or run this runtime.
        Lock held."""

        return runtime is not None and self._closed

    def _publish_locked(self, runtime: _Runtime | None, attempt: _StartAttempt) -> None:
        """Finish a start attempt. Lock held."""

        try:
            if runtime is None:
                self._state = "idle"
                return

            thread = threading.Thread(
                target=self._run_loop,
                args=(runtime,),
                name=f"{self._name}-loop",
                daemon=True,
            )

            try:
                thread.start()
            except BaseException:
                # No loop thread: release everything and report failure.
                runtime.executor.shutdown(wait=False, cancel_futures=True)
                runtime.loop.close()
                self._state = "idle"
                return

            for name in _HTTP_LOGGERS:
                logging.getLogger(name).addFilter(runtime.log_filter)

            runtime.thread = thread
            self._runtime = runtime
            self._state = "started"
            self._finalized = False
            self._loops_created += 1
            attempt.ok = True
        finally:
            attempt.event.set()
            self._idle.notify_all()

    def _run_loop(self, runtime: _Runtime) -> None:
        asyncio.set_event_loop(runtime.loop)

        try:
            runtime.loop.run_forever()
        finally:
            self._finalize(runtime)

    def _finalize(self, runtime: _Runtime) -> None:
        """On the loop thread, after the loop stopped with nothing
        outstanding. Ordered so the cheap steps happen even if closing the
        loop blocks."""

        try:
            # Drops queued lookups; a running lookup cannot be stopped.
            runtime.executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

        for name in _HTTP_LOGGERS:
            logging.getLogger(name).removeFilter(runtime.log_filter)

        try:
            # Can wait without a time limit for cancelled I/O on Windows;
            # that is why this runs here and not in close()'s caller.
            runtime.loop.close()
        except Exception:
            pass

        with self._idle:
            self._finalized = True
            self._idle.notify_all()

    # --- shutdown ----------------------------------------------------------------

    def close(self, timeout: float) -> ShutdownReport:
        """Stop admissions, cancel outstanding requests, and stop the loop
        once nothing is outstanding, waiting at most about `timeout`
        seconds. Idempotent; later calls report the current state."""

        seconds = _positive_seconds(timeout, "timeout")
        give_up_at = time.monotonic() + seconds

        def left() -> float:
            return max(0.0, give_up_at - time.monotonic())

        if not self._lock.acquire(timeout=left()):
            return self._report(locked=False)

        try:
            self._closed = True
            attempt = self._attempt if self._state == "starting" else None
            live = list(self._live)
        finally:
            self._lock.release()

        if attempt is not None:
            attempt.event.wait(left())

        for call in live:
            self._request_cancel(call)

        if self._idle.acquire(timeout=left()):
            try:
                self._idle.wait_for(lambda: self._outstanding == 0, left())
                stop = self._take_stop_locked()
                runtime = self._runtime
            finally:
                self._idle.release()

            if stop:
                try:
                    runtime.loop.call_soon_threadsafe(runtime.loop.stop)
                except RuntimeError:
                    pass

        runtime = self._runtime

        if runtime is not None and runtime.thread is not None:
            runtime.thread.join(left())

        return self._report(locked=self._lock.acquire(timeout=left()), release=True)

    def _take_stop_locked(self) -> bool:
        """Claim the single loop stop once closed and idle. Lock held."""

        if (
            self._closed
            and self._outstanding == 0
            and self._runtime is not None
            and not self._stop_requested
        ):
            self._stop_requested = True
            return True

        return False

    # --- reporting ---------------------------------------------------------------

    def _snapshot(self) -> TransportStats:
        runtime = self._runtime
        return TransportStats(
            started=self._state == "started",
            starting=self._state == "starting",
            closed=self._closed,
            quarantined=self._quarantined,
            loops_created=self._loops_created,
            outstanding=self._outstanding,
            unconfirmed=sum(1 for call in list(self._live) if call.unconfirmed),
            open_clients=self._open_clients,
            unresolved_clients=self._unresolved_clients,
            dns_pending=runtime.executor.pending if runtime else 0,
            dns_high_water=runtime.executor.high_water if runtime else 0,
            loop_thread_alive=bool(
                runtime and runtime.thread and runtime.thread.is_alive()
            ),
            finalized=self._finalized,
        )

    def stats(self) -> TransportStats:
        with self._lock:
            return self._snapshot()

    def _report(self, locked: bool, release: bool = False) -> ShutdownReport:
        # When the lock could not be had in time, read without it: each
        # field is read atomically. Such a report is never clean, because
        # nothing it reads is confirmed.
        try:
            stats = self._snapshot()
        finally:
            if locked and release:
                self._lock.release()

        return ShutdownReport(
            clean=(
                locked
                and stats.closed
                and not stats.starting
                and stats.outstanding == 0
                and stats.unconfirmed == 0
                and not stats.loop_thread_alive
                and stats.finalized
                and stats.dns_pending == 0
                and stats.unresolved_clients == 0
            ),
            closed=stats.closed,
            starting=stats.starting,
            quarantined=stats.quarantined,
            outstanding=stats.outstanding,
            unconfirmed=stats.unconfirmed,
            loop_thread_alive=stats.loop_thread_alive,
            finalized=stats.finalized,
            dns_pending=stats.dns_pending,
            unresolved_clients=stats.unresolved_clients,
        )

    # --- request -----------------------------------------------------------------

    def post_json(
        self,
        url: str,
        payload: Any,
        *,
        headers: Mapping[str, str],
        timeout: float,
        max_bytes: int,
        cancelled: Callable[[], bool] | None = None,
    ) -> Any:
        """POST payload as JSON and return the parsed JSON response.

        timeout is the total time for this call, measured from entry, and
        bounds every wait including startup and lock contention.
        cancelled, if given, is polled; once it returns True the request is
        cancelled. Raises only TransportError subclasses for transport
        outcomes, and TypeError/ValueError for invalid arguments.
        """

        entered = time.monotonic()
        seconds = _positive_seconds(timeout, "timeout")
        limit = _positive_int(max_bytes, "max_bytes")
        target = _check_url(url)
        request_headers = _request_headers(headers)
        body = _encode_payload(payload)
        deadline = entered + seconds

        if cancelled is not None and cancelled():
            raise TransportCancelled()

        self._wait_until_started(deadline, cancelled)
        call = _Call(target, body, request_headers, deadline, limit)
        self._acquire(deadline)

        try:
            if self._closed:
                raise TransportClosed()

            if self._quarantined:
                raise TransportQuarantined()

            runtime = self._runtime

            if (
                self._outstanding >= self._max_outstanding
                # Fast path only; the reservation at submission decides.
                or runtime.executor.pending >= self._max_pending_dns
            ):
                raise TransportOverloaded()

            self._outstanding += 1
            self._live.add(call)
            # Scheduled under the lock: the loop is stopped only after
            # close() has seen this call finish.
            runtime.loop.call_soon_threadsafe(self._begin, call)
        finally:
            self._lock.release()

        return self._await(call, cancelled)

    def _await(self, call: _Call, cancelled) -> Any:
        backstop = call.deadline + self._backstop_grace
        reason = None

        while not call.done.is_set():
            if cancelled is not None and cancelled():
                reason = "cancelled"
                break

            now = time.monotonic()

            if now >= backstop:
                reason = "deadline"
                break

            call.done.wait(min(self._poll, backstop - now))

        if reason is not None and not call.done.is_set():
            self._request_cancel(call)

            if not call.done.wait(self._cleanup_grace):
                # call.lock is only ever held for a constant-time step
                # (_finish takes the transport lock before it), but the wait
                # is bounded anyway. If it cannot be had, the call is
                # reported unconfirmed: the safe answer.
                if not call.lock.acquire(timeout=self._cleanup_grace):
                    call.unconfirmed = True
                    raise TransportCleanupUnconfirmed(reason)

                try:
                    if not call.done.is_set():
                        call.unconfirmed = True
                        raise TransportCleanupUnconfirmed(reason)
                finally:
                    call.lock.release()

            if reason == "cancelled":
                raise TransportCancelled()

            raise TransportDeadlineExceeded()

        return call.outcome()

    def _request_cancel(self, call: _Call) -> None:
        runtime = self._runtime

        if runtime is None:
            return

        try:
            runtime.loop.call_soon_threadsafe(self._cancel_on_loop, call)
        except RuntimeError:
            pass  # loop already closed; the call has finished

    # --- loop-thread side ----------------------------------------------------------

    def _begin(self, call: _Call) -> None:
        try:
            if call.cancel_requested:
                self._finish(call, (TransportCancelled, ()))
                return

            if time.monotonic() >= call.deadline:
                self._finish(call, (TransportDeadlineExceeded, ()))
                return

            call.task = self._runtime.loop.create_task(self._execute(call))
            call.task.add_done_callback(
                lambda task, call=call: self._task_done(call, task)
            )
        except Exception:
            self._finish(call, _GENERIC_FAILURE)

    def _cancel_on_loop(self, call: _Call) -> None:
        # Cancel at most once, so cleanup in _execute's finally runs
        # without a second interruption.
        if call.cancel_requested:
            return

        call.cancel_requested = True

        if call.task is not None:
            call.task.cancel()

    def _task_done(self, call: _Call, task: asyncio.Task) -> None:
        failure = _GENERIC_FAILURE
        value = None

        try:
            if task.cancelled():
                failure = (TransportCancelled, ())
            elif task.exception() is None:
                kind, payload = task.result()

                if kind == "ok":
                    failure, value = None, payload
                elif kind == "error":
                    failure = payload
        except Exception:
            failure, value = _GENERIC_FAILURE, None
        finally:
            call.value = value
            self._finish(call, failure)

    def _finish(self, call: _Call, failure) -> None:
        """Exactly once per call, on the loop thread."""

        # Lock order: transport lock, then call.lock. call.lock is never held
        # while waiting for anything else, so a caller taking it in _await
        # waits only for this constant-time step.
        with self._idle:
            with call.lock:
                if call.done.is_set():
                    return

                call.failure = failure
                call.done.set()

            self._outstanding -= 1
            self._live.discard(call)
            stop = self._take_stop_locked()
            self._idle.notify_all()

        if stop:
            self._runtime.loop.stop()

    def _client_opened(self) -> None:
        with self._lock:
            self._open_clients += 1

    def _client_finished(self, confirmed_closed: bool) -> None:
        with self._lock:
            self._open_clients -= 1

            if not confirmed_closed:
                # The connection may still be open. Stop admitting work
                # rather than let uncertain resources accumulate.
                self._unresolved_clients += 1
                self._quarantined = True

    async def _execute(self, call: _Call):
        """Returns ("ok", value) or ("error", (class, args)). Raises only
        CancelledError."""

        client = None
        outcome = ("error", _GENERIC_FAILURE)

        try:
            remaining = call.deadline - time.monotonic()

            if remaining <= 0:
                return ("error", (TransportDeadlineExceeded, ()))

            try:
                client = httpx.AsyncClient(
                    transport=httpx.AsyncHTTPTransport(
                        verify=self._runtime.ssl_context,
                        trust_env=False,
                        retries=0,
                    ),
                    timeout=httpx.Timeout(remaining),
                    follow_redirects=False,
                    trust_env=False,
                )
            except Exception:
                return ("error", _GENERIC_FAILURE)

            self._client_opened()

            try:
                async with asyncio.timeout(remaining):
                    outcome = ("ok", await self._exchange(client, call))
            except TimeoutError:
                outcome = ("error", (TransportDeadlineExceeded, ()))
            except httpx.TimeoutException:
                outcome = ("error", (TransportDeadlineExceeded, ()))
            except TransportHTTPError as error:
                outcome = ("error", (TransportHTTPError, (error.status,)))
            except TransportError as error:
                outcome = ("error", (type(error), ()))
            except Exception as error:
                if _dns_refused(error):
                    outcome = ("error", (TransportOverloaded, ()))
                elif isinstance(error, httpx.HTTPError):
                    outcome = ("error", (TransportConnectionError, ()))
                else:
                    outcome = ("error", _GENERIC_FAILURE)
        finally:
            if client is not None and not await self._close_client(client):
                # Withhold even a complete result: its connection is not
                # confirmed closed. (A CancelledError still propagates.)
                outcome = ("error", (TransportCleanupUnconfirmed, ("connection",)))

        return outcome

    async def _close_client(self, client: httpx.AsyncClient) -> bool:
        """Close the client; returns whether the close was confirmed."""

        closed = False

        try:
            await client.aclose()
            closed = True
        except Exception:
            pass
        finally:
            # Unconfirmed if aclose raised or was interrupted.
            self._client_finished(confirmed_closed=closed)

        return closed

    @staticmethod
    async def _exchange(client: httpx.AsyncClient, call: _Call) -> Any:
        async with client.stream(
            "POST",
            call.url,
            content=call.body,
            headers=call.headers,
        ) as response:
            if not 200 <= response.status_code < 300:
                raise TransportHTTPError(response.status_code)

            declared = response.headers.get("content-length", "").strip()

            if declared.isdigit() and int(declared) > call.max_bytes:
                raise ResponseTooLarge()

            decoder = _decoder_for(response.headers.get("content-encoding"))
            body = bytearray()
            received = 0

            # Raw bytes as they came off the wire; this module decodes.
            async for chunk in response.aiter_raw():
                received += len(chunk)

                if received > call.max_bytes:
                    raise ResponseTooLarge()

                if decoder is None:
                    body += chunk

                    if len(body) > call.max_bytes:
                        raise ResponseTooLarge()
                else:
                    try:
                        _append_decoded(decoder, chunk, body, call.max_bytes)
                    except zlib.error:
                        raise InvalidJSONResponse() from None

            if decoder is not None:
                try:
                    body += decoder.flush()
                except zlib.error:
                    raise InvalidJSONResponse() from None

                if len(body) > call.max_bytes:
                    raise ResponseTooLarge()

        return _parse_json(bytes(body))
