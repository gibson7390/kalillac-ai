# E1b: billing and entitlement writes behind a reviewed authority (design, not applied)

Status: **design for review.** Nothing here is implemented, migrated or granted. Written 2026-11-17 from the
code at `21258e5` (`fix/file-allowance-accounting`). It finishes the E1 design in
`docs/V1-02DE-ACCOUNT-SECURITY-PLAN.md` ("E1b, entitlements and billing") and is meant to be read by the
independent billing audit **before** any of it is built: section 8 lists the questions whose answers decide
the implementation.

## 1. What the runtime login R holds and uses today

| Table | R's rights | Granted by |
|---|---|---|
| `account_entitlements` | SELECT, INSERT, UPDATE, DELETE | 0002 |
| `account_billing` | SELECT, INSERT, UPDATE, DELETE | 0004 |
| `stripe_webhook_events` | SELECT, INSERT, UPDATE, DELETE | 0004 |
| `billing_cases` | SELECT, INSERT, UPDATE, DELETE | 0017 |

The registration authority A also holds `INSERT (user_id, tier, source, created_at, updated_at, expires_at)`
and SELECT on `account_entitlements` (0011): in V1 mode A, not R, creates the free entitlement. E1b does not
change A.

Every runtime write, from the code (all go through `kalillac_db/repositories/billing.py` and
`entitlements.py`):

