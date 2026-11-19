# Premium corrections integration (2026-11-19)

Codex has independently verified all six Premium audit findings, each at its own correction checkpoint:
* F1 at `364637399d77abf7922a5aab610975882fba6d57`;
* F2 at `68730da`;
* F3 at `a12bf14`;
* F4 at `7d2ed5b`;
* F5 at `4a36dd7`;
* F6 at `8de030b`.

This branch combines them into one development candidate. It is **not** approval to deploy Premium publicly:
erasure, the commercial and legal decisions, and real-provider and Stripe acceptance testing remain separate.

## Candidate

* **Branch** `integration/premium-corrections-v1`, from `release/staging-v1` at `fed4a6c`.
* **Code commit** `1cc32515b3b198feef967e8f531184a0c120725b`, made of two merges:
  * `74a3b72` merges `fix/premium-audit-corrections` at `d66a12c`: F2 to F6, with `feat/data-lifecycle` under
    them. No conflicts.
  * `1cc3251` merges `fix/local-reset-ownership` at `3646373`: local staging and F1. Conflicts in three files,
    resolved below.
* A later commit adds this record, the local install's upgrade procedure and its rehearsal script. It changes
  no application code, test or workflow.

Both correction branches were linear on `release/staging-v1`, so nothing on the release was missing from
either.

## Conflicts and their resolution

| File | Conflict | Resolution |
|---|---|---|
| `.github/workflows/subscription-foundation-verification.yml` | Both branches extended the V1-02D job: different test files added (`test_data_lifecycle_postgres.py`, `test_account_attempts_postgres.py`; `test_local_staging.py`, `test_local_reset.py`, `test_independent_final_f1.py`), a comment, the pinned total (657 against 734), and the new root step | Union: every test file from both; both comment entries; the root step kept unchanged (66 tests). The pinned total is computed, not chosen: the release has 644, the audit branch adds 13 and local staging 90, so **747**. CI ran exactly 747 with 0 skipped |
| `docs/CODEX-HANDOFF-2026-11-17.md` | Both branches added a section numbered 15 | Both kept. Sections 15 to 19 (the audit corrections, which Codex cites) keep their numbers; local staging becomes **section 20**, noting it was 15 on its branch |
| `docs/CLAUDE-SESSION-HANDOFF.md` | Two updates added at the same place | Both kept, in time order; the local staging update's reference now says section 20 |
| `docs/V1-EXECUTION-BLUEPRINT.md` (merged automatically) | Referred to local staging as "handoff section 15" | Now says section 20 |

`docs/DEVELOPMENT_ROADMAP.md` merged automatically.

## The verified corrections are intact

* **Every non-document file the merge changed equals one branch's version byte for byte.** That is 62 files,
  all application code, tests, tools, migrations and the site; the subscription workflow and documents are
  excluded. No code was edited while merging.
* **The verified files equal their checkpoints exactly.**
  * At `3646373` (F1): `tools/local_staging.py`, `tests/test_local_reset.py` and Codex's
    `tests/test_independent_final_f1.py` (sha256 `29664edc…0444f6`).
  * At `7d2ed5b` (F2 to F6): `kalillac_files/store.py`, `router.py` and `retention.py`;
    `app_fastapi_candidate.py`; `kalillac_routing/provider_controls.py` and `reasoning_modes.py`;
    `tests/test_files_physical_quota.py` and `test_files_detached_worker.py`; migration `0026`.
* Every original failing regression test and every later one is present and collected. No test was removed,
  deselected or loosened: the only deselections are the two that already existed on `3646373`.

## CI on the exact commit `1cc3251`

| Workflow | Run | Result |
|---|---|---|
| subscription-foundation-verification (unit and real PostgreSQL) | `44480323689` | success. Suites 1,583; Slice 2 106; Slice 3 917; registration 53; V1-01 96; V1-02A 192; V1-02B 117; V1-02C 154; **V1-02D 747**; local staging as root **66**; all with 0 skipped. Provisioned boot from the staging template passed. Runner kernel 7.0.0-1014-aws |
| file-generation-verification | `44480328832` | success, 1,105 tests, 0 skipped |
| spaces-verification | `44480333303` | success, 1,249 tests |
| provider-ceilings-postgres | `44480338084` | success, 400 + 53 tests |
| anonymous-allowance-offline | `44480350682` | success, 646 tests |
| safety-helpfulness-regression | `44480346739` | success, 820 tests |
| code-structure-frontend-verification | `44480342444` | **attempt 1: failure**. 1,890 passed and 19 skipped, with 24 errors, all in the setup of one module: `test_route_navigation.py`, desktop variants, where the browser did not open its DevTools port. **Attempt 2 on the same commit: success**, 1,933 collected, 1,914 passed, 19 skipped (the workflow's expected counts, as on the audit branch's run `44451769309`) |
| deploy-tooling-verification (control) | `44480374783` | failure, the known one: `test_production_shape_passes` expects production's schema head `0015_finance_ledger`; every release since `0016` fails it (handoff section 14). 1 failed, 46 passed; the systemd job passed |

## Interactions checked

