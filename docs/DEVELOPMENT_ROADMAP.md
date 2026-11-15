# Kalillac AI development roadmap and progress record

The single place a new development session reads first. It records what is verified, not what is
planned to be true. Update it whenever a milestone's state changes.

States used here, never interchangeable:
**IMPLEMENTED** (code exists) · **DEVELOPMENT VERIFIED** (passes the required development tests) ·
**PRODUCTION READY** (deployment prerequisites verified) · **PRODUCTION ACTIVE** (authorized,
deployed, enabled and verified in production).

Last updated: 2026-11-15 (file assistant integration).

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
| `feat/file-generation-v1` | same worktree | Milestone 5 generation and intake cores and the isolated worker, stacked on the Milestone 4 branch. |
| `feat/document-quality-v1` | same worktree | The document quality system (designed PDF, Word, Excel and PowerPoint output), stacked on the files branch. **Under independent Codex audit (read-only); not changed while the audit runs.** |
| `feat/file-assistant-v1` | same worktree | **Current development branch.** GPT-6 Luna file generation and editing integrated into the product (default off), stacked on the document-quality branch. |

The five branches are a stack: each contains the one before it. The worktree has its own `.venv`
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
| 3 | Stripe billing and subscriptions | IMPLEMENTED in part, off; its suites pass in CI. Refunds and disputes now recorded for operator review (`feat/billing-cases-v1`) | The link to financial scopes and Premium entitlements on the chat request is still missing; `docs/STRIPE_TEST_MODE_SETUP.md` is the test-credential procedure |
| 4 | Reasoning toggle controls | Backend and chat control DEVELOPMENT VERIFIED, off (page mark and flag); saved preference and paid permissions wait on accounts and billing | `candidate/REASONING_MODES.md` |
| 5 | File creation, uploads, editing | Generation, intake, editing, the worker, the GPT-6 Luna file assistant, storage, `/api/files` routes and delivery in the chat IMPLEMENTED and DEVELOPMENT VERIFIED behind `KALILLAC_FILES_ENABLED` (default off). **Not owner accepted; not release ready**: owner review found issues, an independent audit is running, and D1-D4 are unresolved (see "Audits") | `candidate/FILE_GENERATION.md` |
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

### Milestone 3: Stripe billing (starting point)

Read from the code on 2026-11-14. Its test suites (billing, concurrency, reconciliation, the
operator tool, the Stripe gateway, and the PostgreSQL billing verification) pass in
`subscription-foundation-verification`; nothing here was exercised against Stripe itself.

| Capability | State |
|---|---|
| Checkout, customer mapping | Present. Server-generated attempt id as the Stripe idempotency key; pinned parameters; nothing read from the request body |
| Subscription creation, renewal, cancellation | Present, driven by `checkout.session.completed` and `customer.subscription.created/updated/deleted`. Cancellation and payment methods go through the Stripe customer portal |
| Verified signatures, replay and duplicate protection | Present. Signature checked upstream; processed event ids recorded in the same transaction; live/test mode mismatch refused |
| Out-of-order events | Present. Arrival order is never trusted: the current subscription is fetched from Stripe under the account lock |
| Entitlement update and revocation | Present. Paid only while Stripe reports active, trialing or past_due for an allowed Price; a paid entitlement always expires at the period end plus a grace period, so a lost cancellation cannot leave access open |
| Payment failure | Present as policy: `past_due` stays paid during dunning, every other non-paying status is free |
| Status reconciliation | Present: webhooks plus an operator command for one account |
| Customer billing status | Present: `GET /api/account/billing` |
| **Refunds and disputes** | **Implemented, default off with billing** (`feat/billing-cases-v1`): `charge.refunded` and `charge.dispute.*` become billing cases (migration 0017) with facts fetched from Stripe under the account lock, deduplicated, reopened when Stripe's facts change; `tools/billing_cases.py` lists, acknowledges and closes them. No automatic refund and no entitlement change (owner decision 2026-11-15); what a refund or a lost dispute should do to access stays the owner's policy decision. No amount or currency is stored, by the existing data rule |
| **Link to Financial Safeguards** | **Missing.** Nothing issues the request-bound paid entitlement, so every chat request is charged to the FREE scope and Premium-only reasoning modes are refused for everyone. This is the one seam that turns a verified subscription into the PAID scope, its request ceiling and its modes |
| **Per-customer dollar budget** | **Missing** (deferred here from Milestone 1) |
| **Lifecycle run against Stripe test mode** | **Not done.** The procedure is written (`docs/STRIPE_TEST_MODE_SETUP.md`); it needs the owner's test-mode keys, a test Price and a webhook secret on the staging server |

The link to Financial Safeguards depends on Milestone 2, because the chat request must first
authenticate the account through the V1 authority.

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