| # | Path | Trigger | Writes | Today's transaction |
|---|---|---|---|---|
| W1 | `service._plan_checkout` (`service.py:292`) | `POST /api/account/billing/checkout`, V1-authorized (A11) | creates the `account_billing` row; `checkout_attempt_id`, `checkout_attempt_created_at`, `checkout_customer_id`, `checkout_price_id`, `checkout_success_url`, `checkout_cancel_url` | R, account row FOR UPDATE, A11's recheck first |
| W2 | `service._store_session` (`:357`) | after Stripe creates the Checkout Session | `stripe_checkout_session_id`, `checkout_session_expires_at`, only for the same attempt | R, account lock |
| W3 | `service._discard_session` (`:346`) | a stored session Stripe reports expired | the eight pending-Checkout columns to NULL | R, account lock |
| W4 | `service._reconcile_subscription` (`:599`) from `process_webhook` | a signed subscription event | billing's six subscription columns; pending Checkout cleared; `account_entitlements` tier, source, expiry; then the event id | R, account lock **held across `gateway.retrieve_subscription`** |
| W5 | `_unmatched` and the no-subscription branches of `process_webhook` | signed events with nothing to reconcile | `stripe_webhook_events` | R |
| W6 | `service._process_case` (`:781`) | a signed refund or dispute event | `billing_cases` upsert and the event id | R, account lock held across the second Stripe fetch |
| W7 | `service.reconcile_account` (`:1002`) via `tools/reconcile_billing_account.py` | an operator | as W4, no event record; fixed lock and statement timeouts | **R** (the application's database settings) |
| W8 | `tools/billing_cases.py` | an operator reviewing a case | `billing_cases.review_status` | **R** |
| W9 | `kalillac_accounts/router.py:394` | legacy registration (V1 off only) | the free entitlement | R |

Reads that stay R's (SELECT only after E1b): `paid_access` (the PAID scope), the quota's plan limit,
`kalillac_files/account_access`, `GET /api/account/billing`, `/api/account/entitlements`, the owner report.

## 2. What E1b protects, and what it does not

Today any statement that runs on R's connection (an injection, an ORM mistake, a future bug in any route)
can make any account Premium with any expiry, rewrite its subscription linkage, mark a webhook event as
processed so Stripe's retry is ignored, or close a dispute case. After E1b, R reads these four tables and
writes none of them; each write is one fixed SECURITY DEFINER function owned by E with the access policy in
its body, callable only by the authority login. The same boundary V1 already draws for credentials and
sessions.

Not in scope, as for the rest of V1: a compromise of the application process itself, which holds the
authority's credential. E1b removes R's write path; it does not make a compromised process honest.

## 3. The constraint that shapes the design

The V1 authority (`kalillac_accounts/v1_authority.py`) runs every call as **its own transaction on S's
private pool**. Today's billing transactions take R's `FOR UPDATE` on the account row and hold it while
calling Stripe (W4, W6). If only the final statements moved to S, the S call would wait on the lock its own
request holds in R's transaction until `lock_timeout` (3 s) and fail. So the whole write transaction moves,
not just its last statements. Three ways:

**A. Optimistic single-call writes (recommended).** `account_billing` gains `version bigint NOT NULL
DEFAULT 0`. A flow reads state (R, no lock), calls Stripe with no database lock held, then makes one
authority call that locks the account row itself, compares `version` with the value it read, applies the
write and increments `version`, all in one transaction. A mismatch returns `stale`: the flow re-reads and
retries (bounded: 3), and a webhook that still loses returns non-2xx so Stripe redelivers.
*Ordering guarantee kept:* today, two events for one account cannot commit snapshots out of order because
each fetches Stripe under the lock. Under A, a write commits only if no billing write committed since its
read, and its Stripe fetch began after that read; so a snapshot fetched before another commit can never
overwrite it. *Gained:* no database lock is held during a Stripe request (today a slow Stripe call holds
the account row, and with it V1's own `FOR UPDATE`, for its whole duration).

**B. Multi-call authority transactions.** Extend `V1Authority` so one S transaction spans "lock",
Python's Stripe call, then "apply". Rejected: it breaks the one-fixed-call-per-transaction contract the
authority's review rests on, and holds one of S's three pooled connections per in-flight Stripe call, so a
burst of webhooks exhausts the pool that sign-in also needs.

**C. A billing login with table DML.** Give a new login B the DML and take it from R. Rejected: B's
connection could still write any tier with any expiry; it only moves the hole.

**Which login executes the functions.** Recommended: **S**, extending `_reviewed_functions` (digest, owner
E, fixed `search_path`, EXECUTE for E and S only), so `verify_service_boundary` proves the new functions
exactly as it proves D1–D5's. A dedicated billing login would separate duties but adds a seventh login, a
credential file, provisioning and transition steps. The operator paths (W7, W8) go through **P**, as D4 did,
with the append-only operator record.

## 4. Proposed functions (migration 0026; owned by E, SECURITY DEFINER, `search_path` fixed, bodies digested)

Each returns one fixed outcome code; arguments are bound; nothing returns a row of another account.

| Function | Replaces | Writes | Refuses with |
|---|---|---|---|
| `v1_billing_checkout_plan(session_id, token_hash, expected_version, attempt_id, created_at, customer_id, price_id, success_url, cancel_url)` | W1 | the pending-Checkout columns only | `not_authenticated` (authorizes the session in the same transaction, so A11's recheck and the write are one), `stale`, `pending_attempt` (one is already pending) |
| `v1_billing_checkout_session(account, expected_version, attempt_id, session_id, expires_at)` | W2 | session id and expiry, only for that attempt | `stale`, `attempt_mismatch` |
| `v1_billing_checkout_discard(account, expected_version, session_id)` | W3 | clears the pending Checkout, only if that session is the stored one | `stale`, `session_mismatch` |
| `v1_billing_apply_subscription(account, expected_version, event_id, event_type, customer_id, subscription_id, price_id, status, cancel_at_period_end, current_period_end, clear_checkout)` | W4 | the subscription columns, the entitlement and the event id, together | `stale`, `duplicate` (event id already recorded), `invalid_state` (paying status without a period end, period end out of range) |
| `v1_billing_record_event(event_id, event_type)` | W5 | the event id only | `duplicate` |
| `v1_billing_record_case(account_or_null, kind, provider_object_id, charge_id, customer_id, provider_status, reason, event_id)` | W6 | the case upsert and the event id, together | `duplicate`, `invalid_case` |
| `v1_operator_billing_reconcile(...)`, `v1_operator_case_review(case_id, from_status, to_status, operation_id)` (P) | W7, W8 | as W4 without an event; one review transition | `stale`, `transition_refused` |

**Where the access policy lives.** Today `tier_for_subscription` (paying status AND a configured Price id),
`paid_entitlement_expiry` (period end + 7 days) and the linkage rules (`_linkage_decision`) are Python. Two
options for `v1_billing_apply_subscription`:

1. *Policy in SQL (recommended):* the function computes the tier and expiry itself from the status, the
   Price and the period end, against an operator-maintained table of allowed Price ids
   (`billing_allowed_prices`, written only through P at provisioning). An R-side bug can then never produce
   a paid row Stripe's state does not justify, and Python's `tier_for_subscription` remains a cross-check:
   the call is refused if the two disagree.
2. *Policy in Python:* the function takes the tier and only checks invariants it can see (paid ⇒ paying
   status, a period end, expiry exactly period end + 7 days). Smaller, but the allowed Price set stays
   outside the database's view.

Either way the function writes an append-only row per entitlement change
(`billing_entitlement_changes`: account, old and new tier, expiry, source event or operation id, time), so a
paid grant can always be traced to the Stripe event or operator action that caused it.

## 5. Grants after E1b

R: SELECT only on the four tables (and on `billing_allowed_prices` if option 1). A: unchanged. E1b's
statements join E1a's in the operator transition (E2, one transaction, ingress closed), and its rollback
statements join E1a's inverse. Because nothing anywhere runs with the latch active yet (no staging, no
production), the simplest classification is to fold E1b into the `final` state at the new head 0026:
`prepared` ⇔ legacy grants, `active` ⇔ E1a + E1b. `registration_startup.effective_privileges` and
`v1_final_grants.runtime_account_state` gain the four tables. V1 off keeps today's paths unchanged (SQLite
tests and legacy mode never see the functions), exactly as D3 did.

## 6. What changes for the product

Nothing a customer sees. Checkout, the portal, webhooks, refund and dispute cases and the operator tools
keep their outcomes. Internally: `account_billing.version`; the webhook no longer holds a row lock during
Stripe calls; Checkout's authorization and its write become one transaction (strictly stronger than A11's
recheck, which commits before the Stripe call).

