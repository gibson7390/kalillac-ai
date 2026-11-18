"""Local staging only: start the real application with the controlled launcher, after installing the
provider simulation (tools/local_staging_sim.py) for every provider without real test credentials.

    python3 -B tools/local_staging_app.py --host 127.0.0.1 --port 8011

Run by tools/local_staging.py as the service identity, with the deployment's environment. The launcher
(kalillac_server.py) is unchanged: it takes its singleton lock first and only then imports the application.
"""
from __future__ import annotations

from pathlib import Path
import sys

TOOLS = Path(__file__).resolve().parent
sys.path[:0] = [str(TOOLS.parent), str(TOOLS)]

import local_staging_sim  # noqa: E402

if __name__ == "__main__":
    local_staging_sim.install()
    import kalillac_server
    sys.exit(kalillac_server.main(sys.argv[1:]))
