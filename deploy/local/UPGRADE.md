# Upgrading the existing local staging install, keeping its data

**For the owner's install**, which runs from `C:\Kalillac\kalillac-ai-local`. On 2026-11-19 it was on
`afa247a`, with database migration `0025` and V1 activated (bound to release `198dbb84…`). It holds test
accounts, Spaces, conversations and files. This procedure moves it to the Premium corrections, without a
reset and without losing anything. Nothing in it deletes data.

**Do not run it until the integration is merged into `release/staging-v1` and the owner has approved the
upgrade.** Never run `reset` or `setup` for an upgrade: `reset` deletes everything, and `setup` refuses an
installed root.

What changes:
* the code (`tools/local_staging*.py`, the application, the site);
* one database migration, `0026_account_provider_attempts`. It adds a counter column, `provider_attempts`,
  that starts at 0 for every existing row, with a non-negative CHECK. No table is rewritten and no row is
  changed;
* three new settings that the application now requires: the per-account daily provider-attempt limits and
  the expired-file deletion period;
* how processes are tracked: the new `stop` recognises the processes the old `start` launched (`run/*.pid`)
  and stops them; the next `start` writes the new records (`run/*.json`).

Rehearsed on 2026-11-19 against a **copy** of this install's database: the copy was made with `pg_basebackup`,
which only reads the running server, and the original was never written. The copy was migrated with the
deployment's own migration login, `0025` to `0026`. All 29 tables and 91 rows were unchanged and the V1
activation was untouched; the original was identical before and after. Separately, on disposable PostgreSQL,
an activated database at `0025` holding data was upgraded to `0026`, downgraded to `0025` and upgraded again,
with every account and usage row kept and the application's startup check satisfied.

All commands are run in a Windows terminal. `L` stands for `wsl -u root --`.

## 1. Stop it with the code that started it

```
deploy\local\Kalillac-Local.cmd stop        (from C:\Kalillac\kalillac-ai-local)
deploy\local\Kalillac-Local.cmd status      expect: application stopped, front server stopped
```

## 2. Back it up (PostgreSQL is stopped, so a file copy is consistent)

```
L cp -a /var/lib/kalillac-local-staging /root/kalillac-local-staging-backup-20261119
L du -sh /var/lib/kalillac-local-staging /root/kalillac-local-staging-backup-20261119
```

The backup is outside `/var/lib` and has a different name and inode, so no command of the tool accepts it as
a root (its marker would not match). Keep it until the upgraded install has been checked.

## 3. Move the checkout to the release that contains the corrections

```
git -C C:\Kalillac\kalillac-ai-local fetch origin
git -C C:\Kalillac\kalillac-ai-local merge --ff-only origin/feat/local-staging   (once it contains the release)
cd C:\Kalillac\kalillac-ai-local\site && npm run build
```

## 4. Add the three new settings (synthetic, labelled; keep everything else)

Append to `/var/lib/kalillac-local-staging/settings.env` (root's, 0644). These are the boot verifier's
synthetic test inputs, the same ones a fresh `setup` writes. They are not owner decisions (A-1, A-2):

```
# Added by the Premium corrections upgrade, 2026-11-19: SYNTHETIC TEST INPUTS, not owner decisions.
KALILLAC_ACCOUNT_DAILY_PROVIDER_ATTEMPTS_FREE=100
KALILLAC_ACCOUNT_DAILY_PROVIDER_ATTEMPTS_PAID=1000
KALILLAC_FILES_DELETE_AFTER_EXPIRY_DAYS=0
```

**Do not change `KALILLAC_RELEASE_SHA`.** The V1 activation is bound to that value: the application's
startup gate refuses any other (`authority_binding_mismatch`). It identifies the activation, not the code.

## 5. Migrate the database to 0026, as the deployment's migration login

The application must not start before this: its startup checks the schema version. PostgreSQL alone is
started as the service user. The migration is then run by the code of the rehearsal
(`deploy/local/rehearse_upgrade.py`), pointed at the live port 55433 instead of a copy. It runs `provision_deployment._migrate`'s upgrade step, with the login from
`deployment/private/migration-login.json` and the role names from `deployment/deployment.env`. It prints only
the versions and each table's row count, before and after, and fails if any count changes. Expect
`0025_v1_row_lock_anchor` before and `0026_account_provider_attempts` after.

Claude runs this step with the owner's approval, after first running `rehearse_upgrade.py` again on a fresh
copy. The live-port variant of the script is prepared and reviewed at that point. The commands are not typed
by hand.

## 6. Start it and check it

```
deploy\local\Kalillac-Local.cmd start        (also stops nothing it did not start; writes run/*.json)
deploy\local\Kalillac-Local.cmd refresh-web  (serves the rebuilt site: the account page's "Your files")
deploy\local\Kalillac-Local.cmd status
```

Then, in the browser at http://localhost:8010:
* sign in with each demo account;
* the Spaces and their saved conversations are there;
* the files are listed under "Your files";
* a chat reply works; a file request works for Premium.

## Rollback

If anything is wrong, keep the backup and:

1. `Kalillac-Local.cmd stop` with the new code. It either confirms every stop or reports a failure. If it
   reports one, stop by hand and go no further.
2. Downgrade the database in place, `0026` to `0025`, which drops only the new counter (verified on disposable
   PostgreSQL with data: every account and usage row kept). Use the same procedure as step 5, with the target
   `0025_v1_row_lock_anchor`.
3. `git -C C:\Kalillac\kalillac-ai-local checkout afa247a` (the previous tip), then `npm run build` in `site`
   and `Kalillac-Local.cmd start`. The older code ignores the three added settings.

Restore from the backup only if the data itself is damaged. Restore the **contents** into the existing root
folder: do not replace the folder. The ownership marker names the folder's inode, and a copied or moved-in
folder would be refused:

```
L mkdir /root/kalillac-local-staging-failed-upgrade
L sh -c "mv /var/lib/kalillac-local-staging/* /var/lib/kalillac-local-staging/.[!.]* /root/kalillac-local-staging-failed-upgrade/"
L cp -a /root/kalillac-local-staging-backup-20261119/. /var/lib/kalillac-local-staging/
```

Nothing is deleted: the failed state is moved aside and the backup copied back. The marker in the backup is
the original's, so it still matches the original folder.
