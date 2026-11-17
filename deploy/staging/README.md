# Staging runbook: a new Kalillac AI staging environment

For the operator who sets up staging on the owner's authorization, with root on the staging host.
Nothing here is done by a development session, and nothing here touches production: no production
file, unit, database, role, key or flag is read or changed.

The design and the decisions it needs are in `docs/STAGING_ENVIRONMENT_DESIGN.md`. This document is
the procedure. The result is the real product with accounts, Premium billing in Stripe **test** mode,
files, reasoning modes and Financial Safeguards on, on synthetic data.

## What is proved before you start, and what is not

`candidate/tools/verify_provisioned_boot.py` runs steps 4, 7 and 8 of this runbook with the real
commands in CI on every change (root, PostgreSQL 16, synthetic values): it provisions a database,
runs the operator transition as an unprivileged service identity, starts the application with the
controlled launcher from `settings.env.template`, and checks it over HTTP. The Premium journey itself
(register, subscribe, Premium, cancel, deletion request) is proved against the real application by
`candidate/tests/test_v1_premium_journey_postgres.py`, with Stripe faked.

Not proved by CI, so check each as you go: the `systemd-run` and unit forms below (CI builds the same
user, environment and credentials without systemd), Nginx, Cloudflare Access, and the real providers
(Stripe test mode, Resend, OpenAI, Brave, Tavily).

## Before you start: the owner's decisions and values

| Needed | From |
|---|---|
| Same host as production or a separate one; hostname; port; what to do with any old `kalillac-v31-staging` | `docs/STAGING_ENVIRONMENT_DESIGN.md` section 5 |
| Every `<OWNER_...>` value in `settings.env.template`: daily response allowances, provider attempt ceilings, spending ceilings, which reasoning modes each tier gets, DEEP's extra output tokens | the owner (blueprint decisions 9, 10 and the budget proposal, `docs/FINANCE_BUDGET_PROPOSAL.md`) |
| The Stripe **test-mode** Price id, secret key and webhook secret | `docs/STRIPE_TEST_MODE_SETUP.md` |
| Staging's own OpenAI, Brave, Tavily and Resend keys, each with its own spend limit | the owner |
| `<PRICE_...>` values | each provider's published price on the day (`candidate/FINANCIAL_SAFEGUARDS.md` says what each one means) |
| `<PROD_...>` values | production's current request and transport limits, read from production's unit by an operator |

Names used below; choose them once and use them everywhere:

| Placeholder | Meaning | Proposed |
|---|---|---|
| `UNIT` | systemd unit and state directory name. Never production's unit name | `kalillac-staging-v1` |
| `ID` | deployment id | `kalillac-staging-v1` |
| `DIR` | configuration directory | `/etc/kalillac-staging-v1` |
| `USER`, `GROUP` | the unprivileged service user and its group | `kalillac-staging` |
| `REL` | the release directory (a Git checkout of exactly the release commit) | `/opt/kalillac-staging-v1/release-<short sha>` |
| `VENV` | staging's own Python environment | `/opt/kalillac-staging-v1/venv` |
| `SHA` | the release's full 40-character commit | |
| `PORT` | 8002, or 8003 if the check in step 1 finds 8002 in use | |

## 1. Read-only checks on the host

```
sudo ss -ltnp '( sport = :8002 or sport = :8003 )'
systemctl list-units --all 'kalillac*'
sudo nginx -T 2>/dev/null | grep -n 'proxy_pass'
sudo -u postgres psql -Atc "SELECT datname, datacl FROM pg_database WHERE NOT datistemplate"
```

Use 8002 only if nothing is bound to it and no unit or Nginx site names it. If staging will share
production's PostgreSQL cluster, read the last command's output: a database whose `datacl` is empty
or grants `=Tc/` (PUBLIC) lets any login of the cluster open a session on it, staging's included
(they would hold no right inside it). **Recommended instead: staging's own cluster**
(`sudo pg_createcluster 16 staging -p 5433`, no cost), which shares no role with production at all.
`pg_hba.conf` must authenticate `127.0.0.1` with `scram-sha-256` (the Debian and Ubuntu default): the
application refuses a registration login that did not authenticate with its own SCRAM password.

## 2. User, code and Python environment

```
sudo adduser --system --group --no-create-home kalillac-staging
sudo git clone <repository> REL && sudo git -C REL checkout --detach SHA
sudo python3.12 -m venv VENV
sudo VENV/bin/pip install -r REL/requirements-production-lock.txt -r REL/requirements-database.txt \
     -r REL/requirements-billing.txt -r REL/requirements-files.txt
```

