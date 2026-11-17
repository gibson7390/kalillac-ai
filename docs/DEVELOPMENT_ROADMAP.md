# Kalillac AI development roadmap and progress record

The single place a new development session reads first. It records what is verified, not what is
planned to be true. Update it whenever a milestone's state changes.

States used here, never interchangeable:
**IMPLEMENTED** (code exists) · **DEVELOPMENT VERIFIED** (passes the required development tests) ·
**INTEGRATED** (wired into the application behind its flag) · **STAGING READY** (verified in a
staging environment) · **OWNER ACCEPTED** (the owner accepted the result) · **PRODUCTION READY**
(deployment prerequisites verified) · **PRODUCTION ACTIVE** (authorized, deployed, enabled and
verified in production). A Codex finding is "corrected, awaiting verification" until Codex verifies
the correction commit; green CI never stands in for that.

Last updated: 2026-11-15 (Codex audit corrections, password-reset authority).

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
| `feat/file-assistant-v1` | same worktree | GPT-6 Luna file generation and editing integrated into the product (default off), stacked on the document-quality branch. |
| `feat/billing-cases-v1`, `feat/file-controls-v1` | same worktree | Refund and dispute records; file requests under the provider controls; V1-02D D1 and the sign-out authority (0018). |
| `feat/account-security-d2` | same worktree | The password-reset authority (0019) and the frontend count-gate correction. |
| `fix/codex-audit-corrections` | same worktree | Corrections for both Codex audits; evidence in `docs/CODEX-AUDIT-CORRECTIONS.md`. |
| `fix/codex-reverification-2` | same worktree | **Current development branch.** Corrections for Codex's reverification findings, stacked on the branch below. |
| `feat/account-security-d2-change` | same worktree | The password-change authority (0020), the total-row rule for edited documents (Codex Q4), the registration activation guard (0021) and Arabic in IBM Plex Sans Arabic in Word. |

The branches are a stack: each contains the one before it. The worktree has its own `.venv`
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
| 5 | File creation, uploads, editing | Generation, intake, editing, the worker, the GPT-6 Luna file assistant, storage, `/api/files` routes and delivery in the chat IMPLEMENTED, DEVELOPMENT VERIFIED and INTEGRATED behind `KALILLAC_FILES_ENABLED` (default off). **Not owner accepted; not release ready**: the Codex findings are corrected but not yet independently verified, two are partly open (Arabic ligature extraction, Word field caches), and the customer workflow is incomplete (see "Audits") | `candidate/FILE_GENERATION.md`, `docs/CODEX-AUDIT-CORRECTIONS.md` |
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
| Account deletion | **Request only, unexposed** (migration 0023): an operator (role P) or the signed-in customer (through S) can mark an account, which ends every session and blocks sign-in. **No erasure**: it needs the owner's retention decisions (plan, section 5). |
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

**Progress on the V1-02D/E plan** (`feat/file-controls-v1`, all default off, nothing exposed):

* **D1, the private S connection: implemented** (`2d33196`). `kalillac_accounts/v1_authority.py` owns
  the only engine logged in as S, calls the fixed authorities with bound parameters and nothing else,
  validates every result against the function's contract, and discards any connection whose
  transaction failed. Building it requires a strict credential bound to the role, database instance
  and deployment (protected-file loader), checks S's identity on every checkout and verifies S's
  boundary (no attribute but LOGIN, no membership, no table or column right, exactly the reviewed
  functions by owner, `search_path`, body digest and ACL, no other SECURITY DEFINER function). Not
  wired into startup: S stays NOLOGIN until the operator transition (E2).
* **D2, first part: the sign-out authority, migration 0018** (`211c337`). `v1_end_session` revokes
  exactly the presented session and answers `ended` for every well-formed call. The upgrade proves the
  exact 0014 catalog first and the exact three-function result after; the downgrade proves both ways.
* **D2, second part: the password-reset authority, migration 0019** (`03cbf97`,
  `feat/account-security-d2`). `v1_reset_password` takes a reset-token digest, an Argon2id hash the
  caller computed and an operation id; it locks the account then the token, charges the reserved
  `recovery` admission class with revocation's windows, replaces the hash, clears
  `password_reset_required`, consumes every open reset token, revokes every session, advances the
  epoch and records the reserved `password_reset` event at one effective time; replays apply nothing.
  E gains only the reset's column grants. `v1_schema` classifies the fourth function exactly and
  `V1Authority.reset_password` calls it. DEVELOPMENT VERIFIED on PostgreSQL 16:
  `subscription-foundation-verification` `43781961163` (130 V1-02D tests, none skipped) and
  `provider-ceilings-postgres` `43781963838` (whole chain through 0019).
