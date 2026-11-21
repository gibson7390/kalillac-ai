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
    assert "*FENCED_LAUNCHER" in command and "CHILDREN" in command
    assert 'FENCED_LAUNCHER = (str(TOOLS / "preview_fenced.py"),)' in source
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


# ==== Review corrections: setup's processes are fenced too; a libpq service cannot go around the fence ====

LINUX_ONLY = pytest.mark.skipif(os.name == "nt", reason="local staging's process tooling is Linux-only (WSL, CI)")


def test_every_lifecycle_starts_its_python_processes_through_one_fenced_path():
    """Statically: local staging's tooling has one way to start a Python process, the preview routes it through
    the fenced launcher and the settings check, and it does so in configure(), which setup, a resumed setup,
    run and status all call first."""
    staging = (TOOLS / "local_staging.py").read_text(encoding="utf-8")
    tree = ast.parse(staging)
    # sys.executable appears once, in python_command; nothing else builds a `python ...` command.
    assert staging.count("sys.executable") == 1
    command = ast.get_source_segment(staging, next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "python_command"))
    assert 'return [sys.executable, "-B", *LAUNCHER, script, *arguments]' in command
    # The application and panel (_spawn) and the V1 transition take their command and environment from it.
    assert staging.count("subprocess.Popen(") == 1
    assert "subprocess.Popen(python_command(_script(name), *extra)" in staging
    assert "env={**child_environment(env), INSTANCE: token}" in staging
    assert 'as_user(python_command("tools/v1_security_transition.py"' in staging
    assert "uid, gid, env=child_environment(env), cwd=CANDIDATE" in staging
    assert "LAUNCHER: tuple = ()" in staging and "CHILD_ENVIRONMENT = None" in staging     # Off for local staging.

    preview = (TOOLS / "dev_preview.py").read_text(encoding="utf-8")
    functions = {n.name: ast.get_source_segment(preview, n) for n in ast.parse(preview).body
                 if isinstance(n, ast.FunctionDef)}
    assert "fence_children()" in functions["configure"]
    assert "ls.LAUNCHER = FENCED_LAUNCHER" in functions["fence_children"]
    assert "ls.CHILD_ENVIRONMENT = child_environment" in functions["fence_children"]
    assert 'FENCED_LAUNCHER = (str(TOOLS / "preview_fenced.py"),)' in preview
    for name in ("setup", "run", "status"):
        assert functions[name].lstrip().split("\n")[1].strip().startswith("configure("), name
    # setup starts children only through local staging's tooling (ls.start, ls.setup): no launch of its own.
    assert "Popen" not in functions["setup"] and "sys.executable" not in functions["setup"]


@pytest.fixture
def preview_tools(monkeypatch):
    """dev_preview and local_staging as the preview configures them, restored afterwards."""
    monkeypatch.syspath_prepend(str(TOOLS))
    monkeypatch.syspath_prepend(str(CANDIDATE))
    import dev_preview as dev
    monkeypatch.setattr(dev.ls, "LAUNCHER", dev.ls.LAUNCHER)
    monkeypatch.setattr(dev.ls, "CHILD_ENVIRONMENT", dev.ls.CHILD_ENVIRONMENT)
    return dev


PREVIEW_SETTINGS = {"KALILLAC_DB_HOST": "127.0.0.1", "KALILLAC_DB_PORT": "55434",
                    "KALILLAC_SITE_ORIGIN": "http://localhost:8020"}


