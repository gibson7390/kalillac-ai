# Claude Code session handoff, 2026-11-17

For the next Claude Code session continuing Kalillac AI V1 development. This is a session transition, not
a new project: read this first, then the three authoritative documents it points to. It records state and
where to look; it does not repeat them.

| Document | What it is |
|---|---|
| `docs/V1-EXECUTION-BLUEPRINT.md` | The authoritative feature status table (Impl / Dev / Indep / Staging / Owner / Prod), Codex findings outstanding, owner decisions, order of work. Correct it first when anything disagrees. |
| `docs/DEVELOPMENT_ROADMAP.md` | The evidence log: commits, CI run ids, regressions, environment limits. |
| `docs/CODEX-HANDOFF-2026-11-17.md` | Every checkpoint for Codex (the independent read-only auditor), with SHAs, CI and questions; section 11 is the allowance correction. |

## Exact state at handoff

* Working directory: `C:\Kalillac\kalillac-ai-b2` (a Git worktree of the `kalillac-ai` repository).
* Branch: `fix/file-allowance-accounting`, pushed and level with `origin`.
* Head: this handoff commit, on top of `5ba50d8` (docs: CI evidence for the allowance correction), which is on
  `c3a7a20` (the correction) and `01bf2ec` (its no-behavior-change preparation).
* Uncommitted changes in this worktree: none.
* Running tasks: none. A disposable local PostgreSQL 16.2 is running in WSL on port 55432 (test only;
  `~/pg16.sh stop` stops it, `~/pg16.sh start` restarts it after a WSL restart).
* Production: unchanged. Nothing has been merged, deployed or run on a real host; production stays at
  `54e38ecf` (Financial Safeguards off).

### The branch stack (each contains everything below it)

In `C:\Kalillac\kalillac-ai-fs-v1-completion`: D3 `feat/account-security-d3-routes` → D5 → D4 →
`feat/account-security-e1-e3` (`dc24d1b`, migration head 0025).
In `C:\Kalillac\kalillac-ai-b2`: `feat/premium-paid-scope-b2` (`84cc7f1`) → `feat/staging-provisioning-m9`
(`70a09ed`) → `feat/v1-single-authorization-a10` (`5ce5750`) → `feat/spaces-frontend-s1` (`856a135`) →
`feat/v1-auth-remaining-a11` (`3525fd8`) → `feat/image-intake-i1` (`8d5a2d4`, Codex-audited) →
`fix/file-allowance-accounting` (`c3a7a20`, `5ba50d8`, this handoff).

### Other worktrees (do not touch, delete or reset)

* `C:\Kalillac\kalillac-ai` (main checkout, `product/account-lifecycle-hardening-v1`): carries the owner's
  **uncommitted** work (modified `app_fastapi_candidate.py`, `v1_security.py`, `user.py`, three tests, a
  workflow; untracked `v1_schema.py`, migration 0012, two tests). Preserve it exactly.
* Eleven older feature, fix and integration worktrees (`git worktree list`); historical, leave as they are.

## Completed milestones (development statuses only)

All of these are implemented and development-verified (their CI green at the SHAs in the Codex handoff).
**None is independently verified by Codex, staging-accepted, owner-accepted or production-active**, except
D3's login-outcome fix (`dcc0d44`), the last independently verified checkpoint.

D3, D5, D4 (account routes, reset tokens, operator tool); E1a, E2, E3 (final grants, operator transition,
lifecycle); B2 (verified Premium → PAID scope on chat, files and Space sends); A9, U1, U2 part (account
pages, code-block copy, responsive fixes); M9 part (staging provisioning tool, settings and unit templates,
web-root builder, runbook, root boot verifier; Premium journeys on PostgreSQL for chat, Spaces, files and
images); A10, A11 (Spaces, billing, usage and the quota through V1 authorization); S1 and S1b (Spaces in the
chat, management, earlier messages, conversation rename and delete); R1 (modes on Space sends); D2f
(exact-edit confirmation in the chat); I1 (secure image upload and understanding, off by default); I2 (image
generation design only); O1 (read-only owner report, `tools/owner_report.py`); the chat-queue test flake
fixed; a `spaces-verification` workflow for the 1,249 SQLite Spaces tests.

