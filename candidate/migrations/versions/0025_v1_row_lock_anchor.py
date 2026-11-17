"""Add the inert row-lock anchor to users (V1-02E, E1 prerequisite), granting nothing.

Revision ID: 0025_v1_row_lock_anchor
Revises: 0024_v1_recovery_credential

PostgreSQL only. PostgreSQL allows SELECT ... FOR UPDATE (and every row-lock mode) only to a role with
UPDATE privilege on at least one column of the table. After E1 the runtime role R keeps account-row
locks (V1-01's authorization, billing, Spaces) but must not change any account state, so its one UPDATE
right will be on this column, which a CHECK constraint holds NULL: the only update R can then make sets
NULL to NULL.

This revision adds the column (nullable, no default: metadata only, no table rewrite) and its constraint,
and grants nothing. The E1 grant change is applied only by the operator transition (E2). Downgrade drops
both while no role holds any right on the column.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic import op

revision = "0025_v1_row_lock_anchor"
down_revision = "0024_v1_recovery_credential"
branch_labels = depends_on = None

COLUMN = "row_lock_anchor"
CONSTRAINT = "ck_users_row_lock_anchor_null"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_credential = _load("v1_0024_for_0025", "0024_v1_recovery_issuance_and_credential.py")

ABSENT = """DO $v1_row_lock_anchor_absent$
BEGIN
 IF EXISTS (SELECT 1 FROM pg_catalog.pg_attribute a WHERE a.attrelid = 'kalillac.users'::pg_catalog.regclass
            AND a.attname = 'row_lock_anchor')
    OR EXISTS (SELECT 1 FROM pg_catalog.pg_constraint c WHERE c.conname = 'ck_users_row_lock_anchor_null') THEN
  RAISE EXCEPTION USING MESSAGE = %(message)s;
 END IF;
END $v1_row_lock_anchor_absent$"""

# Exactly one such column, a nullable smallint with no default, no generation or identity, no collation,
# no column grant to anyone; exactly one validated, immediate CHECK holding it NULL.
POSTCONDITION = """DO $v1_row_lock_anchor_postcondition$
BEGIN
 IF (SELECT pg_catalog.count(*) FROM pg_catalog.pg_attribute a
     WHERE a.attrelid = 'kalillac.users'::pg_catalog.regclass AND a.attname = 'row_lock_anchor'
     AND NOT a.attisdropped AND a.attnum > 0
     AND pg_catalog.format_type(a.atttypid, a.atttypmod) = 'smallint' AND NOT a.attnotnull AND NOT a.atthasdef
     AND a.attidentity = '' AND a.attgenerated = '' AND a.attcollation = 0 AND a.attacl IS NULL) <> 1
 OR (SELECT pg_catalog.count(*) FROM pg_catalog.pg_constraint c
     WHERE c.conrelid = 'kalillac.users'::pg_catalog.regclass AND c.conname = 'ck_users_row_lock_anchor_null'
     AND c.contype = 'c' AND c.convalidated AND NOT c.condeferrable
     AND pg_catalog.pg_get_constraintdef(c.oid, false) = 'CHECK ((row_lock_anchor IS NULL))') <> 1
 OR (SELECT pg_catalog.count(*) FROM pg_catalog.pg_constraint c
     WHERE c.conrelid = 'kalillac.users'::pg_catalog.regclass
     AND c.conkey @> ARRAY[(SELECT a.attnum FROM pg_catalog.pg_attribute a
                            WHERE a.attrelid = 'kalillac.users'::pg_catalog.regclass
                            AND a.attname = 'row_lock_anchor')]::smallint[]) <> 1
 THEN RAISE EXCEPTION USING MESSAGE = %(message)s;
 END IF;
END $v1_row_lock_anchor_postcondition$"""


def _names():
    return _credential._names()


def _preflight(names, message):
    """0024's exact catalog, as 0024 proves it, and no anchor in any form."""
    return [_credential._postcondition(names).replace("'V1_RECOVERY_CREDENTIAL_POSTCONDITION_INVALID'",
                                              "'" + message + "'"),
            ABSENT % {"message": "'" + message + "'"}]


def _postcondition(message="V1_ROW_LOCK_ANCHOR_POSTCONDITION_INVALID"):
    return POSTCONDITION % {"message": "'" + message + "'"}


def upgrade_statements(names):
    return [
        *_preflight(names, "V1_ROW_LOCK_ANCHOR_PREREQUISITE_INVALID"),
        f"ALTER TABLE kalillac.users ADD COLUMN {COLUMN} smallint",
        f"ALTER TABLE kalillac.users ADD CONSTRAINT {CONSTRAINT} CHECK ({COLUMN} IS NULL)",
        _postcondition(),
        # 0024's catalog still holds exactly (the column changes nothing it proves).
        _credential._postcondition(names).replace("'V1_RECOVERY_CREDENTIAL_POSTCONDITION_INVALID'",
                                                  "'V1_ROW_LOCK_ANCHOR_POSTCONDITION_INVALID'"),
    ]


def downgrade_statements(names):
    return [
        _postcondition(),
        f"ALTER TABLE kalillac.users DROP CONSTRAINT {CONSTRAINT}",
        f"ALTER TABLE kalillac.users DROP COLUMN {COLUMN}",
        *_preflight(names, "V1_ROW_LOCK_ANCHOR_DOWNGRADE_INVALID"),
    ]


def upgrade():
    if op.get_context().dialect.name != "postgresql":
        return
    for statement in upgrade_statements(_names()):
        op.execute(statement)


def downgrade():
    if op.get_context().as_sql:
        raise RuntimeError("V1_ROW_LOCK_ANCHOR_OFFLINE_DOWNGRADE_UNSUPPORTED")
    if op.get_context().dialect.name != "postgresql":
        return
    for statement in downgrade_statements(_names()):
        op.execute(statement)
