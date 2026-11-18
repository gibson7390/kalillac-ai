# Kalillac Premium data lifecycle (2026-11-18)

For the owner, counsel and Codex. What customer data exists, how long it lasts today, what cleanup is now
built (and tested on real storage and PostgreSQL), and the plan for account erasure, which is **not**
built: it waits on the owner's retention decisions. Nothing here is scheduled or run against production.
Decision numbers `D-n` are those of `docs/legal/LEGAL-REVIEW-V1.md` section 4.

**Update after Codex's audit (F2, F6; branch `fix/premium-audit-corrections`).** Three statements below were
wrong or are now out of date:
- Expired files *were* deleted, but only at application startup: since the file feature (`36e1053`) a files
  deployment ran `FileStore.purge` once when it started, with no further period. A long-running process never
  deleted anything. Now the application runs the retention cleanup itself at startup and **every hour**
  (`retention.CLEANUP_INTERVAL`), deleting a file once it has been expired for
  `KALILLAC_FILES_DELETE_AFTER_EXPIRY_DAYS` days plus the one-hour margin. Unset, that is 0, the behaviour the
  release already had, so nothing new is decided; the staging template asks the owner for it (D-9). The
  application and the operator command share one lock (`retention.run_exclusive`). Production has no files
  feature, so none of this runs there.
- An account's storage is bounded whether or not cleanup has run: quotas now count every file on disk,
  expired ones included (250 MB / 300 files per account, unchanged).
- A customer can delete an expired file (the API refused before), and every signed-in account, whatever its
  plan, can list, download and delete its own files; a files deployment shows them on the account page
  ("Your files"). Uploading and the file assistant still need Premium.

## 1. The data map

How each piece of customer data is created, where it lives, how it ends today, and what deleting the
account row would do to it (the foreign keys as defined).

| Data | Where | Ends today | If the account row is deleted |
|---|---|---|---|
| Account: email, Argon2id password hash, dates, flags | `users` | Never (deletion request closes sign-in only) | — |
| Sign-in sessions (token hash, times) | `account_sessions` | Expire after 14 days; rows stay | Cascade |
| Password-reset links (digest) | `account_recovery_tokens` | Expire after 30 min, single use; rows stay | Cascade |
| Email-verification links (legacy path) | `account_email_verification_tokens` | Expire; rows stay | Cascade |
| Pending registration (encrypted email) | `account_registration_intents` | **Deleted automatically** after the 24-hour expiry (`registration_authority.cleanup_expired`, every 60 s) | Accounts point to it, `SET NULL` |
| Plan | `account_entitlements` | Rewritten by billing | Cascade |
| Stripe references (customer, subscription, price, status, period) | `account_billing` | Never | Cascade |
| Processed Stripe event ids | `stripe_webhook_events` | Never (no account column) | Not linked |
| Refund and dispute cases | `billing_cases` | Never | `SET NULL` (case kept, unlinked) |
| Daily usage (responses, character counts) | `account_usage_daily` | **Never, until this checkpoint** (cleanup built, section 2) | Cascade |
| Usage reservation slots | `account_usage_slots` | **Never, until this checkpoint** | Cascade |
| Spaces, conversations, messages (full text) | `spaces`, `conversations`, `messages` | When the customer deletes them (cascade) | Cascade |
| Security events (account id, type, time) | `account_security_events` | Never | **Blocks the deletion** (no `ON DELETE`) |
| Security admission records (rate limiting of account actions) | `account_security_admission` | Managed by V1 admission control | **Blocks the deletion** |
| Account operation outbox | `account_outbox` | — | **Blocks the deletion** |
| Files and images | Server disk, per-account folder | Unavailable after 30 days; deleted by the application's hourly cleanup once expired for the configured period (0 unless set, D-9); deleted at once when the customer deletes one, expired or not | Not linked: the folder stays |
| Financial safeguards ledger | `finance_*` | Its own records, **no account identity** | Not linked |
| Anonymous allowance | `anonymous_usage_*` | Current and previous UTC day, pruned lazily | Not linked |
| Temporary conversations | Server memory | Eviction, End & erase, restart | Not stored |
| Application logs | Server | [D-11] | Not linked |
| Backups | **None recorded** in this repository | — | — |
| Held by providers | Stripe (customer, payments), Resend (sent email), OpenAI (requests), Cloudflare, DigitalOcean | Their policies | Not affected |