| Interaction | Evidence |
|---|---|
| Account limits with failed provider requests (F3) | `test_account_attempts.py`, `test_account_attempts_postgres.py`, `test_paid_scope_integration.py` and `test_v1_premium_journey_postgres.py` (subscription); provider ceilings |
| Uploads and physical storage quotas (F2) | `test_files_physical_quota.py`, with real killed processes and concurrency (file generation) |
| End & erase with active file processing (F4) | `test_files_detached_worker.py` and `test_files_session_and_modes.py`, through the real application |
| Quick, Balanced and Deep (F5) | `test_files_session_and_modes.py` (file paths); the Spaces and chat reasoning suites |
| Premium cancellation and retained files (F6) | `test_v1_premium_files_postgres.py` and `test_files_retention_access.py`; the account page's files section (frontend) |
| Spaces and saved conversations | spaces-verification (1,249); `test_v1_premium_spaces_postgres.py` |
| Authentication and authorization | the subscription workflow's V1 suites (Slice 2 to V1-02D) |
| Safe local staging start and stop (F1) | `test_local_reset.py` and Codex's file, as the user and as root; `test_local_staging.py` |
| Local staging against the new required settings | Locally on the merge: `test_local_staging.py`, `test_local_reset.py`, Codex's file, `test_staging_files.py` and `test_staging_preflight.py`: 133 passed, 1 skipped (root-only). Local staging's settings come from the boot verifier's labelled synthetic values, which now include the attempt limits and the deletion period |

## Migration compatibility through 0026

* **Chain.** A single head, `0026_account_provider_attempts`, revising `0025_v1_row_lock_anchor`
  (`test_account_migration.py`).
* **Activated database with data, on disposable PostgreSQL.**
  * Starting point: an activated V1 database at `0025` holding 3 accounts and 6 usage rows.
  * Upgraded to `0026`: every row kept, each counter 0, the CHECK constraint present, the V1 activation
    unchanged, the application's startup gate satisfied, and the runtime login able to count attempts.
  * Downgraded to `0025`: every row kept, only the counter dropped.
  * Upgraded again: the same privileges as after the first upgrade.
  * The check was run once and not committed.
* **Database upgrade rehearsal on a copy of the owner's install.** `deploy/local/rehearse_upgrade.py`, run on
  2026-11-19:
  * `pg_basebackup` copied the running database, read-only for the original.
  * The copy was migrated `0025` to `0026` with the deployment's own migration login.
  * All 29 tables and 91 rows were unchanged, the activation untouched, and the counters 0.
  * The original was identical before and after, and the copy was removed.

## The owner's local install

* It runs from `kalillac-ai-local` at `afa247a`, database `0025`, V1 activated and bound to release `198dbb84…`.
* It was only read: processes, settings keys, the migration version and row counts.
* It was not stopped, reset, rebuilt, migrated or re-pointed.
* The upgrade procedure is `deploy/local/UPGRADE.md`:
  1. stop with the old code;
  2. back up with a file copy while PostgreSQL is stopped;
  3. fast-forward the checkout and rebuild the site;
  4. append the three new settings (labelled synthetic), keeping `KALILLAC_RELEASE_SHA`;
  5. migrate to `0026` with the migration login, row counts compared;
  6. start, `refresh-web`, and check in the browser.

  To roll back, migrate down in place and check out `afa247a`; restoring contents into the original folder is
  the last resort.

## Findings

1. **Frontend attempt 1: browser start-up errors.** In 24 tests of one module, the browser never opened its
   DevTools port during setup. Before rerunning, I checked whether the merge could have caused it. Every
   frontend input (site, frontend, tests, workflow) is identical to the audit branch's, which passed this
   workflow; the only differences are three local staging test files that this workflow does not collect.
   The rerun of the same commit passed with the expected counts. If it recurs, the harness's start-up wait
   (`test_homepage_handoff.py:319`) should be examined.
2. **The owner's install cannot start the new code until its settings are completed.** Its `settings.env`
   predates three settings the application now requires: the Free and Premium daily provider-attempt limits
   and the expired-file deletion period. `deploy/local/UPGRADE.md` step 4 adds them, as labelled synthetic
   values.
3. **The V1 activation is bound to the release ID in the settings.** The install's activation names
   `198dbb84…`, and the startup gate refuses any other `KALILLAC_RELEASE_SHA`. The upgrade must keep that value.
4. **Handoff section numbers collided** (two sections numbered 15). Resolved by renumbering local staging to 20.
5. **Unchanged, known:** the production release tool's preflight still pins production's schema head `0015`
   (`deploy-tooling-verification`). It does not affect staging, but must be resolved before any production
   release procedure.

No code defect was found in the combination.

## Recommendation

**The candidate is ready to merge into `release/staging-v1`.** Every required workflow passed on the exact
commit (frontend on its second attempt, explained above). The verified corrections are byte-identical, and
every conflict is a reconciliation of additions, documented above.

The release has not moved since the candidate was cut, so the merge is a fast-forward of `release/staging-v1`
to this branch's head. It has not been done here; it awaits the owner's go-ahead. Codex asked to inspect the
conflict resolutions and counts before the merge: the three resolutions above, and the 747 total computed and
then confirmed by CI.

**Not resolved by this integration** (they block a public Premium launch, not this merge):
* account erasure (E-1 to E-8);
* the owner's commercial values: the attempt limits (A-1), the deletion period (A-2 and D-9), and the
  allowances, budgets and prices;
* legal documents;
* real-provider and Stripe test-mode acceptance testing;
* the production release tool's schema head.
