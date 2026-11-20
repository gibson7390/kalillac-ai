"""The development preview's network fence: no connection from the preview's tooling may reach the protected local
staging installation (ports 8010, 8011, 55433) or anything but this machine's preview.

It is enforced where every outgoing TCP connection is made (socket.create_connection, which http.client and
urllib use, including for each redirect they follow, and socket.socket.connect beneath it), not only at
urlopen. A connection is refused before it is attempted when:
  * its port is one of local staging's, whatever the host is called (localhost, 127.0.0.1, 127.0.0.2,
    0.0.0.0, ::1, a decimal or hexadecimal address, a trailing dot...); or
  * its host is not this machine's loopback by name or literal address (nothing is resolved first).

Pure standard library, so it can be tested anywhere with mocks and no installation.
"""
from __future__ import annotations

import ipaddress
import socket

PROTECTED_PORTS = frozenset({8010, 8011, 55433})
_LOOPBACK_NAMES = frozenset({"localhost", "localhost."})


class PreviewFenceError(ConnectionRefusedError):
    """A connection the development preview may never make."""


def _loopback(host) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    host = str(host).strip().strip("[]").lower()
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def check(host, port) -> None:
    """Raise PreviewFenceError unless (host, port) is a connection the preview may make."""
    try:
        number = int(port)
    except (TypeError, ValueError):
        raise PreviewFenceError(f"the development preview refused a connection to an unknown port: {port!r}") from None
    if number in PROTECTED_PORTS:
        raise PreviewFenceError(f"the development preview refused a connection to local staging's port {number}")
    if not _loopback(host):
        raise PreviewFenceError(f"the development preview refused a connection outside this machine: {host!r}")


def install() -> None:
    """Fence this process. Idempotent."""
    if getattr(socket.create_connection, "_kalillac_fenced", False):
        return
    real_create = socket.create_connection
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def create_connection(address, *args, **kwargs):
        check(address[0], address[1])
        return real_create(address, *args, **kwargs)

    def _inet(sock, address):
        return sock.family in (socket.AF_INET, socket.AF_INET6) and isinstance(address, tuple)

    def connect(sock, address):
        if _inet(sock, address):
            check(address[0], address[1])
        return real_connect(sock, address)

    def connect_ex(sock, address):
        if _inet(sock, address):
            check(address[0], address[1])
        return real_connect_ex(sock, address)

    create_connection._kalillac_fenced = True
    socket.create_connection = create_connection
    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
