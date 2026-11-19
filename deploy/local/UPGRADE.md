# Local Premium upgrade: prepared procedure, awaiting authorization

This procedure targets only the existing local installation. It has NOT been executed.
Stop, backup, Git update, migration, configuration change, rebuild, restart and recovery each remain
outside this preparation assignment. Obtain separate owner authorization before the live sequence.
Never invoke reset or setup during an upgrade or recovery.

Approved application source: release/staging-v1 at d8f45f71a8d6d4ec80512e7c770b9625293e34bb.
The upgrade tools are delivered on their own development branch and must be independently reviewed.
Run them from that reviewed tools checkout, not by editing the rehearsal script to point at a live port.

## Verified installed identity (read-only inspection, 2026-11-19)

- Checkout: C:/Kalillac/kalillac-ai-local, branch feat/local-staging, commit afa247a.
- Root: /var/lib/kalillac-local-staging; marker device 2096, inode 26328. Re-read these values before authorization;
  never substitute a different identity just to get a command to run.
- Activation release: 198dbb84de5669d33588f9e253323386211a0a4b. This is not the new application commit.
- Prior schema: 0025_v1_row_lock_anchor; target: 0026_account_provider_attempts.
- Eight stored files were present. Their recorded expirations are 2026-12-18; none was eligible for zero-day
  expiration cleanup at inspection. Recheck before starting the upgraded application.

The old rehearsal's counts-only claim is not content-verification evidence. Use a new verified cold backup.

## Preconditions and stop

Use Python 3.12 with the application's pinned dependencies, root in WSL, PostgreSQL 16 tools, and the reviewed
upgrade-tools checkout. The tools have no installed-root default. Each live operation requires all of:
--root, --device, --inode, --activation-release, --checkout, --checkout-sha and --authorize-live-operation.
The flag records the operator's explicit intent; it does not replace the owner's separate authorization.
The expected root is fixed to /var/lib/kalillac-local-staging. Wrong deployment, inode, activation, dirty
checkout, real provider settings, external storage, symlinks and external tablespaces are refused.

First run live_upgrade.py stop with the original installed checkout SHA. This uses the corrected F1
shutdown implementation, with process identity bound to the old checkout's absolute path. Do not use the
old stop implementation as evidence of confirmed exit. A leaderless surviving group requires manual review;
no further step may proceed while shutdown is unconfirmed. No numeric PID guessing is allowed.

Backup additionally requires no live process under the service identity, pg_ctl status 3, and pg_controldata
state 'shut down'. This is deliberately conservative: unrelated service-user shells/jobs also block it.
Resolve those separately; do not remove the guard. Backup and apply never implicitly stop the application.

## Cold backup and rehearsal

1. Run live_upgrade.py backup with the same exact destination/checkout identity and a NEW absolute --backup
   directory outside the installation, under a trusted root-owned parent (for example a new directory in /root).
2. Record its backup_manifest_sha256 outside the backup. The backup includes the entire root: PostgreSQL's
   complete cluster/WAL, roles, account/Space/message/subscription/activation data, documents and file metadata,
   credentials, registration keys, settings, process records and web assets. Treat it as secret customer data.
3. Run the separate rehearsal command, which accepts no live database option:

   python -B deploy/local/rehearse_upgrade.py --backup BACKUP --backup-sha256 HASH --pg-bin PGBIN --receipt NEW_RECEIPT

   It verifies every file's SHA-256, ownership and permissions before copying. It starts only a scratch copy
   on a fresh loopback port with a generated configuration (no source archive/replication/preload commands).
   It migrates using the copied deployment's migration login, verifies the result, tests an immediate offline
   downgrade, then starts a second fresh backup copy to verify recovery. A receipt is issued only after both
   copies have been confirmed stopped. Any failure preserves scratch state and prints its path.
4. Record receipt_sha256 from the successful output. It binds the backup hash, application release, schema
   transition, all-table content/sequence checksums, activation and stored-file checksums/access checks.

Database verification hashes sorted complete JSON representations of every row in every non-system table,
including duplicate rows. Existing columns, sequence positions and all activation fields are compared.
Only Alembic's exact 0025->0026 revision change and the new BIGINT NOT NULL DEFAULT 0 provider_attempts
column are allowed; every new counter must be zero. Existing email/password/session data, subscriptions,
Space instructions, message content and registration/activation records cannot silently change at equal counts.
Filesystem verification includes file ids, metadata bytes, blob bytes, size and owner-account mapping; unexpired
files are also read through FileStore.get. Expired files stay unavailable even when retained on disk.

## Correct fast-forward (only after the verified backup)

The current remote feat/local-staging remains at afa247a. Do not merge that remote branch to obtain Premium.
Keep the installed checkout on its existing branch and preserve its history. In PowerShell, after authorization:

    $localRepo = 'C:/Kalillac/kalillac-ai-local'
    git -C $localRepo status --porcelain=v1 --untracked-files=all

Any output means STOP. Preserve uncommitted work separately with the owner; do not stash, reset, clean or
force-checkout automatically. With a clean checkout, create a unique backup branch pointing at HEAD, then:

    git -C $localRepo fetch origin release/staging-v1
    git -C $localRepo rev-parse FETCH_HEAD
    git -C $localRepo merge-base --is-ancestor HEAD d8f45f71a8d6d4ec80512e7c770b9625293e34bb
    git -C $localRepo merge --ff-only d8f45f71a8d6d4ec80512e7c770b9625293e34bb

Require FETCH_HEAD to equal the approved full SHA and the ancestry command to succeed before merging.
A newer remote release requires another review, not substitution. No branch switch or history rewrite is needed.

