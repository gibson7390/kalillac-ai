# V1-02D and V1-02E: completing account security (plan, reconstructed from the code)

Status: **in progress**. Approved by the owner on 2026-11-15 to be written from the code and then
implemented, keeping the private database authorities and adding no legacy path. Implemented so far
(default off, unexposed): D1, and the sign-out authority of D2 (migration 0018). Nothing here
authorizes a production migration, a role or credential change, or activation.

Sources: the code on `feat/file-assistant-v1` (migrations 0012 to 0014, `kalillac_accounts/v1_security.py`,
the registration authority) and the V1 contract in
`C:/Kalillac/homepage-app-handoff-logs/account-lifecycle-hardening-v1/v1-stock-postgresql-paid-launch-contract-and-implementation-map`
(documents 02 to 04), read only.

## 1. What exists

| Piece | State |
|---|---|
| V1-01 original-session authorization | Implemented, default off (`KALILLAC_V1_ACCOUNT_SECURITY_ENABLED`): `/me` and `/entitlements` authorize the exact original session under the account row lock with fresh READ COMMITTED reads |
| 0012 prepared state | `deletion_requested_at`, `password_reset_required`; owner-only tables `account_security_events`, `account_security_admission(_aggregate)`, `account_outbox`, and the activation latch in state `prepared` |
| 0013 revocation authority | `kalillac.v1_revoke_sessions(...)`: revoke others or all, proven by the original session, with operation ids, admission and an audit event; owned by E, executable by S |
| 0014 issuance authority | `kalillac.v1_issue_session(...)`: one session after a verified password, under the account lock, strictly after the epoch; revoke-all advances the epoch |
| Roles | O owner, M migration login, A registration login, R runtime login, E (NOLOGIN executor), S (NOLOGIN service). S holds no credential, so **nothing can call the authorities yet**; the legacy runtime role R still writes sessions directly |

## 2. What remains, in order

Every sub-slice is default off, adds its own tests (SQLite where the behaviour is dialect-free, real
PostgreSQL 16 in the disposable CI job for every authority, race and privilege property), keeps all
existing suites unchanged, and is committed separately.

### D1. The application's private S connection (implemented: `kalillac_accounts/v1_authority.py`)

- The operator transition (E2 below) gives S `LOGIN` and a credential; the application reads it from a
  protected credential file through the existing strict protected-file loader (the one A uses),
  never from an environment variable. Setting: `KALILLAC_V1_SERVICE_CREDENTIALS_FILE`.
- A second, small engine (pool of 2 to 4) used only by `kalillac_accounts/v1_authority.py`, which
  calls the fixed functions with bound parameters and nothing else. No generic SQL path is reachable
  from it.
- Startup in V1 mode verifies: the latch is `active` for this database instance and release, the
  exact function inventory (owner, signature, body hash, `search_path`, SECURITY DEFINER, PUBLIC
  revoked), and that S has EXECUTE on exactly that inventory and nothing else. Any difference refuses
  account startup; anonymous service is unaffected.
- Tests: wrong/missing/loose-permission credential file; S with extra privilege; tampered function
  body; latch prepared or bound to another instance; pool exhaustion fails closed.

### D2. The remaining authorities (the next migration, functions and grants only)

E-owned, S-executable SECURITY DEFINER functions with `search_path = pg_catalog, pg_temp`, static SQL,
operation ids, admission where the contract requires it, and an audit event in the same transaction:

| Function | Effect |
|---|---|
| `v1_end_session(session, token_hash)` | Logout: revoke exactly the presented session; idempotent. **Implemented, migration 0018**: answers `ended` for every well-formed call, writes no audit event (sign-out is not an audited V1 operation), locks only the session row |
| `v1_reset_password(token_hash, new_hash, op)` | Recovery completion: lock and re-check the token, replace the hash, invalidate every reset token, revoke every session, advance the epoch, clear `password_reset_required` (only here), audit. **Implemented, migration 0019** (`recovery` admission class) |
| `v1_change_password(session, token_hash, verified_hash, new_hash, op)` | Owner-requested (see section 4): proven by the original session and a just-verified current password; replace the hash, revoke every other session, audit. **Implemented, migration 0020** (`manual` admission class; new `password_change` event type). The epoch does not move (see section 4) |
| ~~`v1_verify_email(token_hash, op)`~~ | Dropped (section 4): after activation no account can be unverified; migration 0021 makes activation prove it |
| `v1_quarantine(account, reason, op)`, `v1_reinstate(account, op)` | Operator suspension and reinstatement: quarantine revokes every session and advances the epoch; reinstatement never happens as a side effect of anything else |
| `v1_request_deletion(account, op)` | Sets the deletion marker, revokes every session, advances the epoch; erasure itself follows the retention rules (section 5). **Implemented, migration 0023**, with the customer's own `v1_request_own_deletion` (section 4, "Deletion requests") |

The operator functions are executable by a separate NOLOGIN operator role P, used only by the operator
tool (D4), never by S. New event types extend 0012's allowed list in the same migration.

Tests (PostgreSQL): each effect and its audit commit or roll back together; races of reset against
login, revoke against issuance, change against logout; future-dated and expired sessions are revoked;
clock rollback cannot lower an epoch; R, A and PUBLIC cannot execute or read through any function;
hostile `search_path` and role tampering fail.

### D2, remaining: identity guard v3, quarantine, reinstatement, deletion request, role P (design)

Written before any migration, from the code at `d3081a9`.

**Operator role P.** A NOLOGIN role named by a mandatory setting (`KALILLAC_V1_SECURITY_OPERATOR_ROLE`),
provisioned by the operator like S and E: no attribute but NOLOGIN, NOINHERIT, no membership. P may
EXECUTE exactly the operator functions and nothing else; S may not execute them, and P may not execute
S's. The operator transition (E2) gives P a login only for the operator tool's own connection, with a
credential that is not the application's (D4). Every migration's role checks and the classifier's
`_security_principals` are extended from {S, E} to {S, E, P}; P absent is refused once the operator
functions exist.