## 2. Built in this checkpoint (`feat/data-lifecycle`)

Both are operator commands, run as the service identity: `tools/data_lifecycle.py`. Neither has a default
period, both are dry runs unless `--apply` is given **and** `--confirm-deployment` names the environment's
own deployment id, and both print counts only. **Nothing schedules them**; the owner sets the periods.

**Expired files** (`kalillac_files/retention.py`; `files --delete-after-expiry-days N`). Deletes from disk a
file past its availability period for N more days plus a one-hour margin, abandoned upload temporaries and
orphaned data. Account isolation: only owner folders and FileStore's own names are entered, each file is
decided from its own record in its own folder, and on POSIX every operation is relative to a folder opened
with `O_NOFOLLOW`, so no symlink is followed. Data is deleted before its record. An exclusive lock keeps
two runs apart. `FileStore.purge` now uses these rules; it deleted every `*.tmp` (uploads in progress)
before. Found and fixed while testing: a fresh upload looked old to a cleanup whose expiry clock was set
ahead; ages on disk are now always judged by the real clock (regression test).

**Usage records** (`kalillac_accounts/usage_retention.py`; `usage --keep-days N`, N ≥ 8). Deletes daily usage
rows and reservation slots older than N days, as the runtime login, with the database's UTC date, fixed
lock and statement timeouts, chosen by date only so it never meets the quota's work on today's rows.