## Explicit migration and synthetic settings

Run live_upgrade.py apply from the reviewed tools checkout with destination identity, the installed checkout
now at the exact approved full d8f45f7 SHA, --backup, --backup-sha256, --receipt, --receipt-sha256, and explicit
--authorize-live-operation. It refuses an installation that changed since backup and refuses startup-cleanup
candidates before doing any migration. It verifies the connected server's data directory, database name,
PostgreSQL 16 and activation binding, migrates in one transaction, verifies content, then confirms PostgreSQL
shutdown. It never starts the application, touches Git, or restores/downgrades on a failure.

Only after verified migration/shutdown does it append these labelled SYNTHETIC LOCAL TEST INPUTS:

    KALILLAC_ACCOUNT_DAILY_PROVIDER_ATTEMPTS_FREE=100
    KALILLAC_ACCOUNT_DAILY_PROVIDER_ATTEMPTS_PAID=1000
    KALILLAC_FILES_DELETE_AFTER_EXPIRY_DAYS=3650

These are not commercial limits or owner policy decisions. The long retention period protects local test
records while evaluation continues; it is not unlimited retention. Zero days permits deletion at expiry plus
one hour. Independently of that period, orphan blobs and upload temporaries older than one hour can be removed.
A fresh dry-run must find no expiry/orphan/temporary candidates before authorizing application startup.
The tools reject configured real provider keys or real Stripe settings and preserve simulated payments.
KALILLAC_RELEASE_SHA stays 198dbb84...: changing it would break the existing V1 activation binding.

With separate authorization, build site assets, start through Kalillac-Local.cmd, refresh the web assets and
perform browser acceptance of existing accounts, subscriptions, Spaces, saved messages and documents. First
inspect simulated provider/payment status. This is not permission to send real provider or Stripe requests.

## Rollback and recovery (separate manual authorization)

Keep the application unavailable until verification and acceptance finish. Failed migration rolls back its
transaction. A process interruption may leave the cluster running: verify/stop it before any recovery copy.
After partial committed work or a startup failure, the verified PRE-UPGRADE COLD BACKUP is the recovery source.
Do not automatically downgrade or restore; preserve the failed state first in another verified cold backup.

**Restoring the pre-upgrade backup DISCARDS EVERY WRITE MADE AFTER THAT BACKUP** (accounts, messages, Spaces,
files, subscription changes): they survive only in the failed state the restore preserves. Decide it explicitly.

The exact restore, after a separate authorization, is one command. It is run in WSL as root, from the reviewed
tools checkout, with every value re-read and pasted (never defaulted):

    wsl -u root -- /home/kalillac/kf15/bin/python -B TOOLS/deploy/local/live_upgrade.py restore         --root /var/lib/kalillac-local-staging --device DEVICE --inode INODE         --activation-release 198dbb84de5669d33588f9e253323386211a0a4b         --checkout /mnt/c/Kalillac/kalillac-ai-local --checkout-sha CURRENT_CHECKOUT_SHA         --pg-bin /home/kalillac/pg16tools/lib/python3.12/site-packages/pgserver/pginstall/bin         --backup /root/BACKUP --backup-sha256 MANIFEST_SHA256_RECORDED_AT_BACKUP         --failed-state /root/kalillac-local-staging-failed-YYYYMMDD --authorize-live-operation

What it does, in order, refusing at the first failure:
1. Verifies the backup against the recorded manifest checksum and every file's checksum, owner and mode. Checks
   that it belongs to this root's path, device and inode.
2. Requires no process of the service identity, and PostgreSQL status 3 (not running).
3. Creates the new failed-state folder (0700, under a trusted parent, on the root's own filesystem). It moves
   every entry of the root into it, dotfiles and the marker included, by rename: nothing is copied or deleted.
   It records that folder's manifest beside it.
4. Copies the backup's contents into the SAME root folder (`cp -a payload/. root/`), keeping the root's inode,
   which is what the F1 ownership marker names.
5. Verifies the restored tree against the backup manifest, and verifies that the service identity can read every
   configured credential file and use its state folders.

It never starts anything, never deletes the failed state, and never retries. If it stops part-way, the root
holds a partial copy, the failed state and the backup are intact, and it says so. Review before rerunning it
(a rerun moves the partial copy into a second new failed-state folder).

Proven on disposable installations by `test_restore_after_a_failed_upgrade_into_the_same_root_and_start_the_restored_code`:
* a real upgrade, then post-backup writes;
* the restore, into the same inode;
* the restored tree equal to the manifest;
* the failed state, its settings additions and the post-backup file preserved;
* the database back at 0025 with every row as backed up;
* files and runtime files usable as the service identity;
* the restored release's own V1 startup gate (`afa247a`) accepting the restored database.

Return application code using the saved pre-upgrade Git branch in a clean checkout, retaining the new branch
history. Do not force a checkout over work. Rebuild the old site. Start PostgreSQL alone and verify exact 0025
schema/content, original activation and restored file checksums; confirm it stopped again. Only then authorize
the previous application start and browser checks.

Immediate offline 0026->0025 downgrade was tested but drops provider_attempts. It cannot preserve that column
or undo customer writes after upgrade. Full backup restoration also discards ALL writes after backup. Keep the
maintenance window closed; if any post-upgrade customer writes exist, stop for a reviewed reconciliation plan.
Our recovery tests exercise migration failures before work, within its transaction, after committed work, and
an injected application-startup failure. They verify PostgreSQL restart and original database/file/activation
contents. Browser acceptance and a real old/new application startup on the actual installation remain pending.
