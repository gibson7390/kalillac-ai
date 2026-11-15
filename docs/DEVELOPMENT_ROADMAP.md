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
| `feat/financial-safeguards-v1-completion` | `C:/Kalillac/kalillac-ai-fs-v1-completion` | Milestone 1. Based on `184ee0a`, so it holds the production application and the reviewed tooling. |
| `feat/reasoning-modes-v1` | same worktree | Milestone 4 backend, stacked on the Milestone 1 branch. |
| `feat/file-generation-v1` | same worktree | **Current development branch.** Milestone 5 generation core, stacked on the Milestone 4 branch. |

The three branches are a stack: each contains the one before it. The worktree has its own `.venv`
(ignored by Git) holding the pinned dependencies plus the document libraries; the main checkout's
environment was not changed.
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
| 2 | Secure customer accounts | IMPLEMENTED in part, off in production | Gap analysis done; remaining items wait on the V1 slice plan (owner decision 5) |
| 3 | Stripe billing and subscriptions | IMPLEMENTED in part (test-mode foundation), off | Not yet re-verified in this record |
| 4 | Reasoning toggle controls | Backend DEVELOPMENT VERIFIED, off; interface and saved preference not started | `candidate/REASONING_MODES.md` |
| 5 | File creation, uploads, editing | Generation and upload-intake cores DEVELOPMENT VERIFIED, not reachable from the application; routes, worker, storage, downloads and editing not started | `candidate/FILE_GENERATION.md` |
| 6 | Spaces and persistent conversations | IMPLEMENTED in part, off | Not yet re-verified in this record |
| 7 | Owner administration dashboard | Not started | |
| 8 | Premium product integration | Not started | |
| 9 | Commercial launch readiness | Not started | |

### Milestone 1: Financial Safeguards V1

Last verified commit: `ee1564a6d84497b69509daf5f99ce2b1e38d4fb8` (implementation in `a5ebb52`).
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

Gap analysis against the directive (read from the code on 2026-11-14, not yet test-verified here):

| Capability | State |
|---|---|
| Registration | Present twice. The legacy `/register` answers `409 email_already_registered`, which reveals whether an address is registered. The email-first v2 flow does not. Only v2 should ever be enabled. |
| Login, logout, password hashing | Present. Argon2 behind a bounded work queue; equal work for unknown addresses; a fresh token on every sign-in; `HttpOnly`, `Secure`, `SameSite=Lax`, `__Host-` cookie; only the token digest is stored. |
| Rate limiting and abuse prevention | Present for sign-in, registration, recovery and verification. In-process, correct for the single worker production runs. |
| Password recovery, email verification | Present. Recovery revokes every session of the account. |
| CSRF | Present: the unsafe-request guard checks origin on every unsafe account, billing and Spaces route (683 tests). |
| Suspension | Partly. `is_active` is enforced at sign-in and on every session lookup, but **there is no operator command to suspend or reinstate an account**. |
| Session invalidation by the customer ("sign out everywhere") | **Missing at the HTTP layer.** The database authority exists (`v1_revoke_sessions`, migration `0013`) and is unexposed. |
| Password change while signed in | **Missing.** |
| Account deletion | **Missing.** Only the `deletion_requested_at` column exists. Needs a retention decision from the owner. |
| Full synthetic lifecycle test | **Missing** as one end-to-end test; the parts are tested separately. |

**Dependency that blocks the missing items.** The account-security work follows a slice plan
(V1-01, V1-02A to V1-02E) that is referenced in code and tests but is not recorded in the
repository. Slices 02A to 02C are done: the schema and two private `SECURITY DEFINER` authorities,
owned by an executor role and callable only by a service role, both `NOLOGIN` and
operator-provisioned. Migration `0014` states that no production-wide guarantee holds "until V1-02E
removes" the legacy route by which the runtime role writes sessions directly. Every missing item
above changes session or account state, so each belongs behind those authorities, not on the legacy
path the plan is retiring. Building them requires the plan for V1-02D and V1-02E (in particular how
the application is meant to act as the service role) or the owner's agreement to a plan written
from the code. See "Owner decisions required".

### Milestone 4: reasoning controls (backend implemented, interface not started)

