# Local Premium upgrade tooling: independent review handoff

Development branch: feat/local-premium-upgrade-safe, based on the approved integrated application
release d8f45f71a8d6d4ec80512e7c770b9625293e34bb. This is not independent approval of these new tools.
Review before any execution against the owner's installation.

## Authorization and preservation

The owner authorized implementation, preparation, isolated testing and CI only. No stop, restart,
rebuild, reset, setup, Git update, migration, configuration edit or cleanup was executed against the
existing installation. Production and release/staging-v1 were not modified.

Existing checkout: C:/Kalillac/kalillac-ai-local, branch feat/local-staging, at afa247a.
Read-only SELECTs confirmed PostgreSQL 16.2, database kalillac_local, data directory
/var/lib/kalillac-local-staging/pg, schema 0025_v1_row_lock_anchor, active V1 deployment
kalillac-local-staging, activation release 198dbb84de5669d33588f9e253323386211a0a4b,
activation id e4e8f58a-17e7-4903-8fc0-23477c5da63d. Root marker: device 2096/inode 26328.
These are inspection evidence, not defaults to override a future identity mismatch.

## Files and behavior

- deploy/local/upgrade_support.py: full filesystem SHA-256/permission manifests, backup validation,
  PostgreSQL 16 lifecycle checks, explicit integrity exceptions, consistent read-only all-table
  row/column/sequence fingerprints, endpoint/activation verification and transactional Alembic.
- deploy/local/rehearse_upgrade.py: replaced the running-install/counts-only script with a backup-only
  command. No live URL/port option. It migrates/downgrades a scratch cluster, verifies existing contents
  and actual file reads, then checks a second restored copy. A receipt is issued only after confirmed
  shutdown. Failure preserves scratch state.
- deploy/local/live_upgrade.py: separately gated stop, backup, apply operations. Exact destination,
  inode/device, clean checkout SHA, activation and authorization required. Stop uses F1 code bound to
  the original checkout. Backup requires all service-user processes absent and a clean PostgreSQL
  shutdown. Apply requires an unchanged installation, full backup and successful rehearsal receipt.
  It starts PostgreSQL alone, verifies data, migrates, confirms shutdown, then adds synthetic settings.
  It never starts the application, changes Git, calls reset/setup or restores/downgrades automatically.
- deploy/local/UPGRADE.md: pinned fast-forward from origin/release/staging-v1, preserving branch history
  and refusing dirty work. Describes authorized manual recovery without replacing the original root inode.
- candidate/tests/test_local_upgrade.py: isolated root harness, PostgreSQL running as nobody, synthetic
  credentials, fresh loopback ports, full migrated application schema and active V1.
- .github/workflows/local-upgrade-verification.yml: separate CI check, exactly 17 tests, zero skips.
  Existing workflows and pinned collections were not weakened.

The migration target is exactly 0026_account_provider_attempts, not a future head. Every existing
column/row and sequence is fingerprinted. Only Alembic's exact revision change and the new counter
are normalized; its BIGINT/NOT NULL/default and all-zero values are checked. Activation includes all
record fields, including its id. Files include metadata/blob hashes, ownership mapping, id/size
consistency and FileStore reads of unexpired records. Cold backup includes the complete PostgreSQL
cluster, roles/WAL, credentials, registration/file-access keys and other filesystem state.

## Verification

Local Linux run: 17 passed, no skips, real disposable PostgreSQL 16.
Servers ran as uid 65534; the owner's service identity is separate. Only test clusters were stopped.

Coverage includes:

1. Successful rehearsal, real 0025->0026->0025, fresh restore and retained activation.
2. Equal-row-count corruption of password hash, Space instructions, conversation title, message text,
   entitlement, Stripe subscription status and activation binding, each independently changed/restored.
3. Same-length file corruption; invalid blob, metadata and manifest checksum rejected.
4. Failure before migration, inside its transaction, after committed bad work and injected startup
   failure. A new copy restores original database/files/activation.
5. Unknown statuses 1/2/4/127; failed shutdown leaves an actually running scratch cluster intact.
   The test then manually confirms its stop. No automatic destructive recovery.
6. Live apply helper exercised ONLY against a disposable fixture/fresh port, preserving data and files,
   with unchanged activation and labelled synthetic configuration.
