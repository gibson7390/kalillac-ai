# Independent audit handoff, 2026-11-17

For Codex, the independent read-only auditor. Ten checkpoints since the last independently
verified one (`dcc0d44`, the D3 login-outcome fix). Each is on its own branch, stacked in this order;
each branch contains everything below it. Nothing here has been merged, deployed or run against any
real host or database. The statuses below are development statuses only: "CI green" means the
repository's workflows passed on that exact commit, not that anything was independently verified.

Audit each checkpoint at its SHA. Findings are most useful as: the file and line, the concrete
input or state, and the wrong result.

| # | Checkpoint | Branch | Commit (full) | CI on that commit |
|---|---|---|---|---|
| 1 | D3: remaining V1 account routes | `feat/account-security-d3-routes` | `35f61c88e47fec3ec32ebd08b1c5813b34d734d6` | subscription 43985402680, provider 43985409214, safety 43985416831: green |
| 2 | D5: reset-token issuance and credential read path (0024) | `feat/account-security-d5-credential-path` | `9feb42bab929cba091891831f91f3c8dc290f2c3` | 43993703621, 43993709925, 43993715548: green |
| 3 | D4: operator support tool through P | `feat/account-security-d4-operator-tool` | `f554c2fd91fd2254db6918ac3cdd4312f4f18d8a` | 44009415730, 44009421529, 44009427152: green |
| 4 | E1a, E2, E3: final grants, operator transition, lifecycle | `feat/account-security-e1-e3` | `dc24d1bdb7a3dc076047bc323de2779b01df0339` | 44094320838, 44094326597, 44094334097: green |
| 5 | B2, A9, U1, U2 (part), the Premium journey | `feat/premium-paid-scope-b2` | `84cc7f1790ed37a1fac75ceceb51000c533dae94` | subscription 44102235903 on it; provider 44098090955, safety 44098099108, frontend 44098200220, anonymous allowance 44098265034 on `abbe498` (`84cc7f1` changes only the job timeout): green |
| 6 | M9: staging provisioning | `feat/staging-provisioning-m9` | `70a09ed468c26ceea9aba41ad358138f91d8fe16` | subscription 44102277062 on it; provider 44100305520, safety 44100311426 on `aab49f6` (the same code): green |
| 8 | S1: Spaces in the chat | `feat/spaces-frontend-s1` | `ba03512df40222754f5b617a98ec9f0f129c2e4d` | subscription 44115103292 and frontend 44115118022 on `60957ad`; frontend 44116824283 on `ba03512` (CSS and tests only): green |
| 7 | A10: Spaces through V1 authorization | `feat/v1-single-authorization-a10` | `5ce57506c08c527f9b8d9cf12aacc3d3b3e579be` | 44102284051, 44102290116, 44102296120: green |
| 9 | A11: billing, usage and quota through V1 | `feat/v1-auth-remaining-a11` | `3525fd8` (full SHA in `git log`) | subscription 44166877085, frontend 44166883328, anonymous allowance 44166895596, provider 44166917176: green |
| 10 | I1 image upload and understanding, O1 owner report, D2f confirmation and Space management in the chat | `feat/image-intake-i1` | `796fabeb6e04e9ee0e165a30ff9989633e00a1c5` (I1, O1); Space management after it | files 44173313156 and provider 44173319299 green on `796fabe`; subscription, frontend and anonymous allowance running |

## 1. D3 (`35f61c8`)

Logout, revoke-others, revoke-all, password change, the customer's deletion request and recovery
confirm, each through its V1 authority as S, in V1 mode only (`kalillac_accounts/router.py`,
`recovery_router.py`). Questions worth an independent answer:

* Can any route reach a legacy write path (`users`, `account_sessions`) in V1 mode?
* Does every authority answer get validated by type before it is compared (the class of defect
  fixed in `dcc0d44`)? `_contract_outcome`, `_authority_refusal`.
* Request-body reading (`_read_exact_body`): limits, content type, duplicate keys.
* Cookie handling on each outcome: cleared exactly when the session ended, never otherwise.

Earlier CI failure on `3c4ac4a` (a docstring named the retired verification authority, caught by a
guard test) is corrected in `35f61c8`.

## 2. D5 (`9feb42b`)

Migration 0024: `v1_issue_reset_token` and `v1_credential`, SECURITY DEFINER, owned by E, callable
only by S. After it, recovery issues reset tokens only through S and every V1 read of a password hash
goes through `v1_credential`. HTTP tests pass with R's SELECT on `users.password_hash` revoked.