### Milestone 5: file generation, editing and delivery

**File assistant integration** (`feat/file-assistant-v1`, default off): a chat message asking for a
file (or carrying attachments) from a signed-in account with file access goes to GPT-6 Luna with the
file instructions and three tools (create_file, read_file, edit_file). Every request is validated,
rendered in the isolated worker, checked (PDF layout, formulas, slide fit, and totals that add up),
stored for the account (random ids, quotas, 30-day expiry) and delivered as download links in the
chat reply. `/api/files` adds upload (checked by intake), list, download and delete, all behind the
unsafe-request guard and owner checks. The tier that may use files is configuration, `paid` by
default, pending the owner's Free/Premium decision. Verified locally with a scripted model: the real
`/api/chat` turned a deck request into a stored, downloadable PPTX; refusals are repaired by the
model in the loop; another account's file is never reachable; the chat page renders file links and
the attach control in a real browser. Not yet: streaming, web search inside the file path, the
request budget on the file path, a live run against GPT-6 Luna (needs the owner's API key in a
non-production environment). Reference: `candidate/FILE_GENERATION.md`.

#### Earlier: cores

Implemented on `feat/file-generation-v1`: `kalillac_files.generate(spec, format)` produces real PDF,
DOCX and XLSX files, plus CSV, Markdown, TXT and JSON, from a validated plain-data specification.
Reference: `candidate/FILE_GENERATION.md`, which also lists the limits and what is not built.
`kalillac_files.intake.inspect(data, name)` validates one uploaded PDF, DOCX, XLSX or text file by
content and extracts bounded text, refusing archive bombs, path traversal, macros, encrypted files,
XML entity attacks and malformed files with stable codes.

`kalillac_files.worker` runs both in a separate process with a hard time limit, memory and CPU
limits (POSIX), a concurrency cap, bounded output and an environment that carries no secret.

Evidence: `tests/test_file_generation.py` (155 tests, each generated artifact reopened with an
independent reader), `tests/test_file_intake.py` (63 tests, hostile files built in the tests) and
`tests/test_file_worker.py` (31 tests with real child processes); CI workflow
`file-generation-verification`, run `43676934103` successful on `337cb88` with all 249 tests and
none skipped. Its first Linux run (`43676033805`) failed and found a real defect: the spreadsheet
library imports numpy, which starts threads the child forbids itself; fixed in `337cb88`. Nothing
in the application imports the package and the production dependency lock is unchanged.

Not built, and needing customer accounts first: the upload and download routes, storage, quotas,
cleanup and the model tool.

**Document quality system** (`feat/document-quality-v1`, after the owner's 2026-11-15 directive that
output must be exceptional, not merely functional). Each format has a design system and is checked
by reading the file back and, in development, by rendering it in the Microsoft application itself:

* PDF (`7f7bc9f`): Typst with a designed template, bundled OFL fonts for Latin, Greek, Cyrillic,
  Arabic, Hebrew, Devanagari, Thai and CJK (owner decision 11, resolved), vector charts, tagged PDF,
  right-to-left mirroring, and a PDFium layout inspector that refuses overflow, blank pages and
  missing glyphs.
* Word (`7f7bc9f`): real styles, contents and page fields, embedded fonts, complex-script runs;
  rendered and inspected in Microsoft Word, including Arabic.
* Excel (`b8f4d04`): a formula engine matching 147 Excel-recorded cases and all 438 formulas of the
  benchmark forecast, financial-model conventions, native charts, dashboards.
* PowerPoint (this commit): eleven layouts, native editable charts and tables, text measured with
  Calibri's metrics so it never overflows (PowerPoint's own line counts agree for every text box
  and cell on the test decks), balanced headings, right-to-left mirroring, speaker notes;
  rendered and inspected in Microsoft PowerPoint.
* Word and PowerPoint packages carry only the file's own record and are byte-for-byte
  reproducible (`c8c2985`; `file-generation-verification` run `43691236898` successful, 601 tests,
  none skipped).
* Editing (the commit after `c8c2985`): `editing.outline()` addresses what an uploaded Word,
  PowerPoint or Excel file contains and `editing.edit()` applies replace, set, insert, delete,
  add-row and set-cell operations to a copy, keeping the customer's formatting; Word edits can be
  tracked changes; Excel is edited in its XML (charts, styles and shared strings byte for byte)
  and every stored formula value is recomputed or cleared, never stale. Verified in the
  applications: Word listed and accepted 9 tracked revisions; Excel's recalculation of an edited
  forecast agreed with all 438 recomputed values; PowerPoint opened and rendered the edited deck.
  Worker tasks `run_outline` and `run_edit`; intake now reads PPTX.