7. Wrong destination/release/authorization refused; integrity checks execute under Python -O.
8. Ambient local-root or PostgreSQL-bin settings cannot redirect the verified stop destination.

An additional disposable-copy probe executed the real previous-version (afa247a) V1 startup gate,
usage-model reads and file reads before upgrade and after restoration, plus the current-version gate
after migration: passed. This is stronger than schema equality and does not claim full browser acceptance.

CI runs on the development PR; inspect the exact checked-out SHA in Actions logs. The final implementation
report supplies the run links and results. Successful CI does not establish commercial launch readiness.

## Retention and finance

Read-only inventory: eight files expiring 2026-12-18. Retention dry run at zero days: zero expired files,
orphans, stale temporaries, unreadable records or skipped entries. All providers are simulated:
OpenAI, Brave, Tavily, Stripe and email. No real financial operation was run.

Prepared local-only settings: FREE attempts 100, PAID attempts 1000, delete-after-expiry 3650 days.
These are SYNTHETIC TEST INPUTS, not approved commercial limits. Expired files remain unavailable.
Stale orphan/temporary cleanup is independent of that period. Apply refuses known startup cleanup
candidates; repeat the dry run immediately before any separately authorized application startup.

## Independent review targets and limitations

- Inspect shutdown and cleanup: failed body or unconfirmed stop preserves scratch state. Cleanup deletes
  only a fresh scratch root after inode verification and a confirmed clean stop. Installed roots are never deleted.
- Scratch configuration overrides copied auto.conf archive/replication/preload/external-path settings.
  Replica/recovery clusters, symlinks and external tablespaces are unsupported. Port collision fails closed.
- Backup hashes require a trusted, separately recorded manifest hash. Backups/failed copies contain secrets
  and customer data. Their later retention/cleanup needs an explicit decision.
- The service-uid guard is conservative: any live process blocks backup/apply. It never guesses what to kill.
- Apply accepts only the exact approved integrated application SHA. Future releases require another review.
- Content verification covers rows, columns, sequences and activation, not every catalog definition.
  Existing schema/security verifiers remain relevant. Migration files are unchanged.
- Offline downgrade drops provider_attempts. Backup restoration discards ALL post-backup writes.
  Keep ingress closed; reconcile separately if post-upgrade writes exist.
- Startup failure is fault-injected after migration. Actual browser acceptance and genuine old/new
  application startup on the owner's data are not claimed.
- No new backup or rehearsal of the existing installation was performed: its clean shutdown awaits
  separate authorization. Previous counts-only rehearsals do not establish content preservation.

Account erasure, legal/commercial decisions, real-provider/subscription acceptance and public launch
approval remain separate.

## Corrections after the audit of ca92dc6 (U1 to U4, recovery): another review requested

The audit (`tmp/upgrade-audit-evidence-a8s3a0ii/`, preserved unchanged) found four confirmed defects. Each was
reproduced before correction. Codex's own probe file was run, unchanged, in a disposable `git archive` snapshot of
`ca92dc6`, as root in WSL with a disposable world-readable copy of PostgreSQL 16.2. The result was **4 failed and
4 passed**, matching the audit's log:
* U1: requested port 56385, connected to 37477;
* U2: WSL Git exited 128;
* U3: the service account got PermissionError, yet the rehearsal reported accessible=1;
* U4: mode 0o600 became 0o644.

The 17 original tests passed there too. Nothing here is marked verified.

