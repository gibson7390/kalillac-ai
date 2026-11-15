# Financial Safeguards V1

Financial Safeguards stop Kalillac from dispatching a provider request it has not first paid for,
in its own ledger, out of a dollar budget the operator set. They run beside the provider attempt
ceilings (request counts) and never replace them.

Status: **development verified, off in production.** Nothing here is active until
`KALILLAC_FINANCIAL_SAFEGUARDS_ENABLED=true`, and with the flag off the application reads no
financial setting and touches no financial table.

This document covers what is guaranteed, how it is configured, what happens on each failure, how
an operator audits and reconciles the ledger, and the proposed activation and rollback procedure.

## What is enforced

Every provider request goes through one of three protected seams (OpenAI Responses, Brave search,
Tavily search/extract). With the flag on, each attempt does this, in order:

1. **Price the worst case.** OpenAI: (request body bytes + configured overhead tokens) at the
   highest input rate, plus `max_output_tokens` at the output rate. Brave: one request. Tavily: the
   configured maximum credits for the operation and depth. An attempt that cannot be bounded (another
   model, no `max_output_tokens`) is refused.
2. **Check the request's own ceiling.** The attempt's worst case is added to what this chat request
   has already authorized. Above the request ceiling for its scope, the attempt is refused before any
   ledger is touched (`422 processing_limit_reached`).
3. **Reserve, in one database transaction**, under row locks: the attempt-ceiling slot, then the
   worst case in every bucket the attempt is charged to. The transaction commits before the request
   may be sent. If any bucket lacks room the whole transaction rolls back (`429 busy`) and no attempt
   slot is used.
4. **Dispatch**, only with both leases held.
5. **Settle or release.** Never dispatched: the whole reservation returns. Dispatched and usage
   reported: the actual cost is charged and the rest returns. Dispatched and the outcome is unknown
   (failure, timeout, unreadable usage): the full reservation stays charged.

An attempt is charged to these buckets, for the UTC day and the UTC month of its reservation:

| Bucket | Ceiling variable |
|---|---|
| GLOBAL | `KALILLAC_FINANCE_{DAY,MONTH}_GLOBAL_LIMIT_USD` |
| FREE (anonymous and signed-in non-paying) or PAID (verified paid entitlement) | `..._FREE_LIMIT_USD`, `..._PAID_LIMIT_USD` |
| Its provider | `..._OPENAI_LIMIT_USD`, `..._BRAVE_LIMIT_USD`, `..._TAVILY_LIMIT_USD` |

Exposure is reserved plus settled. Admission requires exposure plus the new reservation to fit in
every one of those buckets.

### Guarantees

* No provider request is dispatched without a committed reservation for its worst-case cost.
* At admission, FREE, PAID and each provider never exceed their day and month ceilings, including
  under concurrent requests racing for the last dollars (row locks, verified with separate processes
  on PostgreSQL).
* FREE traffic can never use capacity reserved for PAID, and the reverse: FREE + PAID must be
  configured below GLOBAL, with a minimum headroom.
* One chat request can never have more than its request ceiling authorized across all of its model
  calls, tool-loop turns, searches and retries.
* Money whose outcome is unknown is always charged, never refunded on a guess. This includes a
  process death, a database failure at settlement and an operator reconciliation.
* Every release and settlement applies exactly once. A duplicate or late resolution of the same
  reservation changes nothing.
* Paid status is never inferred. A request is charged to PAID only with a request-bound entitlement
  issued from the verified entitlement record; no production code issues one yet.

### Limits of the guarantee

* The ledger is Kalillac's own conservative accounting from the configured prices. It is not the
  provider's invoice. A wrong configured price makes the ledger wrong.
* The OpenAI input bound depends on the configured overhead
  (`KALILLAC_FINANCE_OPENAI_INPUT_OVERHEAD_TOKENS`) covering tokens the provider adds itself. If a
  response ever reports more tokens than the bound, the actual cost is still charged in full, the
  excess is recorded as an overrun and the scope is latched (below). The GLOBAL headroom exists to
  absorb that case.
* Tavily usage is not requested from the provider, so each Tavily operation is charged its
  configured maximum credits.
* There is no per-customer dollar budget. One paying customer is bounded per request and by the
  PAID ceilings, not individually. Per-customer budgets depend on accounts and subscriptions and are
  scheduled with the Stripe milestone.
* `finance_reservations` grows by one row per provider attempt and is never pruned by the
  application, which has no DELETE privilege. A retention procedure is a follow-up.

## Unsafe latches

