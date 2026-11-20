"""Every process of the running development preview is fenced off from the protected local staging installation,
not only the manager.

  * the application (8021) and the panel (8022): separate Python processes, started through tools/preview_fenced.py;
    the fence covers Python sockets (urllib, http.client, httpx, requests, asyncio, redirects) and libpq (psycopg,
    SQLAlchemy), which does not use Python sockets;
  * Vite: a Node process with its own fence (site/preview-fence.mjs, installed by vite.preview.config.ts).

Nothing here contacts local staging. Protected ports are proven with stand-in services on ephemeral loopback ports,
added to the protected set for the child only (KALILLAC_PREVIEW_FENCE_EXTRA_PORTS, which can only add); each
stand-in counts the connections it receives, and must receive none. Allowed mock services stand in for the
preview's own application and database, and must still be reached.
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

CANDIDATE = Path(__file__).resolve().parents[1]
REPO = CANDIDATE.parent
TOOLS = CANDIDATE / "tools"
sys.path.insert(0, str(TOOLS))
import preview_guard  # noqa: E402


class Listener:
    """A loopback TCP service that counts the connections it accepts and answers each with a fixed HTTP reply."""

    def __init__(self, body=b"allowed"):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.accepted = 0
        self.body = body
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.accepted += 1
            try:
                conn.settimeout(2)
                try:
                    conn.recv(4096)
                except OSError:
                    pass
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s"
                             % (len(self.body), self.body))
            except OSError:
                pass
            finally:
                conn.close()

    def close(self):
        self.sock.close()


@pytest.fixture
def services():
    protected = [Listener(b"PROTECTED-STAND-IN") for _ in range(3)]
    allowed = Listener(b"preview")
    yield protected, allowed
    for listener in (*protected, allowed):
        listener.close()


def fenced_env(protected, **extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("KALILLAC_", "PG"))}
    env.update({"KALILLAC_PREVIEW_FENCE_EXTRA_PORTS": ",".join(str(s.port) for s in protected),
                "PYTHONDONTWRITEBYTECODE": "1", **extra})
    return env


# ---- the application's process: a stand-in that uses the application's own network stacks -------------------

APPLICATION_STAND_IN = textwrap.dedent(r'''
    import asyncio, http.client, json, os, socket, sys, urllib.request
    import httpx, psycopg, requests, sqlalchemy
    import preview_guard
    P = [int(p) for p in os.environ["KALILLAC_PREVIEW_FENCE_EXTRA_PORTS"].split(",")]
    A = int(os.environ["ALLOWED_PORT"])
    out = {}
    def attempt(name, fn):
        try:
            value = fn()
            out[name] = "reached:" + (value if isinstance(value, str) else type(value).__name__)
        except preview_guard.PreviewFenceError:
            out[name] = "fenced"
        except Exception as error:
            cause, seen = error, set()
            while cause is not None and id(cause) not in seen:
                seen.add(id(cause))
                if isinstance(cause, preview_guard.PreviewFenceError):
                    out[name] = "fenced"
                    return
                cause = cause.__cause__ or cause.__context__
            out[name] = "error:" + type(error).__name__
    attempt("urllib", lambda: urllib.request.urlopen(f"http://127.0.0.1:{P[0]}/", timeout=3).read().decode())
    attempt("urllib-localhost", lambda: urllib.request.urlopen(f"http://localhost:{P[1]}/", timeout=3).read().decode())
    attempt("http.client", lambda: (lambda c: (c.request("GET", "/"), c.getresponse().read().decode())[1])(
        http.client.HTTPConnection("127.0.0.1", P[2], timeout=3)))
    attempt("httpx", lambda: httpx.get(f"http://127.0.0.1:{P[0]}/", timeout=3).text)
    async def _httpx_async():
        async with httpx.AsyncClient(timeout=3) as c:
            return (await c.get(f"http://127.0.0.1:{P[1]}/")).text
    attempt("httpx-async", lambda: asyncio.run(_httpx_async()))
    async def _create():
        return await asyncio.get_running_loop().create_connection(asyncio.Protocol, "localhost", P[2])
    attempt("asyncio-create-connection", lambda: asyncio.run(_create()))
    attempt("requests", lambda: requests.get(f"http://127.0.0.1:{P[2]}/", timeout=3).text)
    attempt("asyncio", lambda: asyncio.run(asyncio.open_connection("127.0.0.1", P[0])))
    attempt("socket-v6-name", lambda: socket.create_connection(("localhost", P[1]), timeout=3))
    attempt("staging-8010", lambda: socket.create_connection(("127.0.0.2", 8010), timeout=3))
    # libpq: never through Python sockets, so fenced at psycopg itself.
    attempt("psycopg-keywords", lambda: psycopg.connect(host="127.0.0.1", port=P[0], user="x", dbname="x", connect_timeout=3))
    attempt("psycopg-url", lambda: psycopg.connect(f"postgresql://x@localhost:{P[1]}/x?connect_timeout=3"))
    attempt("psycopg-dsn", lambda: psycopg.Connection.connect(f"host=127.0.0.1 port={P[2]} user=x connect_timeout=3"))
    attempt("psycopg-unix-55433", lambda: psycopg.connect("host=/var/run/postgresql port=55433 user=x connect_timeout=3"))
    attempt("psycopg-multihost", lambda: psycopg.connect(f"host=127.0.0.1,127.0.0.1 port={A},{P[0]} user=x connect_timeout=3"))
    attempt("psycopg-async", lambda: asyncio.run(psycopg.AsyncConnection.connect(f"host=127.0.0.1 port={P[1]} user=x connect_timeout=3")))
    os.environ["PGPORT"] = str(P[2])
    attempt("psycopg-pgport", lambda: psycopg.connect("host=127.0.0.1 user=x connect_timeout=3"))
    del os.environ["PGPORT"]
    attempt("sqlalchemy", lambda: sqlalchemy.create_engine(
        f"postgresql+psycopg://x@127.0.0.1:{P[0]}/x", connect_args={"connect_timeout": 3}).connect())
    # Still allowed: the preview's own services (here a mock) over HTTP, and libpq to an allowed port, which is
    # attempted (the mock is not PostgreSQL, so libpq fails after connecting, not the fence).
    attempt("allowed-http", lambda: urllib.request.urlopen(f"http://127.0.0.1:{A}/", timeout=3).read().decode())
    attempt("allowed-httpx", lambda: httpx.get(f"http://localhost:{A}/", timeout=3).text)
    async def _allowed_async():
        async with httpx.AsyncClient(timeout=3) as c:
            return (await c.get(f"http://127.0.0.1:{A}/")).text
    attempt("allowed-httpx-async", lambda: asyncio.run(_allowed_async()))
    attempt("allowed-libpq", lambda: psycopg.connect(host="127.0.0.1", port=A, user="x", dbname="x", connect_timeout=3))
    print(json.dumps(out))
''')


def run_fenced(tmp_path, script_text, env):
    script = tmp_path / "stand_in.py"
    script.write_text(script_text, encoding="utf-8")
    return subprocess.run([sys.executable, "-B", str(TOOLS / "preview_fenced.py"), str(script)],
                          cwd=CANDIDATE, env={**env, "PYTHONPATH": str(TOOLS)}, capture_output=True, text=True,
                          timeout=120)


def test_the_application_process_reaches_no_protected_port_by_any_network_stack(tmp_path, services):
    protected, allowed = services
    proc = run_fenced(tmp_path, APPLICATION_STAND_IN, fenced_env(protected, ALLOWED_PORT=str(allowed.port)))
    assert proc.returncode == 0, proc.stderr[-3000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    fenced = {k: v for k, v in out.items() if not k.startswith("allowed")}
    assert set(fenced.values()) == {"fenced"}, fenced
    assert out["allowed-http"] == out["allowed-httpx"] == out["allowed-httpx-async"] == "reached:preview"
    assert out["allowed-libpq"].startswith("error:") and out["allowed-libpq"] != "fenced"
    # The stand-ins for local staging received nothing at all; the allowed service was reached.
    assert [s.accepted for s in protected] == [0, 0, 0]
    assert allowed.accepted >= 3
    assert "preview fence installed in stand_in.py" in proc.stderr


@pytest.mark.parametrize("override", [
    {"KALILLAC_DB_PORT": "55433"}, {"KALILLAC_DB_PORT": "5432"}, {"KALILLAC_DB_HOST": "10.0.0.5"},
    {"KALILLAC_SITE_ORIGIN": "http://localhost:8010"}, {"KALILLAC_BILLING_SUCCESS_URL": "http://127.0.0.1:8011/x"},
    {"PGPORT": "55433"}, {"KALILLAC_SOMETHING_PORT": "8011"},
])
def test_a_child_whose_settings_name_staging_never_starts(tmp_path, services, override):
    protected, _ = services
    marker = tmp_path / "ran"
    env = fenced_env(protected, KALILLAC_DB_HOST="127.0.0.1", KALILLAC_DB_PORT="55434",
                     KALILLAC_PREVIEW_DATABASE_PORT="55434", KALILLAC_SITE_ORIGIN="http://localhost:8020")
    env.update(override)
    proc = run_fenced(tmp_path, f"open({str(marker)!r}, 'w').write('ran')\n", env)
    assert proc.returncode != 0 and not marker.exists()
    assert "PreviewFenceError" in proc.stderr


def test_the_preview_settings_themselves_pass():
    preview_guard.check_environment({
        "KALILLAC_DB_HOST": "127.0.0.1", "KALILLAC_DB_PORT": "55434", "KALILLAC_SITE_ORIGIN": "http://localhost:8020",
        "KALILLAC_ACCOUNT_REGISTRATION_URL": "http://localhost:8020/account/create/confirm",
        "KALILLAC_BILLING_SUCCESS_URL": "http://localhost:8020/account?checkout=success",
        "KALILLAC_TRANSPORT_MAX_OUTSTANDING": "4"}, database_port=55434)


# ---- the panel's process: the real tools/local_staging_web.py, fenced ---------------------------------------

def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_panel(tmp_path, protected, upstream_port):
    port = free_port()
    (tmp_path / "webroot").mkdir(exist_ok=True)
    env = fenced_env(protected, KALILLAC_DEPLOYMENT_ID="kalillac-local-test", KALILLAC_STRIPE_MODE="test",
                     KALILLAC_SITE_ORIGIN="http://localhost:8020", KALILLAC_LOCAL_STATE=str(tmp_path / "state"),
                     KALILLAC_DB_HOST="127.0.0.1", KALILLAC_DB_PORT="55434", KALILLAC_PREVIEW_DATABASE_PORT="55434",
                     PYTHONPATH=str(TOOLS))
    proc = subprocess.Popen([sys.executable, "-B", str(TOOLS / "preview_fenced.py"), str(TOOLS / "local_staging_web.py"),
                             "--webroot", str(tmp_path / "webroot"), "--upstream", f"http://127.0.0.1:{upstream_port}",
                             "--port", str(port)], cwd=CANDIDATE, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return proc, port
        except OSError:
            if proc.poll() is not None:
                raise AssertionError(proc.stdout.read().decode(errors="replace")[-3000:])
            time.sleep(0.1)
    proc.kill()
    raise AssertionError("the panel did not start")


def panel_get(port, path):
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as reply:
            return reply.status, reply.read()
    except urllib.error.HTTPError as error:
        error.close()                    # The status is the answer; the stand-ins' counts prove the rest.
        return error.code, b""


def test_the_real_panel_process_proxies_to_the_preview_and_never_to_a_protected_port(tmp_path, services):
    protected, allowed = services
    for upstream, expect_reached in ((protected[0], False), (allowed, True)):
        proc, port = start_panel(tmp_path, protected, upstream.port)
        try:
            status, body = panel_get(port, "/api/health")
        finally:
            proc.terminate()
            output = proc.communicate(timeout=20)[0].decode(errors="replace")
        assert "preview fence installed in local_staging_web.py" in output
        if expect_reached:
            assert status == 200 and body == b"preview" and upstream.accepted >= 1
        else:
            assert status >= 500 and b"PROTECTED-STAND-IN" not in body
    assert [s.accepted for s in protected] == [0, 0, 0]


# ---- the manager starts both children fenced -----------------------------------------------------------------

def test_the_manager_starts_every_child_through_the_fence():
    source = (TOOLS / "dev_preview.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    run = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run")
    run_source = ast.get_source_segment(source, run)
    # Every Popen in run() takes its command from child_command and its environment from child_environment.
    assert run_source.count("subprocess.Popen(") == 1
    assert "subprocess.Popen(child_command(script, *extra)" in run_source
    assert "env = child_environment(ls.service_env(p, home))" in run_source
    assert run_source.count('spawn("local_staging_app.py"') == 3 and run_source.count('spawn("local_staging_web.py"') == 1
    assert f'f"http://127.0.0.1:{{APP_PORT}}"' in run_source
    sys.path.insert(0, str(TOOLS))
    command = source[source.index("def child_command"):source.index("def run(")]
    assert '"preview_fenced.py"' in command and "CHILDREN" in command
    environment = source[source.index("def child_environment"):source.index("def child_command")]
    assert "check_environment(env, database_port=PG_PORT)" in environment
    assert '"KALILLAC_PREVIEW_DATABASE_PORT": str(PG_PORT)' in environment


# ---- Vite's process ------------------------------------------------------------------------------------------

NODE_STAND_IN = r'''
import http from 'node:http';
import net from 'node:net';
import { pathToFileURL } from 'node:url';
const { installPreviewFence } = await import(pathToFileURL(process.argv[2]).href);
installPreviewFence();
installPreviewFence();
const P = process.env.KALILLAC_PREVIEW_FENCE_EXTRA_PORTS.split(',').map(Number);
const A = Number(process.env.ALLOWED_PORT);
const get = (opts) => new Promise((resolve) => {
  const req = http.get(opts, (res) => { let b = ''; res.on('data', (c) => (b += c)); res.on('end', () => resolve('reached:' + b)); });
  req.on('error', (e) => resolve(e.code === 'EKALILLACPREVIEWFENCE' ? 'fenced' : 'error:' + e.code));
});
const raw = (make) => new Promise((resolve) => {
  const s = make();
  s.on('connect', () => { s.destroy(); resolve('reached'); });
  s.on('error', (e) => resolve(e.code === 'EKALILLACPREVIEWFENCE' ? 'fenced' : 'error:' + e.code));
});
const out = {
  'http-127': await get({ host: '127.0.0.1', port: P[0], path: '/' }),
  'http-localhost': await get(`http://localhost:${P[1]}/`),
  'proxy-like-agent': await get({ host: '127.0.0.1', port: P[2], path: '/api/x', agent: new http.Agent({ keepAlive: true }) }),
  'net-connect': await raw(() => net.connect(P[0], '127.0.0.1')),
  'net-options': await raw(() => net.connect({ port: P[1], host: 'localhost' })),
  'socket-connect': await raw(() => new net.Socket().connect(P[2], '127.0.0.1')),
  'staging-8010': await raw(() => net.connect({ port: 8010, host: '127.0.0.2' })),
  'outside': await raw(() => net.connect({ port: 443, host: '192.0.2.1' })),
  'allowed-http': await get({ host: '127.0.0.1', port: A, path: '/' }),
};
console.log(JSON.stringify(out));
process.exit(0);
'''


def test_the_vite_process_reaches_no_protected_port_and_still_proxies_to_the_preview(tmp_path, services):
    node = shutil.which("node")
    assert node, "Node.js is required to verify the Vite process's fence"
    protected, allowed = services
    script = tmp_path / "stand_in.mjs"
    script.write_text(NODE_STAND_IN, encoding="utf-8")
    env = {**os.environ, "KALILLAC_PREVIEW_FENCE_EXTRA_PORTS": ",".join(str(s.port) for s in protected),
           "ALLOWED_PORT": str(allowed.port)}
    proc = subprocess.run([node, str(script), str(REPO / "site" / "preview-fence.mjs")], env=env,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[-3000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out.pop("allowed-http") == "reached:preview"
    assert set(out.values()) == {"fenced"}, out
    assert [s.accepted for s in protected] == [0, 0, 0] and allowed.accepted == 1


def test_vite_installs_its_fence_from_its_own_configuration():
    vite = (REPO / "site" / "vite.preview.config.ts").read_text(encoding="utf-8")
    assert "import { installPreviewFence } from './preview-fence.mjs';" in vite
    assert vite.index("installPreviewFence();") < vite.index("export default")
    build = (REPO / "site" / "vite.config.ts").read_text(encoding="utf-8")
    assert "preview-fence" not in build                     # Never part of a build.


# ---- the fence's own rules -----------------------------------------------------------------------------------

def test_libpq_targets_follow_the_connection_string_keywords_and_environment():
    assert preview_guard.libpq_targets("host=a,b port=1,2", environ={}) == [("a", "1"), ("b", "2")]
    assert preview_guard.libpq_targets("host=a,b port=7", environ={}) == [("a", "7"), ("b", "7")]
    assert preview_guard.libpq_targets("", environ={"PGHOST": "127.0.0.1", "PGPORT": "55434"}) == [("127.0.0.1", "55434")]
    assert preview_guard.libpq_targets("postgresql://u@h:9/d", environ={}) == [("h", "9")]
    assert preview_guard.libpq_targets("", environ={}, host="x", port=3) == [("x", "3")]
    assert preview_guard.libpq_targets("", environ={}) == [("/var/run/postgresql", "5432")]


def test_the_children_keep_outbound_access_except_to_protected_ports():
    # Not strict: an external provider (when the owner supplies a real key) stays possible; staging never is.
    preview_guard.check("api.example.com", 443, strict=False)
    preview_guard.check("127.0.0.1", 55434, strict=False)
    for host, port in (("api.example.com", 8010), ("192.168.1.20", 8011), ("localhost", 55433)):
        with pytest.raises(preview_guard.PreviewFenceError):
            preview_guard.check(host, port, strict=False)
