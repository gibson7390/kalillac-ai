-- Read-only catalog checks for migration 0004 on kalillac_staging.
-- Run as a role able to read pg_catalog (for example the postgres superuser):
--   sudo -u postgres psql -d kalillac_staging -X -v ON_ERROR_STOP=1 \
--        -f candidate/tools/verify_billing_schema.sql
-- Every query only reads. Expected results are noted above each query.

\echo '== database and revision (expect kalillac_staging / 0004_account_billing)'
SELECT current_database() AS database, version_num AS revision
FROM kalillac.alembic_version;

\echo '== table owners (expect all six tables + alembic_version owned by kalillac_staging_owner)'
SELECT tablename, tableowner
FROM pg_tables
WHERE schemaname = 'kalillac'
ORDER BY tablename;

\echo '== account_billing columns (expect 17 columns, all nullable except user_id, cancel_at_period_end, created_at, updated_at)'
\echo '   user_id, stripe_customer_id, stripe_subscription_id, stripe_price_id, subscription_status,'
\echo '   cancel_at_period_end, current_period_end, checkout_attempt_id, checkout_customer_id,'
\echo '   checkout_attempt_created_at, checkout_price_id, checkout_success_url, checkout_cancel_url,'
\echo '   stripe_checkout_session_id, checkout_session_expires_at, created_at, updated_at'
SELECT column_name, data_type, character_maximum_length, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = 'kalillac' AND table_name = 'account_billing'
ORDER BY ordinal_position;

\echo '== account_billing column count (expect 17)'
SELECT count(*) AS account_billing_columns
FROM information_schema.columns
WHERE table_schema = 'kalillac' AND table_name = 'account_billing';

\echo '== stripe_webhook_events columns (expect event_id, event_type, processed_at only)'
SELECT column_name, data_type, character_maximum_length, is_nullable
FROM information_schema.columns
WHERE table_schema = 'kalillac' AND table_name = 'stripe_webhook_events'
ORDER BY ordinal_position;

\echo '== constraints (expect pk/fk(CASCADE)/3 unique on account_billing; pk on stripe_webhook_events)'
SELECT conrelid::regclass AS table_name, conname, contype,
       pg_get_constraintdef(oid) AS definition
FROM pg_constraint
WHERE conrelid IN ('kalillac.account_billing'::regclass,
                   'kalillac.stripe_webhook_events'::regclass)
ORDER BY 1, 2;

\echo '== forbidden columns anywhere in schema kalillac (expect zero rows)'
SELECT table_name, column_name
FROM information_schema.columns
WHERE table_schema = 'kalillac'
  AND column_name ~ '(card|payment|invoice|amount|cents|currency|payload|raw|secret|iban|cvc|last4|message|history|transcript|prompt|reply)';

\echo '== table grants on the new tables (expect kalillac_staging_app: SELECT, INSERT, UPDATE, DELETE only, plus owner privileges)'
SELECT table_name, grantee, string_agg(privilege_type, ', ' ORDER BY privilege_type) AS privileges
FROM information_schema.role_table_grants
WHERE table_schema = 'kalillac'
  AND table_name IN ('account_billing', 'stripe_webhook_events')
GROUP BY table_name, grantee
ORDER BY table_name, grantee;

\echo '== runtime role effective privileges (expect t,t,t,t,f,f,f for both tables)'
SELECT t.tbl,
       has_table_privilege('kalillac_staging_app', t.tbl, 'SELECT')     AS sel,
       has_table_privilege('kalillac_staging_app', t.tbl, 'INSERT')     AS ins,
       has_table_privilege('kalillac_staging_app', t.tbl, 'UPDATE')     AS upd,
       has_table_privilege('kalillac_staging_app', t.tbl, 'DELETE')     AS del,
       has_table_privilege('kalillac_staging_app', t.tbl, 'TRUNCATE')   AS trunc,
       has_table_privilege('kalillac_staging_app', t.tbl, 'REFERENCES') AS refs,
       has_table_privilege('kalillac_staging_app', t.tbl, 'TRIGGER')    AS trig
FROM (VALUES ('kalillac.account_billing'), ('kalillac.stripe_webhook_events')) AS t(tbl);

\echo '== runtime role on alembic_version and schema (expect all f)'
SELECT has_table_privilege('kalillac_staging_app', 'kalillac.alembic_version', 'SELECT') AS av_select,
       has_table_privilege('kalillac_staging_app', 'kalillac.alembic_version', 'INSERT') AS av_insert,
       has_table_privilege('kalillac_staging_app', 'kalillac.alembic_version', 'UPDATE') AS av_update,
       has_schema_privilege('kalillac_staging_app', 'kalillac', 'CREATE')               AS schema_create;

\echo '== default privileges (expect zero rows)'
SELECT defaclrole::regrole, defaclnamespace::regnamespace, defaclobjtype, defaclacl
FROM pg_default_acl;

\echo '== row counts in new tables (expect 0 and 0 before the harness; 0 and 0 after its cleanup)'
SELECT (SELECT count(*) FROM kalillac.account_billing)       AS billing_rows,
       (SELECT count(*) FROM kalillac.stripe_webhook_events) AS webhook_event_rows;
