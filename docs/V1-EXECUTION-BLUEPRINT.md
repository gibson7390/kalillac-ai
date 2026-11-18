# Kalillac AI V1: authoritative execution blueprint

The one place that says what V1 is, where each part stands, what it depends on, and what "done" means
for it. It guides the work; `docs/DEVELOPMENT_ROADMAP.md` stays the evidence log (commits, CI runs,
regressions) and `docs/V1-02DE-ACCOUNT-SECURITY-PLAN.md` the account-security design. When they
disagree, this file is corrected first.

Last reconciled with the repository: 2026-11-18, at the staging release `release/staging-v1` (`7d857c5`),
which contains every completed V1 feature and Codex's verified file corrections (handoff section 14).

## Statuses (never interchangeable)

**Impl** implemented · **Dev** development verified (its tests and CI pass) · **Indep** independently
verified by Codex · **Staging** accepted in the isolated staging environment · **Owner** owner accepted ·
**Prod** production active. A blank means not reached. Production stays at `54e38ecf` (Financial
Safeguards off); nothing here is Staging, Owner-accepted for release or Prod.

## Fixed product identity (not changed without the owner)

One assistant, Kalillac, on GPT-6 Luna only; Brave primary search with controlled Tavily fallback; no
model marketplace. Anonymous use: 10 responses per browser per UTC day. Capability first: waste is
removed (repeated calls, redundant context, repeated searches, file-loop retries, oversized images),
quality is not traded for tokens. Every billable provider call passes Financial Safeguards.

## The customer journey V1 must pass end to end (staging, real application)

Registration → sign-in → Free usage (chat, search, reasoning within Free) → Premium subscription
(Stripe test mode) → Premium capabilities (modes, files, images) → file generation and editing with
confirmation → saved work (conversations, Spaces, files) → account management (sessions, password,
recovery) → subscription cancellation → account deletion request. Owned by M9 (E2E suite).

## Features

