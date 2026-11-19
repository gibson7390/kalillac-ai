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
| 10 | I1 image upload and understanding, O1 owner report, D2f confirmation, Space management, R1 modes and the paid scope on Space sends, two PostgreSQL journeys | `feat/image-intake-i1` | `5652d1d0b1709cfbdeb21d99c672d231c0e6ae84` | subscription 44184165507, frontend 44184178130, files 44184189261, provider 44184198447, anonymous allowance 44184206620, Spaces 44184146093: green. After it (conversation editing, the chat-queue test fix, docs): `995b994`, CI running |

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
* `_file_request_scope` and `_file_failure` now live at module level (unchanged bodies) so the PostgreSQL
  journey can run the real scope; `FILE_REQUEST_MODEL_ATTEMPTS` mirrors `MAX_TOOL_ROUNDS + 1`, checked
  at startup with files on. Is there any path where the mirrored value is used without that check?
* The chat's Spaces dialog: Space rename, instructions and delete; conversation rename and delete; each
  write sends the version read just before it. The dialog's late `close` event no longer ends a newer
  opening (it did, intermittently, in the browser tests).
* Two new real-PostgreSQL journeys under V1: Space sends (FREE, PAID with DEEP after Stripe's webhook,
  FREE again after cancellation) and files and images (refused on Free, the clean copy and a PAID-scope
  image request on Premium, refused again after cancellation).

## 11. Correction of the allowance finding on `8d5a2d4` (`fix/file-allowance-accounting`)

The finding: file- and image-assisted responses returned without being counted against the customer's
daily response allowance. Confirmed and corrected:

* `01bf2ec` (no behavior change): the /api/chat file handoff is built by `_build_file_chat_handoff`, so a
  PostgreSQL test can drive the real one.
* The correction (`app_fastapi_candidate.py` `_admit_file_response`, `_counted_file_response`;
  `kalillac_files/router.py` `count_response`): the handoff and `/api/files/assist` reserve one response
  through the same quota request as an ordinary chat, before the assistant runs, only after the file
  account was accepted, and only for that same account; `_run_chat_request` completes and settles it once
  on a 200 and releases it otherwise. Space sends were already counted in their save transaction.