* **D2, third part: the password-change authority, migration 0020** (`00197bf`,
  `feat/account-security-d2-change`). `v1_change_password` is proven by the exact original session
  (revocation's own proof and lock order) and the stored hash the caller has just verified, charges the
  customer's `manual` admission budget, replaces the hash, ends every other session and keeps the
  presented one, and records a new `password_change` event (0012's event-type constraint widened in the
  same migration; the downgrade refuses while such an event exists). The epoch does not move (plan,
  section 4). `provider-ceilings-postgres` `43789685681` success (chain through 0020); the 176-test V1-02D
  group passed in `subscription-foundation-verification` `43789681911` (none skipped). DEVELOPMENT VERIFIED.
* **Owner decision 14 (conditional removal of `v1_verify_email`).** Verified from the code: after
  activation, only the registration role inserts accounts, each with a registration intent consumed in
  the same transaction (proof the customer received the emailed token) and `email_verified_at` set; no
  role can change `email` or `email_verified_at`; recovery runs on its own reset tokens. **One condition
  failed:** the activation transition did not count unverified accounts (the CHECK constraint only
  required the operator-recorded count to be 0). **Corrected by migration 0021** (`540a523`): the
  transition is refused while any account is unverified, exact under concurrency. PostgreSQL regressions
  for every condition: `subscription-foundation-verification` `43792363270` (192 tests, none skipped),
  `provider-ceilings-postgres` `43792366823`. With 0021 all six conditions hold, so `v1_verify_email` is
  dropped from the V1-02D/E design and the legacy verification route is retired in E1.
* **D2: quarantine and reinstatement by the operator role P, identity guard v3, migration 0022**
  (`feat/account-security-d2-quarantine`, checkpoint `53869d73a509df07643e91dde7dff18ba6bbf984`). Codex
  independently passed its 19 PostgreSQL tests at that checkpoint. Not owner accepted.
* **D2: deletion requests, migration 0023** (`feat/account-security-d3-deletion-requests`, on `53869d7`).
  `v1_request_deletion` (P) and the customer's `v1_request_own_deletion` (S, proven and admitted as a
  password change is) set `deletion_requested_at`, move the epoch strictly forward, revoke every session
  and record `account_deletion_request` at one effective time; replays apply nothing. E gains UPDATE on
  the marker only; guard v3 is unchanged. No withdrawal and no erasure (plan, section 4). Checkpoint
  `49540698736f95d9309d4902915a0b4e50f4ca4a`: source reviewed, PostgreSQL CI verified, HTTP login
  verification pending. Owner acceptance pending.
* **D3, first route: V1 login** (`feat/account-security-d3-v1-login`, on `4954069`). In V1 mode
  `POST /api/account/login` verifies the password and takes its session only from `v1_issue_session` as
  S; a refusal is 401 with no cookie, an unavailable authority 503 with no cookie, never the legacy path.
  V1-off login and the startup gate are unchanged; the application passes no authority. HTTP tests on
  disposable PostgreSQL cover sign-in before a deletion request, refusal after either kind of request,
  overlapping logins and requests, and unavailable authorities. At `ac9f12c`: Codex independently found
  the deletion-related sign-in defect resolved in the D3 HTTP fixture and the 303-test PostgreSQL group
  passing. Its next finding (a malformed authority answer gave 500 and held the limiter slot) was
  corrected at `d0247a4`, and Codex independently verified the limiter leak resolved. Its follow-up (an
  outcome that is not a string but compares equal to a contract value) was corrected at `dcc0d44`, and
  Codex independently verified it resolved (23 focused login tests, 11 PostgreSQL HTTP tests, no new
  finding). Owner acceptance pending.