Quality benchmark (`tests/quality_benchmark.py`), state of the eight owner-named tasks:

| Task | State |
|---|---|
| Business report (PDF, DOCX) | Built, rendered and inspected; not owner accepted |
| Financial workbook, cross-sheet workbook | The forecast workbook (inputs, monthly forecast, summary, loan, dashboard) covers both; verified against Excel; not owner accepted |
| Executive presentation | Built, rendered in PowerPoint and inspected; not owner accepted |
| Multilingual document | Built (English, Spanish, Arabic, Japanese, Hindi, Russian in one document), PDF and Word rendered and inspected; plus the Arabic report and deck; not owner accepted |
| Research document, Word proposal | Built, rendered and inspected (editorial theme; US Letter proposal); not owner accepted |
| Complex editing | Built: the business report restated as tracked changes, and the forecast with a changed input; not owner accepted |

`tools/render_benchmark.py` writes all of it to a folder with a README for the owner. Writing the
new entries found and fixed: a crash on any Word summary over 255 characters, the PDF inspector
treating intended hanging punctuation (and invisible line-end spaces) as overflow, line charts
squeezed by a forced zero baseline, hyphenated cover subtitles, short tables split across pages,
and right-to-left headings and callouts in a left-to-right document set from the left (PDF and
Word).

Evidence: `file-generation-verification` run `43684790630` on `b8f4d04` successful (536 tests, none
skipped); the PowerPoint commit's run is recorded under "Regression evidence" when it completes.
Locally on this commit: all 601 file tests pass on Windows, and on Linux (WSL, Python 3.12.15, the
exact CI requirements) with none skipped. The PowerPoint Linux run found a real defect, the worker
not starting the typesetter's thread pool before forbidding threads when rendering a deck's
artwork; fixed and tested.

## Audits and owner review

* **Owner review of the document-quality benchmark (2026-11-15):** visual results promising, benchmark
  **not accepted**. Observations: financial inconsistencies, Word pagination, tracked editing, Arabic
  text handling, document-content accuracy. An independent, read-only Codex audit of
  `feat/document-quality-v1` covers them; its findings will be supplied by the owner. That branch is
  not changed while the audit runs; corrections will follow the findings.
* **Found here while integrating, recorded for the audit:** the benchmark's complex edit
  (`quality_benchmark.report_edit`) restates revenue ($18.4M to $18.6M) but leaves gross profit,
  gross margin and the income statement's change column as they were, and adds the audit-adjustment
  row between cost of goods sold and gross profit. That is exactly the class of financial
  inconsistency the owner observed. The file assistant's instructions now require every derived
  figure to change with its source, and `consistency.py` refuses a total row that does not add up,
  but neither yet checks derived ratios in prose or in an edited document.
* **D1-D4 from the previous Codex audit:** these defect identifiers are not recorded anywhere in the
  repository, the worktrees or the handoff logs (the "D" numbers in the account-security handoff logs
  are architecture decision ids, not these). The owner is asked to supply the list. Until corrected
  and independently verified, everything they may affect is treated as not release ready and stays
  unexposed (every file feature is behind `KALILLAC_FILES_ENABLED`, off by default).

## Known defects and environment limits

* **Fixed here, present on the production commit:** four cases of
  `test_unsafe_request_guard.py::test_signed_in_bodyless_routes_never_read_a_body` began failing on
  2026-11-13 on every branch of the release lineage, including `54e38ecf`. The shared test world
  seeds sign-in sessions that expire 14 days after a hardcoded 2026-10-30, and the billing router
  checks them against the real clock. It is a test-fixture defect, not a product defect: no
  application code changed. `subscription-foundation-verification` is red on any commit without
  the fix. Other tests with hardcoded 2026 dates use an injected clock and still pass.
* `candidate/tests/test_frontend_trust_presentation.py` drives a real browser and fails
  intermittently on this machine with "unknown scenario" (a few of 215 cases). It is not caused by
  the work here: with byte-identical frontend files, harness and fixtures, the untouched baseline
  worktree failed 1 of 6 runs and this worktree 2 of 6. Re-run it; treat CI as authoritative.
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
* Not run for `ee1564a`: `safety-helpfulness-regression` and `code-structure-frontend-verification`
  (they trigger on pull requests; no pull request has been opened), and the deploy-tooling suite (no
  tooling file changed).

## Regression evidence for `59ec9d1` (reasoning modes) and `337cb88` (files)

* `59ec9d1`: `provider-ceilings-postgres` `43673630186`, `subscription-foundation-verification`
  `43673633447` and `safety-helpfulness-regression` `43673636732`, all successful. Local Windows run
  of all 126 test files: 8,765 passed, with only the environment limits above and the intermittent
  browser test failing.