def mocked_setup(dev, monkeypatch, tmp_path, *, resumed, settings):
    """dev_preview.setup with process creation, health checks and provisioning mocked: nothing is started and
    nothing is contacted. Returns the commands and environments it would have launched."""
    ls = dev.ls
    root = tmp_path / "preview"
    paths = {name: root / name for name in ("state", "run", "webroot")}
    for path in paths.values():
        path.mkdir(parents=True)
    if resumed:
        (root / "local.json").write_text("{}")
        (root / "demo-accounts.txt").write_text("synthetic")
    launched = []

    class Process:
        pid = 1234567

    def popen(command, **kwargs):
        launched.append((list(command), dict(kwargs.get("env") or {})))
        return Process()

    def configure(create=False):
        dev.fence_children()                           # As the real configure() does, last.
        return root

    def provisioned(args):
        # A first setup: provisioning (PostgreSQL, the deployment, the transition) is not run here; it ends by
        # starting the application and panel through local staging's own start, as build_and_finish does.
        (root / "local.json").write_text("{}")
        ls.start(args)

    for name, value in (("ROOT", root), ("APP_PORT", 8021), ("WEB_PORT", 8020), ("PG_PORT", 55434),
                        ("ORIGIN", "http://localhost:8020")):
        monkeypatch.setattr(ls, name, value)
    monkeypatch.setattr(dev, "configure", configure)
    monkeypatch.setattr(ls, "identity", lambda: (os.getuid(), os.getgid(), "synthetic", str(root)))
    monkeypatch.setattr(ls, "paths", lambda: paths)
    monkeypatch.setattr(ls, "pg_start", lambda *a, **k: None)
    monkeypatch.setattr(ls, "pg_bin", lambda home: "/unused-mock")
    monkeypatch.setattr(ls, "service_env", lambda p, home: dict(settings))
    monkeypatch.setattr(ls, "_running", lambda *a, **k: False)
    monkeypatch.setattr(ls, "_get", lambda *a, **k: (200, b""))
    monkeypatch.setattr(ls, "_started", lambda pid: 1)
    monkeypatch.setattr(ls, "_boot", lambda: "synthetic")
    monkeypatch.setattr(ls, "status", lambda args: None)
    monkeypatch.setattr(ls, "stop", lambda args: None)
    monkeypatch.setattr(ls, "seed", lambda paths: None)
    monkeypatch.setattr(ls, "setup", provisioned)
    monkeypatch.setattr(ls.subprocess, "Popen", popen)
    dev.setup(None)
    return launched


@LINUX_ONLY
@pytest.mark.parametrize("resumed", [False, True], ids=["first-setup", "resumed-setup"])
def test_setup_starts_the_application_and_panel_through_the_fenced_launcher(preview_tools, monkeypatch, tmp_path,
                                                                           resumed):
    launched = mocked_setup(preview_tools, monkeypatch, tmp_path, resumed=resumed, settings=PREVIEW_SETTINGS)
    fenced = str(TOOLS / "preview_fenced.py")
    assert len(launched) == 2
    for (command, env), script, port in zip(launched, ("local_staging_app.py", "local_staging_web.py"), ("8021", "8020")):
        # python -B preview_fenced.py <the script> ...: the fence is installed in that process before its code runs.
        assert command[1:3] == ["-B", fenced] and command[3].endswith(os.sep + script), command
        assert command[command.index("--port") + 1] == port
        # Its settings were checked, and carry the database port the launcher checks again inside the child.
        assert env["KALILLAC_PREVIEW_DATABASE_PORT"] == "55434" and env["KALILLAC_DB_PORT"] == "55434"
        assert "KALILLAC_LOCAL_INSTANCE" in env
    # run uses the same launcher for the same scripts.
    assert preview_tools.child_command("local_staging_app.py")[1:3] == ["-B", fenced]


@LINUX_ONLY
@pytest.mark.parametrize("resumed", [False, True], ids=["first-setup", "resumed-setup"])
@pytest.mark.parametrize("override", [{"KALILLAC_DB_PORT": "55433"}, {"KALILLAC_SITE_ORIGIN": "http://localhost:8010"},
                                      {"PGSERVICE": "staging"}])
def test_setup_refuses_to_start_a_process_whose_settings_name_staging(preview_tools, monkeypatch, tmp_path,
                                                                      resumed, override):
    with pytest.raises(SystemExit):
        launched = mocked_setup(preview_tools, monkeypatch, tmp_path, resumed=resumed,
                                settings={**PREVIEW_SETTINGS, **override})
        assert launched == []                              # Not reached: the refusal comes before any launch.
    # Nothing was started: the refusal is the first thing that happens.
    assert not (tmp_path / "preview" / "run").exists() or list((tmp_path / "preview" / "run").iterdir()) == []


