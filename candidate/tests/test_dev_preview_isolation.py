"""The development preview can never reach the protected local staging installation (localhost:8010, its
application on 8011, its PostgreSQL on 55433), directly, through a redirect, or under another name for loopback.

Everything here is mocked and isolated: a child process installs tools/preview_guard.py's fence over a backstop
that records and refuses every protected port beneath it, so even a broken fence could not contact the protected
installation; the only server is a mock on an ephemeral loopback port. Nothing else is contacted.
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

CANDIDATE = Path(__file__).resolve().parents[1]
REPO = CANDIDATE.parent

CHILD = r'''
import http.server, json, socket, sys, threading, urllib.error, urllib.request
sys.path.insert(0, "tools")

attempts = []
real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex
def backstop(sock, address):          # Beneath the fence: a protected port is never actually connected to.
    if isinstance(address, tuple) and int(address[1]) in (8010, 8011, 55433):
        attempts.append([str(address[0]), int(address[1])])
        raise ConnectionRefusedError("test backstop")
    return real_connect(sock, address)
def backstop_ex(sock, address):
    if isinstance(address, tuple) and int(address[1]) in (8010, 8011, 55433):
        attempts.append([str(address[0]), int(address[1])])
        return 111
    return real_connect_ex(sock, address)
socket.socket.connect, socket.socket.connect_ex = backstop, backstop_ex

REDIRECTS = {"/to-site": "http://localhost:8010/", "/to-app": "http://127.0.0.1:8011/api/health",
             "/to-v6": "http://[::1]:8010/", "/to-other-loopback": "http://127.0.0.2:8010/",
             "/to-outside": "http://example.com/", "/chain": "/to-site"}
class Mock(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in REDIRECTS:
            self.send_response(302); self.send_header("Location", REDIRECTS[self.path]); self.end_headers(); return
        body = b"preview"
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass
server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Mock)
threading.Thread(target=server.serve_forever, daemon=True).start()
port = server.server_address[1]

import preview_guard
preview_guard.install()
preview_guard.install()              # Idempotent.

def fetch(url):
    try:
        return "ok:" + urllib.request.urlopen(url, timeout=5).read().decode()
    except preview_guard.PreviewFenceError:
        return "fenced"
    except urllib.error.URLError as error:
        return "fenced" if isinstance(error.reason, preview_guard.PreviewFenceError) else "other:" + repr(error.reason)
    except Exception as error:
        return "other:" + type(error).__name__ + ":" + str(error)

def raw(address, ex=False):
    family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
    s = socket.socket(family, socket.SOCK_STREAM)
    try:
        if ex:
            return "fenced-ex" if s.connect_ex(address) is None else "returned"
        s.connect(address)
        return "connected"
    except preview_guard.PreviewFenceError:
        return "fenced"
    except Exception as error:
        return "other:" + type(error).__name__
    finally:
        s.close()

def create(address):
    try:
        socket.create_connection(address, timeout=2).close()
        return "connected"
    except preview_guard.PreviewFenceError:
        return "fenced"
    except Exception as error:
        return "other:" + type(error).__name__

out = {"allowed": [fetch(f"http://127.0.0.1:{port}/"), fetch(f"http://localhost:{port}/")]}
out["direct"] = {u: fetch(u) for u in (
    "http://localhost:8010/", "http://127.0.0.1:8010/", "http://127.0.0.1:8011/api/health", "http://127.0.0.2:8010/",
    "http://[::1]:8010/", "http://0.0.0.0:8010/", "http://localhost.:8010/", "http://LOCALHOST:8011/",
    "http://2130706433:8010/", "http://0x7f000001:8010/", "http://[::ffff:127.0.0.1]:8010/")}
out["outside"] = {u: fetch(u) for u in ("http://example.com/", f"http://10.0.0.1:{port}/", f"http://192.168.1.1:{port}/")}
out["redirects"] = {p: fetch(f"http://127.0.0.1:{port}{p}") for p in REDIRECTS}
out["sockets"] = {"connect-8010": raw(("127.0.0.1", 8010)), "connect-55433": raw(("127.0.0.1", 55433)),
                  "connect-v6-8011": raw(("::1", 8011)), "create-55433": create(("localhost", 55433)),
                  "create-outside": create(("example.com", 80))}
try:
    socket.socket().connect_ex(("127.0.0.1", 8010)); out["sockets"]["connect_ex-8010"] = "returned"
except preview_guard.PreviewFenceError:
    out["sockets"]["connect_ex-8010"] = "fenced"
out["attempts"] = attempts
print(json.dumps(out))
'''


def _child():
    proc = subprocess.run([sys.executable, "-I", "-c", CHILD], cwd=CANDIDATE, capture_output=True, text=True,
                          timeout=120)
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_the_fence_refuses_every_route_to_the_protected_installation_and_allows_the_preview():
    out = _child()
    assert out["allowed"] == ["ok:preview", "ok:preview"]
    assert out["direct"] and set(out["direct"].values()) == {"fenced"}, out["direct"]
    assert set(out["outside"].values()) == {"fenced"}, out["outside"]
    # A redirect from an allowed preview address to local staging (or outside) is refused at its connection.
    assert set(out["redirects"].values()) == {"fenced"}, out["redirects"]
    assert set(out["sockets"].values()) == {"fenced"}, out["sockets"]
    # The backstop beneath the fence saw nothing: no protected port was ever even attempted.
    assert out["attempts"] == []


def test_the_fence_judges_hosts_without_resolving_them():
    sys.path.insert(0, str(CANDIDATE / "tools"))
    try:
        import preview_guard
    finally:
        sys.path.remove(str(CANDIDATE / "tools"))
    for host in ("localhost", "LOCALHOST", "localhost.", "127.0.0.1", "127.0.0.2", "::1", "[::1]", b"127.0.0.1"):
        preview_guard.check(host, 8021)
    for host, port in (("localhost", 8010), ("127.0.0.1", "8011"), ("::1", 55433), ("example.com", 8021),
                       ("localhost.example.com", 8021), ("10.0.0.1", 8021), ("", 8021), ("localhost", "x")):
        try:
            preview_guard.check(host, port)
        except preview_guard.PreviewFenceError:
            continue
        raise AssertionError((host, port))


def test_the_preview_uses_only_its_own_ports_and_installs_the_fence():
    source = (CANDIDATE / "tools" / "dev_preview.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    ports = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Tuple) and \
                [t.id for t in node.targets[0].elts] == ["SITE_PORT", "APP_PORT", "PANEL_PORT", "PG_PORT"]:
            ports = [c.value for c in node.value.elts]
    assert ports == [8020, 8021, 8022, 55434]
    assert not {8010, 8011, 55433} & set(ports)
    guard = source[source.index("def _guard_protected_ports"):source.index("def setup(")]
    assert "preview_guard.install()" in guard and "preview_guard.check(" in guard
    # configure() fences the process before anything can be sent.
    configure = source[source.index("def configure("):source.index("def _guard_protected_ports")]
    assert "_guard_protected_ports()" in configure and "ls._http.__defaults__" in configure
    # The browser side: Vite proxies /api and /__local__ only to the preview's own panel.
    vite = (REPO / "site" / "vite.preview.config.ts").read_text(encoding="utf-8")
    assert "const PANEL = 'http://127.0.0.1:8022';" in vite
    assert re.findall(r"target: (\w+)", vite) == ["PANEL", "PANEL"]
    code = re.sub(r"//[^\n]*", "", vite)               # Its comments name local staging's ports; its code never does.
    assert not re.search(r"\b(8010|8011|55433)\b", code)