* The function bodies against their recorded digests (`v1_schema.ISSUE_RESET_*`, `CREDENTIAL_*`).
* Whether `v1_credential` can be used as an oracle (who may call it, what it returns for a
  quarantined, unverified or deleted account).

## 3. D4 (`f554c2f`)

`tools/v1_account_support.py`: suspend, reinstate, request deletion through P with P's own credential;
dry run by default; operation ids for replay; fixed reason codes; an append-only record written
before and after each call; a bounded export with no credential. The application never connects
as P.

## 4. E1a, E2, E3 (`dc24d1b`)

* Migration 0025: `users.row_lock_anchor`, always NULL (CHECK). R needs UPDATE on some column of a
  row to take `FOR UPDATE`/`FOR KEY SHARE` on it (checked on PostgreSQL 16.2); this column is the only
  one R may update.
* E1a (`kalillac_accounts/v1_final_grants.py`): R reads every `users` column except `password_hash`,
  updates only the anchor, and has no INSERT or DELETE on `users`; SELECT only on sessions and recovery
  tokens; nothing on verification tokens; SELECT on the latch.
* The classifier (`v1_schema.classify`) pairs a `prepared` latch with the legacy grants and an `active`
  latch with E1a's grants and S and P logins, and refuses anything else, including an active latch
  below the head.
* E2 (`tools/v1_security_transition.py`): one transaction; SCRAM verifiers only; dry run rolls back;
  rollback is the exact inverse. `require_qualified_authority` binds startup to
  `KALILLAC_DEPLOYMENT_ID` and `KALILLAC_RELEASE_SHA`.
* E3 (`tests/test_v1_lifecycle_postgres.py`): the whole lifecycle through the routes after the
  transition, recording which login wrote which account table.

**A regression this checkpoint introduced and fixed, worth a second look.** In `492ad2f` the E1a
column check in `registration_startup.effective_privileges` was inserted inside the per-table loop;
the loop's last block (any right of the registration role on an unrelated table refuses startup)
became the body of the new check and stopped running in the legacy state. CI's root-run bootstrap
verifier caught it (run 44090797912, "unrelated column grant refuses"). `dc24d1b` moves the check after
the loop; against `f554c2f` the function now differs only by additions. Six regression tests that need
no root were added (four legacy, two under E1a), all failing on `492ad2f`. Please confirm the loop
structure is now exactly the pre-E1 one plus the additions, and whether any other check in that
function could be silently skipped the same way.

## 5. B2, A9, U1, U2, the Premium journey (`84cc7f1`)

* `kalillac_accounts/paid_access.py`: the chat request's V1-verified account and effective
  entitlement, read once; PAID evidence is issued only when the verified account is paid AND is exactly
  the account the request is metered for (`paid_evidence`). Never from the legacy path. The same
  evidence reaches the file routes (`FileAccount.verified`) and the reasoning permission.
  Question: can a request be classified PAID for an account other than the one it is charged to, or
  without V1, or after the entitlement lapsed mid-request?
* The account page's sessions, password and deletion sections, `GET /api/account/security`
  (`site/src/account/`), 96 browser tests.
* Per-block code copy in the chat (`frontend/app.js` `addCodeCopy`), and the chat's account link on an
  accounts deployment.
* `tests/test_v1_premium_journey_postgres.py`: register → Free → Checkout + webhook → Premium (PAID
  scope, DEEP allowed) → password change → portal → cancellation → Free → deletion request, through
  the real application on PostgreSQL under E1a. Stripe is faked at its gateway; the model is scripted.
* `84cc7f1` only raises the `billing-postgres` job timeout from 20 to 45 minutes (the previous run was
  cancelled by the timeout with no test failure).

## 6. M9 staging provisioning (`70a09ed`)

* `tools/provision_deployment.py` creates a new, empty deployment: seven roles, the database,
  migrations, the protected files and the registration activation. It only creates; it marks every
  role and the database it creates and refuses anything unmarked before changing anything; it is
  repeatable only while unused. Questions: can it be pointed at an existing deployment and change it?
  Can a password reach SQL, a server log or its output? Are the files' ownership and modes exactly
  what the application's strict loader accepts, and no wider?
* `deploy/staging/`: the settings template (every allowance, budget, price and Stripe id is a
  placeholder), the unit template, the web-root builder and the runbook.
