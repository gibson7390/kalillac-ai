"""Disposable-test stand-in for the root-only production launcher (launch-production.py).

Serves GET /api/health -> {"status":"ok"} on 127.0.0.1:$KDT_PORT from the release it was started in
(cwd = <release>/candidate); every other path is 404. It starts listening after KDT_STARTUP_SECONDS,
like an application import.

Faults, for tests only, are read once at start-up from the file named by KDT_FAULT_FILE (outside every
release directory, so releases stay pristine): lines "<mode> <release directory name>", where mode is
crash (exit before listening), unhealthy (health answers 503) or slow (20 s extra start-up).
"""
import http.server
import os
import sys
import time

release = os.path.basename(os.path.dirname(os.getcwd()))
mode = ""
fault_file = os.environ.get("KDT_FAULT_FILE", "")
if fault_file and os.path.exists(fault_file):
    with open(fault_file) as handle:
        for line in handle:
            parts = line.split()
            if len(parts) == 2 and parts[1] == release:
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
            if mode == "unhealthy":
                self._send(503, b'{"status":"unavailable"}')
            else:
                self._send(200, b'{"status":"ok"}')
        else:
            self._send(404, b'{"detail":"Not Found"}')

    def do_POST(self):
        self._send(404, b'{"detail":"Not Found"}')

    def log_message(self, *args):
        pass


http.server.ThreadingHTTPServer(("127.0.0.1", int(os.environ["KDT_PORT"])), Handler).serve_forever()
