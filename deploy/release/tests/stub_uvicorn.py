"""Disposable-test stand-in for <venv>/bin/uvicorn: the process exec_launcher.py replaces itself with.

Accepts uvicorn's command line (only --host and --port are used) and serves, from the release it
runs in (cwd = <release>/candidate), GET /api/health -> {"status":"ok"}; everything else is 404.
It starts listening after KDT_STARTUP_SECONDS, like an application import.

Faults, for tests only, read once at start-up from KDT_FAULT_FILE: lines "<mode> <release directory
name>", mode crash (exit before listening), unhealthy (health answers 503) or slow (20 s extra).
"""
import http.server
import os
import sys
import time

args = sys.argv[1:]
host = args[args.index("--host") + 1]
port = int(args[args.index("--port") + 1])
release = os.path.basename(os.path.dirname(os.getcwd()))
mode = ""
fault_file = os.environ.get("KDT_FAULT_FILE", "")
if fault_file and os.path.exists(fault_file):
    with open(fault_file) as handle:
        for line in handle:
            parts = line.split()
            if len(parts) == 2 and parts[1] == release and parts[0] in ("crash", "unhealthy", "slow"):
                mode = parts[0]
if mode == "crash":
    sys.exit(3)
time.sleep(float(os.environ.get("KDT_STARTUP_SECONDS", "3")) + (20 if mode == "slow" else 0))


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] == "/api/health":
            self._send(503, b'{"status":"unavailable"}') if mode == "unhealthy" else self._send(200, b'{"status":"ok"}')
        else:
            self._send(404, b'{"detail":"Not Found"}')

    def do_POST(self):
        self._send(404, b'{"detail":"Not Found"}')

    def log_message(self, *args):
        pass


http.server.ThreadingHTTPServer((host, port), Handler).serve_forever()
