"""Registration activation proves, in the database, that no unverified account exists.

Revision ID: 0021_registration_activation
Revises: 0020_v1_password_change

PostgreSQL only; one trigger function and one trigger (no table, column, grant on data or data).

Email-first registration activates by moving kalillac.account_registration_activation from
'unactivated' to 'active_zero_legacy'. Its CHECK constraint requires the recorded
legacy_unverified_count to be 0, but until now the database never counted: an operator could record 0
while unverified legacy accounts existed (registration startup would then refuse, but the account
would have passed the transition). From this revision the transition itself counts: a BEFORE UPDATE
trigger refuses the move to 'active_zero_legacy' while any account has no email_verified_at.

The count is exact under concurrency without locking users: every account insert (the provenance
guard) and every email_verified_at change (the identity guard) first takes FOR SHARE on the same
activation row, so the activation's row lock waits for them to commit and blocks new ones, and the
trigger's query then sees every committed account.

The function is not SECURITY DEFINER (it runs as the role updating the activation row, which only the
schema owner can), has a fixed search_path, is owned by the schema owner and executable by nobody else.
Owner decision 14 (2026-11-15) made this a precondition for dropping a V1 email-verification authority.
"""
from __future__ import annotations

import hashlib

from alembic import op

revision = "0021_registration_activation"
down_revision = "0020_v1_password_change"
branch_labels = depends_on = None

SIGNATURE = "kalillac.registration_activation_guard()"
TRIGGER = "trg_registration_activation_zero_legacy"

BODY = """
BEGIN
 IF NEW.state = 'active_zero_legacy' AND OLD.state IS DISTINCT FROM 'active_zero_legacy' THEN
  IF NEW.legacy_unverified_count IS DISTINCT FROM 0
     OR EXISTS (SELECT 1 FROM kalillac.users u WHERE u.email_verified_at IS NULL) THEN
   RAISE EXCEPTION USING ERRCODE = '23514', CONSTRAINT = 'ck_registration_activation_zero_legacy',
    MESSAGE = 'REGISTRATION_LEGACY_UNVERIFIED';
  END IF;
 END IF;
 RETURN NEW;
END
"""

FUNCTION = ("CREATE FUNCTION kalillac.registration_activation_guard() RETURNS trigger LANGUAGE plpgsql"
            " SET search_path = pg_catalog, pg_temp AS $activation_guard$" + BODY + "$activation_guard$")
CREATE_TRIGGER = (f"CREATE TRIGGER {TRIGGER} BEFORE UPDATE ON kalillac.account_registration_activation"
                  f" FOR EACH ROW EXECUTE FUNCTION {SIGNATURE}")
# PostgreSQL 16's rendering, compared exactly by the postcondition and the V1 classifier.
TRIGGER_DEFINITION = (f"CREATE TRIGGER {TRIGGER} BEFORE UPDATE ON kalillac.account_registration_activation"
                      " FOR EACH ROW EXECUTE FUNCTION kalillac.registration_activation_guard()")
BODY_SHA256 = hashlib.sha256(BODY.encode()).hexdigest()

ABSENT = """DO $registration_activation_absent$
BEGIN
 IF EXISTS (SELECT 1 FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'kalillac' AND p.proname = 'registration_activation_guard')
    OR EXISTS (SELECT 1 FROM pg_catalog.pg_trigger t WHERE t.tgname = '""" + TRIGGER + """') THEN
  RAISE EXCEPTION USING MESSAGE = '%(message)s';
 END IF;
END $registration_activation_absent$"""

POSTCONDITION = """DO $registration_activation_postcondition$
DECLARE
 keeper oid := (SELECT c.relowner FROM pg_catalog.pg_class c WHERE c.oid = 'kalillac.account_registration_activation'::pg_catalog.regclass);
 fn oid := pg_catalog.to_regprocedure('""" + SIGNATURE + """');
BEGIN
 IF fn IS NULL
  OR (SELECT p.proowner FROM pg_catalog.pg_proc p WHERE p.oid = fn) IS DISTINCT FROM keeper
  OR (SELECT p.prosecdef FROM pg_catalog.pg_proc p WHERE p.oid = fn)
  OR (SELECT p.proconfig FROM pg_catalog.pg_proc p WHERE p.oid = fn) IS DISTINCT FROM ARRAY['search_path=pg_catalog, pg_temp']
  OR (SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(p.prosrc, 'UTF8')), 'hex')
      FROM pg_catalog.pg_proc p WHERE p.oid = fn) IS DISTINCT FROM '""" + BODY_SHA256 + """'
  OR EXISTS (SELECT 1 FROM pg_catalog.pg_proc p, pg_catalog.aclexplode(p.proacl) x
             WHERE p.oid = fn AND x.grantee <> keeper)
  OR (SELECT p.proacl FROM pg_catalog.pg_proc p WHERE p.oid = fn) IS NULL
  OR (SELECT pg_catalog.count(*) FROM pg_catalog.pg_trigger t
      WHERE t.tgrelid = 'kalillac.account_registration_activation'::pg_catalog.regclass AND NOT t.tgisinternal) <> 1
  OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_trigger t
                 WHERE t.tgname = '""" + TRIGGER + """' AND t.tgfoid = fn AND t.tgenabled = 'O'
                 AND pg_catalog.pg_get_triggerdef(t.oid, false) = '""" + TRIGGER_DEFINITION + """')
 THEN RAISE EXCEPTION USING MESSAGE = 'REGISTRATION_ACTIVATION_GUARD_POSTCONDITION_INVALID';
 END IF;
END $registration_activation_postcondition$"""


def upgrade_statements():
    return [ABSENT % {"message": "REGISTRATION_ACTIVATION_GUARD_PREREQUISITE_INVALID"}, FUNCTION,
            f"REVOKE ALL ON FUNCTION {SIGNATURE} FROM PUBLIC", CREATE_TRIGGER, POSTCONDITION]


def downgrade_statements():
    return [POSTCONDITION, f"DROP TRIGGER {TRIGGER} ON kalillac.account_registration_activation",
            f"DROP FUNCTION {SIGNATURE}", ABSENT % {"message": "REGISTRATION_ACTIVATION_GUARD_DOWNGRADE_INVALID"}]


def upgrade():
    if op.get_context().dialect.name != "postgresql":
        return
    for statement in upgrade_statements():
        op.execute(statement)


def downgrade():
    if op.get_context().as_sql:
        raise RuntimeError("REGISTRATION_ACTIVATION_GUARD_OFFLINE_DOWNGRADE_UNSUPPORTED")
    if op.get_context().dialect.name != "postgresql":
        return
    for statement in downgrade_statements():
        op.execute(statement)