| Defect | Correction | After |
|---|---|---|
| U1 connection redirect | `upgrade_support.connection_target` builds both URLs with `URL.create` from validated parts: driver `postgresql+psycopg`, host `127.0.0.1`, the given port, the deployment's database name, and identifier-checked users. It refuses any URL query parameter, a malformed migration login, and any set libpq variable that could redirect a connection (`PGHOST`, `PGHOSTADDR`, `PGPORT`, `PGDATABASE`, `PGSERVICE`, `PGSERVICEFILE`, `PGSYSCONFDIR`, `PGOPTIONS`, `PGTARGETSESSIONATTRS`, `PGLOADBALANCEHOSTS`), all before either engine exists. `_bound_engine` adds an explicit `hostaddr` and empty `options`, and compares the driver's parameters with the destination before any connection. The database identity check is unchanged | Codex's probe: refused ("connection options in the administrative URL are refused"), no engine created |
| U2 Windows-created worktree | `checkout_identity` classifies the `.git` layout. For a worktree created by Git for Windows (a `gitdir: C:/…` file), it runs Git for Windows through WSL interop: the Git, `core.autocrlf` and index cache that created it. Linux layouts use Linux Git. Both run with `--no-optional-locks` (status never rewrites the index) and a cleaned environment (`GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE` and `WSLENV` cannot redirect it). Checks: the selected path is the checkout's own top level, HEAD is the exact SHA, the branch when one is required, and nothing tracked, staged or untracked. Without Git for Windows it fails closed. The remote-URL idea was dropped: the 40-character SHA already pins the content | Codex's probe and its synthetic worktree: accepted |
| U3 root reads | `stored_files` and the new `runtime_access` check access in a child process with the service's uid and primary gid and **no supplementary groups**, which is how `local_staging._spawn` launches the application. The child reads every metadata file and unexpired document, and lists and checks write access to every owner folder and the files root. It also reads every `*_FILE` setting (registration keyring, activation, database credential) and every file in `credentials/`, which must lie inside the installation, and therefore the backup. Contents are compared with the manifest or root's own hash and never printed; any denial or indeterminate result refuses. These run in rehearsal (original and restored copies), apply and restore | Codex's probe: the rehearsal is refused ("a stored file cannot be read by the application service identity"); no receipt |
| U4 settings mode | `stage_settings` refuses a symlink, a second hard link, extended attributes (ACLs), an existing staging file and a missing final newline. It writes the new file **before** the migration: created 0600, then given the original's uid, gid and mode, written fully, fsynced. `commit_settings` replaces only if the original and the staged file are unchanged, then fsyncs the directory. If the migration fails, the staged file is removed and the original left byte-for-byte untouched. If it fails after the migration, the staged file is kept and reported | Codex's probe: 0o600 kept |

