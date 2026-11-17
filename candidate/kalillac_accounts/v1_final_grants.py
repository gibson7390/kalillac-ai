"""E1a: the runtime login's final account grants (V1-02E), as prepared statements and exact checks.

Nothing here runs a statement on its own. The operator transition (E2, tools/v1_security_transition.py)
applies E1A_STATEMENTS inside its single transaction, with ingress closed, together with moving the
activation latch to active; its rollback command applies ROLLBACK_STATEMENTS the same way. Startup never
changes a grant.

Legacy (before E1a, as 0001, 0008, 0009 and 0011 left them) and final (after E1a) rights of the runtime
login R on the account tables, each checked exactly by runtime_account_state together with
registration_startup.effective_privileges (which covers users):

| table | legacy | final |
|---|---|---|
| users | SELECT, DELETE; INSERT (identity columns); UPDATE (4 columns) | SELECT on every column but password_hash; UPDATE (row_lock_anchor) only |
| account_sessions | SELECT, INSERT, UPDATE, DELETE | SELECT |
| account_recovery_tokens | SELECT, INSERT, UPDATE | SELECT |
| account_email_verification_tokens | SELECT, INSERT, UPDATE | nothing |
| account_security_activation (the latch) | nothing | SELECT (the startup gate reads it as R) |

Entitlements and billing (E1b) are unchanged here: their writes move behind a reviewed path first.
"""
from __future__ import annotations

LEGACY, FINAL = "legacy", "final"
USER_INSERT_COLUMNS = ("id", "email", "password_hash", "created_at", "updated_at", "is_active", "email_verified_at",
                       "registration_intent_id", "quarantined_at", "sessions_valid_after")
USER_UPDATE_COLUMNS = ("password_hash", "updated_at", "is_active", "email_verified_at")
TABLE_RIGHTS = {
    "account_sessions": {LEGACY: {"SELECT", "INSERT", "UPDATE", "DELETE"}, FINAL: {"SELECT"}},
    "account_recovery_tokens": {LEGACY: {"SELECT", "INSERT", "UPDATE"}, FINAL: {"SELECT"}},
    "account_email_verification_tokens": {LEGACY: {"SELECT", "INSERT", "UPDATE"}, FINAL: set()},
    # The startup gate classifies as R and must read the latch it qualifies.
    "account_security_activation": {LEGACY: set(), FINAL: {"SELECT"}},
}
RIGHTS = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


class GrantStateInvalid(RuntimeError):
    """R's rights are neither exactly the legacy nor exactly the final state. Carries no detail."""

    def __init__(self):
        super().__init__("V1 runtime grants are not in a reviewed state.")


def _role(name):
    from kalillac_db.migration_roles import validate_role_name
    return validate_role_name(name, "runtime role")


def e1a_statements(runtime, user_columns):
    """Legacy to final. user_columns: every users column, in order (read from the catalog by the caller)."""
    runtime = _role(runtime)
    readable = ", ".join(c for c in user_columns if c != "password_hash")
    if "row_lock_anchor" not in user_columns or "password_hash" not in user_columns:
        raise GrantStateInvalid()
    return [
        f"REVOKE SELECT, INSERT, UPDATE, DELETE ON kalillac.users FROM {runtime}",
        f"REVOKE INSERT ({', '.join(USER_INSERT_COLUMNS)}), UPDATE ({', '.join(USER_UPDATE_COLUMNS)})"
        f" ON kalillac.users FROM {runtime}",
        f"GRANT SELECT ({readable}), UPDATE (row_lock_anchor) ON kalillac.users TO {runtime}",
        f"REVOKE INSERT, UPDATE, DELETE ON kalillac.account_sessions FROM {runtime}",
        f"REVOKE INSERT, UPDATE ON kalillac.account_recovery_tokens FROM {runtime}",
        f"REVOKE SELECT, INSERT, UPDATE ON kalillac.account_email_verification_tokens FROM {runtime}",
        f"GRANT SELECT ON kalillac.account_security_activation TO {runtime}",
    ]


def rollback_statements(runtime, user_columns):
    """Final back to exactly legacy."""
    runtime = _role(runtime)
    readable = ", ".join(c for c in user_columns if c != "password_hash")
    return [
        f"REVOKE SELECT ({readable}), UPDATE (row_lock_anchor) ON kalillac.users FROM {runtime}",
        f"GRANT SELECT, DELETE ON kalillac.users TO {runtime}",
        f"GRANT INSERT ({', '.join(USER_INSERT_COLUMNS)}), UPDATE ({', '.join(USER_UPDATE_COLUMNS)})"
        f" ON kalillac.users TO {runtime}",
        f"GRANT INSERT, UPDATE, DELETE ON kalillac.account_sessions TO {runtime}",
        f"GRANT INSERT, UPDATE ON kalillac.account_recovery_tokens TO {runtime}",
        f"GRANT SELECT, INSERT, UPDATE ON kalillac.account_email_verification_tokens TO {runtime}",
        f"REVOKE SELECT ON kalillac.account_security_activation FROM {runtime}",
    ]


def user_columns(session):
    from sqlalchemy import text
    return [r[0] for r in session.execute(text(
        "SELECT a.attname FROM pg_catalog.pg_attribute a WHERE a.attrelid = 'kalillac.users'::pg_catalog.regclass"
        " AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum"))]


def runtime_account_state(session, runtime):
    """LEGACY or FINAL for R's rights on the session and token tables, checked exactly (effective rights,
    so PUBLIC and inherited grants count), and agreeing with users' state from effective_privileges.
    Raises GrantStateInvalid otherwise."""
    from sqlalchemy import text
    runtime = _role(runtime)
    states = set()
    for table, expected in TABLE_RIGHTS.items():
        held = {right for right in RIGHTS if session.execute(text(
            "SELECT pg_catalog.has_table_privilege(:p, :t, :r)"), {"p": runtime, "t": "kalillac." + table,
                                                                    "r": right}).scalar_one()}
        columns = {right for right in ("SELECT", "INSERT", "UPDATE", "REFERENCES") if session.execute(text(
            "SELECT pg_catalog.has_any_column_privilege(:p, :t, :r)"), {"p": runtime, "t": "kalillac." + table,
                                                                         "r": right}).scalar_one()}
        matched = [state for state, rights in expected.items() if held == rights and columns == rights & set(
            ("SELECT", "INSERT", "UPDATE", "REFERENCES"))]
        if len(matched) != 1:
            raise GrantStateInvalid()
        states.add(matched[0])
    if len(states) != 1:
        raise GrantStateInvalid()
    return states.pop()