* **D3, remaining routes** (`feat/account-security-d3-routes`, on `dcc0d44`). In V1 mode: sign-out
  through `v1_end_session`; new `sessions/revoke-others` and `sessions/revoke-all`; new `password`
  (current password proven, sign-in limiter shared); new `deletion-request` (the customer's own request,
  no erasure); recovery confirmation through `v1_reset_password`; no email-verification routes. Each
  only through its authority as S, never the legacy path; real PostgreSQL HTTP tests prove R writes
  nothing. Implemented; awaiting Codex.
* **D5, migration 0024** (`feat/account-security-d5-credential-path`, on the D3 branch). Reset tokens are
  issued only by `v1_issue_reset_token` as S, and V1 sign-in, password change and the deletion request
  read the stored hash only through `v1_credential` as S; HTTP tests pass with R's read of
  `users.password_hash` revoked. Implemented; awaiting Codex.
* **D4, operator tool** (`feat/account-security-d4-operator-tool`, on D5): `tools/v1_account_support.py`
  through P with P's own credential, dry-run by default, operation ids, fixed reason codes and an
  append-only record; bounded export without credentials. Implemented; awaiting Codex.
* **E1a, E2, E3** (`feat/account-security-e1-e3`, on D4): migration 0025 (the inert row-lock anchor),
  the runtime login's final account grants, the operator transition with rollback
  (`tools/v1_security_transition.py`), the startup gate qualifying an active latch bound to deployment and
  release, and the lifecycle test through the routes under the final grants. Implemented; awaiting Codex.
  CI on `492ad2f` failed once and was right to (subscription run 44090797912, the root-run bootstrap
  verifier, "unrelated column grant refuses"): the E1a column check had been inserted inside the startup
  privilege loop, so a stray grant to the registration role on an unrelated table no longer stopped
  startup. Fixed in `dc24d1bdb7a3dc076047bc323de2779b01df0339` with regression tests that need no root
  (six, all failing on `492ad2f`); green on it: subscription 44094320838, provider 44094326597, safety
  44094334097. Development verification only, not an independent one.
  E1b (entitlement and billing grants) waits on Milestone 3's reviewed billing path; erasure after the
  owner's retention decisions.
* **Premium seam, account pages, chat polish and the journey** (`feat/premium-paid-scope-b2`, on E1–E3):
  the PAID scope and paid reasoning modes from the V1-verified entitlement (`paid_access.py`); the account
  page's sessions, password and deletion sections with `GET /api/account/security`; per-block code copy;
  the chat's account link; and `test_v1_premium_journey_postgres.py`, the whole Premium journey through
  the real application on disposable PostgreSQL under the final grants. Implemented; awaiting Codex.
  CI green at `84cc7f1` (subscription 44102235903; provider 44098090955, safety 44098099108, frontend
  44098200220 and anonymous allowance 44098265034 on `abbe498`). The first subscription run (44098082640)
  was cancelled by the 20-minute job timeout with no failure; `84cc7f1` raises it to 45.
* **Staging provisioning** (`feat/staging-provisioning-m9`, on the Premium branch): until now nothing
  could stand up a new deployment except test fixtures. `tools/provision_deployment.py` creates a new,
  empty deployment's roles and database, migrates it, writes the protected files the application reads
  at startup and records the registration activation (the database itself refuses unless no unverified
  account exists); it only creates, marks what it creates, and refuses anything else, so it cannot touch
  another deployment's roles on a shared cluster. `deploy/staging/` holds the settings template (no
  allowance, budget, price or Stripe id is decided in it), the unit template, the web-root builder and
  the operator runbook. `tools/verify_provisioned_boot.py` runs the runbook's commands as root in CI:
  provision, the operator transition as an unprivileged identity, the controlled launcher from the
  template, real HTTP. Implemented; awaiting Codex. Not run on any host. CI green at `70a09ed`
  (subscription 44102277062; provider 44100305520 and safety 44100311426 on `aab49f6`).
* **Spaces under V1 authorization** (`feat/v1-single-authorization-a10`, on staging provisioning): with V1
  on, Spaces authorized accounts by the legacy session lookup, which does not check quarantine or the
  session epoch (it relied on their revocations). Every Spaces request now goes through V1-01's
  original-session authorization, as accounts and files do, and a send's save rechecks it inside the
  save transaction under the account's V1 lock, taken before the quota's lock on the same row so two
  saves for one account queue instead of deadlocking on a lock upgrade. Proved on PostgreSQL under the
  final grants, including that the quarantine case fails on the legacy path. Remaining legacy lookups
  in V1 mode (billing, usage, quota, email verification) are listed for the same change. Implemented;
  awaiting Codex. CI green at `5ce5750` (44102284051, 44102290116, 44102296120).