**Recovery.** `live_upgrade.py restore` is explicit and gated. It verifies the backup (manifest checksum, every
file, and its root's path, device and inode). It requires no service process and PostgreSQL status 3. It moves
every root entry by rename into a new 0700 failed-state folder on the same filesystem, with its manifest recorded
beside it. It then copies the backup's contents into the same root inode, and verifies the manifest and the
service's access to its runtime files. It starts nothing and deletes nothing. `UPGRADE.md` has the exact command,
and states that a restore discards every write made after the backup.

`test_restore_after_a_failed_upgrade_into_the_same_root_and_start_the_restored_code`:
* a real upgrade, then post-backup database and file writes;
* refusals for another root, a failed state inside the root, and a running PostgreSQL;
* the restore: same inode, tree equal to the manifest;
* the failed state preserved, including its settings additions and post-backup file;
* the database at 0025 with every row as backed up;
* files and runtime files usable as the service;
* `afa247a`'s own `load_v1_startup` qualifying the restored database. CI extracts that release with `git archive`.

**Tests.** `test_local_upgrade.py`: the original 17, unchanged in their assertions, plus 68 new ones, 85 in total.
The fixture's stored files are now owned by the service identity (65534), as the application owns them on a real
installation. Root-owned 0600 fixture files would otherwise be exactly the U3 defect.
`test_local_upgrade_windows.py` (9, WSL only; it needs Git for Windows) covers:
* Codex's fixture;
* new disposable Windows-created detached and branch worktrees with CRLF files;
* dirty, wrong-SHA, wrong-branch and subdirectory refusals;
* an environment that cannot redirect the check;
* the index never being written.

Local, as root in WSL: **94 passed, 0 skipped** (85 + 9). On `ca92dc6`, the new `test_local_upgrade.py` fails at
collection (`DESTINATION_ENVIRONMENT` does not exist there). With only that name given a fallback in the
snapshot's copy, it gives 57 failed and 27 passed: the 17 originals and 10 controls that hold on both versions.
Codex's probes on the correction: 6 passed. The 2 that remain fail because they asserted the old outcome, a
correct port and `accessible == 0`, where the correction now refuses outright.

**Known limitations.**
* The Windows-layout tests run only where Git for Windows exists, so not in Linux CI. There, the Linux layouts
  and the fail-closed refusal are tested.
* The service check models the launcher's identity: uid, primary gid, no supplementary groups. A future launcher
  that grants supplementary groups would need this check changed with it.
* `runtime_access` covers what the application reads at startup through configuration (`*_FILE`,
  `credentials/`). It is not a full application boot.
* The limits Codex listed under "Limits and pending evidence" 2 to 5 and 7 to 9 stand: catalog definitions
  beyond the fingerprints, ACL and xattr attestation, the offline window not being a lock, the startup-failure
  model, discarded writes, time-sensitive cleanup, and the disposal of private backups.
* While testing, the tool's own failure-preserved scratch copies filled WSL's 3.8 GB `/tmp` (tmpfs). I removed
  only the copies my runs created, and ran the suite with `TMPDIR` on disk. An operator should give the
  rehearsal a `TMPDIR` with room for two full copies.

**Preservation.** The owner's installation was not accessed in this round, not even to read it. No live backup,
migration, restore or upgrade was run. `release/staging-v1`, production and every worktree are unchanged apart
from this branch. PR #40 stays in draft.

## Corrections after the verification of 93ac509 (R1, R2, U3-R1, U4-R1): another review requested

Codex verified U1 and U2 at `93ac509`. Its evidence (`tmp/local-upgrade-correction-evidence-yqeyxbmk/`) is preserved
unchanged. Its test file, unchanged (sha256 prefix `264740dae3246302`), reproduced **7 failed** on a disposable
`git archive` snapshot of `93ac509`, matching its log. It ran as root in WSL, with `TMPDIR` on disk
(`/var/tmp`, 921 GB free), a disposable PostgreSQL 16.2 copy and `afa247a` extracted for the startup gate. Nothing
here is marked verified. U1 and U2 code is unchanged.

| Defect | Correction | Codex's probe after |
|---|---|---|
| R1 restore could write into its own backup | `restore_preflight` runs before any change. Paths must be absolute and normal, with no symlink. The failed state, `FAILED.manifest.json` and `FAILED.restore.json` must not exist. It refuses a failed state inside the installation, the backup or an earlier failed state; a backup inside the installation; and an installation inside the backup. It checks this by path, and by device and inode for every folder above the failed state, so a bind-mount alias is caught. It also requires a trusted parent (F1's `_open_parent`), the installation's own mount (from `/proc/self/mountinfo`; a rename cannot cross mounts), and free space and inodes for the backup plus 256 MB | passes: backup valid, nothing created |
| R2 no way to finish an interrupted restore | A recovery journal (`FAILED.restore.json`, root's, 0600, created exclusively, replaced atomically) records the root's path, device, inode and activation, the backup path and checksum, the failed state's own identity, and the phase. A separate `resume-restore` operation has its own gate (`recovery_destination`): the fixed root, as root, its device and inode, a clean checkout. It does not use `owned_root`, which is unchanged. Its evidence is the backup verified by its recorded checksum (which names this root's path, device and inode); the journal chain (each journal root's own, naming this root, backup and checksum, each recorded failed state still the same folder); and the original marker, found in the root or a recorded failed state and byte-identical to the backup's, with no foreign marker. It requires F1's `checked_path`, nothing running, and the R1 preflight. It moves the partial state into another new failed state and restores into the same inode | the probe reruns `restore`, which still refuses (the F1 gate, by design); the supported route is `resume-restore`, covered below |
| U3-R1 evidence companion omitted | `runtime_access` derives startup's inventory from `deployment.env` plus `settings.env`. With registration enabled, it needs all four protected files: keyring, activation, `activation + ".evidence.json"` and database credential. Each must be configured, inside the installation, and meet `read_protected`'s rules: root-owned, the service's group, exactly 0440, one link, size within the loader's ceiling, no ACL; every folder from the installation down root's, group 0 or the service's, not group- or world-writable, no ACL; above the root too on the live root. Each must also be readable as the service. With the database enabled, `credentials/kalillac_db_password` must exist; with billing enabled, both Stripe credentials | missing-evidence rehearsal passes: refused, no receipt. `[missing]` and `[root-only]` are refused at their own baseline, which configures no `DATABASE_CREDENTIAL_FILE`; registration startup reads it unconditionally (`read_protected("")` refuses), so that baseline cannot start |
| U4-R1 in-place edit overwritten | `stage_settings` records the original's fingerprint: SHA-256 of its bytes read through the descriptor, size, mtime_ns, ctime_ns and link count. `commit_settings` reopens the original without following links. It requires the same device, inode, owner, mode, fingerprint and no xattrs; otherwise it refuses, keeping the edited original and the staged file. `apply_upgrade` reports "migrated, settings not replaced" with the staged path. The remaining interval between the check and the rename is covered by the maintenance window's rule (no settings writer), stated in `UPGRADE.md`, not by a claim of atomicity | passes |

**Tests.** `test_local_upgrade.py`: the earlier 85 with their assertions intact, plus 38 new ones, 123 in total;
the workflow pins 123. Two adjustments to existing tests:
* the round-2 restore test's expected return dictionary gains the `journal` path;
* round 2's own nested-path test now accepts the untrusted-parent refusal, which comes before the containment
  check when the parent is the service's `state/` folder.

`test_local_upgrade_windows.py`: 9, unchanged. Locally, as root in WSL: **132 passed, 0 skipped** (123 + 9).
The 38 new tests on `93ac509`'s tools: **35 failed, 3 passed**. The 3 are controls that hold on both versions: a
closed protected folder, a `chmod` after staging, and the unchanged-settings success case.

New regressions:
* **U3-R1 (13):** the complete registration set passes. A missing, root-group, 0640, service-owned, symlinked,
  empty or unconfigured file is refused, as is a closed folder, a missing database credential and a missing
  billing credential. The complete PostgreSQL rehearsal without the companion issues no receipt.
* **U4-R1 (7):** same-size and longer in-place edits, a `touch`, a `chmod` and an added xattr are all refused,
  with the edit and the staged file kept. An unchanged original gets exactly the three additions, mode 0600 and
  the activation unchanged. An edit made during a real migration is kept, apply reports it, and the database is
  migrated with nothing lost.
* **R1 (10):** Codex's overlap; the top of the backup, deep in the payload and inside the root; a symlink alias;
  a bind-mount alias; existing folder, manifest and journal collisions; a backup inside the installation; and
  insufficient space. Each is refused with the backup and installation manifests unchanged and nothing created.
* **R2 (7):** interruptions at the first rename, the third rename, before the copy, part-way through the copy
  and on a full disk. Each leaves every original entry present, only the original marker, the same inode and a
  valid backup. Where the root is unmarked, the ordinary `restore` CLI still refuses and `resume-restore`
  completes it.
* **The chain:** four interruptions in a row, then `resume-restore` through the CLI. Afterwards: the same root
  inode, the backup's tree unchanged, five failed states with their journals, the restored tree equal to the
  manifest, the F1 marker valid again, the post-backup writes preserved exactly once among the failed states, the
  database and activation exactly as backed up, files and runtime resources usable by the service, and
  `afa247a`'s V1 startup gate qualifying.
* **Refusals:** another marked installation, a forged journal checksum and a foreign marker are each refused,
  with nothing changed.
* **Python -O:** an overlap, an in-place edit and a missing companion are still refused.

**Known limitations.**
* The startup gate is a gate, not acceptance: no full application or browser start of a restored installation
  was run. `runtime_access` checks presence, the loader's file rules and the service's reads; it does not parse
  or decrypt the protected files, as `bootstrap` does.
* On a rehearsal copy, the folders above the payload are scratch, so they are not checked against the loader's
  rules there; they are checked on the live root (apply, restore).
* The commit-time settings check is not a lock. The maintenance window must exclude any settings writer between
  the check and the rename.
* The journal is written by the tool and trusted only together with the backup checksum the operator recorded
  and the marker comparison. An attacker with root could forge all three, which is outside the threat model.
* The free-space check measures the root's filesystem at the start. Other writers during the restore are
  excluded by the offline requirement, not reserved against.
* The R2 fault injections stand in for real crashes (`os.rename` and `cp` failures, a simulated full disk). A
  `kill -9` mid-rename leaves the same states, but it was not exercised as a real signal.
* Codex's limits that still apply: catalog definitions beyond the fingerprints, ACL and xattr attestation of
  manifests, the offline window not being a lock, time-sensitive cleanup, and the retention of private failed
  states.

**Preservation.** The owner's installation was not accessed, even to read it. No live stop, backup, migration,
restore or upgrade was run. Production, `release/staging-v1` and every other worktree are unchanged. PR #40 stays
in draft. Disposable resources lived under `/var/tmp/ku-r3`.

## Corrections after the verification of 30d11a5: another focused review requested

Codex's final response was blocked by the platform, but its disposable evidence survived at
`/var/tmp/kalillac-pr40-final-audit.AMGCsb0u` (WSL). It was read and not modified: logs, fixtures and two test
files, `test_pr40_final_independent.py` (sha256 `c3e53a18…`) and `test_pr40_receipt_independent.py`
(`630016f4…`). Copied unchanged into a new disposable snapshot of `30d11a5` (base `/var/tmp/k4`, kept short), they
reproduced **9 failed, 8 passed**: the four findings named by the owner and two more in the same log. U1 and U2,
U4 (within its maintenance-window rule) and F1 to F6 are not reopened. Nothing here is marked verified.

| Finding | Reproduced on 30d11a5 | Correction |
|---|---|---|
| 1. Recovery accepted pg_ctl status 1 and 4 as stopped | status 4 restored the root; statuses 1 and 4 passed `_offline_for_recovery` | Only status 3 authorizes a restore; 0, 1, 4 and anything else refuse with nothing changed. One exception, for `resume-restore` only: a half-copied data folder with neither `PG_VERSION` nor `postmaster.pid` answers 4 and cannot run, so it is accepted once no service process exists. A non-folder or link `pg` refuses |
| 2. An older journal restarted a completed recovery | the interrupted ancestor journal was accepted after a later resume completed, and moved later data into a new failed state | Before changing anything, a resume writes `superseded_by` (the new journal's path) into the journal it continues, atomically. A journal whose `superseded_by` names an existing journal is refused; the chain must be consistent. A crash between the two writes leaves the older journal usable (its successor does not exist). Each journal update now uses its own unique temporary, so a partial temporary left by an interrupted update (Codex's journal-fault tests) never blocks the next one |
| 3. A bind-mount alias hid a backup inside the installation | restore accepted it and moved the backup into the failed state | The device and inode of every folder inside the installation, the backup and earlier failed states (links not followed) are compared with the backup, its payload, the installation, and every folder above the failed state |
| 4. The rehearsal checked the default credentials folder, not the configured one | a receipt was issued while the real database and billing loaders found no secret | `startup_environment` builds what `local_staging.service_env` gives the application: `CREDENTIALS_DIRECTORY` = `ROOT/credentials`, overridden by `deployment.env` and then `settings.env`. That effective folder is the one checked (inside the installation, with the files the enabled features require). `KALILLAC_FILES_ROOT`, when set, must exist and be writable by the service |
| 5. `ROOT//elsewhere` escaped the payload | a keyring outside the root was read and accepted | `_inside` refuses non-canonical paths (`//`, `.`, `..`, trailing `/`) and any mapped path that leaves the payload |
| 6. `TMPDIR` mode 0700, which UPGRADE.md recommended, fails every rehearsal | exit 2 after copying; Codex's 0710 case failed only because its deep path made the socket exceed 107 bytes (0710 passes with a short base) | Before copying anything, a probe file in the scratch folder is read as the service identity; a `TMPDIR` the service cannot traverse is refused with that reason. UPGRADE.md now says 0711 (or 0710 with the service's group). The copy's socket now lives in its own short folder under `/tmp`, and `Cluster.start` refuses a socket path longer than 107 bytes with a clear message rather than a PostgreSQL start failure |

Socket length in the fixtures: Codex's rerun of `test_r2_repeated_interruptions…` failed with "PostgreSQL startup
failed". Its PostgreSQL log shows the Unix socket path exceeded 107 bytes under a deep `TMPDIR`. That was an
environmental fixture failure, not an application defect. The PostgreSQL fixture's base is now always
`/var/tmp/ku-pg-*` (on disk and short), and the tools refuse an over-long socket clearly.

**Codex's files against the correction** (in a disposable snapshot): **15 passed, 2 failed**, both by design:
* `test_double_slash…` first asserts that `_inside` maps the bad path outside the payload, which describes the
  defect. `_inside` now refuses it at that line; `runtime_access` refuses it (permanent test below).
* `test_documented_tmpdir…[0o700]` expects the old 0700 advice to work. It is now refused up front with the
  reason, and the documented 0711 and 0710 cases pass.

**Tests.** `test_local_upgrade.py`: the earlier 123 unchanged, plus 34 new ones, 157 in total; the workflow pins
157. The fixture change is only the short `/var/tmp` base. Locally, as root in WSL: **166 passed, 0 skipped**
(157 + 9 Windows). The 34 new tests on `30d11a5`'s tools: **28 failed, 6 passed**. The 6 are accept-side
controls, plus the alias-of-a-folder-inside case, which the old code also refused but by its mount check (it
failed only on the message).

New regressions:
* **Status:** 3 accepted; 0, 1, 2, 4 and 127 refused. In resume mode, a half-copied folder is accepted, and
  `PG_VERSION` or `postmaster.pid` present, 1 or 0 refused. Codex's status-4 restore is refused with nothing
  changed.
* **Journals:** a completed recovery cannot be restarted by the older journal, nor by the completed one, with
  the later data untouched and earlier failed states kept. Only the newest interrupted journal resumes. The
  crash window and a leftover partial temporary do not block.
* **Aliases:** Codex's bind-aliased backup inside the installation, and a failed state under an alias of a folder
  inside it.
* **Paths:** five non-canonical forms; Codex's double slash through `runtime_access`.
* **Credentials:** the configured folder valid passes; missing, outside or empty refuses; a missing configured
  files root refuses; a complete PostgreSQL rehearsal with a missing configured credentials folder issues no
  receipt.
* **TMPDIR:** 0711 and 0710 rehearse; 0700 refuses and leaves nothing behind; a 120-character-deep `TMPDIR`
  rehearses; an over-long socket is refused clearly.
* **Python -O:** a non-canonical path is still refused.

**Known limitations.**
* The status-4 exception for resume relies on the absence of `PG_VERSION` and `postmaster.pid` together with no
  service process. A cluster started by another user against a half-copied folder is outside the offline
  assumption.
* `superseded_by` is written into the earlier journal: the only change ever made to an earlier recovery
  artifact, and it adds a field without removing anything.
* `startup_environment` mirrors `local_staging.service_env`'s precedence by reading its code, not by importing it.
  If the launcher's precedence changes, both must change together.
* The bind-mount tests need root and `mount`: available in WSL and on GitHub's runners.
* Everything Codex listed before still stands: a startup gate is not acceptance, no decryption of the protected
  files, and the maintenance window as the settings serialization.

**Preservation.** The owner's installation was not accessed. No live operation was run. Production,
`release/staging-v1` and the other worktrees are unchanged. PR #40 stays in draft. Codex's audit folder is kept
as found; this round's resources were under `/var/tmp/k4` and `/var/tmp/ku-pg-*`.

## Corrections after the focused verification of 0a0957a (C1 to C4): another focused review requested

Codex's evidence (`tmp/pr40-focused-verification-0a0957a/`) is preserved unchanged. Its test file
`test_pr40_focused_additional.py` (sha256 `3ca3dac7…`), copied into a disposable snapshot of `0a0957a` (base
`/var/tmp/k6`, short socket paths), reproduced **6 failed**, matching its log. The probes:
* `launcher-parser-mismatch` (credentials and files);
* `launcher-parser-false-receipt`;
* `partial-successor-journal`;
* `tmpdir-cli-diagnostic`;
* `backup-destination-bind-alias`.

F1 to F6, U1, U2, U4 and the verified restore protections are not reopened. Nothing is marked verified.

| Finding | Correction |
|---|---|
| C1 journal publication (High) | `_publish_journal` writes a new journal in full to an exclusive temporary of its own (`NAME.new-<random>`, 0600, fsynced), renames it into place (refusing an existing journal), then fsyncs the folder. `_write_journal` updates the same way, with `os.replace`. Order in `_restore_into`: create the failed-state folder, publish the journal as `prepared`, mark the predecessor `superseded_by`, update to `moving`, then move. A failure up to publication leaves the predecessor current; after it, the successor is complete and resumable. `resume_restore` first records any link left unrecorded by a crash (sets `superseded_by` on an earlier journal whose successor exists), so nothing earlier in the chain can be replayed; resuming from the predecessor instead leaves the stray successor's chain inconsistent, so it is refused. A journal that exists but cannot be parsed is refused for review, never ignored. These are several separate filesystem operations, not one atomic act; what is guaranteed is that no partial journal ever exists under a journal's name |
| C3 backup destination (Medium) | `backup_destination` runs before anything is created and again after the offline check. The destination must be absolute and normal, and new. Its parent must be a real folder with no symlink on the path, and trusted (F1's `_open_parent`). It must be neither inside nor containing the installation, by path and by identity: no folder above it may have the device and inode of the installation or of any folder inside it, which catches bind-mount aliases |
| C2 configuration parsing (Medium) | `launcher_settings` reads each file with the launcher's own parser, `local_staging.read_env` (names and values stripped, comments skipped, including indented ones), so the values checked are the values the application gets. It also refuses, rather than reinterprets, a file the launcher would read differently from how it is written: a non-blank line without `=`, an invalid name, or one name set twice. Precedence is the launcher's: `ROOT/credentials` by default, then `deployment.env`, then `settings.env`. An empty `KALILLAC_FILES_ROOT`, or none while file storage is enabled, refuses |
| C4 rehearsal diagnostics (Low) | `rehearse_upgrade.main` prints a known `Refused` as `rehearsal of BACKUP refused: <reason>`, says that no receipt was written, and exits 2. The reasons name checks and paths, never contents or credentials. Unexpected exceptions still show only their class |

**Results**
* Locally, as root in WSL: **198 passed, 0 skipped** (189 Linux + 9 Windows).
* The 32 new tests on `0a0957a`'s tools: **23 failed, 9 passed**. The 9 are controls that hold on both: the
  separated destination, the unexpected-class diagnostic, some journal faults the old code also survived.
* Codex's file on the correction: **5 passed, 1 failed by design**. `test_partial_successor_creation…` injects its
  ENOSPC into `save_json` on the successor's final path. Publication now writes a temporary and renames it, so the
  injection never fires ("DID NOT RAISE") and recovery completes. The same fault, at the corrected boundaries, is in
  the tests below.

New regressions (32; the workflow now pins 189):
* **C1, the eight injection points** (one test, invariants checked together: the backup unchanged and valid, the
  root's identity, every failed state kept, completion to exactly the manifest, and no journal replayable
  afterwards): before the successor exists; during serialization (partial temporary); before publication;
  after publication before the predecessor is marked; during that update; right after it; during file movement;
  after completion. Plus Codex's exact partial-successor scenario, the stray-successor case, and a damaged
  journal.
* **C3:** Codex's bind alias through the public `backup` command; nested, deeply nested, symlinked, existing,
  the root itself and relative destinations. For each, the installation is unchanged and no folder is created. A
  separated destination still works.
* **C2:** four whitespace forms read exactly as the launcher reads them. The test compares
  `local_staging.service_env` with `startup_environment`. Also covered: a spaced `KALILLAC_FILES_ROOT`; duplicate
  (including spaced) names, a non-setting line and invalid names refused; precedence across `deployment.env` and
  `settings.env` with comments; and Codex's exact false-receipt configuration through a complete PostgreSQL
  rehearsal, which now issues no receipt.
* **C4:** the CLI names the TMPDIR reason and shows no credential. An unexpected failure shows only its class.

**Known limitations**
* Journal publication and supersession are several operations. The guarantee is: no partial journal under a
  journal's name, and at most one chain can proceed. Power loss without the fsyncs being honoured by storage is
  outside what software can promise.
* The parser equivalence relies on `local_staging.read_env` (imported, not copied) and on the launcher's
  precedence, mirrored in `startup_environment`. A change to `service_env`'s order needs the same change there.
* Settings a launcher would accept but that are malformed (duplicates, non-setting lines) are refused: an operator
  must clean them up before an upgrade.
* All earlier limitations stand: a startup gate is not acceptance, there is no decryption of protected files,
  the maintenance window is an operational rule, and the fault injection stands in for real crashes.

**Preservation.** The owner's installation was not accessed. No live operation was run. Production,
`release/staging-v1` and the other worktrees are unchanged. PR #40 stays in draft. Codex's evidence folder is
intact.

