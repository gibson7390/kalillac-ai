# Legal-document review for Kalillac Premium (checkpoint, 2026-11-18)

**For the owner and legal counsel. Not legal advice and not in effect.** The live Privacy Policy (last
updated November 6, 2026) and Terms of Use (October 27, 2026) are unchanged and stay in force until the
owner approves a Premium release. This checkpoint compares them with what the V1 release candidate
(`release/staging-v1`, `198dbb8`) actually does, lists every statement that becomes inaccurate when Premium
is enabled, and proposes drafts:

* `PRIVACY-POLICY-PREMIUM-DRAFT.md`
* `TERMS-OF-USE-PREMIUM-DRAFT.md`

Every fact below was read from the code (file named). Nothing here sets a retention period, a price, a
refund rule or a legal right: where the drafts need one, they carry a bracketed decision marker (`[D-n]`,
section 4) instead of a value.

## 1. What the release actually does (facts with their source)

| Area | Implemented behaviour | Source |
|---|---|---|
| Temporary chat (anonymous or signed in) | Unchanged from today: page memory plus a server-memory session entry (at most 200 sessions, 50 facts each), never written to the database; End & erase | `app_fastapi_candidate.py`, `frontend/app.js` |
| Anonymous allowance | Unchanged: `__Host-kalillac_visitor`, 10 responses per browser per UTC day, daily SHA-256 digest | `kalillac_accounts/anonymous_quota.py` |
| Registration | Email-first: the address is stored **encrypted** (AES-GCM) with lookup and token digests while a registration link is pending; the link expires after **24 hours**; the account is created only when the customer opens it and chooses a password. The pending record (encrypted address included) is **deleted automatically** once it has expired: the registration worker removes expired intents every 60 seconds, 250 at a time (`cleanup_expired`); an account keeps no link to it afterwards (`ON DELETE SET NULL`) | `kalillac_accounts/registration_authority.py`, `kalillac_db/models/account_registration.py` |
| Account record | Email, password hash (**Argon2id**; the password itself is never stored), created and updated times, email-verified time, active flag, quarantine and deletion-request markers | `kalillac_db/models/user.py`, `kalillac_accounts/passwords.py` |
| Sign-in session | Cookie `kalillac_account`: HttpOnly, Secure, SameSite=Lax, Path=/, lifetime **14 days** (configurable). The database stores only a **hash** of the session token, its creation, expiry and revocation times; **no IP address or browser data** | `kalillac_accounts/router.py`, `kalillac_db/models/account_session.py` |
| Password reset | A single-use link valid for **30 minutes**; every session is signed out on reset; the page never confirms whether an address has an account | `kalillac_accounts/recovery_tokens.py`, account pages |
| Email | Registration, password-reset, password-changed and verification emails are sent through **Resend** (the address and the message, which contains the link) | `kalillac_accounts/resend_gateway.py`, `email_messages.py` |
| Security events | For account-security actions: account id, event type and time (no IP address, no content) | `kalillac_accounts/v1_schema.py` (`account_security_events`) |
| Account deletion request | Sign-in is closed at once and every session revoked. **Erasure of the data is not built**: it waits on the owner's retention decision (V1-02D/E plan, section 5). A paid subscription is **not** cancelled by the request (the page says so) | `v1_request_own_deletion`, `docs/V1-02DE-ACCOUNT-SECURITY-PLAN.md` §5, account page |
| Spaces (saved conversations) | Space name and instructions; conversation titles; **every message's full text** (customer and Kalillac) saved in PostgreSQL under the account. Deleting a conversation or a Space removes it and its messages at once (database cascade). Spaces are currently limited to an allowlist of accounts | `kalillac_spaces/repository.py`, `kalillac_db/models/{space,conversation,message}.py`, `kalillac_spaces/config.py` |
| Files (Premium) | Uploads, generated files and edited copies, stored on the server's disk under the account (folder named by a hash of the account id), at most **10 MiB** each, **250 MB and 300 files** per account; a file is **no longer available after 30 days**. **Expired files are not yet deleted from disk** (`FileStore.purge` is never scheduled) | `kalillac_files/store.py` |
| Files in a temporary chat | A file attached to or created in a *temporary* conversation is still stored in the account's files for the period above: the conversation is temporary, the file is not | `app_fastapi_candidate.py` (file handoff), `kalillac_files/router.py` |
| Images (Premium) | PNG, JPEG, WebP, GIF accepted; decoded in an isolated worker and stored only as a **new image without metadata** (EXIF, location, ICC removed), at most 2048 px; sent to OpenAI as image data with the message | `kalillac_files/images.py`, `kalillac_files/assistant.py` |
| What OpenAI receives (additionally) | Saved-conversation history and Space instructions for a Space message; the outline and text of attached Word, Excel and PowerPoint files when the assistant reads them; attached images; the specification of a generated file | `kalillac_files/assistant.py`, `app_fastapi_candidate.py` |
| Usage records (signed in) | Per account and UTC day: number of successful responses, and the **character counts** (not the text) of requests and replies. **Never pruned** today | `kalillac_db/models/account_usage_daily.py` |
| Financial safeguards ledger | Spending reservations by provider and scope, **without account identity** | `kalillac_db/models/finance_ledger.py` |
| Billing | Stripe Checkout and the Stripe customer portal; **card details are entered with Stripe and never reach Kalillac**. Kalillac stores the Stripe customer, subscription and Price ids, status, cancel-at-period-end and period end, and Checkout attempt references; processed webhook event ids | `kalillac_billing/`, `kalillac_db/models/account_billing.py` |
| Paid access | Premium while Stripe reports the subscription active, trialing or past due (during Stripe's payment retries); cancellation at period end keeps access until the period ends; access always expires at the period end plus 7 days unless Stripe renews it | `kalillac_billing/service.py` |
| Refunds and disputes | Recorded as cases for an operator to review (charge or dispute id, Stripe status, reason); **Kalillac issues no refund automatically** and access keeps following the subscription | `kalillac_billing/service.py`, `tools/billing_cases.py` |
| Operator tools | Suspend, reinstate, request deletion, and a **bounded export of what is held about an account** (usable for an access request); a read-only owner report of counts | `tools/v1_account_support.py`, `tools/owner_report.py` |
| Not offered | Image generation (designed, off); persistent memory across temporary chats; a mobile app | blueprint I2 |

## 2. Statements that become inaccurate or misleading when Premium is enabled

Privacy Policy (live text quoted):

| # | Statement | Why it fails with Premium | Draft section |
|---|---|---|---|
| P1 | "No account or paid subscription is currently offered." | Accounts and Premium exist | Introduction |
| P2 | "Kalillac does not ask for your name, email address, phone number, or account details." | An account needs an email address and password | Information we process |
| P3 | "The current service has no file upload and does not read documents from your device." | Premium uploads files and images | Files and images |
| P4 | "Kalillac does not save chat transcripts as persistent conversations in its own application database." / "store chat history across visits" | Spaces save full conversation text | Temporary and saved conversations |
| P5 | "…does not keep a user profile tied to you" | An account holds email, plan, billing references, usage and saved work | Your account |
| P6 | "[Session state] is not written to a database." | True only of temporary chat; must be scoped | Temporary conversations |
| P7 | Browser storage: identifiers "not written to … cookies" | A signed-in browser holds the `kalillac_account` session cookie | Cookies |
| P8 | Daily allowance: "does not use … account identity" | True for anonymous use; signed-in allowances are counted per account | Allowances |
| P9 | Model inference list of what OpenAI receives | Omits saved history, Space instructions, file content, images, file specifications | AI processing |
| P10 | Providers: OpenAI, Brave, Tavily, Cloudflare, DigitalOcean only | Stripe (payments) and Resend (email) process personal data | Service providers |
| P11 | Retention table, "Chat transcripts: Not saved…"; "There is no Kalillac account and no Kalillac conversation archive to export or delete." | Saved conversations, files, account and billing records exist and can be exported or deleted | Retention; Your choices and rights |
| P12 | "The service does not ask for an email address and has no account settings." | Account pages exist | Your choices |
| P13 | "Possible later features, such as optional accounts or paid plans, are not part of the current service." | They are | Changes |
| P14 | "Ordinary use of Kalillac does not require [passwords …]" | An account has its own password (the warning about other secrets remains right) | Security |

Terms of Use:

| # | Statement | Why it fails | Draft section |
|---|---|---|---|
| T1 | "The service is currently free and does not require an account." | Premium is paid; an account is required for it | Introduction |
| T2 | The whole "No account and no paid subscription" section, including "These Terms are posted … because there is no registration step" and "Nothing here means a paid plan exists now" | Accounts and a paid plan exist | Accounts; Premium |
| T3 | "There is no user account to close. The action is a limit on access to the service." | Accounts can be suspended or closed | Suspension and closure |
| T4 | Service description and providers | Omits files, images, Spaces, reasoning modes, Stripe and Resend | The service |
| T5 | Prompts and output: "the text you submit" | Files and images are submitted; saved content is stored | Your content |
| T6 | No terms for subscription, billing, renewal, cancellation, refunds, price changes, taxes | Required for a paid service (several are owner and legal decisions) | Premium |
| T7 | No terms for saved content availability and deletion | Files expire; Spaces persist until deleted | Saved work |
| T8 | Buy Me a Coffee paragraph | Owner decision D-19: keep, reword or remove alongside a paid plan | Support |

Homepage and product copy (outside the two documents):

| # | Where | Current text | Needed with Premium |
|---|---|---|---|
| H1 | Hero description | "…without creating an account or building a permanent chat history." | True for anonymous use only; e.g. "…with no account needed, or save your work with an account." |
| H2 | Privacy promise card "No account required" | "…The current product does not offer permanent account chat history." | Spaces are permanent account chat history |
| H3 | How it works, step 2 | "There is no permanent account chat history." | Scope to temporary chat |
| H4 | How it works intro | "Your conversation stays in a temporary session and isn't saved to an account." | Unless it is in a Space |
| H5 | "Your session and providers" | "Kalillac … doesn't offer permanent chat history." | Same |
| H6 | Privacy promise heading | "Your conversation stays temporary." | Scope it ("Temporary chat stays temporary") |
| H7 | Trust row "No account required" | Still true (optional); keep or "Account optional" | Owner's wording choice |
| A1 | Account overview note | "Your account currently holds your sign-in email, verification status, plan and billing settings." | Also saved Spaces, files (30 days) and daily usage counts |
| A2 | Sign-in and create notes | "You decide what becomes part of your account." | Add that files used in any chat while signed in are kept in the account's files for 30 days |
| C1 | Chat status tooltip | "…not saved to an account or a chat history…" | Add: files attached or created are kept in the account's files |
| C2 | Chat footer | "Free · adults 18+" | Premium exists; e.g. "Adults 18+" |
| N1 | Header account entry | `fix/homepage-account-navigation` adds Sign in / Create account where accounts are offered | No legal change; ships with the release that offers accounts |

## 3. Product gaps the documents expose (before any policy can describe them truthfully)

1. **Expired files are never deleted from disk** (`purge()` is not scheduled). Until it is, a policy can
   only say files stop being available, not that they are deleted.
2. **Per-account daily usage records are never pruned**; their retention is undefined.
3. ~~Expired or consumed registration intents have no purge.~~ **Corrected 2026-11-18:** they are deleted
   automatically after their 24-hour expiry (`registration_authority.cleanup_expired`, every 60 seconds).
4. **Account erasure after a deletion request is not built** (waits on D-8).
5. Spaces are limited to an allowlist: the documents must say whether Spaces are part of Premium at launch.
6. Backups: no backup or restore schedule is recorded in this repository for staging or production
   databases and files; a retention statement must account for backups.

## 4. Decisions the owner (with counsel) must make before approval

| # | Decision | Needed for |
|---|---|---|
| D-1 | The legal entity that operates Kalillac (Aroge Tech's legal name and form), postal address, and the privacy contact | Both documents (controller and contracting party) |
| D-2 | Where Kalillac Premium is offered and which privacy laws apply (for example US states such as California, the EU or UK) | Which rights section and legal bases to include |
| D-3 | Premium price, currency, billing interval, taxes, and whether a free trial exists (blueprint decision 10) | Premium terms |
| D-4 | Refund policy (blueprint decision 10). The system never refunds automatically today | Premium terms |
| D-5 | Cancellation terms: confirm "cancel any time, access until the end of the paid period, no partial refunds" or another rule; and whether to disclose the payment-retry period | Premium terms |
| D-6 | Price-change notice period and how notice is given | Premium terms |
| D-7 | What Premium includes at launch (allowances, reasoning modes, files, images, Spaces) and Free's allowance (decisions 9, 10) | Both documents |
| D-8 | Account deletion: erasure window, what is kept and for how long (billing records for tax and accounting, security events), whether a request can be withdrawn, confirmation email (blueprint decision 6, plan §5) | Retention; deletion |
| D-9 | File availability period (30 days today) and the deletion schedule once purge exists | Retention |
| D-10 | Saved-conversation retention: until the customer deletes it or the account is erased, or a limit | Retention |
| D-11 | Retention of usage records, registration intents, security events, application logs and backups | Retention |
| D-12 | Data-subject requests: how a customer asks for access, correction or deletion, identity checks, response time | Rights section |
| D-13 | Whether OpenAI's data-handling commitments for API data (for example on training and retention) will be stated, with their source, or only linked | AI processing |
| D-14 | Suspension and closure: grounds, notice, and what happens to a paid subscription and saved work | Terms |
| D-15 | Liability limit for paid customers, dispute resolution (courts or arbitration, class-action terms) and whether Indiana law remains | Terms |
| D-16 | Automatic-renewal disclosures and consent wording required where Premium is sold | Checkout and Terms |
| D-17 | Age requirement for paid accounts (18+ today) and any verification | Both |
| D-18 | Content licence wording for stored files and saved conversations (storage and processing only) | Terms |
| D-19 | Whether the Buy Me a Coffee link remains | Terms |
| D-20 | Effective dates and how existing users are notified of the new documents | Both |

## 5. How to use the drafts

The drafts keep the live documents' voice (precise, no promises the system does not keep) and their
structure where it still fits. Bracketed `[D-n: …]` markers show exactly where a decision above is
needed; nothing in brackets is a proposal of a value. When the decisions are made: fill the markers,
have counsel review, then replace `PrivacyPage` and `TermsPage` in `site/src/App.tsx` (and the copy in
section 2's homepage table) in the release that enables Premium, with a new "Last updated" date. Until
then production keeps the current documents.