* **Spaces in the chat** (`feat/spaces-frontend-s1`, on A10): on a page marked `data-kalillac-spaces="on"`
  the chat asks once whether the account may use Spaces and only then shows a Spaces button: pick or
  create a Space and a conversation; an opened conversation is saved to the account (a bar says so; the
  "Temporary session" status and End & erase are hidden; files and reasoning modes are not offered);
  each message carries an idempotency key that a retry reuses, so it is never saved twice; a saved answer
  has Copy and no Retry; leaving returns to a new temporary conversation. Five browser tests (Edge) on
  the real page; the staging template turns Spaces on for the owner's listed account, and the boot
  verifier now starts the application with Spaces on (16 cases). Implemented; awaiting CI and Codex.
  The authoritative feature status is `docs/V1-EXECUTION-BLUEPRINT.md`.
* Known flaky test, to fix in its own checkpoint: `test_usage_enforcement.py::
  test_chat_queue_saturation_stays_busy_and_releases_the_lease[unbudgeted]` read the usage slot as
  `pending` once in CI (run 43982872312, D3); it reads the slot right after the response, before the
  lease release always lands. It passed 5 of 5 locally; the chat queue is untouched by D3.

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
the attach control in a real browser. Not yet: streaming, web search inside the file path, a live
run against GPT-6 Luna (needs the owner's API key in a non-production environment), counting a file
request against the account's daily chat allowance (an owner decision: whether a file request counts
as one chat), and the PAID scope for paying customers' file requests (Milestone 3). Reference:
`candidate/FILE_GENERATION.md`.

**File requests under the controls** (`a0df0fc`, `feat/file-controls-v1`): with provider ceilings or
request budgets on, each file request runs its model calls in its own control request and
RequestBudget (600-second deadline, one model attempt per tool round plus the reply). It is classed
as a signed-in account's request in the FREE scope, like every chat request until Milestone 3 issues
paid evidence, so a paying customer's file request is bounded by the FREE request ceiling for now.
Before this, the file path failed closed whenever provider ceilings were on. Stops raised by the
controls answer with the chat's stable codes.

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

* **Codex reverification (2026-11-16, against `6337c12`):** CODEX-D1 to D4, the 1904 date system, stale
  chart caches, the report's financial reconciliation and the About-sheet numbers are **independently
  verified resolved**. Remaining defects A to D and the exact-edit authorization are corrected on
  `fix/codex-reverification-2` (`e7abd44`, `7a5466c`, `b742ff6`, `61618c2`, `c274f90`) and await another
  targeted reverification. Word finalization, Arabic PDF logical text and general semantic consistency
  remain open. Details: `docs/CODEX-AUDIT-CORRECTIONS.md`.
* **Codex audits, findings supplied 2026-11-15** (the first audit's CODEX-D1 to CODEX-D4, and the
  document-quality audit of `6191b53`). Every correction, with the commit, how the original failure is
  reproduced, how the correction is verified and its CI run, is in `docs/CODEX-AUDIT-CORRECTIONS.md`.
  State: CODEX-D1 to D4, the 1904 date system, chart caches, PowerPoint fitting, the About sheet and
  the benchmark content (Q1, Q4, Q7, Q8, Q10) **corrected, awaiting Codex verification**. Arabic PDF
  extraction **partly corrected** (letters now exact; ligature order still reversed by PDFium and
  pypdf). Word page-field caches and the update prompt (Q3): **cause verified in Word, correction
  waits on an owner decision**. Arabic Word mirroring: **not reproduced in Word**; other viewers
  unverified. Codex's G1 to G3 (account authorization convergence, paid entitlements, refund and
  dispute handling) are the Milestone 2 and 3 work below.

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
* **D1-D4 from the previous Codex audit:** supplied by the owner on 2026-11-15 (see the first item).
  Until Codex verifies the corrections, everything they may affect is treated as not release ready and
  stays unexposed (every file feature is behind `KALILLAC_FILES_ENABLED`, off by default).

## Known defects and environment limits

