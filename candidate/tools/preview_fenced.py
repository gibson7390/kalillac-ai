"""Run one of the development preview's processes inside its network fence.

    python3 -B tools/preview_fenced.py tools/local_staging_app.py --host 127.0.0.1 --port 8021
    python3 -B tools/preview_fenced.py tools/local_staging_web.py --webroot ... --upstream http://127.0.0.1:8021 ...

tools/dev_preview.py starts the application and the panel this way: a socket patch made in the manager does not
reach its child processes, so each child installs the fence itself (tools/preview_guard.py) before any of its
own code runs. The script then runs exactly as `python3 tools/<script> ...` would, as __main__.

Before anything runs, the environment is checked: a setting that names local staging's ports, or a database that
is not this machine's preview database, stops the process.
"""
from __future__ import annotations

import os
from pathlib import Path
import runpy
import sys

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import preview_guard  # noqa: E402

DATABASE_PORT = "KALILLAC_PREVIEW_DATABASE_PORT"


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        sys.exit("usage: preview_fenced.py SCRIPT [ARGS...]")
    script = Path(argv[0]).resolve()
    expected = os.environ.get(DATABASE_PORT)
    preview_guard.check_environment(os.environ, database_port=int(expected) if expected and expected.isdigit() else None)
    preview_guard.install(strict=False)
    print(f"preview fence installed in {script.name} (pid {os.getpid()}): refusing ports "
          f"{sorted(preview_guard.protected_ports())}", file=sys.stderr, flush=True)
    sys.argv = [str(script), *argv[1:]]
    sys.path[0] = str(script.parent)          # As when the script is run directly.
    runpy.run_path(str(script), run_name="__main__")


if __name__ == "__main__":
    main()
