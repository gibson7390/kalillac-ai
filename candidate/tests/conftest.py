"""Shared safety for every candidate test.

1. External-network guard. Outbound network access is limited to loopback
   for the whole test process, at every layer a provider request can take:
   name resolution (socket.getaddrinfo), socket connect/connect_ex/sendto,
   asyncio's sock_connect on selector and proactor loops (the bounded
   transport's path, including on Windows), and requests' HTTPAdapter (the
   tavily-python path). A blocked attempt raises an ordinary connection or
   resolution error at the call site, so the code under test behaves as if
   the network were down, AND is recorded: the autouse fixture fails the
   test that made it, even if a broad handler swallowed the error.
   Placeholder provider keys set by tests therefore cannot reach a provider.
   Loopback (localhost, 127.0.0.0/8, ::1) stays usable for the bounded
   transport's loopback-server tests.

2. Tavily transport isolation. Each test gets a fresh Tavily transport slot
   whose factory refuses to build a real transport, so an unfaked bounded
   Tavily request fails the test, and the app's shutdown hook (run by every
   TestClient exit) never closes another test's slot.
"""

from __future__ import annotations

import ipaddress
from pathlib import Path
import socket
import sys
from urllib.parse import urlsplit

import pytest


CANDIDATE_DIR = Path(__file__).resolve().parents[1]

if str(CANDIDATE_DIR) not in sys.path:
    sys.path.insert(0, str(CANDIDATE_DIR))


# --- 1. external-network guard ------------------------------------------------------


_BLOCKED: list[tuple[str, str]] = []


class ExternalNetworkBlocked(ConnectionRefusedError):
    """A test tried to reach a non-loopback address."""


def is_loopback_host(host) -> bool:
    if host is None:
        # getaddrinfo(None, port): the local host.
        return True

    if isinstance(host, (bytes, bytearray)):
        host = bytes(host).decode("ascii", "replace")

    host = str(host).strip().strip("[]").rstrip(".").lower()

    if host == "localhost":
        return True

    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False

    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped

    return address.is_loopback


def _block(kind: str, host) -> None:
    if isinstance(host, (bytes, bytearray)):
        host = bytes(host).decode("ascii", "replace")

    _BLOCKED.append((kind, str(host)))


def _check_address(kind: str, address) -> None:
    # AF_INET/AF_INET6 addresses are tuples; AF_UNIX paths are local.
    if isinstance(address, tuple) and address and not is_loopback_host(address[0]):
        _block(kind, address[0])
        raise ExternalNetworkBlocked(f"external network access blocked in tests ({kind})")


_real_getaddrinfo = socket.getaddrinfo
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_sendto = socket.socket.sendto


def _guarded_getaddrinfo(host, *args, **kwargs):
    if not is_loopback_host(host):
        _block("getaddrinfo", host)
        raise socket.gaierror(socket.EAI_NONAME, "external name resolution blocked in tests")

    return _real_getaddrinfo(host, *args, **kwargs)


def _guarded_connect(self, address):
    _check_address("connect", address)
    return _real_connect(self, address)


def _guarded_connect_ex(self, address):
    _check_address("connect_ex", address)
    return _real_connect_ex(self, address)


def _guarded_sendto(self, data, *args):
    if args:
        _check_address("sendto", args[-1])

    return _real_sendto(self, data, *args)


def _guard_loop_class(cls) -> None:
    real = cls.sock_connect

    async def sock_connect(self, sock, address):
        _check_address("sock_connect", address)
        return await real(self, sock, address)

    cls.sock_connect = sock_connect


def _guard_requests() -> None:
    import requests
    from requests.adapters import HTTPAdapter

    real_send = HTTPAdapter.send

    def send(self, request, *args, **kwargs):
        host = urlsplit(request.url).hostname

        if not is_loopback_host(host):
            _block("requests", host)
            raise requests.exceptions.ConnectionError(
                "external network access blocked in tests (requests)"
            )

        return real_send(self, request, *args, **kwargs)

    HTTPAdapter.send = send


def _install_guard() -> None:
    socket.getaddrinfo = _guarded_getaddrinfo
    socket.socket.connect = _guarded_connect
    socket.socket.connect_ex = _guarded_connect_ex
    socket.socket.sendto = _guarded_sendto

    from asyncio import selector_events

    _guard_loop_class(selector_events.BaseSelectorEventLoop)

    try:
        from asyncio import proactor_events
    except ImportError:
        proactor_events = None

    if proactor_events is not None:
        _guard_loop_class(proactor_events.BaseProactorEventLoop)

    _guard_requests()


_install_guard()


class NetworkAttempts:
    """The external attempts the current test made (already blocked)."""

    def __init__(self, start: int) -> None:
        self._start = start

    def take(self) -> list[tuple[str, str]]:
        """Return and clear this test's blocked attempts, for tests that
        deliberately exercise the guard."""

        taken = _BLOCKED[self._start:]
        del _BLOCKED[self._start:]
        return taken


@pytest.fixture(autouse=True)
def external_network_guard():
    attempts = NetworkAttempts(len(_BLOCKED))
    yield attempts
    leaked = attempts.take()

    if leaked:
        pytest.fail(
            "external network access was attempted (and blocked): "
            + ", ".join(f"{kind}:{host}" for kind, host in leaked),
            pytrace=False,
        )


# --- 2. Tavily transport isolation ----------------------------------------------------


class UnexpectedTavilyTransport(BaseException):
    """A test reached a real bounded Tavily transport without installing a
    fake. BaseException, so no broad handler can hide it."""


def _refuse_tavily_transport(**settings):
    raise UnexpectedTavilyTransport()


@pytest.fixture(autouse=True)
def isolated_tavily_transport(monkeypatch):
    from kalillac_routing import tavily_transport

    monkeypatch.setattr(
        tavily_transport,
        "_SLOT",
        tavily_transport.TavilyTransportSlot(factory=_refuse_tavily_transport),
    )
