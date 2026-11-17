"""O1 on real disposable PostgreSQL 16 after the operator transition: the owner report runs as the
runtime login under E1a's final grants, in a REPEATABLE READ, READ ONLY snapshot, and writes nothing."""
# ruff: noqa: F811  (fixtures imported from other test modules are used as parameters)
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

from test_v1_lifecycle_postgres import EMAIL, PASSWORDS, live, post, sign_in, writers  # noqa: F401
from test_v1_transition_postgres import world  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import owner_report  # noqa: E402


def test_the_owner_report_reads_as_the_runtime_login_and_writes_nothing(live):
    w = live
    assert post(w, "/api/account/register", {"email": EMAIL}).status_code == 202
    assert post(w, "/api/account/register/confirm", {"token": w.token(), "password": PASSWORDS[0]}).status_code == 200
    sign_in(w, PASSWORDS[0])
    out = io.StringIO()
    assert owner_report.run(["report", "--json"], factory=w.runtime_factory, out=out) == 0
    sections = json.loads(out.getvalue())["sections"]
    assert sections["accounts"] == {"total": 1, "verified": 1, "quarantined": 0, "deletion_requested": 0,
                                    "new_7_days": 1, "new_30_days": 1, "live_sessions": 1}
    assert sections["plan"] == {"paid_now": 0}
    assert sections["usage"]["today"]["chats"] == 0 and sections["saved_work"] == {
        "spaces": 0, "conversations": 0, "messages": 0}
    # Every section is readable by the runtime login under the final grants.
    assert not [name for name, value in sections.items() if "unavailable" in value]
    assert EMAIL not in out.getvalue()
    assert w.names["app"] not in writers(w)