Implemented on `feat/reasoning-modes-v1`: an optional `reasoning_mode` on `/api/chat`, validated
and permission-checked in the backend, applied at the single OpenAI seam outside the financial
reservation, default off, with no mapping in source. Reference: `candidate/REASONING_MODES.md`.
Evidence, all on `59ec9d1`: `provider-ceilings-postgres` run `43673630186` (52 PostgreSQL tests and
375 finance and reasoning-mode tests), `subscription-foundation-verification` run `43673633447` and
`safety-helpfulness-regression` run `43673636732`, all successful. Remaining:
the interface control, saved preferences (needs accounts), paid permissions taking effect (needs
billing entitlements on the chat request), a mode on Spaces sends, and owner decision 9.

Verified on 2026-11-14 from OpenAI's model page for `gpt-6-luna`
(`developers.openai.com/api/docs/models/gpt-6-luna`):

* `reasoning.effort` accepts `none`, `low`, `medium` (the default), `high`, `xhigh` and `max`. A
  three-level QUICK / BALANCED / DEEP control is therefore genuinely supported; no simulation is
  needed. The application already sends `reasoning.effort` on the Responses API and currently
  configures `low` (`OPENAI_REASONING_EFFORT`).
* Limits: 1,050,000-token context, 128,000 maximum output tokens.
* Published prices per million tokens: input $0.10, cached input $0.01, cache writes $0.125, output
  $0.50; above 272,000 input tokens the whole request is billed at 2x input and cache rates and 1.5x
  output. These equal the prices the finance tests configure. Batch, Flex, regional and Fast-mode
  modifiers exist; the application pins the standard tier when Financial Safeguards are on.
* Not stated on that page: that reasoning tokens are billed as output tokens, and that
  `max_output_tokens` bounds reasoning and visible tokens together. `kalillac_finance/cost.py`
  relies on both. Confirm them in the reasoning guide before activating Financial Safeguards.

Design constraints already known: which effort each mode maps to is a product decision (owner);
higher effort spends more of the same `max_output_tokens` on reasoning, so DEEP needs its own output
ceiling and therefore a larger worst-case reservation, which the request ceiling must allow; mode
permission must be checked in the backend from the verified entitlement.

### Milestone 5: file generation and upload intake (cores only)

Implemented on `feat/file-generation-v1`: `kalillac_files.generate(spec, format)` produces real PDF,
DOCX and XLSX files, plus CSV, Markdown, TXT and JSON, from a validated plain-data specification.
Reference: `candidate/FILE_GENERATION.md`, which also lists the limits and what is not built.
`kalillac_files.intake.inspect(data, name)` validates one uploaded PDF, DOCX, XLSX or text file by
content and extracts bounded text, refusing archive bombs, path traversal, macros, encrypted files,
XML entity attacks and malformed files with stable codes.

Evidence: `tests/test_file_generation.py` (155 tests, each generated artifact reopened with an
independent reader) and `tests/test_file_intake.py` (63 tests, hostile files built in the tests);
CI workflow `file-generation-verification`, run `43675051766` successful for the generation core on
`8ed456c`. Nothing in the application imports the package and the production dependency lock is
unchanged.

Not built, and needing customer accounts first: the upload and download routes, storage, quotas,
cleanup, the worker process (with time and memory limits) that must run both cores off the chat
path, the model tool, and editing.

## Known defects and environment limits

* **Fixed here, present on the production commit:** four cases of
  `test_unsafe_request_guard.py::test_signed_in_bodyless_routes_never_read_a_body` began failing on
  2026-11-13 on every branch of the release lineage, including `54e38ecf`. The shared test world
  seeds sign-in sessions that expire 14 days after a hardcoded 2026-10-30, and the billing router
  checks them against the real clock. It is a test-fixture defect, not a product defect: no
  application code changed. `subscription-foundation-verification` is red on any commit without
  the fix. Other tests with hardcoded 2026 dates use an injected clock and still pass.
* `candidate/tests/test_frontend_trust_presentation.py` drives a real browser and is sensitive to
  load: it passed alone (215 tests) and failed intermittently with "unknown scenario" while another
  full test run was using the same machine and browser. Run it on an otherwise idle machine.