* `337cb88`: `file-generation-verification` `43676934103` successful. The three file suites also pass
  on Linux locally (see below). The commits after `59ec9d1` add files only; no application module
  changed.
* Not run on any branch: `code-structure-frontend-verification` (no frontend file changed).
* `feat/file-assistant-v1` (`36e1053`, then `95573dd` and `f43c61c`): `file-generation-verification`
  `43742343520` (715 tests, none skipped), `provider-ceilings-postgres` `43742375159`,
  `safety-helpfulness-regression` `43742382222` and `subscription-foundation-verification`
  `43744273219` successful. The first subscription run (`43742372419`) failed only on a pinned
  count: the Slice 3 group ran 917 tests with no failure (818 + 99 guard cases for the new
  `/api/files` routes), fixed in `95573dd`. `code-structure-frontend-verification` `43742378449`
  found a real defect (a file input in the app page, which must expose no upload control), fixed in
  `f43c61c`; its rerun is `43746443932`. Locally: a full Linux run of all 139 test files (9,083
  passed) whose only failures match the unmodified baseline exactly (no browser, built site or
  provider configuration in WSL).
* `feat/billing-cases-v1` (`d4efa40`): `provider-ceilings-postgres` `43745249227` (the whole migration
  chain through 0017 on PostgreSQL 16) and `subscription-foundation-verification` `43745246171`
  (1566 tests, none skipped) successful.
* `feat/document-quality-v1`, `file-generation-verification` (all file suites, none skipped):
  `43684790630` on `b8f4d04` (536 tests), `43691236898` on `c8c2985` (601, PowerPoint),
  `43700356071` on `a822f39` (642, editing), `43701878737` on `5b4259d` (653, benchmark), all
  successful. These commits change only `candidate/kalillac_files`, its tests and tools, the
  workflow and documents; no application module.

A disposable Linux environment for reproducing Linux-only failures exists in WSL at
`~/kalillac-files-venv` (Python 3.12 managed by `uv`, the production lock plus the document
libraries). It was created during this work, lives outside the repository and can be deleted.

## Owner decisions required

1. **Financial budgets:** the fifteen `KALILLAC_FINANCE_*` values. A worked proposal with the
   arithmetic is in `docs/FINANCE_BUDGET_PROPOSAL.md` (cautious launch: $10 a day, $150 a month at
   most across all providers); the owner chooses.
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
11. ~~PDF fonts~~: resolved 2026-11-15 (approved); bundled as described under Milestone 5.

Resolved by the owner on 2026-11-15, recorded so they are not asked again: decision 5 (write the
V1-02D/E plan from the code, keeping the private database authorities and adding no legacy path),
decision 7 (keep the uncommitted worktree untouched), and the provisional reasoning mapping for
development (QUICK low, BALANCED medium, DEEP high; which modes Free and Premium may use stays
open). Also approved: refund and dispute webhook handling with no automatic refunds, Stripe work
against mocks with a written test-credential procedure, proposals (not decisions) for budget values
and retention rules, and a staging design. Still forbidden: anything in production, applying
`0016` there, enabling enforcement, live billing, unapproved paid infrastructure.

## Next autonomous action

In this order; each is independent of the open owner decisions unless it says otherwise.

1. Codex audit findings on the document-quality work, when supplied: correct each, with tests and a
   rendered check, on a branch of its own.
2. Milestone 2: implement `docs/V1-02DE-ACCOUNT-SECURITY-PLAN.md` (written 2026-11-15 from the code
   and the handoff records): D1 the private S connection, D2 the remaining authorities,
   D3 route wiring (sign-out-everywhere, password change), D4 the operator tool (suspension,
   reinstatement, deletion requests), E1 to E3 the legacy-route removal, the operator transition and
   the synthetic lifecycle test. Its section 5 is the deletion-retention proposal for the owner.
3. Milestone 3: the link from a verified subscription to the PAID scope and Premium entitlements on
   the chat request (which also gives files and reasoning modes their tier check in the chat).
4. Milestone 1: proposed budget values with their cost calculations; release tooling for an
   enforcement-on release.
5. Spaces re-verification and the owner administration dashboard. The staging design is written
   (`docs/STAGING_ENVIRONMENT_DESIGN.md`): port 8002 was used by an earlier V31 staging and must be
   checked on the host by an operator first (8003 otherwise); production's unit is named
   `kalillac-ai-fastapi-staging.service`. The deletion-retention proposal is section 5 of the
   V1-02D/E plan.
6. Files under Financial Safeguards: give the file assistant its own request scope with the PAID
   request ceiling (today it fails closed when provider ceilings are on).
7. Pull requests for the stacked branches, prepared for independent review, when the owner wants
   them.