* **Arabic PDF ligature order (open):** PDFium (Chrome's viewer) and pypdf reverse the two letters of
  a required two-letter ligature glyph inside right-to-left runs, so words containing lam-alef or
  another `rlig` pair are not found by search. Letters are otherwise exact since `dee4831`. Typst's
  `/ActualText` is not used by PDFium; a different export approach is needed. Clipboard and
  screen-reader behaviour not yet verified.
* **Word field caches (open, owner decision 12):** cached `PAGE`/`SECTIONPAGES` results are `1`; Word
  desktop recalculates them, viewers that do not show stale totals. The contents field is marked
  dirty, which is what makes Word ask to update fields on opening (verified in Word 16).
* **Cross-viewer checks unavailable here:** no LibreOffice (installing needs administrator rights), so
  Word, PowerPoint and Excel output is verified in the Microsoft applications only.

* **Regression found and fixed here:** `feat/file-assistant-v1` (`f43c61c`) and
  `feat/billing-cases-v1` made every unbudgeted chat call pass an explicit `timeout` to
  `_post_openai_responses`, which changed the existing call; 33 Linux tests in the provider-usage,
  request-budget and search-policy suites caught it (those suites are in no CI workflow, which is why
  CI stayed green). Fixed in `a0df0fc`: the existing call is made unchanged, and only the file
  assistant passes its wider timeout. Those two branch tips still carry it; use
  `feat/file-controls-v1` or later.

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
12. ~~Word contents and page fields~~: decided 2026-11-15: never trade page-number accuracy for the
    prompt; keep the accurate Word refresh; prototype LibreOffice finalization in isolation only. The
    prototype's evaluation is `docs/DECISION-12-WORD-FINALIZATION.md`: LibreOffice paginated 16 of 16
    benchmark headings like Word, finalized files opened with no prompt, and their cached contents equalled
    Word's own recomputation; cost 738 MB, ~280 MB and 16 to 56 s per document. **Owner acceptance
    needed** for a sandboxed finalization service before anything changes in the product.
13. ~~Arabic font~~: decided 2026-11-15: IBM Plex Sans Arabic in every format, after verification.
    Done for PDF (`dee4831`) and Word (`9c9a80d`, verified in Word 16 with the embedded font).
    **PowerPoint not changed:** a deck cannot embed the font (python-pptx has no embedding; PowerPoint's
    embedded-font parts are a proprietary compressed form), so a deck naming Plex would show an
    unpredictable substitute; decks keep Arial until embedding is solved.
14. ~~Email verification under V1~~: decided 2026-11-15 (conditional); the conditions now hold with 0021
    (see Milestone 2).

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

1. Codex findings still open: Arabic PDF ligature order; the Word fields once decision 12 is made;
   the rest of the semantic-editing contract (dependency checks for figures in prose and metrics, and
   typed facts shared by tables, charts and text; total rows are enforced on edits since `888293e`).
   Then ask Codex to verify the corrections against `fix/codex-audit-corrections` and `888293e`.
2. Milestone 2: continue `docs/V1-02DE-ACCOUNT-SECURITY-PLAN.md`. D1 and the sign-out authority
   (0018) are implemented; next the rest of D2 (reset, change, verification, quarantine,
   reinstatement, deletion request, role P), then D3 route wiring, D4 the operator tool, and E1 to E3
   (legacy-route removal, operator transition, synthetic lifecycle test). Its section 5 is the
   deletion-retention proposal for the owner.
3. Milestone 3: the link from a verified subscription to the PAID scope and Premium entitlements on
   the chat request (which also gives files and reasoning modes their tier check in the chat).
4. Milestone 1: proposed budget values with their cost calculations; release tooling for an
   enforcement-on release.
5. Spaces re-verification and the owner administration dashboard. The staging design is written
   (`docs/STAGING_ENVIRONMENT_DESIGN.md`): port 8002 was used by an earlier V31 staging and must be
   checked on the host by an operator first (8003 otherwise); production's unit is named
   `kalillac-ai-fastapi-staging.service`. The deletion-retention proposal is section 5 of the
   V1-02D/E plan.
6. ~~Files under Financial Safeguards~~: done (`a0df0fc`), in the FREE scope until Milestone 3.
7. Pull requests for the stacked branches, prepared for independent review, when the owner wants
   them.
