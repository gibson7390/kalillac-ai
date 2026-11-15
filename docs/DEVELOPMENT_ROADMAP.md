# Kalillac AI development roadmap and progress record

The single place a new development session reads first. It records what is verified, not what is
planned to be true. Update it whenever a milestone's state changes.

States used here, never interchangeable:
**IMPLEMENTED** (code exists) · **DEVELOPMENT VERIFIED** (passes the required development tests) ·
**PRODUCTION READY** (deployment prerequisites verified) · **PRODUCTION ACTIVE** (authorized,
deployed, enabled and verified in production).

Last updated: 2026-11-14.

## Production baseline (as stated by the owner, checked against Git where possible)

| Fact | Value | Checked |
|---|---|---|
| Production application commit | `54e38ecf36a4a25db20946019dd298311a632c1d` | Exists; tip of `feat/financial-safeguards-v1-batch1`; contains Financial Safeguards V1 batches 1 and 2 |
| Reviewed deployment tooling | `184ee0ab17f77fb0983fa27f54e6c1f0c7844f76` | Exists; `ops/fs-v1-deploy-tooling`, directly on top of `54e38ecf` |
| Production database revision | `0015_finance_ledger` | Matches the release facts in `deploy/release/releases/54e38ecf.env`; not independently queried |
| Post-activation checker | `ACTIVE_54e38ecf` | Owner-reported; not re-run |
| End-to-end Free Chat smoke test after activation | **Not reported as complete** | Open |

Production is never touched from a development session: no SSH, deploy, restart, flag change,
production migration, live Stripe or customer data.

## Where the work lives

| Branch | Worktree | Purpose |
|---|---|---|
| `feat/financial-safeguards-v1-completion` | `C:/Kalillac/kalillac-ai-fs-v1-completion` | **Current development branch.** Based on `184ee0a`, so it holds the production application and the reviewed tooling. |
| `product/account-lifecycle-hardening-v1` | `C:/Kalillac/kalillac-ai` | Holds **uncommitted** account work. Not modified. See "Uncommitted account work" below. |
| `feat/financial-safeguards-v1-batch1`, `ops/fs-v1-deploy-tooling` | their own worktrees | Production baseline and tooling. Not modified. |

`origin/main` is far behind the release lineage; releases have been cut from feature branches.

### Uncommitted account work in `C:/Kalillac/kalillac-ai`

Inspected read-only on 2026-11-14. The eleven changed or untracked files (last written 2026-11-03
09:25 to 09:36) are an **earlier draft of the V1 account-security schema foundation**. The finished
version was committed later that day as `df4c624` on the remote branch (roughly twice the size in
every file), continued through `24e7630` and `a991698`, and is an ancestor of the production
commit and of the current development branch. Five of the files are identical to `df4c624` after
line-ending normalisation. The five source and test files that differ have 10 to 47 local-only
lines each against 77 to 611 lines that exist only upstream; the workflow file differs by three
lines.

Conclusion: nothing in that worktree needs integrating, and Milestone 2 builds on the committed
lineage. The worktree has been left exactly as found. **Owner decision:** confirm it may be
discarded, or name anything in it that should be kept.

## Milestones

| # | Milestone | State | Notes |
|---|---|---|---|
| 1 | Financial Safeguards V1 | **DEVELOPMENT VERIFIED**, off in production | Activation proposal written; tooling update and owner decisions outstanding |
| 2 | Secure customer accounts | IMPLEMENTED in part, off in production | Gap analysis is the next action |
| 3 | Stripe billing and subscriptions | IMPLEMENTED in part (test-mode foundation), off | Not yet re-verified in this record |
| 4 | Reasoning toggle controls | Not started | Required Premium milestone; must first verify GPT-6 Luna's actual reasoning parameters |
| 5 | File creation, uploads, editing | Not started | Required Premium milestone |
| 6 | Spaces and persistent conversations | IMPLEMENTED in part, off | Not yet re-verified in this record |
| 7 | Owner administration dashboard | Not started | |
| 8 | Premium product integration | Not started | |
| 9 | Commercial launch readiness | Not started | |

### Milestone 1: Financial Safeguards V1

Last verified commit: `a5ebb52a0435b7bd86df41bbd84feb9aac6e1604`.
Reference: `candidate/FINANCIAL_SAFEGUARDS.md` (guarantees, failure matrix, configuration, operator
command, activation and rollback).

Already present at the production baseline: exact micro-dollar money and pricing, worst-case
reservation, settlement by reported usage, FREE/PAID/GLOBAL day and month ceilings, durable
reservations committed before dispatch, unsafe latches, crash-stays-charged behaviour.

Added on this branch:

* Provider-specific day and month budgets (`finance_provider_buckets`, migration `0016`).
* A per-request dollar ceiling for FREE and PAID requests, refused before any ledger is touched.
* An operator command to report, verify, reconcile stale reservations and clear latches, with an
  append-only action record (`finance_operator_actions`).
* Tests proving exactly-once release and settlement.

Acceptance criteria and evidence:

| Criterion | Evidence |
|---|---|
| Ceilings hold under concurrent requests for the last dollars | 52 multi-process PostgreSQL tests, GitHub Actions run `43671417421`, success, 0 skipped |
| Every financially relevant failure path has tested behaviour | Failure matrix in `candidate/FINANCIAL_SAFEGUARDS.md`; 323 SQLite tests, same run |
| Dollar limits run beside, not instead of, attempt ceilings | `test_attempt_ceiling_and_financial_ceiling_are_both_enforced`, `test_finance_and_attempt_ceilings_are_both_required` |
| Budgets are configuration, with no amounts in source | 15 required variables; start-up refuses when any is missing or inconsistent |
| Flag off leaves production behaviour unchanged | `test_disabled_finance_leaves_the_attempt_ceiling_path_untouched`, `test_startup_without_the_flag_is_unchanged` |
| Migration `0016` is safe on the production schema | Applied from `0015`, guard and downgrade exercised on PostgreSQL 16 in run `43671417421` |
| Wider account, billing and migration suites unaffected (Linux) | GitHub Actions run `43671452813` on `a5ebb52`: PostgreSQL job passed (full migration chain including `0016`); suites job passed 1551 + 106 tests, then failed 4 of 818 on a pre-existing expired test fixture (see "Known defects"), fixed in the following commit. See "Open verification" for the re-run |
| Activation and rollback procedure documented | `candidate/FINANCIAL_SAFEGUARDS.md` |

Not done, required before PRODUCTION READY:

1. Release tooling for an enforcement-on release (the reviewed tooling assumes the flag is off and
   the ledger tables are empty). Detailed in the activation proposal.
2. Owner decisions (below).
3. Per-customer dollar budgets: deliberately deferred to Milestone 3, where subscriptions define
   the allowance. Until then a customer is bounded per request and by the PAID ceilings.
4. A retention procedure for `finance_reservations`.

### Milestone 2: Secure customer accounts (starting point, not yet verified here)

Present in the code, each behind its own default-off flag: registration (`/register`, and an
email-first v2 flow), login, logout, `/me`, password hashing with a bounded work queue, sign-in
rate limiting, account recovery, email verification through Resend, an unsafe-request guard, and
the V1 security schema with private session revocation and issuance authorities (migrations
`0012` to `0014`, in production schema, flag off).

To establish next: which of suspension, account deletion, session invalidation on password change,
enumeration resistance and CSRF coverage are complete end to end, and what a full synthetic
account-lifecycle test still needs.

## Known defects and environment limits

* **Fixed here, present on the production commit:** four cases of
  `test_unsafe_request_guard.py::test_signed_in_bodyless_routes_never_read_a_body` began failing on
  2026-11-13 on every branch of the release lineage, including `54e38ecf`. The shared test world
  seeds sign-in sessions that expire 14 days after a hardcoded 2026-10-30, and the billing router
  checks them against the real clock. It is a test-fixture defect, not a product defect: no
  application code changed. `subscription-foundation-verification` is red on any commit without
  the fix. Other tests with hardcoded 2026 dates use an injected clock and still pass.
* `candidate/tests/test_product_identity_aroge.py` fails locally (16 cases, same on the untouched
  baseline) without provider configuration in the environment.
* `candidate/tests/test_v1_schema.py` (five `hostile_diagnostics` cases) uses `/proc` and
  `O_NONBLOCK`; on Windows they fail and can hang a piped run. Same on the untouched baseline.
  They run on Linux in CI. Run Windows suites one file at a time with a time limit.
* `candidate/tests/test_account_ui.py::test_built_account_entry_is_noindex_and_outside_the_sitemap`
  needs the built site (`site/dist`), absent in a fresh worktree.
* The PostgreSQL suites refuse to run outside the disposable GitHub Actions job by design; there
  is no local PostgreSQL. Dispatch `provider-ceilings-postgres` on the branch instead.

## Open verification

* Re-run `subscription-foundation-verification` on the commit that carries the fixture fix and
  record the result here. Its steps after the failed one (protected registration files, later
  slices) did not execute in run `43671452813`.
* Local Windows regression, one file at a time: 100 of 126 files finished when this was written,
  with no failure other than the environment limits listed above. Record the remainder.

## Owner decisions required

1. **Financial budgets:** the fifteen `KALILLAC_FINANCE_*` values (day and month ceilings for
   GLOBAL, FREE, PAID, OpenAI, Brave, Tavily; the minimum headroom percent; the FREE and PAID
   request ceilings).
2. **Prices:** confirm the configured provider prices against the providers' current price lists,
   plus the OpenAI overhead tokens and Tavily credit maxima.
3. **Migration `0016`:** approval to apply it to production.
4. **Activation:** approval of the tooling update once written, and an activation window.
5. **Uncommitted account worktree:** discard or keep (see above).
6. **Free Chat smoke test:** the post-activation end-to-end test for `54e38ecf` is still unreported.
7. Later milestones: subscription price, Stripe product and price IDs, billing interval, trial,
   refund policy, Premium allowances. Nothing has been assumed.

## Next autonomous action

1. Record the result of run `43671452813` and the local regression; fix anything they show.
2. Milestone 2 gap analysis against the directive's required capabilities and threat list, then
   implement the gaps with a full synthetic account-lifecycle test.
3. In parallel when Milestone 1 activation is approved in principle: the release-tooling change for
   an enforcement-on release, with its disposable-systemd tests.