| # | Feature | Impl | Dev | Indep | Remaining work | Depends on |
|---|---|---|---|---|---|---|
| A1 | Accounts: email-first registration (role A), sign-in, `/me`, entitlements read | ✔ | ✔ | part | V1 mode must require registration v2 (E2) | — |
| A2 | V1 authorities D1–D2 (0013–0023) | ✔ | ✔ | 0022 ✔, 0023 source+CI | — | — |
| A3 | D3 V1 routes (login, logout, revoke, password, deletion request, recovery confirm) | ✔ | ✔ | login ✔ (`dcc0d44`) | Codex on `35f61c8` | A2 |
| A4 | D5 reset-token issuance + credential read (0024) | ✔ | ✔ | | Codex on `9feb42b` | A3 |
| A5 | D4 operator tool (suspend, reinstate, deletion request, bounded export) through P | ✔ | ✔ CI (`f554c2f`) | | Codex | A2 |
| A6 | E1 legacy-route removal: E1a account grants + 0025 row-lock anchor | ✔ | ✔ CI (`dc24d1b`) | | Codex; **E1b** (entitlement/billing grants) after a reviewed billing write path: designed in `docs/E1B-BILLING-AUTHORITY-DESIGN.md` (inventory of every runtime write, optimistic versioned functions on S, operator paths through P), not built until the billing audit answers its section 8 | A4; E1b: B1 |
| A7 | E2 operator transition (S/P login, latch active, startup gate qualified, rollback) | ✔ | ✔ CI (`dc24d1b`) | | Codex; running it anywhere real is the owner's decision | A6 |
| A8 | E3 synthetic lifecycle test under final grants | ✔ | ✔ CI (`dc24d1b`) | | Codex | A7 |
| A9 | Account management UI (sessions, password change, deletion request, recovery) | ✔ | ✔ CI (`84cc7f1`; 96 browser tests) | | CI + Codex; drop the legacy verify page when V1 is the only mode | A3 |
| A10 | Spaces through V1 authorization (every request; the save transaction's recheck under the account's V1 lock) | ✔ | ✔ CI (`5ce5750`) | | Codex | A3 |
| A11 | V1 authorization on the last legacy account paths: billing (status, portal, Checkout, with a recheck in each Checkout planning transaction under the account lock before any Stripe call), usage (route and meter) and the chat's daily quota (authorized inside its reservation transaction, V1's FOR UPDATE before the quota's KEY SHARE). Email verification already mounts no routes in V1 mode. The chat shows fixed copy for `daily_limit_reached`, `account_unavailable` and `not_authenticated` | ✔ | ✔ CI (`3525fd8`: 44166877085, 44166883328, 44166895596, 44166917176) | | Codex | A3, B1 |
| B1 | Stripe Checkout, portal, webhooks, reconciliation, refund/dispute cases | ✔ | ✔ | | test-mode lifecycle run (owner's test keys) | A1 |
| B2 | Verified Premium entitlement on the chat request (PAID scope) | ✔ | ✔ CI (`84cc7f1`) | | CI + Codex. `kalillac_accounts/paid_access.py`: V1-01's authorized read → effective entitlement → request-bound evidence at classification (chat, files and, since R1, **Space sends**, which until then were always classified FREE) and the reasoning permission; matching account only; never the legacy path | A3, B1, F1 |
| B3 | Per-customer financial safeguards | ✔ (allowance × request ceiling) | ✔ | | Each customer is bounded per day by the tier's response allowance (`KALILLAC_USAGE_LIMIT_{FREE,PAID}_DAILY`, chosen from the effective entitlement) times the scope's request ceiling. An explicit per-account dollar budget is designed (per-account ledger buckets, identity kept out of reservations) and waits on owner decision 18 | B2, F1 |
| B4 | Subscription management + cancellation in the UI | ✔ (upgrade, Manage billing → Stripe portal, period and cancellation state; **the return from Stripe**: Checkout's success and cancel URLs and the portal's return URL are `/account?checkout=success`, `/account?checkout=cancelled` and `/account`, with fixed notices and no second upgrade offer while Stripe confirms; the earlier `/account/billing` address, which rendered Not found, redirects there) | ✔ local (4 browser tests, `13c5bd3`; exact query match `32a0e77`) | | Stripe test-mode run in staging (owner's keys) | B1, A9 |
| F1 | Financial Safeguards (ledger, ceilings, provider budgets, operator) | ✔ | ✔ | | owner budgets and activation (production) | — |
| R1 | QUICK / BALANCED / DEEP reasoning | ✔ backend, control, tier permission from the verified entitlement; **a mode on Space sends** (optional `reasoning_mode` in the send body, resolved and permitted exactly as on `/api/chat`; the chat keeps the picker in a Space and a retry keeps its mode) | ✔ local (5 Space tests, 1 browser test) | | a saved preference (needs a schema change; the page deliberately stores nothing today); owner decision 9 (which modes each tier gets) | B2 |
| D1f | File generation, editing, worker, document quality | ✔ | ✔ | part | open Codex items (Arabic ligatures, Word fields/decision 12, semantic consistency) | — |
| D2f | File assistant in chat, `/api/files`, delivery; exact-edit confirmation in the chat (a held-back edit offers "Apply exactly as asked", which sends one fixed message with the server's single-use confirmation id and the file; the draft is kept; malformed ids are not offered) | ✔ (flag off) | ✔; confirmation UI local (1 browser test) | | CI; count vs allowance (owner decision 16) | B2 |
| S1 | Spaces + persistent conversations (backend `/api/spaces`) | ✔ (flag off); V1-01 authorization in V1 mode (every request, and the save transaction's recheck under the account's V1 lock, taken first) | part (1194 SQLite + 4 real PostgreSQL under the final grants, CI `5ce5750`); **frontend in the chat** (pick or create a Space and a conversation, saved history, sends with an idempotency key, leave): 6 browser tests, CI `ba03512` ; **managing a Space in the chat** (rename, instructions, delete, each with the version last read; a change made elsewhere is reported, never overwritten) and **earlier messages** of a long conversation loaded above, 2 browser tests (local) | | CI + Codex (conversation rename and delete in the chat and a mode on Space sends are done, local) | A3 |
| S2 | Saved conversations outside Spaces | | | | decide shape (a default Space vs separate history); build | S1 |
| U1 | Code-block rendering with per-block copy | ✔ | ✔ CI (`84cc7f1`; browser) | | CI | — |
| U2 | Responsive, coherent customer frontend (chat + account + Spaces) | ✔: every account page and state rendered at 320–1440 px and on a landscape phone and corrected (form measure on tablets, a sticky note beside the long overview, full-width phone actions, short landscape screens; `3ad5979`); the chat's Spaces dialog on phones (44 px targets, 16 px fields, the conflict reads as a problem, the title field fits 360 px; `e17dd1f`); the chat itself was reviewed by screenshots earlier | ✔ local (account UI 105, chat contract 135 browser tests) | | CI + Codex | A9, S1 |
| I1 | Secure image upload + image understanding | ✔ (flag `KALILLAC_FILES_IMAGES_ENABLED`, off by default; page mark `images`): `kalillac_files/images.py` in the isolated worker (format from the signature: PNG, JPEG, WebP, GIF; header dimension caps before decoding; animation refused; orientation applied, sRGB, a brand-new image with no metadata, at most 2048 px, re-encoded PNG/JPEG); only that copy is stored. The file assistant shows attached images to GPT-6 Luna as inline `input_image` parts (at most 4, fixed detail); Financial Safeguards reserves each image at `KALILLAC_FINANCE_OPENAI_IMAGE_INPUT_TOKENS` (not its bytes) and refuses any other image reference; startup refuses images with safeguards on and no bound. Chat: image types in the chooser, a preview of the stored copy, fixed copy for refused uploads | local (18 image tests, 5 browser tests) | | CI + Codex; **measure** the per-image bound in staging (runbook §9); Premium-only through the files tier; counts as one response (decision 16) | B2, F1 |
| I2 | Image generation | design: `docs/I2-IMAGE-GENERATION-DESIGN.md` (flow, cost controls, safety, activation checklist) | | | stays off for V1; nothing built or activated until owner decisions 15, 16 (and 1, 2 for its prices and budget line) | F1, I1 |
| O1 | Owner administration dashboard | read-only first: `tools/owner_report.py report [--json] [--html PATH]`, one REPEATABLE READ, READ ONLY snapshot with the application's database environment, counts only (accounts, live sessions, paid plan now, subscriptions by status, billing cases, usage today and 7 days, anonymous visitors, saved work); a self-contained HTML page (no script, no external resource, new file 0600). Costs stay in `finance_ledger_admin.py report`; actions stay in the reviewed tools | local (6 SQLite + 1 real PostgreSQL as the runtime login under E1a, every section readable) | | CI + Codex; account security events are deliberately not in it (only the V1 authorities read them; a guard forbids runtime modules naming those tables): an operator-run summary through P if the owner wants one; a web dashboard only after this is accepted | A5, B1, F1 |
| M9 | Staging environment + end-to-end journey suite | part: the Premium journey test (`test_v1_premium_journey_postgres.py`); **staging provisioning**: `tools/provision_deployment.py` (roles, database, migrations, protected files and the registration activation of a new, empty deployment; only creates, refuses anything it did not mark), `deploy/staging/` (the settings template, the unit template, the web-root builder and the operator runbook), and `tools/verify_provisioned_boot.py`, which runs the runbook's own commands as root in CI and starts the real application from the template; **the staging preflight** (`tools/staging_preflight.py`, runbook step 8): read-only checks of the filled configuration (fixed identity, nothing of production's, test-mode Stripe only, return addresses, provisioned settings not overridden, coherent owner numbers, root-only secrets, the application's own loaders), printing no value | ✔ CI (`70a09ed`; 18 PostgreSQL + 9 unit tests; boot verifier 15/15); preflight 34 Linux tests local (`351058e`) | | CI + Codex; the systemd-run and unit forms exercised on a runner with systemd; then the run on the owner's staging host with Stripe test mode (decision 17) | all |

## Codex findings outstanding

* **Codex finding on `8d5a2d4`, corrected on `fix/file-allowance-accounting` (development verified,
  awaiting Codex):** file- and image-assisted responses were not counted against the signed-in customer's
  daily response allowance. `/api/chat` handed a file request to the file assistant before the quota's
  reservation, so the request wrapper saw no admission and charged nothing; `/api/files/assist` had no
  allowance at all. Both now take the chat's own reservation before any model work (V1's identity, the
  effective plan's limit, the file service's account must be the quota's account), complete and settle it
  once on success and release it on failure or cancellation. One file or image response counts as one
  response (decision 16's recorded recommendation; no value changed). Reproduced before the fix: 5 of 7
  real-PostgreSQL tests failed (an image-assisted chat answered with a count of 0; concurrent requests
  past the limit); after it, 7 of 7 three times, plus 4 unit tests of the route's allowance hook.
  CI green on `c3a7a2083d3f527da4a9a67b02f78b61db37c9d3`: subscription 44218217622, files 44218222254, frontend 44218226795, provider 44218230840, anonymous allowance 44218235431.
* Codex's question on the correction (can any model-backed path still return without a reservation?) was also checked here, not as verification: the model is reached only through `/api/chat` (ordinary, anonymous and the file handoff), `/api/files/assist` and Space sends; Space sends run through `_run_chat_request` with the quota, and startup refuses Spaces without usage limits. No further path found.
* **The staging release** `release/staging-v1` (`7d857c5`): integration of the release line with Codex's verified prose-total and confirmation corrections (`78b7a05`); CI green on every applicable workflow; awaits Codex as a combination. No earlier finding is resolved by it.
* **Local staging** (`feat/local-staging`, `a4d5e71`): the release on this machine for the owner's evaluation, simulated providers labelled; a tool, not a product change; awaits Codex like every checkpoint (handoff section 15).
* D3 routes at `35f61c8` and D5 at `9feb42b`: awaiting Codex.
* Document quality: Arabic PDF ligature order (open), Word field caches (owner decision 12 pending
  acceptance of the finalization service), semantic-consistency contract (partly built). Details:
  `docs/CODEX-AUDIT-CORRECTIONS.md`.
* The chat-queue saturation test's occasional "pending" read is fixed (the test read the lease after its
  event loop closed; roadmap).

## Owner decisions (recorded once; work continues around them)

| # | Decision | Recommended option | Blocks |
|---|---|---|---|
| 1 | Financial budget values (15 settings) | `docs/FINANCE_BUDGET_PROPOSAL.md` cautious launch | F1 activation |
| 2 | Provider price confirmation | confirm against current price lists | F1 activation |
| 3/4 | Migration 0016 and Financial Safeguards activation in production | after staging | production only |
| 6 | Deletion retention and erasure (plan section 5) | the proposal table | erasure, not the request |
| 8 | Free Chat smoke test of `54e38ecf` | run it | production evidence |
| 9 | Which reasoning modes Free/Premium may use | Free: QUICK, BALANCED; Premium: all (dev mapping low/medium/high) | R1 final permissions |
| 10 | Price, Stripe product/price ids, interval, trial, refund policy, Premium allowances | — (owner) | B1 live, B3 values |
| 12 | Word finalization service | accept the sandboxed LibreOffice service, or keep the prompt | D1f Word fields |
| 15 | Image generation: model, cost, allowance | keep off for V1 launch; design ready | I2 |
| 16 | Whether a file request / image counts as one chat against Free allowances | count each as one response | D2f, I1 allowances |
| 17 | Staging host and test-mode Stripe keys | provision per `docs/STAGING_ENVIRONMENT_DESIGN.md` | M9, B1 lifecycle |
| 18 | An explicit per-account dollar budget on top of allowance × request ceiling | not needed for launch; revisit with real usage | B3 extension |

## Order of work (no stopping between items)

1. A5 D4 operator tool → A6 E1 design + migration (row-lock column, R's final grants) → A7 E2
   transition tool (dry-run, fixtures) → A8 E3 lifecycle test. Codex handoff at each.
2. B2 the PAID seam on the chat request, then R1 permissions, D2f Premium gate, B3 allowance mechanism.
3. A9 + B4 account and subscription pages; U1 code-block copy; S1 Spaces re-verification and frontend;
   S2 saved conversations; U2 navigation and responsive pass.
4. I1 image upload and understanding (measured cost); I2 design document.
5. O1 owner dashboard (read-only first).
6. M9 staging configuration and the end-to-end journey suite; operations documentation.