* Evidence: `tests/test_v1_file_allowance_postgres.py` (7, under V1 and E1a: counting across image chat,
  direct assist and ordinary chat; exhaustion refused before model work; failure released; concurrency;
  cancellation during model work; Premium cancellation; quarantine) failed 5 of 7 on `01bf2ec` and passes
  on the correction; `tests/test_file_allowance.py` (4 unit tests of the route's hook).

CI green on `c3a7a2083d3f527da4a9a67b02f78b61db37c9d3`: subscription 44218217622, files 44218222254, frontend 44218226795, provider 44218230840, anonymous allowance 44218235431.

Questions worth an independent answer: can any model-backed response path still return without a
reservation (the handoff returns None only before admission)? Is the account match
(`str(quota user) == file account owner`) sufficient in legacy mode as well as V1?

## 13. Second session of 2026-11-17: responsive pass, the return from Stripe, the staging preflight

Stacked on section 11's branch: `feat/responsive-account-u2` (`3ad5979`, `13c5bd3`) and on it
`feat/staging-preflight` (`351058e`, `e17dd1f`, then the record). Development statuses only.

* `3ad5979` U2, account pages: rendered every account page and state at 320–1440 px and on a landscape
  phone; corrected the form measure on tablets, an empty note column beside the long overview (now
  sticky; the surface uses `overflow: clip`, since `hidden` made it the sticky element's scroll container),
  the one phone action that stayed content-width, and short landscape screens.
* `13c5bd3` B4: **a customer-journey defect no test could see.** The staging template and the Stripe guide
  sent Checkout's success and cancel URLs and the portal's return URL to `/account/billing`, which the
  account site does not route (Not found). They are now `/account?checkout=success|cancelled` and
  `/account`; `/account/billing` redirects there. The overview reads `?checkout=` once without side
  effects, removes it through the router, and only chooses fixed copy (access always follows the
  server's plan); no second upgrade is offered while Stripe confirms. Question: can the query make the
  page claim or do anything the server's plan does not support?
* `351058e` M9: `tools/staging_preflight.py`, read-only, run at runbook step 8. Questions: can any line
  it prints carry a value from the files it reads (its own messages name settings; the application's
  loaders' messages are passed through, truncated)? Does its in-process `os.environ` replacement
  (restored afterwards; tested) matter for a tool run as root on the host?
* `e17dd1f` U2, the chat's Spaces dialog on phones: 44 px targets and 16 px fields below 560 px and on
  coarse pointers, the conflict and failure statuses marked `data-tone="problem"` (derived only from the
  four fixed failure messages), the title field's placeholder shortened to fit 360 px.
* `32a0e77` (U2 branch) and its cherry-pick `55ae0ff` (preflight branch): the first frontend runs on
  `13c5bd3` (44231534575: 1 failed, 1,895 passed, 19 skipped) and `e17dd1f` (44233204310) **failed** one
  source guard, `test_storage_is_only_the_one_time_sessionstorage_handoff`, which forbids
  `URLSearchParams` anywhere in the site. The overview now compares `location.search` with the two exact
  strings (extra parameters show no notice); the guard is unchanged.

CI: frontend 44261585705 on `32a0e77` (1,896 passed, 19 skipped) and 44261589782 on `55ae0ff` (1,897 passed, 19 skipped), subscription 44231539374 on `13c5bd3` and 44232364958 on `351058e`: green. The first frontend runs (44231534575, 44233204310) failed one source guard, corrected in `32a0e77`.

Not changed: any backend behaviour, any grant, any price, allowance or provider setting.

## 14. The integrated staging release: `release/staging-v1` at `7d857c5aaa35b8877db1deb33286daceb3af5d9e`

One branch for staging, authorized by the owner for this purpose only (no production branch, no deploy).
Built from `feat/staging-preflight` at `e6aa7a5`:

* `c832fcb`, merge of `docs/codex-reverification-6-closeout` (`c5291b4`, `856d9a5`, `0ea08a8`, `78b7a05`,
  `67c1e25`): Codex's verified prose-total and atomic-confirmation corrections had forked at `53869d7` and were
  never on the release line. One textual conflict (the file workflow's pinned counts), resolved from collection
  on the merged tree. The merge applies exactly the correction's lines (router 30, assistant 20, editing 92)
  and drops nothing; no code uses the removed `confirmed=`/`_presented`/`used` API.
* **An interaction worth an independent look, now tested** (`test_files_router.py`,
  `test_confirmed_edits_are_counted_once_and_a_failed_confirmed_attempt_is_not_charged`): the release line's
  chat retry resends the same confirmation id. On `e6aa7a5`, after a provider failure that followed a stored
  confirmed edit, that retry applied the exact edit a second time (the test fails there). Merged, the retry is
  refused with a fresh offer, the failed attempt returns 502 through the allowance hook (uncharged) and each
  request passes the hook once. Question: is any other path able to present a consumed id and get an effect?
* `bdc153a`, `5b0a864`: the host assessment, the read-only host script and the edge configuration
  (`deploy/staging/CLOUDFLARE-ACCESS.md`). Question: is the webhook exception (Access bypass for Stripe's
  webhook addresses on the exact path, POST only at Nginx, Authenticated Origin Pulls with the zone's own
  certificate) as narrow as claimed?
* `7d857c5`: two checks this session's earlier work had missed, found by the release's CI: the finance
  package guard now names the staging preflight (an operator command, like `finance_ledger_admin.py`), and
  the anonymous-allowance workflow counts the Spaces phone test (645). Cherry-picked to `feat/staging-preflight`
  as `fe7a43f`.

CI: file generation 44368926553 (1,054 passed), subscription 44368936581 (every pinned set; PostgreSQL job 644; root verifiers: provisioned boot 16/16 starting the application from the staging template, registration bootstrap 27/27, protected files 26/26), frontend 44368944911 (1,897 passed, 19 skipped), provider ceilings 44368884844, anonymous allowance 44368891907 (645), safety 44368955624 (820), Spaces 44368966234 (1,249): all green on `7d857c5aaa35b8877db1deb33286daceb3af5d9e`. Local real PostgreSQL on the merge `c832fcb`: 25 of 25 (file allowance 7, Premium files, journey and Spaces 1 each, billing/usage/quota 10, Spaces under V1 4, lifecycle 1).

`deploy-tooling-verification` fails on the release (44361179716) exactly as on `21258e5`, before this work (control run 44368695381): its `test_production_shape_passes` pins production's schema head `0015_finance_ledger`, and the release line has been past it since 0016 (2026-11-15). That is the production release tool correctly refusing a schema production does not have; the staging runbook does not use it. Not changed.

**Nothing is resolved by this integration.** Codex verified `78b7a05`; the merge is new code. Every pending
audit (D3 through the allowance correction, sections 1–13) stays open and now applies to this combination too.

## 15. Data lifecycle: expired files and usage records (`feat/data-lifecycle` at `c0beeb9`, docs `1057222`)

From `release/staging-v1` `fed4a6c`. Not merged into the release. Nothing is scheduled and no environment ran it.

* `kalillac_files/retention.py` and `tools/data_lifecycle.py files`: delete a file once it has been
  expired for an explicit period plus a one-hour margin, plus abandoned upload temporaries and orphaned data.
  It enters only `[0-9a-f]{32}` owner folders and FileStore's own names. Each file is decided from its own
  record in its own folder. On POSIX, every stat and unlink is relative to a folder opened `O_NOFOLLOW`. Data
  is deleted before its record, unreadable records are never deleted, and a run lock (`.retention.lock`, flock)
  keeps runs apart. Nothing is deleted without `--apply --confirm-deployment <KALILLAC_DEPLOYMENT_ID>`, there
  is no default period, and the output is counts only.
* `FileStore.purge` now uses these rules. **Behaviour change worth checking:** it used to delete every
  `*.tmp`, uploads in progress included.
* **A race found and fixed while testing**: a cleanup whose expiry clock ran ahead judged a fresh upload
  (data written, record not yet) as orphaned. Ages on disk now use the real clock; regression test
  `test_an_upload_caught_between_its_data_and_its_record_survives_any_expiry_clock`.
* `kalillac_accounts/usage_retention.py` and `tools/data_lifecycle.py usage`: delete
  `account_usage_daily` and `account_usage_slots` rows older than an explicit number of days (at least 8),
  as the runtime login, by the database's UTC date, with `lock_timeout` 5 s and `statement_timeout` 60 s.
* `docs/DATA-LIFECYCLE.md`: the data map, the erasure plan (not built; V1's security tables reference
  `users` with no `ON DELETE`, so a tombstone is proposed), what is buildable now and what needs the owner.
  It also records a **gap: no customer page lists or deletes stored files** (the API does both).

Questions for Codex:
1. Can any interleaving of `FileStore.save`/`read`/`delete` with a cleanup run delete live data, or leave a
   record pointing at deleted data? (On Windows an open file cannot be unlinked; that run leaves it for the
   next. The exactly-once deletion count is asserted on POSIX only.)
2. Is a one-hour stale-temporary age safe against the slowest legitimate upload (10 MiB)?
3. Should the usage cleanup run as R (today), or as a narrower function owned by S/P?

Tests: `test_file_retention.py` 24 on a real filesystem (threshold, dry run, isolation across accounts
and foreign names, symlinks, interrupted runs, temporaries, orphans, unreadable records, repeated runs,
threads and separate processes with uploads and reads, the command); `test_data_lifecycle_postgres.py`
9 on real PostgreSQL under the final grants (exactly the old rows of two accounts deleted as the runtime
login with no account table written, dry run, repeated runs, the owner report still reading its week, the
8-day floor and malformed periods refused, four concurrent cleanups racing today's quota writes with each
old row deleted once, the command's confirmation and counts-only output). CI on `c0beeb9`: file generation 44406198275
(1,078 passed), subscription 44406206211 (both jobs green; the PostgreSQL job ran its pinned 653).

**Nothing here resolves an audit finding.** The legal review's gaps 1 and 2 are mechanisms only until the
owner sets D-9 and D-11 and approves a schedule per environment.

## 16. Corrections for the Premium audit of 2026-11-18 (F1 to F6): verification requested

Details, original failures, coverage and open questions: `docs/CODEX-AUDIT-CORRECTIONS.md`, section "Premium
audit of 2026-11-18". Nothing is marked resolved; please verify each at its SHA.

| Finding | Branch | Full SHA | Reproduce the original failure |
|---|---|---|---|
| F1 | `fix/local-reset-ownership` (fast-forwarded into `feat/local-staging`) | `afa247ad58c33c9e11e601d4ee261b0f23942d8a` | `test_local_reset.py` against `8760bfc`'s `tools/local_staging.py` (the new API does not exist there; the commit message shows the direct reproduction: an unrelated directory deleted) |
| F4, F5 | `fix/premium-audit-corrections` | `4a36dd7a47fa7812d5424311e7cad722dfe35572` | `test_files_session_and_modes.py` fails on `ac1f2a9` (both tests) |
| F2, F6 | same | `8de030b172487d2339c769cb2292f280aa9730b9` | `test_files_retention_access.py` on `4a36dd7` (the store and router assertions) |
| F3 | same | `a12bf1411610b05ade658736187a5a2783451d71` (test-stub follow-up `52b2408927aabb7c46bc840007e684e0d532d53b`) | `test_without_the_account_limit_failing_requests_reach_the_provider_every_time` keeps the original behaviour as evidence |

Worth an independent look:
* F3 adds migration `0026_account_provider_attempts` (a counter on `account_usage_daily`) and moves the release
  head, so `tools/v1_security_transition.py`'s `HEAD` is now 0026. The column is covered by the runtime role's
  existing table grant; the V1 transition's catalog checks were rerun on real PostgreSQL (173 passed).
* F3's limit is required configuration whenever accounts and provider ceilings are on; production (accounts
  off) is unaffected. Is refusing a signed-in request that carries no account id (rather than counting it
  nowhere) the right failure mode?
* F2: the application now deletes expired files on its own schedule. The default period is 0 days when unset,
  which matches the startup purge it replaces; the staging template requires an explicit value.
* F6: `GET /api/files` now answers 200 to every signed-in account, so the chat reads `can_add` instead of
  the status. Any client that relied on 403 for "files not offered" must follow.

Also on this branch: `docs/DATA-LIFECYCLE.md` corrected (expired files were deleted at startup, not never) and
the decisions that complete account erasure (E-1 to E-8).

## 17. Second-round corrections after your verification of `c6fcaeb`: verification requested

You verified F3, F5 and F6. These correct the four residual defects you reproduced. Details, the probe
results before and after, the regression tests, CI and known limitations are in `docs/CODEX-AUDIT-CORRECTIONS.md`,
section "Second round". Nothing is marked resolved.

| Defect | Branch | Full SHA | Your reproduction, rerun |
|---|---|---|---|
| F1-A ancestor exchange, F1-B process identity | `fix/local-reset-ownership` (not merged into `feat/local-staging`; the running local install is untouched) | `8ae23d51c0c7f408fb5cdd10134da26b4f7b7bc7` | Your probe, run as root in WSL: on `afa247a` the unrelated data was deleted and the other checkout's process killed (-15). On `8ae23d5` the reset is refused (exit 2), the data survives, and the process is not classified as ours or signalled |
| F2 physical quota | `fix/premium-audit-corrections` | `68730da364f18014d04c73d1d03d7f11f8a2b99d` | Your probe: 12 bytes before, 4 after (Linux and Windows) |
| F4 detached worker | same | `7d2ed5bdeb817763b252a848a4e564f86d99b4eb` (tip of the branch) | Your `test_disconnected_file_worker_still_belongs_to_the_session`, unchanged, passes: registered 1, active 1, cancellation requested, 1 model call, 0 files |

Worth an independent look:
* F1: `_open_parent` trusts uid 0 and `ROOT_OWNER` only, and accepts a sticky ancestor (not the parent).
  `reset` then deletes with `shutil.rmtree(name, dir_fd=parent)`, which needs Python 3.11 or later; the
  command refuses on older versions.
* F1: the pidfd liveness check before `killpg` (limitation 1), and the legacy PID-file window (limitation 2).
* F2: reclaiming relies on every writer of an owner folder holding its `flock`. `FileStore.put` is the only
  writer. The retention cleanup does not take the owner lock: it deletes only expired files, and data or
  temporaries older than an hour.
* F4: `asyncio.shield` and `run_in_executor` replace `run_in_threadpool` for the assistant thread, with
  `contextvars.copy_context()` so that the request budget and account context still reach it. The assistant
  slot is now held until the thread ends, also on `/api/files/assist`.

## 18. F1-C and F1-D after your second verification: final F1 verification requested

You verified F2 (`68730da`) and F4 (`7d2ed5b`). This corrects the two F1 points you raised. Details, the
reproduction, tests and limitations are in `docs/CODEX-AUDIT-CORRECTIONS.md`, section "Third round". F1 is not
marked resolved.

| Defect | Branch | Full SHA | Your reproduction, rerun |
|---|---|---|---|
| F1-C, F1-D | `fix/local-reset-ownership` (not merged into `feat/local-staging`; the running install is untouched) | `69c8e8b755fd0e2b4045bb2c2d8475156bc29e3c` | Your mocked `stop` sequence: on `8ae23d5` exit 0, `numeric_group_signal`, records removed, "stopped"; on `69c8e8b` exit 2, only `pidfd_signal` events (`15, 4` then `9, 4`: the group flag), no record removed, the reasons printed |

CI: `44464689210` (main job 715, root step 48, runner kernel 7.0.0-1017-azure (the group flag is supported; the fallback is exercised by the test that forces the kernel to refuse the flag)).

Worth an independent look:
* `_signal_group`'s fallback, used where the kernel refuses `PIDFD_SIGNAL_PROCESS_GROUP`: members are taken
  from `/proc` (group and session equal to the leader's PID, started no earlier than it), and each is signalled
  through its own pidfd only after a read made while both it and the leader are alive.
* `_wait_gone` confirms an exit through the leader's pidfd, then waits for the group to empty. It errs towards
  failing.
* `_identity` is now three-way ("ours", "other", "unknown"); only "ours" is ever signalled. Its last plain
  `False` return was caught by the foreign-checkout test while I was making this change.

## 19. The two remaining F1-C cases after your verification of `69c8e8b`: final F1 verification requested

You verified F1-D. This corrects the two F1-C cases you reproduced. Details are in
`docs/CODEX-AUDIT-CORRECTIONS.md`, section "Final F1-C round". F1 is not marked resolved.

| Case | Branch | Full SHA | Your tests |
|---|---|---|---|
| Unreaped leader with a live child; PostgreSQL status errors | `fix/local-reset-ownership` (not merged into `feat/local-staging`; your local install and its data untouched) | `364637399d77abf7922a5aab610975882fba6d57` | `test_independent_final_f1.py` committed unchanged (sha256 `29664edc...0444f6`): 6 failed and 6 passed on `69c8e8b`, as in your evidence; 12 passed on `3646373`, as the user and as root |

CI: `44473349667` (main job 734, root step 66, runner kernel 7.0.0-1017-azure).

Worth an independent look:
* `_exited` is a non-blocking `select` on the leader's pidfd, made before `_identity` and again whenever the
  identity reads "other". So "other" means "absent" only for a live process that is not ours.
* `stop` treats only `pg_ctl status` 3 as "stopped"; it checks before and after `pg_ctl stop`.
* The F1-D signalling functions are byte-identical to `69c8e8b`.

## 20. Local staging (`feat/local-staging`, `a4d5e71`, on `release/staging-v1`; numbered 15 on that branch)

The owner's staging, on the development machine instead of a server. No application module changes;
four tools, a test file, a Windows launcher and a guide (`deploy/local/README.md`).

* `tools/local_staging_sim.py` replaces only the outermost network exchange of each simulated provider:
  `provider_transport.post_json_within_budget` and `search_providers.post_json_unbudgeted` (for the
  OpenAI, Brave and Tavily hosts only), the application's `_post_openai_responses` (installed after the
  module executes, so the launcher still imports the application only after its singleton lock), a
  `StripeGateway` subclass overriding its five network calls (`parse_webhook` stays Stripe's), and
  `ResendGateway._deliver`. Questions: can it install anywhere but a local deployment (`require_local`:
  a `kalillac-local-` deployment id, a localhost origin, Stripe test mode)? Can any control be skipped
  because of where it hooks in (the wrappers around `_post_openai_for_attempt` are untouched; the
  budgeted transport's own deadline checks are not run for simulated hosts)? Can a simulated provider's
  host be reached (`guard_network` blocks resolution in the application's process)?
* `tools/local_staging_web.py` proxies `/api` and adds `/__local__/` (outbox, simulated Checkout and
  portal), loopback only, same-origin actions. Question: can the panel act for an account other than the
  browser's own session?
* `tools/local_staging.py` provisions and activates exactly as the boot verifier does, into
  `/var/lib/kalillac-local-staging`, with the boot verifier's synthetic commercial values, labelled.
* Guard: `test_the_application_never_references_local_staging`.

CI: subscription 44385988423 on `a4d5e71` green (PostgreSQL job 668 tests, none skipped, including the 24 simulation tests; root verifiers included). Browser evidence (Edge through the repository's isolated CDP harness, against the running local instance, simulated providers): Premium 6/6 (sign-in with the Secure cookie on localhost, account page, chat, PDF generated and downloaded 101 KB, image checked and re-encoded), Free journey 8/8 (end now, Deep refused, no file on Free, simulated Checkout to Premium with the return notice, a Space with a saved conversation, cancel at period end), editing 2/2 (a Word edit held back by the total check, applied as `proposal-edited.docx` after the customer's confirmation).

## Not in these checkpoints

E1b (entitlement and billing grants behind a reviewed billing path), erasure (owner decision 6), image
generation (design only: `docs/I2-IMAGE-GENERATION-DESIGN.md`), the owner dashboard. Open document-quality findings are tracked in
`docs/CODEX-AUDIT-CORRECTIONS.md`.

## Reproducing

Every PostgreSQL suite needs a disposable local PostgreSQL 16 and
`KALILLAC_REGISTRATION_TEST_POSTGRES_URL` pointing at its `postgres` database on loopback (the
workflows show the role layout). The two root-only verifiers (`tools/verify_registration_bootstrap.py`,
`tools/verify_provisioned_boot.py`) create one `/run` fixture each and remove it.