* `tools/verify_provisioned_boot.py` runs the runbook's commands as root in CI: provision, the
  operator transition as an unprivileged identity, the controlled launcher from the template, HTTP.
  15 cases; locally 15/15.

## 7. A10 Spaces through V1 (`5ce5750`)

With V1 on, Spaces still authorized accounts by the legacy session lookup. Now every Spaces request
goes through V1's authorization, and a send's save transaction rechecks it under the account's V1
lock, taken before the quota's KEY SHARE lock on the same row. Questions: is that lock order the same
on every path that locks the account row in a Spaces save (otherwise two saves for one account can
deadlock on a lock upgrade)? Is any Spaces path still on the legacy lookup in V1 mode? Other legacy
lookups remain in V1 mode, on purpose for now: billing, the usage meter, the quota and the email
verification router. Each relies on V1's revocations having ended the session row. Whether that is
acceptable for launch is a question for this audit.

## 9. A11: the last legacy account paths through V1 (`feat/v1-auth-remaining-a11`)

Billing (`kalillac_billing/router.py`), the usage route and meter (`kalillac_accounts/usage.py`) and the
chat's daily quota (`kalillac_accounts/quota.py`) authorize through V1-01 in V1 mode. Questions:

* Checkout: `start_checkout(authorize=...)` runs the V1 authorization at the start of every planning
  transaction, before `lock_account`. Can any pass reach `gateway.create_checkout_session` or
  `retrieve_checkout_session` without a planning transaction that authorized in the same pass?
* Quota: `_v1_identity` inside `reserve`'s transaction (READ COMMITTED, 3 s lock and statement timeouts),
  FOR UPDATE before the quota's KEY SHARE. Is there any transaction that holds KEY SHARE on the account
  row and then asks for FOR UPDATE (a lock-upgrade deadlock with this order)? `validate`, `complete`,
  `record_sent` and `release` do not re-authorize: is that acceptable given the reservation is bound to
  the account and the request?
* The chat's 401 with the quota on in V1 mode is now `not_authenticated` for a dead session cookie
  (before: the same for the legacy quota; the meter-only path still treats it as anonymous).

## 10. I1: secure image upload and image understanding (`feat/image-intake-i1`); D2f confirmation in the chat

* `kalillac_files/images.py` (runs only in the worker, `worker.run_image`): can any input make the stored
  bytes anything other than Kalillac's own PNG/JPEG encoding of the decoded pixels? Can metadata (EXIF,
  XMP, ICC, text chunks) survive `Image.frombytes`? Are the header checks before `load()` enough against
  decompression bombs in every accepted format (PNG, JPEG with `draft`, WebP, GIF)?
* `kalillac_finance/cost.py` `_without_image_data`: images are reserved at the configured per-image bound,
  not their bytes. Can a payload carry image input the walk does not see (another key, another type),
  or a reference other than an inline PNG/JPEG data URL, and still be reserved?
* `kalillac_files/assistant.py` `_image_parts`: only attached files of this owner, at most four, re-checked
  signature and size.
* The chat's confirmation control (`frontend/app.js` `renderConfirmations`, `sendFixed`): the id is
  only ever sent with the one fixed message it was pressed for; a retry of that exchange keeps it.
* Space sends (`app_fastapi_candidate.py` `_space_generate`): before this, a Space send never set the
  verified-account context, so a Premium customer's Space send was always classified FREE. It now reads
  the V1-verified account and entitlement (only for the Space's own account) and applies an optional
  `reasoning_mode` with the same permission as `/api/chat`. Can the PAID evidence reach a Space send for
  any account other than the one the send is saved and charged to?
* `tools/owner_report.py`: counts only, one read-only snapshot; does any output carry a person's data?

## Not in these checkpoints

E1b (entitlement and billing grants behind a reviewed billing path), erasure (owner decision 6), image
generation (design only: `docs/I2-IMAGE-GENERATION-DESIGN.md`), the owner dashboard. Open document-quality findings are tracked in
`docs/CODEX-AUDIT-CORRECTIONS.md`.

## Reproducing

Every PostgreSQL suite needs a disposable local PostgreSQL 16 and
`KALILLAC_REGISTRATION_TEST_POSTGRES_URL` pointing at its `postgres` database on loopback (the
workflows show the role layout). The two root-only verifiers (`tools/verify_registration_bootstrap.py`,
`tools/verify_provisioned_boot.py`) create one `/run` fixture each and remove it.