`REL` and `VENV` stay owned by root and readable by `USER`. The large CJK document fonts are fetched
and verified separately (`candidate/FILE_GENERATION.md`).

## 3. A temporary administrative login, and the role names

The provisioning tool and the operator transition each need one administrative connection.

```
sudo -u postgres psql        # on staging's cluster
  CREATE ROLE kalillac_staging_provisioner LOGIN SUPERUSER;
  \password kalillac_staging_provisioner       -- psql hashes it; nothing reaches a log or history
sudo install -o root -g root -m 0600 /dev/stdin /root/kalillac-staging-admin-url
  postgresql+psycopg://kalillac_staging_provisioner:<password>@127.0.0.1:<cluster port>/postgres
  (Ctrl-D)
```

Staging's roles have their own names. The tool refuses a role or database it did not create, so it
cannot touch production's even on a shared cluster. Put these in `/root/kalillac-staging-roles.env`:

```
export KALILLAC_MIGRATION_OWNER_ROLE=kalillac_stg_owner
export KALILLAC_MIGRATION_APP_ROLE=kalillac_stg_app
export KALILLAC_MIGRATION_REGISTRATION_ROLE=kalillac_stg_registration
export KALILLAC_V1_SECURITY_SERVICE_ROLE=kalillac_stg_v1_service
export KALILLAC_V1_SECURITY_EXECUTOR_ROLE=kalillac_stg_v1_executor
export KALILLAC_V1_SECURITY_OPERATOR_ROLE=kalillac_stg_v1_operator
```

## 4. Provision the database and the protected files

As root, from `REL/candidate`:

```
. /root/kalillac-staging-roles.env
P="VENV/bin/python3 -B tools/provision_deployment.py"
A="--deployment-id ID --release-sha SHA --database kalillac_staging_v1 --db-host 127.0.0.1 --db-port <cluster port>
   --migration-login kalillac_stg_migrate --admin-url-file /root/kalillac-staging-admin-url
   --directory DIR --service-group GROUP"
$P plan $A                 # reports what exists; changes nothing. Expect every role and the database "absent"
$P provision $A --apply
```

It creates the seven roles and the database, migrates to the release's head, writes
`DIR/protected/` (six files, root:GROUP 0440), `DIR/private/` (the runtime login's password and the
migration login, root 0600) and `DIR/deployment.env`, records the registration activation, and
verifies the result. Exit 2 is a refusal with nothing changed; exit 1 before the activation is safe
to repeat. The tool's own text (`provision_deployment.py`, the module docstring) is the reference.

## 5. Settings and secrets

```
sudo install -o root -g root -m 0644 REL/deploy/staging/settings.env.template DIR/settings.env
sudoedit DIR/settings.env        # replace every <PLACEHOLDER>; KALILLAC_FILES_ROOT=/var/lib/UNIT/files
sudo install -o root -g root -m 0600 /dev/stdin DIR/private/stripe_secret_key        # the sk_test_ key
sudo install -o root -g root -m 0600 /dev/stdin DIR/private/stripe_webhook_secret    # the whsec_ secret
sudo install -o root -g root -m 0600 /dev/stdin DIR/private/provider-keys.env
  OPENAI_API_KEY=...
  BRAVE_API_KEY=...
  TAVILY_API_KEY=...
  KALILLAC_RESEND_API_KEY=...
```

`grep -c '<' DIR/settings.env` must print 0. Never a `sk_live_` key: the application refuses a key
whose mode is not `KALILLAC_STRIPE_MODE=test`.

## 6. The unit (not started yet)