A settlement above its reservation, or usage above the reserved bound, inserts a durable latch for
that scope. A latched scope admits nothing; a GLOBAL latch (overrun reaching the configured
headroom, or GLOBAL exposure reaching its ceiling during an overrun) stops everything. The
application can insert a latch but cannot update or delete one, so neither a restart nor
application code clears it. Clearing is an explicit operator action with the owner role, after the
cause is understood.

## Failure behaviour

| Situation | Result | Evidence (test) |
|---|---|---|
| Scope, GLOBAL or provider ceiling reached, including concurrent racers | Refused before dispatch, `429 busy`, no attempt slot used | `test_finance_concurrent_*`, `test_provider_day_ceiling_refuses_only_that_provider` |
| Request ceiling reached | Refused before any ledger, `422 processing_limit_reached` | `test_request_ceiling_bounds_all_attempts_of_one_request` |
| Database unavailable at reservation | No dispatch, `503` | `test_database_unavailable_means_no_dispatch` |
| Local rejection before dispatch | Both leases released | `test_failure_before_dispatch_releases_both_leases` |
| Provider failure or timeout after dispatch | Full reservation charged | `test_dispatched_failure_settles_at_the_full_reservation` |
| Usage missing, malformed or partial | Full reservation charged | `test_unpriceable_usage_keeps_the_full_reservation` |
| Provider retry | A new attempt with its own reservation, counted toward the same request ceiling | `test_request_ceiling_bounds_all_attempts_of_one_request` |
| Process killed before or after dispatch | Reservation stays outstanding and charged across a restart | `test_finance_killed_worker_stays_charged_across_a_restart` |
| Database failure at settlement | Reservation stays outstanding and charged; the request still counts it in full | `test_settlement_database_failure_stays_charged`, `test_unconfirmed_settlement_and_dispatched_failure_stay_authorized` |
| Duplicate or conflicting settlement or release | No change | `test_duplicate_and_conflicting_resolutions_apply_exactly_once` |
| Actual cost above the reservation | Charged in full, overrun recorded, scope latched | `test_free_overrun_latches_free_and_keeps_safe_paid_capacity` |
| Overrun reaching the GLOBAL headroom | GLOBAL latched, everything stops | `test_overrun_exhausting_global_headroom_stops_everything` |
| UTC day or month rollover during a request | Settled in the period it was reserved in | `test_utc_day_rollover_keeps_old_reservations_in_their_own_day` |
| Duplicate client request | A separate request with its own reservations and its own ceiling; there is no client idempotency key | by design |

SQLite tests: `tests/test_financial_pricing.py`, `tests/test_finance_ledger.py`,
`tests/test_finance_budgets.py`. Multi-process PostgreSQL tests:
`tests/test_provider_controls_postgres.py`, run by `.github/workflows/provider-ceilings-postgres.yml`.

## Configuration

Every value is explicit. There are no defaults and no production amounts in source; with the flag
on, a missing, malformed, inconsistent or unrecognised `KALILLAC_PRICE_*` / `KALILLAC_FINANCE_*`
variable stops start-up. Dollar amounts are plain decimals with at most six decimal places.
`KALILLAC_PROVIDER_CEILINGS_ENABLED` must also be on.

**Prices** (`kalillac_finance/pricing.py`): `KALILLAC_PRICE_OPENAI_MODEL` (must equal the configured
model), `KALILLAC_PRICE_OPENAI_{INPUT,CACHED_INPUT,CACHE_WRITE,OUTPUT}_USD_PER_MTOK`,
`KALILLAC_PRICE_OPENAI_LONG_CONTEXT_ABOVE_INPUT_TOKENS` (a threshold or `none`) with its four
`..._LONG_CONTEXT_*_USD_PER_MTOK` rates when a threshold is set,
`KALILLAC_FINANCE_OPENAI_INPUT_OVERHEAD_TOKENS`, `KALILLAC_PRICE_BRAVE_USD_PER_REQUEST`,
`KALILLAC_PRICE_TAVILY_USD_PER_CREDIT`,
`KALILLAC_FINANCE_TAVILY_MAX_CREDITS_{SEARCH,EXTRACT}_{BASIC,ADVANCED}`.

**Budgets** (`kalillac_finance/limits.py`), fifteen variables:

| Variables | Rule |
|---|---|
| `KALILLAC_FINANCE_{DAY,MONTH}_{GLOBAL,FREE,PAID}_LIMIT_USD` | FREE + PAID below GLOBAL by at least the minimum headroom; each day value at most its month value |
| `KALILLAC_FINANCE_MIN_HEADROOM_PERCENT` | Whole percent of GLOBAL, 1 to 90 |
| `KALILLAC_FINANCE_{DAY,MONTH}_{OPENAI,BRAVE,TAVILY}_LIMIT_USD` | At most the period's GLOBAL; day at most month; `0` closes that provider |
| `KALILLAC_FINANCE_MAX_REQUEST_{FREE,PAID}_USD` | At most the scope's day ceiling |

