"""The development preview's network fence: no connection from any of the preview's Python processes may reach
the protected local staging installation (ports 8010, 8011, 55433), whatever the host is called and however the
connection is made.

Every Python process of the preview installs it: the manager (tools/dev_preview.py) and, through the fenced
launcher (tools/preview_fenced.py), the application (8021) and the panel (8022). Vite, a Node process, has its own
equivalent (site/preview-fence.mjs).

It is enforced where connections are made, never only at urlopen:
  * socket.create_connection, socket.socket.connect and connect_ex (urllib, http.client, httpx, requests,
    asyncio and every redirect any of them follows), checked before the connection is attempted;
  * libpq, which does not use Python sockets: psycopg's Connection.connect (and the async one) checks every
    host and port the connection string, its keywords and the PG* environment would use, before libpq runs.

A protected port is refused whatever the host is called (localhost, 127.0.0.2, 0.0.0.0, ::1, a LAN address, a
decimal address, a Unix socket of that port). In strict mode (the manager, which only ever talks to the preview),
anything that is not this machine's loopback is refused too; the application and panel keep their ordinary
outbound access (a real provider key, when the owner supplies one), minus the protected ports.

KALILLAC_PREVIEW_FENCE_EXTRA_PORTS (comma-separated) adds ports to the protected set; it can only add. The tests
use it for stand-in services on ephemeral ports, so isolation is proven without touching local staging.

Pure standard library (psycopg is fenced only when it is installed).
"""
from __future__ import annotations

import ipaddress
import os
import socket

PROTECTED_PORTS = frozenset({8010, 8011, 55433})
EXTRA_PORTS = "KALILLAC_PREVIEW_FENCE_EXTRA_PORTS"
_LOOPBACK_NAMES = frozenset({"localhost", "localhost."})
_STATE = {"strict": None}


class PreviewFenceError(ConnectionRefusedError):
    """A connection the development preview may never make."""


def protected_ports(environ=None) -> frozenset:
    environ = os.environ if environ is None else environ
    extra = set()
    for item in (environ.get(EXTRA_PORTS) or "").split(","):
        item = item.strip()
        if item.isdigit():
            extra.add(int(item))
    return PROTECTED_PORTS | extra


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


def check(host, port, *, strict=True) -> None:
    """Raise PreviewFenceError unless (host, port) is a connection the preview may make."""
    try:
        number = int(port)
    except (TypeError, ValueError):
        raise PreviewFenceError(f"the development preview refused a connection to an unknown port: {port!r}") from None
    if number in protected_ports():
        raise PreviewFenceError(f"the development preview refused a connection to local staging's port {number}")
    if strict and not _loopback(host):
        raise PreviewFenceError(f"the development preview refused a connection outside this machine: {host!r}")


def libpq_targets(conninfo="", environ=None, **kwargs):
    """Every (host, port) libpq would try for this connection: the connection string and keywords, then the
    PG* environment, then libpq's defaults. Several comma-separated hosts or ports are all returned."""
    environ = os.environ if environ is None else environ
    from psycopg.conninfo import conninfo_to_dict
    params = conninfo_to_dict(conninfo, **{k: v for k, v in kwargs.items() if v is not None})
    hosts = str(params.get("host") or params.get("hostaddr") or environ.get("PGHOST")
                or environ.get("PGHOSTADDR") or "/var/run/postgresql").split(",")
    ports = str(params.get("port") or environ.get("PGPORT") or "5432").split(",")
    if len(ports) == 1:
        ports = ports * len(hosts)
    return [(h.strip(), p.strip() or "5432") for h, p in zip(hosts, ports)]


def check_libpq(conninfo="", environ=None, **kwargs) -> None:
    for host, port in libpq_targets(conninfo, environ, **kwargs):
        # A Unix socket directory is this machine; its socket file is named after the port, so the port decides.
        check("localhost" if host.startswith(("/", "@")) else host, port, strict=bool(_STATE["strict"]))


