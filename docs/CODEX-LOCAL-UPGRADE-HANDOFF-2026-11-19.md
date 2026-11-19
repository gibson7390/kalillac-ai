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
- .github/workflows/local-upgrade-verification.yml: separate CI check, exactly 16 tests, zero skips.
  Existing workflows and pinned collections were not weakened.

The migration target is exactly 0026_account_provider_attempts, not a future head. Every existing
column/row and sequence is fingerprinted. Only Alembic's exact revision change and the new counter
are normalized; its BIGINT/NOT NULL/default and all-zero values are checked. Activation includes all
record fields, including its id. Files include metadata/blob hashes, ownership mapping, id/size
consistency and FileStore reads of unexpired records. Cold backup includes the complete PostgreSQL
cluster, roles/WAL, credentials, registration/file-access keys and other filesystem state.

## Verification

Local Linux run: 16 passed, no skips, real disposable PostgreSQL 16.
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