Sizing a request ceiling: one model attempt reserves
`(body bytes + overhead tokens) x highest input rate + max_output_tokens x output rate`, and a chat
request may make up to `KALILLAC_REQUEST_MAX_MODEL_ATTEMPTS` model attempts and
`KALILLAC_REQUEST_MAX_SEARCH_ATTEMPTS` searches. A confirmed settlement returns the unused part of
each reservation to the request, so the ceiling binds on what is in flight plus what was actually
charged, not on the sum of worst cases.

## Operator command

`tools/finance_ledger_admin.py`, run from `candidate/`. It prints amounts, dates, scope and
provider names only.

| Command | Database identity | Effect |
|---|---|---|
| `report [--json]` | application | Read-only. Exposure against every ceiling for the current UTC day and month, outstanding reservations, latches, and a recomputation of every bucket from the reservation rows. Exit 3 if any bucket differs. |
| `resolve-stale --older-than-hours N [--apply]` | application | Finalises outstanding reservations older than N hours (at least 1) at their full reserved amount. Exposure does not change. Dry run without `--apply`. One transaction and one `finance_operator_actions` row per reservation. |
| `clear-latch --scope S --expect-reason R --apply` | migration login (assumes the owner role) | Removes one latch only if its reason matches, and records the action. |

`finance_operator_actions` is append-only for the application role.

## Schema

Migration `0015_finance_ledger` (in production): `finance_ledger_buckets`, `finance_reservations`,
`finance_unsafe_latches`. Migration `0016_finance_provider_budgets` (not in production):
`finance_provider_buckets`, `finance_operator_actions`. `0016` refuses to run if
`finance_reservations` holds any row, because provider buckets start at zero.

## Activation proposal

Nothing below has been done. Each numbered step is an owner approval gate.

**Decisions needed from the owner first**

1. The fifteen budget values above. No amount has been chosen or suggested in source.
2. Confirmation that the configured prices match the providers' current published prices on the
   day of activation, and the OpenAI overhead and Tavily credit maxima.
3. Approval to apply migration `0016` to the production database.
4. Approval of a release-tooling update (below) and an activation window.

**Prerequisite engineering, not yet done**

The reviewed release tooling (`deploy/release`, commit `184ee0a`) assumes Financial Safeguards are
off: `preflight.py` requires no financial configuration, release facts list the flag under
`FLAGS_OFF`, and the database check requires the three ledger tables to be empty. Activating
enforcement needs a reviewed tooling change that (a) carries release facts for the new application
commit with `MIGRATION_HEAD` and `DB_REVISION` at `0016` and five financial tables, (b) lets a
release declare the flag on and verifies the complete financial configuration by name and hash,
never by value, and (c) accepts a non-empty ledger once the flag is on.

**Procedure**

1. Apply migration `0016` with the migration login. The running release (`54e38ecf`, flag off)
   ignores the new tables. Confirm revision `0016_finance_provider_budgets` and five empty tables.
2. Stage and activate the new application release **with the flag still off**, using the release
   tooling. Confirm `ACTIVE_<short>` and an end-to-end Free Chat response.
3. Add one systemd drop-in holding the complete financial configuration and the flag. No secret
   is involved.
4. Restart once through the release tooling. Start-up refuses on any configuration error, which the
   tooling treats as a failed activation and rolls back.
5. Verify: `finance_ledger_admin.py report` exits 0 with ceilings shown; one Free Chat request
   produces settled rows; no `WARN: FINANCE_` line other than expected ceiling refusals.
6. For the first days, run `report` daily and compare the ledger with the providers' own usage
   pages.

**Never enable the flag on release `54e38ecf`.** That release predates provider buckets; with
migration `0016` applied it would write reservations without them.

## Rollback

* **Disable enforcement:** remove the financial drop-in (flag off) and restart once. The
  application returns to exactly the attempt-ceiling behaviour. Ledger rows stay as history.
* **Roll back the release:** `rollback.sh`, as today. Do this only with the flag off.
* **Database:** do not downgrade `0016`. The additional tables are unused while the flag is off,
  and a downgrade would discard financial history.
* Re-enabling later is safe: buckets are only written while the flag is on, so they stay
  consistent with the reservation rows. Run `report` first.