## 7. Tests before anything is applied

* Catalog: each function's owner, `search_path`, digest and EXECUTE (E and S, or E and P).
* Each function's refusals: stale version, wrong attempt or session, duplicate event, paying status without
  a period end, a disallowed Price (option 1), a tier that disagrees with the policy.
* Under the final grants R gets 42501 for INSERT, UPDATE and DELETE on all four tables and can SELECT them.
* Concurrency on PostgreSQL: two webhooks for one account with interleaved Stripe snapshots, the newer
  always winning. Today's `check_fetch_after_lock_ordering` (`tools/verify_billing_postgres.py`) proves
  this for the lock design specifically; under option A it is rewritten for the version check (a delivery
  whose fetch predates another's commit must end `stale` and retry), not merely re-run. Also Checkout
  planning racing a webhook, and concurrent duplicate deliveries of one event.
* The existing PostgreSQL journeys re-run under E1a + E1b: the Premium journey, Spaces, files and images,
  the allowance correction; and the root-run bootstrap verifier for the new grant state.

## 8. Questions for the independent billing audit, before implementation

1. Is optimistic versioning (A) an acceptable replacement for holding the account lock across Stripe's
   retrieve, given the ordering argument in section 3?
2. Policy in SQL with an operator-maintained Price table (option 1), or invariants only (option 2)?
3. S for the runtime functions and P for the operator ones, or a dedicated billing login?
4. Should Checkout planning authorize inside the write function (session id and token hash, as D3's
   functions do) rather than in R's transaction?
5. Do any findings on B1, B2 or A11 change the write set in section 1?

## 9. Order once the audit has answered

0026 with the functions only, inert and granted to no one but E and S/P (as 0024 was); the billing
service's V1 path behind them, with the tests in section 7; E1b's grants added to the transition and the
classifier; the journeys re-run; a Codex checkpoint. Nothing is applied to any real database until the
staging run, on the owner's authorization.
