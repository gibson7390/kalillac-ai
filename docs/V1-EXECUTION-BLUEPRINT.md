# Kalillac AI V1: authoritative execution blueprint

The one place that says what V1 is, where each part stands, what it depends on, and what "done" means
for it. It guides the work; `docs/DEVELOPMENT_ROADMAP.md` stays the evidence log (commits, CI runs,
regressions) and `docs/V1-02DE-ACCOUNT-SECURITY-PLAN.md` the account-security design. When they
disagree, this file is corrected first.

Last reconciled with the repository: 2026-11-17, at `9feb42bab929cba091891831f91f3c8dc290f2c3`
(`feat/account-security-d5-credential-path`).

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
| A5 | D4 operator tool (suspend, reinstate, deletion request, bounded export) through P | ✔ | ✔ local | | CI + Codex | A2 |
| A6 | E1 legacy-route removal (grants), incl. the row-lock design | | | | design + migration | A4, M3 billing writes |
| A7 | E2 operator transition (S login, latch active, startup gate qualified) | | | | build; never run in production without the owner | A6 |
| A8 | E3 synthetic lifecycle test under final grants | | | | build | A7 |
| A9 | Account management UI (sessions, password change, deletion request, recovery) | part (sign-in, create, recovery, overview) | part | | V1 pages: sessions, password, deletion; drop the verify page in V1 | A3 |
| B1 | Stripe Checkout, portal, webhooks, reconciliation, refund/dispute cases | ✔ | ✔ | | test-mode lifecycle run (owner's test keys) | A1 |
| B2 | Verified Premium entitlement on the chat request (PAID scope) | | | | the seam: V1-authorized account → entitlement → PAID scope, request ceiling, modes, files, images | A3, B1, F1 |
| B3 | Per-customer financial safeguards (allowance enforcement) | | | | mechanism built with allowances as configuration (values: owner) | B2, F1 |
| B4 | Subscription management + cancellation in the UI | part (overview billing section) | | | cancel/manage via portal from the account page, status display | B1, A9 |
| F1 | Financial Safeguards (ledger, ceilings, provider budgets, operator) | ✔ | ✔ | | owner budgets and activation (production) | — |
| R1 | QUICK / BALANCED / DEEP reasoning | backend + control ✔ | ✔ | | tier permission from B2, saved preference, Spaces sends, final UI | B2 |
| D1f | File generation, editing, worker, document quality | ✔ | ✔ | part | open Codex items (Arabic ligatures, Word fields/decision 12, semantic consistency) | — |
| D2f | File assistant in chat, `/api/files`, delivery | ✔ (flag off) | ✔ | | customer confirmation controls; Premium gate via B2; count vs allowance (owner) | B2 |
| S1 | Spaces + persistent conversations (backend `/api/spaces`) | ✔ (flag off) | part | | re-verify; V1-01 authorization on Spaces; **frontend** (list, create, conversation history, instructions) | A3 |
| S2 | Saved conversations outside Spaces | | | | decide shape (a default Space vs separate history); build | S1 |
| U1 | Code-block rendering with per-block copy | | | | build in the chat renderer (copy keeps exact source text) | — |
| U2 | Responsive, coherent customer frontend (chat + account + Spaces) | part | part | | navigation between chat, Spaces, account; mobile layouts; one design language | A9, S1 |
| I1 | Secure image upload + image understanding | | | | intake (type sniffing, size/pixel caps, re-encode, strip metadata), GPT-6 Luna vision call under Financial Safeguards with measured cost | B2, F1 |
| I2 | Image generation | | | | **design only**: cost-control architecture; no model activated, no allowance (owner) | F1 |
| O1 | Owner administration dashboard | | | | read-only first: accounts, subscriptions, ledger/costs, billing cases, security events; actions only through reviewed tools | A5, B1, F1 |
| M9 | Staging environment + end-to-end journey suite | design doc | | | isolated staging (no production data), E2E through the real app, Stripe test mode | all |

## Codex findings outstanding

* D3 routes at `35f61c8` and D5 at `9feb42b`: awaiting Codex.
* Document quality: Arabic PDF ligature order (open), Word field caches (owner decision 12 pending
  acceptance of the finalization service), semantic-consistency contract (partly built). Details:
  `docs/CODEX-AUDIT-CORRECTIONS.md`.
* Known flaky test (not a product finding): chat-queue saturation slot read (roadmap).

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

## Order of work (no stopping between items)

1. A5 D4 operator tool → A6 E1 design + migration (row-lock column, R's final grants) → A7 E2
   transition tool (dry-run, fixtures) → A8 E3 lifecycle test. Codex handoff at each.
2. B2 the PAID seam on the chat request, then R1 permissions, D2f Premium gate, B3 allowance mechanism.
3. A9 + B4 account and subscription pages; U1 code-block copy; S1 Spaces re-verification and frontend;
   S2 saved conversations; U2 navigation and responsive pass.
4. I1 image upload and understanding (measured cost); I2 design document.
5. O1 owner dashboard (read-only first).
6. M9 staging configuration and the end-to-end journey suite; operations documentation.