**Identity guard v3.** `quarantined_at` stays a guarded identity column; v3 admits exactly two
transitions and nothing else, for every writer (the guard cannot tell callers apart: inside a
SECURITY DEFINER trigger the current user is the guard's owner):

| Transition | Admitted only when, in the same UPDATE |
|---|---|
| quarantine: `quarantined_at` NULL to a time | every other identity column unchanged, and `sessions_valid_after` moves strictly forward to at least the new `quarantined_at` (so every session ends) |
| reinstatement: `quarantined_at` a time to NULL | every other identity column unchanged, the epoch unchanged |

Who can attempt a transition is decided by column privilege: E alone gains UPDATE (`quarantined_at`);
R and A never hold it. v3 is v2 plus these two clauses, built by asserted substitution like v2 was from
v1. `registration_startup.validate_security_definitions` accepts v1, v2 or v3, and `v1_schema` pairs each
version with its revision exactly (v3 if and only if the quarantine functions exist). That change to
registration startup ships in the same release as the migration.

**Functions** (E-owned SECURITY DEFINER, fixed `search_path`, operation ids, one audit event each, new
event types widening the constraint as 0020 did):

| Function | Caller | Effect |
|---|---|---|
| `v1_quarantine(account, reason, op)` | P | lock the account; refuse if already quarantined; set `quarantined_at`, advance the epoch past it, revoke every session; event `account_quarantine` with a fixed reason code (no free text) |
| `v1_reinstate(account, op)` | P | lock the account; refuse unless quarantined; clear `quarantined_at`; event `account_reinstate`. Sessions stay ended; the customer signs in again |
| `v1_request_deletion(account, op)` | P | lock; set `deletion_requested_at` if unset; advance the epoch; revoke every session; event `account_deletion_request`. Erasure itself follows the owner's retention decision (section 5) |
| `v1_request_own_deletion(op, session, token_hash, verified_hash)` | S | the customer's own request, proven like password change; the same effects |

Quarantined and deletion-requested accounts are already refused by issuance, revocation, reset and
change, so no other function changes.

**Order of migrations.** 0022: role P checks, guard v3, `v1_quarantine` and `v1_reinstate`, E's
UPDATE (`quarantined_at`) and UPDATE (`deletion_requested_at`), the two event types. 0023: both deletion
functions and their event type. Each with preflight, postcondition, exact classifier support and the
same PostgreSQL regression shape as 0018 to 0021 (effects and audit together, uniform denials, replay,
only the intended role can call, tampering refused, downgrade refused while its events exist).

### D3. Route wiring in V1 mode

| Route | V1 behaviour |
|---|---|
| `POST /api/account/login` | Existing gate and password work, then `v1_issue_session`. **Implemented (D3, first route)**: in V1 mode the route verifies the password and writes nothing as R (no legacy session, no rehash); the session comes only from the authority as S. A refusal is `401 invalid_credentials` with no cookie; an absent or failing authority, or any unexpected answer, is `503 account_unavailable` with no cookie, never the legacy path. V1-off login is unchanged. The application passes no authority and its startup gate still refuses V1 mode, so this is reachable only where both are injected (tests) |
| `POST /api/account/logout` | `v1_end_session`; same bodyless idempotent contract |
| `POST /api/account/sessions/revoke-others`, `.../revoke-all` (new) | Exact `{}` body (4096-byte bounded, duplicate and extra keys refused); `200 {"ok":true}`; revoke-all clears the cookie; admission `429 too_many_operations` with `Retry-After`, capacity `503 security_unavailable` |
| `POST /api/account/recovery/confirm` | `v1_reset_password` |
| `POST /api/account/email-verification/confirm` | `v1_verify_email` |
| `POST /api/account/password` (new) | `{"current_password", "new_password"}`; existing password policy and work gate; `v1_change_password`; keeps the current session |

All new routes join the unsafe-request guard inventory. Off mode is byte-for-byte unchanged (tests).

### D4. Operator tool

`tools/v1_account_support.py`: suspend, reinstate, request deletion, and a bounded export of what is
held about an account, each through P with an operator credential that is not the application's,
with a dry-run mode, a required reason code and an operation id. Tested against fixtures; writing it
is not permission to run it against production.

### E1. Removing the legacy route (a later migration, prepared, not self-applied)

Revokes R's direct DML on `users`, `account_sessions`, recovery and verification tokens, entitlements
and billing, and its reads of credential digests, **only** inside the operator transition. The
migration itself prepares and verifies; startup never migrates or changes grants.

**Dependency recorded before E1 (from D3's login route).** V1 login reads the account by email and its
stored password hash (`users.password_hash`) through the runtime role R, then passes that exact hash to
`v1_issue_session`. E1 removes R's reads of credential digests, so before E1 lands a reviewed credential
read path must exist (for example an S-executable SECURITY DEFINER lookup returning only the account id
and hash for an email, with its own catalog proof and tests), and HTTP login must be tested against the
final post-E1 grants: login succeeds through the new path, and R alone can no longer read a digest. E1
itself stays its own checkpoint.

### E2. The operator transition

`tools/v1_security_transition.py`: with ingress closed, checks the catalog exactly, gives S its login
and credential, applies E1, moves the latch to `active` bound to the database instance, schema version
and release, and records it. Dry-run and fixture tested. Once active, a V1-off or older binary refuses
account startup (an old broad-grant binary is not a rollback).

### E3. The synthetic lifecycle test

One PostgreSQL test through the real routes: register, verify, log in twice, revoke others, change the
password, log out, request recovery, reset, revoke all, operator quarantine (every session refused),
reinstate, request deletion (login refused) — asserting sessions, epochs and audit events after each
step, and that R cannot write any of it.

## 3. Order and checkpoints

D1 and D2 first (no route changes), then D3 route by route, then D4, then E1 to E3. Each commit runs the
three existing workflows plus the new PostgreSQL group with measured counts and no unexpected skips.

## 4. Decisions and deviations recorded

- **Password change.** The V1 contract lists password change as an optional post-launch item (H1, "no
  optional password-change API" in V1). The owner's 2026-11-15 directive asks for it; it is built
  default off as above and needs no other V1 item to change.
- **Password change does not advance the epoch.** The earlier line in D2 said it would, but advancing
  the epoch ends every session created before it, including the presenter's, which the change must keep.
  Revoking the other sessions is exactly revocation's `others` scope, which also leaves the epoch alone;
  and a sign-in with the old password that is still in flight cannot create a session afterwards,
  because issuance re-checks the stored hash under the account lock the change holds.
- **`v1_verify_email` is dropped (owner decision 14, its conditions verified).** After activation only
  the registration role inserts accounts, each with a registration intent consumed in the same
  transaction and `email_verified_at` set; the identity guard refuses any change to `email` or
  `email_verified_at`; recovery uses its own reset tokens. The one failing condition, that activation
  did not itself count unverified accounts, is corrected by migration 0021. PostgreSQL regressions cover
  each condition (`test_registration_email_guarantees_postgres.py`). The legacy verification route is
  retired in E1.
- **Quarantine needs identity guard v3.** `quarantined_at` is one of the guarded identity columns, so
  `v1_quarantine` and `v1_reinstate` need a guard version that admits exactly their transitions (set with
  a forward epoch; cleared only by reinstatement) while R, A and every other path stay refused, and
  `registration_startup`'s trusted guard definitions must accept the new version, which changes code the
  registration path runs in production when deployed. Designed and tested before any migration.
- **S as a login.** The contract describes S as the private application login; 0013 created it NOLOGIN
  so nothing could call the authorities early. The transition (E2) is where it gains its login.
- **Operator role P.** The contract places support operations in operator tools, not the application;
  P keeps them out of S's reach.

- **Deletion requests (migration 0023, implemented, default off, unexposed).** As designed above, with
  these details fixed in the code:
  - E's UPDATE on `users.deletion_requested_at` is granted by 0023, not 0022 as "Order of migrations"
    said: 0022 granted only `quarantined_at`, and no 0022 function writes the marker.
  - Both functions take the operation id first, as 0022's do: `v1_request_deletion(op, account)` (P) and
    `v1_request_own_deletion(op, session, token_hash, verified_hash)` (S). Both set the marker and the epoch
    to one effective time strictly after the current epoch, revoke every session (the presenting one too)
    and record `account_deletion_request` at that time. No identity-guard change: the marker is not an
    identity column, and the epoch moves through v2's forward transition.
  - The operator's request answers `requested`, `already_requested` (marker already set; the first time is
    kept, nothing recorded), `not_found` or `security_unavailable`, and is not charged admission (as
    quarantine). The customer's is proven and admitted exactly as a password change is (`manual` class),
    answers `not_authenticated` uniformly for any stale or wrong proof (including an account already
    marked), `account_unavailable` when quarantined, and `too_many_operations` with the seconds to wait.
    A recorded operation id is reported again (`replayed`) and never applied twice.
  - One event type for both paths, as designed. The audit table has no actor column, so the event does
    not say whether the customer or an operator asked; the operator tool (D4) records its reason with the
    operation id.
  - The request is the whole lifecycle: nothing withdraws it (no function clears the marker), reinstatement
    of a quarantined account leaves it in place (sign-in stays refused), and nothing erases or purges an
    account or its data. Erasure is a later checkpoint, after the owner's decisions in section 5.
  - **Status of 0023 (`49540698736f95d9309d4902915a0b4e50f4ca4a`):** source reviewed, PostgreSQL CI
    verified, HTTP login verification pending. Owner acceptance pending.
  - **Why D3's login route was needed for it.** Before D3, `POST /api/account/login` created sessions as R
    directly, without the deletion check in `v1_issue_session`. Reproduced on the `4954069` router with V1
    on: after an operator deletion request, correct credentials returned 200 with a cookie and `/me`
    answered 200. With D3's route the same login is `401` with no cookie and no session row.
- **Password rehash in V1 mode.** The legacy login rehashes an outdated Argon2 hash as R. V1 login does
  not: R must not write account state, and the authority compares the exact hash just verified. A
  rehash path behind an authority is open (it matters only if the hashing parameters change).
- **D3 login at `ac9f12c669970550957d64d1508400ef0a69ee87` (Codex review).** Codex independently checked
  the routing and found the original deletion-related sign-in defect resolved in the D3 HTTP fixture, and
  verified that the PostgreSQL CI group passed 303 tests with no skips or failures. Owner acceptance
  pending. Codex also found that an issuance authority answering `None` made the route return 500 and
  left the login limiter's reservation held. **Corrected on `fix/d3-v1-login-malformed-authority`**
  (`d0247a448ca9b77dc5fea67c58140e20ddf173f6`): only the authority's own `SessionIssued` with a
  contract outcome counts; anything else is `503 account_unavailable`, the reservation is released
  exactly once, no cookie is set and no legacy session is created. Successful issuance (200, success
  recorded), denied credentials (401, failure recorded) and cancellation (released once, re-raised)
  are unchanged. **Codex independently verified the original limiter leak resolved at `d0247a4`.**
  Owner acceptance pending.
- **Outcome type at `d0247a4` (Codex review).** Codex found that `v1_login` compared the outcome to
  contract values without first requiring it to be a string, so an outcome that is not a string but
  compares equal to `"issued"` signed in (200, cookie, success recorded), and one equal to
  `"not_authenticated"` was counted as a failed guess. **Corrected on `fix/d3-v1-login-outcome-type`**
  (awaiting Codex): the outcome must be exactly `str` before any comparison; otherwise it is treated as
  an unavailable authority (`503 account_unavailable`, the reservation released exactly once, no
  cookie, no legacy session), and a valid retry signs in.

## 5. Account deletion: retention proposal (for the owner's decision)

Proposed, not decided. Figures are placeholders for the owner and, where applicable, legal review.

| Data | On a verified deletion request | Kept, and for how long (proposed) |
|---|---|---|
| Sessions, recovery and verification tokens | Revoked and deleted at once | Nothing |
| Login identity (email, password hash) | Login refused at once; erased within 30 days | Nothing after erasure |
| Conversations, Spaces, files (`/api/files`) | Inaccessible at once; erased within 30 days | Nothing after erasure |
| Usage counters linked to the account | Erased with the account | Aggregate, unlinked totals only |
| Subscription | Cancelled through Stripe by the operator; no automatic refund | Stripe keeps its own records |
| Local billing references (customer and subscription ids, webhook records) | Kept, unlinked from the login identity | As long as tax and accounting law requires (owner to set; often 6 to 10 years) |
| Financial safeguards ledger | Unchanged (it holds no identity) | Its own retention |
| Security audit events | Account id kept only as a pseudonymous reference | 12 months, then deleted |

Backups age out on their own schedule; a restore re-applies outstanding deletions before reopening
(contract document 03, section 6).

Needed from the owner before erasure (the next deletion milestone) is built: the periods above (30 days,
12 months, the billing retention period), whether a request can be withdrawn during the erasure window and
by whom, whether the customer is sent a confirmation, and how the subscription is cancelled (the Stripe
side stays with the billing work). A customer-facing deletion route is not in D3's table yet; adding one
is part of that decision.