LIFECYCLE_STAND_IN = textwrap.dedent(r"""
    import json, os, socket, sys, time
    import psycopg
    import preview_guard
    P, A = int(os.environ["STAND_IN_PROTECTED"]), int(os.environ["STAND_IN_ALLOWED"])
    out = {"argv": sys.argv[1:]}
    def attempt(name, fn):
        try:
            fn()
            out[name] = "reached"
        except preview_guard.PreviewFenceError:
            out[name] = "fenced"
        except Exception as error:
            out[name] = "error:" + type(error).__name__
    attempt("socket", lambda: socket.create_connection(("127.0.0.1", P), timeout=3))
    attempt("libpq", lambda: psycopg.connect(host="127.0.0.1", port=P, user="x", dbname="x", connect_timeout=3))
    attempt("allowed", lambda: socket.create_connection(("127.0.0.1", A), timeout=3).close())
    with open(os.environ["STAND_IN_RESULT"] + ".tmp", "w") as handle:
        json.dump(out, handle)
    os.replace(os.environ["STAND_IN_RESULT"] + ".tmp", os.environ["STAND_IN_RESULT"])
    time.sleep(60)
""")


@LINUX_ONLY
def test_a_process_started_by_local_stagings_own_launcher_runs_inside_the_fence(preview_tools, monkeypatch,
                                                                              tmp_path, services):
    """The real launch path setup uses (local_staging._spawn), with a stand-in for the application: the process
    it starts cannot reach a protected stand-in by socket or libpq, still reaches an allowed service, is
    recognised as local staging's own process and is stopped by its own stop."""
    dev = preview_tools
    ls = dev.ls
    protected, allowed = services
    script = tmp_path / "stand_in_app.py"
    script.write_text(LIFECYCLE_STAND_IN, encoding="utf-8")
    result = tmp_path / "result.json"
    p = {"state": tmp_path / "state", "run": tmp_path / "run"}
    for path in p.values():
        path.mkdir()
    monkeypatch.setattr(ls, "SCRIPTS", {"app": str(script)})
    # run/ records are trusted only when root wrote them; this test's own user stands in for root here.
    monkeypatch.setattr(ls, "_trusted_owners", lambda: {0, os.getuid()})
    dev.fence_children()
    env = fenced_env(protected[:1], **PREVIEW_SETTINGS, STAND_IN_PROTECTED=str(protected[0].port),
                     STAND_IN_ALLOWED=str(allowed.port), STAND_IN_RESULT=str(result))
    uid, gid = os.getuid(), os.getgid()
    process = ls._spawn("app", ["--port", "8021"], p, uid, gid, env)
    try:
        deadline = time.monotonic() + 60
        while not result.exists() and time.monotonic() < deadline and process.poll() is None:
            time.sleep(0.1)
        log = (p["state"] / "app.log").read_text(encoding="utf-8", errors="replace")
        assert result.exists(), log[-2000:]
        out = json.loads(result.read_text())
        assert out == {"argv": ["--port", "8021"], "socket": "fenced", "libpq": "fenced", "allowed": "reached"}
        assert protected[0].accepted == 0 and allowed.accepted == 1
        assert "preview fence installed in stand_in_app.py" in log
        # Local staging still recognises, and can stop, the process it started this way.
        assert ls._running(p, "app", uid) == process.pid
        assert ls._terminate(p, "app", uid) == "stopped"
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(10)


