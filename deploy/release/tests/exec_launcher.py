"""Disposable-test stand-in for the root-only production launcher (launch-production.py).

Like production, it replaces itself with the application server: os.execv keeps the PID systemd
started (the unit's MainPID), and the process then carries the server's argv:

    <venv>/bin/python3 <venv>/bin/uvicorn app_fastapi_candidate:api --host 127.0.0.1 --port <port> --workers 1

<venv>/bin/uvicorn is stub_uvicorn.py in the tests. KDT_VENV and KDT_PORT come from the unit.

Fault, for tests only, read from KDT_FAULT_FILE (outside every release directory, so releases stay
pristine): a line "badargs <release directory name>" makes this launcher exec the server with
non-approved arguments (--workers 2). The server itself handles crash/unhealthy/slow.
"""
import os

venv = os.environ["KDT_VENV"]
release = os.path.basename(os.path.dirname(os.getcwd()))
workers = "1"
fault_file = os.environ.get("KDT_FAULT_FILE", "")
if fault_file and os.path.exists(fault_file):
    with open(fault_file) as handle:
        if any(line.split() == ["badargs", release] for line in handle):
            workers = "2"
python = venv + "/bin/python3"
os.execv(python, [python, venv + "/bin/uvicorn", "app_fastapi_candidate:api",
                  "--host", "127.0.0.1", "--port", os.environ["KDT_PORT"], "--workers", workers])