* `candidate/tests/test_product_identity_aroge.py` fails locally (16 cases, same on the untouched
  baseline) without provider configuration in the environment.
* `candidate/tests/test_v1_schema.py` (five `hostile_diagnostics` cases) uses `/proc` and
  `O_NONBLOCK`; on Windows they fail and can hang a piped run. Same on the untouched baseline.
  They run on Linux in CI. Run Windows suites one file at a time with a time limit.
* `candidate/tests/test_account_ui.py::test_built_account_entry_is_noindex_and_outside_the_sitemap`
  needs the built site (`site/dist`), absent in a fresh worktree.
* The PostgreSQL suites refuse to run outside the disposable GitHub Actions job by design; there
  is no local PostgreSQL. Dispatch `provider-ceilings-postgres` on the branch instead.

## Regression evidence for `ee1564a`

* `subscription-foundation-verification`, GitHub Actions run `43672294537` on `ee1564a`: **success**,
  both jobs (Linux suites and the PostgreSQL job, which applies the whole migration chain).
* `provider-ceilings-postgres`, run `43671417421` on `a5ebb52`: **success** (52 PostgreSQL tests and
  323 finance tests, none skipped). `ee1564a` changes only a test fixture and this document.
* Local Windows run of all 126 test files, one file at a time: 8,897 tests passed. Twelve files
  skip some or all of their tests locally: the seven PostgreSQL suites (CI only, by design) and five
  browser-driven frontend suites. The only failures are the environment limits listed above (built
  site, provider configuration, Linux-only diagnostics).
* Not run: `safety-helpfulness-regression` and `code-structure-frontend-verification` (they trigger
  on pull requests; no pull request has been opened), and the deploy-tooling suite (no tooling file
  changed).

## Owner decisions required

1. **Financial budgets:** the fifteen `KALILLAC_FINANCE_*` values (day and month ceilings for
   GLOBAL, FREE, PAID, OpenAI, Brave, Tavily; the minimum headroom percent; the FREE and PAID
   request ceilings).
2. **Prices:** confirm the configured provider prices against the providers' current price lists,
   plus the OpenAI overhead tokens and Tavily credit maxima.
3. **Migration `0016`:** approval to apply it to production.
4. **Activation:** approval of the tooling update once written, and an activation window.
5. **Account-security slice plan:** supply the plan for V1-02D and V1-02E, or approve one being
   written from the code. This is what Milestone 2's missing items wait on.
6. **Account deletion:** what is deleted, what is retained (billing records, the financial ledger
   holds no identity), and after how long.
7. **Uncommitted account worktree:** discard or keep (see above).
8. **Free Chat smoke test:** the post-activation end-to-end test for `54e38ecf` is still unreported.
9. **Reasoning modes:** which `reasoning.effort` value QUICK, BALANCED and DEEP each use, and which
   modes Free and Premium may select.
10. Later milestones: subscription price, Stripe product and price IDs, billing interval, trial,
    refund policy, Premium allowances. Nothing has been assumed.
11. **PDF fonts:** whether to bundle a Unicode font so generated PDFs can carry text beyond Western
    European. Until then such documents are refused as PDF and generate as DOCX.

## Next autonomous action

In this order; each is independent of the open owner decisions unless it says otherwise.

1. Record the CI result of `file-generation-verification` for the intake commit on
   `feat/file-generation-v1`.
2. Milestone 4 interface: the mode control in the chat frontend, sending `reasoning_mode` and
   handling `403 reasoning_mode_not_allowed`, shown only when the backend reports modes enabled.
   The frontend suites need a real browser and an idle machine, and the web root is deployed
   separately from the backend release.
3. Milestone 5 worker: a subprocess runner with a hard time and memory limit for `generate()` and
   `inspect()`, still unexposed, so the cores are safe to call once accounts exist.
4. Milestone 1 release tooling for an enforcement-on release, with its disposable-systemd tests.
5. Milestone 2: once decision 5 is answered, the operator suspension command, sign-out-everywhere,
   password change and deletion behind the private authorities, then one end-to-end synthetic
   lifecycle test.
