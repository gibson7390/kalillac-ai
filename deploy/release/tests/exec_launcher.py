"""Disposable-test stand-in for the root-only production launcher (launch-production.py).

Like production, it replaces itself with the application server: os.execv keeps the PID systemd
started (the unit's MainPID), and the process then carries the server's argv:

    <venv>/bin/python3 <venv>/bin/uvicorn app_fastapi_candidate:api --host 127.0.0.1 --port <port> --workers 1

<venv>/bin/uvicorn is stub_uvicorn.py in the tests. KDT_VENV and KDT_PORT come from the unit.
Before exec it adds KDT_LAUNCHER_MARK=1 to the environment: a launcher transformation the test
environment approves through LAUNCHER_ENV_VARS.

Faults, for tests only, read from KDT_FAULT_FILE (outside every release directory):
  badargs <release>   exec the server with non-approved arguments (--workers 2);
  plantpyc <release>  just before exec, write a metadata-valid but altered
                      __pycache__/kalillac_server.<tag>.pyc into the release (header = the source's
                      mtime and size; code = the source plus a line writing KDT_TAMPER_MARK), i.e.
                      unverified bytecode appearing after the last check and before start-up.
"""
import importlib._bootstrap_external as bootstrap
import importlib.util
import os

venv = os.environ["KDT_VENV"]
release = os.path.basename(os.path.dirname(os.getcwd()))
faults = set()
fault_file = os.environ.get("KDT_FAULT_FILE", "")
if fault_file and os.path.exists(fault_file):
    with open(fault_file) as handle:
        faults = {tuple(line.split()) for line in handle}
if ("plantpyc", release) in faults:
    source = os.path.abspath("kalillac_server.py")
    stat = os.stat(source)
    with open(source) as handle:
        text = handle.read()
    text += "\nimport pathlib as _kdt\n_kdt.Path(%r).write_text('tampered bytecode executed')\n" % os.environ["KDT_TAMPER_MARK"]
    code = compile(text, source, "exec", dont_inherit=True)
    target = importlib.util.cache_from_source(source)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "wb") as handle:
        handle.write(bootstrap._code_to_timestamp_pyc(code, int(stat.st_mtime), stat.st_size))
workers = "2" if ("badargs", release) in faults else "1"
os.environ["KDT_LAUNCHER_MARK"] = "1"
python = venv + "/bin/python3"
os.execv(python, [python, venv + "/bin/uvicorn", "app_fastapi_candidate:api",
                  "--host", "127.0.0.1", "--port", os.environ["KDT_PORT"], "--workers", workers])