def _fence_psycopg() -> None:
    try:
        import psycopg
    except ImportError:
        return
    for cls in (psycopg.Connection, getattr(psycopg, "AsyncConnection", None)):
        if cls is None or getattr(cls.connect, "_kalillac_fenced", False):
            continue
        real = cls.connect.__func__

        if cls is psycopg.Connection:
            def connect(klass, conninfo="", *args, _real=real, **kwargs):
                check_libpq(conninfo, **kwargs)
                return _real(klass, conninfo, *args, **kwargs)
        else:
            async def connect(klass, conninfo="", *args, _real=real, **kwargs):
                check_libpq(conninfo, **kwargs)
                return await _real(klass, conninfo, *args, **kwargs)
        connect._kalillac_fenced = True
        cls.connect = classmethod(connect)
    # psycopg.connect is bound to Connection.connect when psycopg is imported, and SQLAlchemy calls it.
    psycopg.connect = psycopg.Connection.connect


def install(*, strict=True) -> None:
    """Fence this process. Idempotent; once strict, always strict."""
    if _STATE["strict"] is not None:
        _STATE["strict"] = _STATE["strict"] or strict
        _fence_psycopg()
        return
    _STATE["strict"] = strict
    real_create = socket.create_connection
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def create_connection(address, *args, **kwargs):
        check(address[0], address[1], strict=_STATE["strict"])
        return real_create(address, *args, **kwargs)

    def _inet(sock, address):
        return sock.family in (socket.AF_INET, socket.AF_INET6) and isinstance(address, tuple)

    def connect(sock, address):
        if _inet(sock, address):
            check(address[0], address[1], strict=_STATE["strict"])
        return real_connect(sock, address)

    def connect_ex(sock, address):
        if _inet(sock, address):
            check(address[0], address[1], strict=_STATE["strict"])
        return real_connect_ex(sock, address)

    create_connection._kalillac_fenced = True
    socket.create_connection = create_connection
    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    _fence_asyncio()
    _fence_psycopg()


def _fence_asyncio() -> None:
    """asyncio connects through loop.sock_connect, and the Windows proactor loop does so with IOCP (ConnectEx),
    never socket.connect: fence sock_connect itself, on every loop class that defines it."""
    import asyncio.base_events
    import asyncio.proactor_events
    import asyncio.selector_events
    for cls in (asyncio.base_events.BaseEventLoop, asyncio.selector_events.BaseSelectorEventLoop,
                getattr(asyncio.proactor_events, "BaseProactorEventLoop", None)):
        if cls is None or "sock_connect" not in vars(cls) or getattr(cls.sock_connect, "_kalillac_fenced", False):
            continue
        real = cls.sock_connect

        async def sock_connect(loop, sock, address, _real=real):
            if sock.family in (socket.AF_INET, socket.AF_INET6) and isinstance(address, tuple):
                check(address[0], address[1], strict=_STATE["strict"])
            return await _real(loop, sock, address)
        sock_connect._kalillac_fenced = True
        cls.sock_connect = sock_connect


# The environment a preview child is started with: the settings that say where it connects.
_ADDRESS_KEYS = ("_HOST", "_PORT", "_URL", "_ORIGIN", "_ENDPOINT", "_BASE")


def check_environment(environ, *, database_port=None) -> None:
    """Refuse to start a preview process whose settings name a protected port anywhere, or whose database is not
    this machine's loopback (or, when given, not exactly database_port)."""
    from urllib.parse import urlsplit
    ports = protected_ports(environ)
    host, port = environ.get("KALILLAC_DB_HOST"), environ.get("KALILLAC_DB_PORT")
    if host is not None and not _loopback(host) and not str(host).startswith("/"):
        raise PreviewFenceError(f"the preview's database must be on this machine, not {host!r}")
    if port is not None and (not str(port).isdigit() or int(port) in ports
                             or (database_port is not None and int(port) != database_port)):
        raise PreviewFenceError(f"the preview's database port must be {database_port or 'its own'}, not {port!r}")
    for key in ("PGPORT",):
        if environ.get(key, "").isdigit() and int(environ[key]) in ports:
            raise PreviewFenceError(f"{key} names local staging's port")
    for key, value in environ.items():
        if not any(key.endswith(suffix) for suffix in _ADDRESS_KEYS) and "://" not in str(value):
            continue
        text = str(value)
        if key.endswith("_PORT") and text.isdigit() and int(text) in ports:
            raise PreviewFenceError(f"{key} names local staging's port {text}")
        if "://" in text:
            try:
                named = urlsplit(text).port
            except ValueError:
                raise PreviewFenceError(f"{key} is not a valid address") from None
            if named in ports:
                raise PreviewFenceError(f"{key} names local staging's port {named}")