def test_the_v1_transition_tool_runs_under_the_fenced_launcher(tmp_path):
    # The one other Python process setup starts. Its command line is unchanged by the launcher (here: its help).
    env = {k: v for k, v in os.environ.items() if not k.startswith(("KALILLAC_", "PG"))}
    proc = subprocess.run([sys.executable, "-B", str(TOOLS / "preview_fenced.py"), "tools/v1_security_transition.py",
                           "--help"], cwd=CANDIDATE, env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "activate" in proc.stdout and "preview fence installed in v1_security_transition.py" in proc.stderr


# ---- libpq services -------------------------------------------------------------------------------------------

SERVICE_STAND_IN = textwrap.dedent(r"""
    import asyncio, json, os
    import psycopg, sqlalchemy
    import preview_guard
    A = int(os.environ["ALLOWED_PORT"])
    out = {}
    def attempt(name, fn):
        try:
            fn()
            out[name] = "reached"
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
    # What libpq itself would do with the service, read without connecting: it names the protected stand-in.
    out["service-resolves-to"] = psycopg.pq.Conninfo.parse(b"service=stand_in") and [
        (o.keyword.decode(), (o.val or b"").decode()) for o in psycopg.pq.Conninfo.parse(b"service=stand_in")
        if o.keyword in (b"service",)]
    attempt("service-dsn", lambda: psycopg.connect("service=stand_in"))
    attempt("service-keyword", lambda: psycopg.connect(service="stand_in"))
    attempt("service-uri", lambda: psycopg.connect("postgresql:///?service=stand_in"))
    attempt("service-with-allowed-host", lambda: psycopg.connect(f"service=stand_in host=127.0.0.1 port={A}"))
    attempt("service-async", lambda: asyncio.run(psycopg.AsyncConnection.connect("service=stand_in")))
    attempt("service-sqlalchemy", lambda: sqlalchemy.create_engine(
        "postgresql+psycopg://", connect_args={"service": "stand_in"}).connect())
    os.environ["PGSERVICE"] = "stand_in"
    attempt("service-environment", lambda: psycopg.connect(""))
    attempt("service-environment-with-allowed-host", lambda: psycopg.connect(host="127.0.0.1", port=A, user="x"))
    del os.environ["PGSERVICE"]
    # Still allowed: a database named by host and port (the mock is not PostgreSQL: libpq fails after connecting).
    attempt("allowed-host-and-port", lambda: psycopg.connect(host="127.0.0.1", port=A, user="x", dbname="x",
                                                              connect_timeout=3))
    print(json.dumps(out))
""")


def service_file(tmp_path, port):
    path = tmp_path / "pg_service.conf"
    path.write_text(f"[stand_in]\nhost=127.0.0.1\nport={port}\nuser=synthetic\ndbname=synthetic\n"
                    "sslmode=disable\nconnect_timeout=2\n", encoding="utf-8")
    return path


def test_a_libpq_service_cannot_reach_a_protected_port(tmp_path, services):
    """A synthetic service file that names a protected stand-in: every way of using it is refused before libpq
    runs, and the stand-in receives nothing. A database named by host and port still connects."""
    protected, allowed = services
    env = fenced_env(protected, ALLOWED_PORT=str(allowed.port),
                     PGSERVICEFILE=str(service_file(tmp_path, protected[0].port)))
    proc = run_fenced(tmp_path, SERVICE_STAND_IN, env)
    assert proc.returncode == 0, proc.stderr[-3000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    refused = {k: v for k, v in out.items() if k.startswith("service-") and k != "service-resolves-to"}
    assert len(refused) == 8 and set(refused.values()) == {"fenced"}, refused
    assert out["allowed-host-and-port"].startswith("error:")           # Attempted: libpq, not the fence, failed.
    assert [s.accepted for s in protected] == [0, 0, 0] and allowed.accepted == 1


def test_a_process_started_with_pgservice_never_starts(tmp_path, services):
    protected, allowed = services
    marker = tmp_path / "ran"
    env = fenced_env(protected, PGSERVICE="stand_in", PGSERVICEFILE=str(service_file(tmp_path, protected[0].port)),
                     KALILLAC_DB_HOST="127.0.0.1", KALILLAC_DB_PORT="55434", KALILLAC_PREVIEW_DATABASE_PORT="55434")
    proc = run_fenced(tmp_path, f"open({str(marker)!r}, 'w').write('ran')\n", env)
    assert proc.returncode != 0 and not marker.exists()
    assert "PreviewFenceError" in proc.stderr and "PGSERVICE" in proc.stderr
    assert [s.accepted for s in protected] == [0, 0, 0]


def test_the_fence_refuses_a_service_wherever_it_is_named():
    for conninfo, environ, keywords in (("service=x", {}, {}), ("", {"PGSERVICE": "x"}, {}), ("", {}, {"service": "x"}),
                                        ("postgresql:///?service=x", {}, {}),
                                        ("host=127.0.0.1 port=55434 service=x", {}, {})):
        with pytest.raises(preview_guard.PreviewFenceError):
            preview_guard.libpq_targets(conninfo, environ=environ, **keywords)
    with pytest.raises(preview_guard.PreviewFenceError):
        preview_guard.check_environment({"PGSERVICE": "x", "KALILLAC_DB_HOST": "127.0.0.1", "KALILLAC_DB_PORT": "55434"},
                                        database_port=55434)
    # A service file on its own names no destination: the preview's own database is unaffected.
    assert preview_guard.libpq_targets("host=127.0.0.1 port=55434", environ={"PGSERVICEFILE": "/nowhere"}) == [
        ("127.0.0.1", "55434")]
    preview_guard.check_environment({"PGSERVICEFILE": "/nowhere", "KALILLAC_DB_HOST": "127.0.0.1",
                                     "KALILLAC_DB_PORT": "55434"}, database_port=55434)