Copy `kalillac-staging.service.template` to `/etc/systemd/system/UNIT.service`, replace its
placeholders (`MEMORY_MAX` and `CPU_QUOTA` from the owner's answer on host headroom), then
`sudo systemctl daemon-reload`. Do not enable or start it.

## 7. The V1 account-security transition

The operator transition gives the runtime login its final grants and the two service roles their
logins, in one transaction, and binds the database to this deployment and release. It runs as the
service user, in the service's own context:

```
sudo install -o root -g GROUP -m 0440 /dev/stdin DIR/protected/transition-admin-url
  postgresql+psycopg://kalillac_staging_provisioner:<password>@127.0.0.1:<cluster port>/kalillac_staging_v1
  (Ctrl-D)
T="systemd-run --wait --collect --pipe -p User=USER -p Group=GROUP -p WorkingDirectory=REL/candidate
   -p StateDirectory=UNIT -p EnvironmentFile=DIR/deployment.env -p EnvironmentFile=DIR/settings.env
   -p LoadCredential=kalillac_db_password:DIR/private/kalillac_db_password
   -E KALILLAC_V1_TRANSITION_ADMIN_URL_FILE=DIR/protected/transition-admin-url
   -E KALILLAC_V1_OPERATOR_RECORD_FILE=/var/lib/UNIT/operator-record.jsonl
   VENV/bin/python3 -B tools/v1_security_transition.py"
sudo $T check                                   # {"state": "prepared", "version": "<head>"}
sudo $T activate --release-sha SHA              # a dry run: every step runs and is rolled back
sudo $T activate --release-sha SHA --apply      # {"applied": true, ..., "to": "active"}
sudo rm DIR/protected/transition-admin-url /root/kalillac-staging-admin-url
sudo -u postgres psql -c 'DROP ROLE kalillac_staging_provisioner'
```

`SHA` must equal `KALILLAC_RELEASE_SHA` in `settings.env`: the application starts only against a
database activated for its own deployment id and release. To undo: `... rollback --apply` (the same
context, with the administrative file back in place), never by starting an older application.

## 8. Start, and check

```
sudo systemctl start UNIT
systemctl status UNIT --no-pager                 # active; the journal shows SINGLETON_LOCK_ACQUIRED and REGISTRATION_V2_READY
curl -s http://127.0.0.1:PORT/api/health          # {"status":"ok"}
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:PORT/api/account/me     # 401
```

A start that fails names the setting at fault in the journal, never its value. Enable the unit only
once the journey below passes.

## 9. The web root, Nginx and access

```
(cd REL/site && npm ci && npm run build)
python3 REL/deploy/staging/build_webroot.py --site REL/site/dist/public --frontend REL/frontend \
    --out /var/www/UNIT --marks accounts,files,reasoning,spaces
```

For Nginx, start from production's own site file (`nginx -T`) and change only: `server_name` (the
staging hostname), `root` (`/var/www/UNIT`), the upstream port (`PORT`), and add
`add_header X-Robots-Tag "noindex, nofollow" always;`. Two limits must allow what staging turns on:
`client_max_body_size` of at least 11m (file uploads are bounded at 10 MiB by the application) and a
`proxy_read_timeout` of at least 620s on `/api/` (a file request may take up to 600 seconds).
Publish the hostname only behind **Cloudflare Access** limited to the owner; never link it from the
public site. Then add the Stripe test-mode webhook endpoint for the hostname
(`docs/STRIPE_TEST_MODE_SETUP.md`).

## 10. The journey to accept staging

In a browser, as the owner, with Stripe's test cards only:

| Step | Expect |
|---|---|
| Create an account (email-first), follow the emailed link, set a password | signed in; the account page shows Free |
| Chat as Free; choose a reasoning mode the Free tier does not include | answers; that mode is refused with the plan-aware message |
| Upgrade, pay with `4242 4242 4242 4242` | back on the account page; Premium within seconds of the webhook |
| Chat as Premium with that mode; ask for a PDF, a Word file, a spreadsheet and a deck; edit one | each file downloads and opens; the edit keeps the rest |
| Account page: sign out other sessions, change the password | other browsers are signed out; the old password no longer works |
| Manage billing: cancel in the Stripe portal | the account page shows the cancellation and the period end |
| Recover the password by email | the link works once; every session ends |
| Request deletion of the account | signed out; sign-in is refused |

Spaces are offered only to the accounts in `KALILLAC_SPACES_BETA_ACCOUNT_IDS`. Before the journey,
create the owner's account, read its id
(`sudo -u postgres psql -d kalillac_staging_v1 -Atc "SELECT id FROM kalillac.users WHERE email = '<the owner's email>'"`),
put it in `settings.env` and restart the unit. Then, in the chat: Spaces → create a Space and a
conversation, send a message, reload the page and open it again (it is still there), and leave it.

Operator support (suspend, reinstate, a deletion request, a bounded export) is
`tools/v1_account_support.py`, run in the same `systemd-run` context as step 7 without the
administrative file: it uses `KALILLAC_V1_OPERATOR_CREDENTIALS_FILE`, already in `deployment.env`, and
the same `KALILLAC_V1_OPERATOR_RECORD_FILE`. It is a dry run unless `--apply` is given.

## Stopping staging

`sudo systemctl disable --now UNIT` stops it; nothing else depends on it. Removing its database,
roles, files or keys is the owner's decision and no tool here does it.
