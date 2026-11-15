# Isolated staging environment: design proposal

Status: **proposal for the owner's decision**. Nothing here has been provisioned, and nothing in this
document is done by a development session: no SSH, no host change, no service, database or
credential is created. Every step below is for the owner or an operator, on their authorization.

Purpose: a place where the owner can use the real product with every new capability turned on
(accounts, Premium billing in Stripe test mode, file creation and editing, reasoning modes,
Financial Safeguards), on synthetic data, without any effect on production.

## 1. What the repository records (checked 2026-11-15)

| Fact | Source |
|---|---|
| Production runs Uvicorn on `127.0.0.1:8001` behind Nginx (origin HTTPS) and Cloudflare | `deploy/release/production.env` (`LISTEN_PORT=8001`), the application's recorded network facts |
| **Production's systemd unit is named `kalillac-ai-fastapi-staging.service`** (a historical name), with drop-ins in `/etc/systemd/system/kalillac-ai-fastapi-staging.service.d` | `deploy/release/production.env` |
| **Port 8002 was used by an earlier staging deployment, `kalillac-v31-staging`** ("Live V31 staging" is cited in a regression test); it may still be bound | `candidate/tests/test_kalillac_server.py`, `candidate/tests/test_router_generation_regressions.py` |
| The launcher's host singleton is keyed by deployment id, so two deployments with different ids do not block each other | `candidate/kalillac_server.py` |

**Port 8002 cannot be verified from here** (no host access, by design). Before anything is proposed
for it, an operator runs these read-only commands on the host:

```
sudo ss -ltnp '( sport = :8002 or sport = :8003 )'
systemctl list-units --all 'kalillac*'
sudo nginx -T 2>/dev/null | grep -n 'proxy_pass'
```

If 8002 is free and no unit or Nginx site refers to it, staging may use 8002. If anything is bound to
it or names it (for example a surviving V31 staging), staging uses **8003** instead and the old
deployment is left untouched until the owner decides about it.

## 2. Proposed layout

| Item | Staging | Never shared with production |
|---|---|---|
| Name | systemd unit `kalillac-staging-v1.service`, deployment id `kalillac-staging-v1` (deliberately not containing "fastapi-staging", the production unit's name) | yes |
| Listen | `127.0.0.1:8002` or `:8003` (section 1) | yes |
| Code | its own release directory and virtual environment (`/opt/kalillac-staging-v1/`), built from the production lock plus `requirements-files.txt`, `requirements-database.txt`, `requirements-billing.txt` | yes |
| System user | its own unprivileged user; cannot read production's files, credentials or database | yes |
| Database | its own PostgreSQL 16 database (`kalillac_staging_v1`) with its own owner, migration, runtime, registration, V1 executor and service roles; migrated to the head on this branch | yes |
| Secrets | systemd credentials in `/etc/credstore/kalillac-staging-v1/`: Stripe **test** key and webhook secret, a separate OpenAI API key with its own monthly spend limit, a separate email-provider key | yes |
| Files | `KALILLAC_FILES_ROOT=/var/lib/kalillac-staging-v1/files` | yes |
| Public access | its own hostname (for example `staging.kalillac.ai`) through Cloudflare with **Cloudflare Access** limited to the owner's identity; `noindex`; never linked from the public site | yes |
| Resources | systemd `MemoryMax` and `CPUQuota` so staging can never starve production on a shared host | |

Flags on in staging: accounts and the database; email verification (through the email provider's test
or sandbox mode, to the owner's address only); billing in Stripe test mode (`docs/STRIPE_TEST_MODE_SETUP.md`);
files (`KALILLAC_FILES_ENABLED`, tier `paid`, the page marked `data-kalillac-files="on"`); reasoning modes
with the provisional mapping (QUICK low, BALANCED medium, DEEP high; the page marked
`data-kalillac-reasoning="on"`); Financial Safeguards with small budgets (the proposal values, scaled
down); V1 account security only once V1-02D/E is complete and its transition has been rehearsed here.

## 3. Cost

No new paid infrastructure if staging shares the existing host (owner to confirm the host has
headroom; otherwise a second small host is a new expense needing approval). Real spending is limited
to the owner's own testing: GPT-6 Luna calls (bounded by the separate key's spend limit and by
Financial Safeguards), Tavily searches, and test emails. Stripe test mode is free.

## 4. Rules

* Synthetic data only; no customer data is ever copied into staging.
* No production credential, database or file is readable from staging, and the reverse.
* Release and rollback use the reviewed `deploy/release` tooling pointed at staging's own environment
  file (a new `staging.env`, reviewed like `production.env`), never at production's.
* Staging is not a backup of production and is not used to serve customers.

## 5. Decisions for the owner

1. Same host or a separate host (a separate host is a new expense).
2. The staging hostname, and Cloudflare Access for it.
3. Port: after the section 1 check, 8002 if free, otherwise 8003.
4. Whether to keep, inspect or retire any surviving `kalillac-v31-staging` deployment (left untouched
   until then).
5. The separate OpenAI key's monthly spend limit for staging.