Tests: `tests/test_file_retention.py` (real filesystem: threshold, dry run, explicit period, isolation
across accounts and foreign names, symlinks, a crash between data and record, temporaries and orphans,
unreadable records, idempotence, threads and separate processes with uploads and reads, the command's
refusals, counts-only output, the run lock); `tests/test_file_store.py` (purge); and
`tests/test_data_lifecycle_postgres.py` (real PostgreSQL 16 under the final grants: exact rows, nothing
else written, the owner report's window kept, refusals, concurrent runs with today's quota writes, the
command's confirmation).

## 3. Account erasure: the plan (not built)

**What happens today.** A deletion request (customer or operator, through V1's authorities) sets
`deletion_requested_at`, revokes every session and closes sign-in. No data is erased.

**Proposed shape**, for review:

1. **Window.** A request becomes erasable after the owner's window [D-8] (the V1-02D/E plan proposes 30
   days), during which it can be withdrawn by [D-8: who].
2. **Prerequisites checked first.** No live subscription: an active, trialing or past-due subscription is
   cancelled through Stripe by the operator first (the request page already tells customers to cancel),
   never refunded automatically [D-4].
3. **Erasable at once when the window ends** (customer content and identity):
   - files and images: the account's folder, through a per-account variant of the retention rules (same
     isolation and symlink protections; one owner's folder only);
   - Spaces, conversations, messages; sessions; reset and verification links; entitlements; usage rows
     (all cascade today);
   - the email address and the password hash.
4. **Possibly retained** (the owner and counsel decide what and for how long; until then it is kept):
   - billing references and refund or dispute cases, for tax and accounting [D-8];
   - security events, as a pseudonymous account id with types and times [D-11; plan proposal 12 months];
   - processed Stripe event ids (no account column);
   - provider-held records (Stripe, Resend, OpenAI): outside Kalillac's control; the operator can delete
     the Stripe customer where Stripe allows [D-8].
5. **The account row.** Because security events, admission and outbox rows reference the account with no
   delete action, the row cannot simply be deleted while they are kept. Recommended: a **tombstone** — the
   row stays with its id only (email and hash removed, marked erased), so retained records stay
   pseudonymous and referentially intact; it is deleted outright once nothing retained references it.
   Alternative: delete those V1 records first (then nothing pseudonymous survives). This is a security
   design change (the V1 authorities and the classifier) and needs Codex review.
6. **How it runs.** An operator-run erasure through P (the operator authority, like D4's tool): one
   transaction per account for the database (a new SECURITY DEFINER function, digested and audited like
   0023), then the account's files; an operation id and the append-only operator record make it
   resumable; a dry run first. A confirmation email to the customer is [D-8].
7. **Backups.** None are recorded today. If backups are added, a restore must re-apply completed erasures
   before reopening (V1-02D/E plan §5); the retention statement must say how long backups keep erased data
   [D-11].

### 3.1 The decisions that complete erasure (nothing irreversible is built before them)

| # | Decision (owner, with counsel where marked) | Options | What it unblocks |
|---|---|---|---|
| E-1 | The erasure window after a request [D-8] | e.g. none, 7, 14 or 30 days | When erasure may run |
| E-2 | Whether the customer can withdraw a request in the window, and how [D-8] | no / by signing in / by contacting support | The withdrawal path and its copy |
| E-3 | What is retained after erasure, and for how long (counsel) [D-8, D-11] | billing references and refund/dispute cases (tax period); security events (pseudonymous; the plan proposes 12 months); none | Which tables erasure empties, and the retention cleanup for what stays |
| E-4 | The account row: tombstone (recommended) or delete the V1 security records first | tombstone / delete | The V1 authority change (Codex review) |
| E-5 | Subscriptions at erasure: operator cancels first; refunds [D-4] | cancel at once, or at period end; refund policy | The prerequisite check |
| E-6 | The Stripe customer: delete it where Stripe allows, or keep for accounting (counsel) [D-8] | delete / keep | The provider step |
| E-7 | A confirmation email when erasure completes [D-8] | yes / no | One outbox message |
| E-8 | Backups: whether they exist, and how long they keep erased data [D-11] | none / schedule and retention | The restore rule and the policy statement |

The erasure itself waits on these: its database shape depends on E-3 and E-4, and when it may run on E-1.
Once E-1, E-3 and E-4 are recorded, the erasure function, the per-account file removal (one owner's folder with the
retention rules' protections) and the operator command can be built and tested on real PostgreSQL and storage
without touching production.

## 4. What can be built now, and what needs the owner

| Item | Status |
|---|---|
| Expired-file deletion | **Built, tested and scheduled by the application itself** (startup and hourly) in a files deployment; the period is D-9 (0 days unless set, as before) |
| Usage-record cleanup mechanism | **Built and tested**; needs D-11 (the period, at least 8 days) and approval to schedule |
| Registration intents | **Already purged** after 24 hours; nothing to build |
| Expired sessions and used reset links (rows) | Could be built now as a P authority function (rows are V1's); needs D-11 for how long expired rows are kept for security review |
| Security events retention | Needs D-11 and the tombstone decision (section 3.5); V1 authority change, Codex |
| Account erasure | Plan above; needs D-4, D-8, D-11 and the tombstone decision before any irreversible code |
| Customer files page (list, download, delete) | **Built** (`fix/premium-audit-corrections`): "Your files" on the account page of a files deployment, for every plan; expired files are listed for deletion only |
| Backups | Needs the owner: whether and where backups exist, and their retention [D-11] |
| Scheduling | Files: the application (above). Usage records: an operator command; a timer per environment only after D-11, never on production without the owner's approval |

## 5. Customer-facing disclosure: files from temporary conversations

The fact: a file attached to, or created in, a **temporary** conversation by a signed-in Premium customer
is stored in the account's files and stays available for 30 days, although the conversation is not saved.
The exact changes proposed for review are on `fix/files-storage-disclosure` (not merged), with their
before and after text in `docs/FILES-STORAGE-DISCLOSURE.md` there: the chat's session description,
an attachment note, and the account pages' notes. The copy says files are stored and *stay available*
for 30 days, never that they are deleted then, because the deletion period is D-9. The legal drafts on
`docs/legal-review-v1` (1ee7cf0) mark their "you can delete a file" sentences `[FILES-PAGE]` until a
customer files page exists. Production's Privacy Policy and Terms are unchanged.