## Outstanding audit findings

* **Allowance accounting (Codex, on `8d5a2d4`): development-verified, awaiting independent Codex
  acceptance.** File- and image-assisted responses were not counted against the daily allowance. Reproduced
  (5 of 7 new PostgreSQL tests failed on `01bf2ec`), corrected in `c3a7a20`, 7 of 7 three times locally, all
  five workflows green (subscription 44218217622, files 44218222254, frontend 44218226795, provider
  44218230840, anonymous allowance 44218235431). Do not mark it resolved until Codex accepts it.
* All checkpoints D3 through the allowance correction await Codex (blueprint, "Codex findings
  outstanding"; handoff rows 1–10 and section 11).
* Open document-quality items: Arabic PDF ligature order, Word field caches (decision 12), the
  semantic-consistency contract (`docs/CODEX-AUDIT-CORRECTIONS.md`).

## Remaining development tasks

* **M9, the staging run** (blocked on owner decision 17): provision the owner's staging host per
  `deploy/staging/README.md`, Stripe test mode, the whole Premium journey on the real application, and
  measure `KALILLAC_FINANCE_OPENAI_IMAGE_INPUT_TOKENS` there (runbook §9) before images are switched on.
* **E1b**: entitlement and billing grants behind a reviewed billing write path (after Codex reviews B1/B2).
* R1: a remembered reasoning-mode preference (needs a schema change that touches the V1 grant model; held
  back deliberately; the page stores nothing in the browser by design).
* O1: an operator-run summary of security events through P, and a web dashboard, both only if the owner
  asks after accepting the report.
* U2: a full responsive pass across the account site (the chat was reviewed by screenshots).
* Erasure (owner decision 6) and I2 activation (decisions 15, 16) stay off.

## Owner decisions still required

From the blueprint's table: 1 (financial budget values), 2 (provider price confirmation), 3/4 (migration
0016 and Financial Safeguards in production), 6 (deletion retention and erasure), 8 (Free Chat smoke test of
`54e38ecf`), 9 (which reasoning modes each tier may use), 10 (price, Stripe product and price ids, interval,
trial, refunds, Premium allowances), 12 (Word finalization service), 15 (image generation), 16 (whether a
file request or image counts as one response; the implementation follows the recorded recommendation,
"one response each"), 17 (staging host and Stripe test keys), 18 (a per-account dollar budget). Every price,
allowance, budget and Stripe id in `deploy/staging/settings.env.template` is a placeholder for these.

## Next highest-priority milestone

**M9: a staging-ready Premium deployment, run end to end on the owner's staging host.** All Premium
capabilities are built and CI-verified; what stands between them and "staging-ready" is the staging run
itself, which needs owner decision 17 (host, Stripe test keys) and the placeholder values (decisions 1, 2,
9, 10). Until those arrive, the highest-value unblocked work is: answer Codex's findings on the eleven
pending checkpoints as they come (each fix as its own checkpoint, reproduced first), then E1b.

## Working notes for the next session

Environment facts and pitfalls are in the memory file `kalillac-roadmap-location` and the roadmap's
"Known defects and environment limits". The most important:

* Windows Python: `C:/Kalillac/kalillac-ai-fs-v1-completion/.venv/Scripts/python.exe`; WSL Python
  `~/kf15/bin/python`; PostgreSQL tests: `~/pgtest_here.sh <pytest args>` from the worktree's `candidate/`.
* CI: `gh workflow run <workflow>.yml --ref <branch>`; `anonymous-allowance-offline` needs
  `-f verification_sha=<full sha>`; `spaces-verification` runs on pushes (it is not on the default branch).
  Workflows pin exact test counts: update them with every test added.
* Source guards to respect: no runtime module names a V1 security table (`test_v1_schema`); every
  `kalillac_files` reference stays inside the application's files block (`test_file_worker`); `send()` and
  each way into a request begin with the authority check (`test_homepage_handoff`).
* Edit with the Edit tool or Python scripts written with Write; backslashes in Bash heredocs get mangled.
* Long browser suites take an hour locally when the machine is busy; CI's frontend workflow is the gate.
