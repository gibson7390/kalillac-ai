# Privacy Policy — DRAFT for Kalillac Premium

> **Draft for owner and legal review. Not in effect.** The live Privacy Policy (November 6, 2026) applies
> until this replaces it. Bracketed markers `[D-n: …]` are decisions listed in `LEGAL-REVIEW-V1.md`
> section 4; nothing in brackets is a proposed value. Facts are from the V1 release candidate
> (`release/staging-v1`) and are listed with their sources in that review.

**Last updated:** [D-20: effective date]

This page describes how information is handled when you use Kalillac AI at kalillac.ai, including the
chat application, Kalillac accounts and Kalillac Premium. Kalillac AI is operated by [D-1: legal entity
name, form and postal address] ("Kalillac", "we").

Kalillac is an AI assistant for questions, research, writing, coding, learning, files, and answers that
may use the current web. You can use it without an account. An account is optional for the free service
and required for Kalillac Premium. This page describes the service as it is designed now. It is not a
promise that the service is anonymous, that data is never stored at any layer, or that the service will
never change.

## 1. Two kinds of conversation

**Temporary conversations** are the default, with or without an account. Kalillac keeps their context
in the memory of the running application, not in a database, and your browser loses access when you
refresh or leave the page (section 4).

**Saved conversations** exist only when you are signed in and choose to work in a **Space**. A Space's
name and instructions, the titles of its conversations, and the full text of every message in them
(yours and Kalillac's) are saved to your account in Kalillac's database until you delete them or your
account is erased (section 11). The chat shows which kind you are in: "Temporary session · Not saved",
or "Saved to your account in" the Space's name.

Files are separate from both: a file you attach or that Kalillac creates while you are signed in is kept
in your account's files, even when the conversation itself is temporary (section 7).

## 2. Information Kalillac processes

**When you use the chat** (with or without an account):

- the message you type (currently limited to 4,000 characters) and recent turns of the open conversation;
- a temporary session identifier, and facts you explicitly ask Kalillac to remember during the session;
- timestamps of recent web searches in the session, used only to enforce the search limit;
- the reasoning mode you choose, where offered (Quick, Balanced or Deep);
- when you are not signed in and the anonymous daily allowance is active, a daily allowance cookie (section 6).

**When you create and use an account:**

- your email address, and your password, which is stored only as an Argon2id hash (the password itself
  is never stored);
- the times your account was created and last updated, and whether and when your email address was
  verified;
- sign-in sessions (section 5);
- your plan and, if you subscribe, billing references (section 8);
- daily usage counts (section 6);
- account-security events: the type and time of actions such as sign-in, sign-out, password change or
  reset, and a deletion request, linked to your account (no IP address and no message content);
- your saved Spaces and conversations (section 1) and your files and images (section 7).

While a registration link is pending, the email address you entered is stored encrypted, with digests
used to find and check the link, and the link expires after 24 hours. The account itself is created only
when you open the link and choose a password.

The systems that deliver the site, including the web server, the operating system, the hosting
provider, and Cloudflare, also process ordinary request data such as IP address, date and time, and user
agent.

## 3. What Kalillac does not do

- Kalillac does not sell personal information or use it for advertising. [D-2: confirm, and add any
  statement a jurisdiction requires]
- Kalillac's own frontend does not include advertising code, browser analytics code, remote fonts, or
  third-party asset dependencies. Cloudflare may separately add the security code described in section 10.
- Kalillac does not save temporary conversations in its database, and does not keep search results in a
  Kalillac database after using them to write an answer.
- Kalillac does not receive or store payment-card numbers: you enter them with Stripe (section 8).
- Kalillac does not generate images.

## 4. Temporary conversations

Kalillac's temporary session state is an entry in the memory of the running application process. It is
not written to a database. The entry holds memory facts from that session and timestamps of recent
searches. The application keeps at most 200 session entries at once and at most 50 memory entries per
session; when the store is full, the oldest entry is removed.

Refreshing or closing the page removes the browser's access to that session. That does not promise
immediate deletion of the server-memory entry, which may remain until eviction or service restart.

**End & erase** ends a temporary conversation on demand: it clears the conversation, the session
identifier and any unsent draft from the page, and asks Kalillac to remove that session's server-memory
entry and to stop any response still being prepared. Stopping is best effort. End & erase does not
retract what was already sent to OpenAI, Brave or Tavily, and does not erase account, usage, billing or
file records, saved conversations, or operational and security logs. It is not offered inside a Space,
because a Space's conversations are saved; delete them instead (section 11).

The homepage message box sends nothing until you press Send, then hands that one message to the chat
in this browser tab's sessionStorage, as described in [current "Browser storage" text, unchanged].

## 5. Sign-in sessions and cookies

When you sign in, Kalillac sets one first-party cookie, `kalillac_account`, that keeps your browser
signed in: HttpOnly (page scripts cannot read it), Secure, SameSite=Lax, Path=/, for up to 14 days
unless you sign out sooner. Kalillac stores only a hash of its value, with its creation, expiry and
revocation times. From the account page you can sign out this browser, every other session, or every
session. Changing or resetting your password signs out your other sessions or all of them.

When you are not signed in and the anonymous daily allowance is active, Kalillac uses the
`__Host-kalillac_visitor` cookie described in section 6.

Cloudflare may set an HttpOnly `cf_clearance` cookie when its security checks run (section 10).

Kalillac uses no advertising or analytics cookies.

## 6. Daily allowances

**Without an account:** [current "Daily allowance cookie" section, unchanged: one strictly necessary
cookie `__Host-kalillac_visitor`; 10 successful responses per browser per UTC day; a daily SHA-256
digest, reservation state and timestamps; current and previous UTC day retained, pruned lazily; no IP
address, fingerprinting or account identity.]

**With an account:** your allowance is counted against your account instead. For each UTC day Kalillac
records the number of successful responses and the number of characters (not the text) in your
requests and in Kalillac's replies. These records are kept for [D-11: period]. Your plan's allowance is
[D-7: Free and Premium allowances].

## 7. Files and images (Kalillac Premium)

Premium lets you attach files and images, and lets Kalillac create and edit files.

- **What is stored:** files you upload, files Kalillac creates, and edited copies (the original is
  kept), each with its name, type, size and dates, in storage on Kalillac's server that is separate for
  each account. A file can be at most 10 MiB; an account can hold up to 250 MB and 300 files.
- **Images:** an image you attach (PNG, JPEG, WebP or GIF) is opened in an isolated process and stored
  only as a new copy made from its pixels, at most 2048 pixels on a side. Metadata in the original, such
  as camera details, location or embedded text, is not kept.
- **How long:** a file is available for 30 days from when it was stored, then it is no longer shown or
  downloadable. [D-9: state when expired files are deleted from storage, once that deletion is
  scheduled.] You can delete a file sooner; it is removed from storage at once.
- **Where files go:** when you ask Kalillac to work with a file, its outline and text, or the image
  itself, is sent to OpenAI with your message (section 9).
- **Temporary conversations:** files attached to or created in a temporary conversation are still kept
  in your account's files as above.

## 8. Kalillac Premium, payments and Stripe

Kalillac Premium is a paid subscription for a signed-in account. Payments are handled by **Stripe**:
you enter your payment details on Stripe's checkout page, and you manage or cancel the subscription in
Stripe's customer portal, reached from your account page. Kalillac never receives your card number.

Kalillac stores the identifiers Stripe gives it (customer, subscription and price), the subscription's
status, its current period end and whether it is set to end, references to a checkout in progress, and
the identifiers of Stripe events it has processed. If a payment is refunded or disputed, Kalillac records
the case (Stripe's identifiers, status and reason) for review.

Stripe processes payment and billing information under its own privacy policy: [Stripe Privacy Policy
link]. Price, billing, renewal, cancellation and refunds are governed by the Terms of Use.

## 9. AI processing: OpenAI

OpenAI may process requests sent for model inference. Kalillac does not run its model on the Kalillac
server. The current production model setting is GPT-6 Luna; OpenAI is Kalillac's only model provider,
with no automatic fallback to another provider. Some requests are handled by Kalillac's own code without
OpenAI.

A request sent to OpenAI can include, as applicable:

- your current message and relevant recent turns;
- relevant temporary session-memory entries;
- in a Space, the Space's instructions and earlier messages of that saved conversation;
- the outline and text of files you ask Kalillac to read or edit, and images you attach;
- the specification of a file Kalillac is creating;
- the instructions Kalillac assembles, and retrieved web-search text when the answer uses a search.

OpenAI handles what it receives under its own terms and policies, which Kalillac does not control:
[OpenAI policies link]. [D-13: whether to state OpenAI's API data commitments, with their source.]

## 10. Other service providers

| Provider | What it does for Kalillac | What it receives |
|---|---|---|
| Brave, Tavily | Web search (Brave primary, Tavily fallback or verification) | Query text, up to 400 characters [current section unchanged] |
| Stripe | Payments, subscriptions, the customer portal | Payment and billing details you give Stripe; the account identifier Kalillac attaches to the subscription |
| Resend | Sends account emails (registration, password reset and change, verification) | Your email address and the message, which may contain a single-use link |
| Cloudflare | Reverse proxy, CDN, security and DNS for kalillac.ai | All traffic, including chat requests, while proxying it [current sections unchanged] |
| DigitalOcean | Hosts Kalillac's server, database and stored files | The data on that server |

Each handles what it receives under its own policies, which Kalillac does not control.

## 11. Retention and deletion

| Information | Where it lives | When it ends |
|---|---|---|
| Temporary conversation | Page memory and server memory | As in section 4 |
| Saved Spaces, conversations and messages | Kalillac's database | When you delete them (at once), or when your account is erased [D-10] |
| Files and images | Kalillac's server storage | Available 30 days; deleted when you delete them; [D-9: deletion of expired files] |
| Account record (email, password hash, dates) | Kalillac's database | Until the account is erased [D-8] |
| Sign-in sessions | Kalillac's database (token hash) and your browser | At most 14 days, or sign-out |
| Pending registration link (encrypted email) | Kalillac's database | Link expires after 24 hours; record kept for [D-11] |
| Password-reset links | Kalillac's database (digest) | Expire after 30 minutes; used once |
| Daily usage counts | Kalillac's database | [D-11] |
| Account-security events | Kalillac's database | [D-11; plan proposal: 12 months] |
| Billing references and refund or dispute cases | Kalillac's database | [D-8: period required for tax and accounting] |
| Anonymous allowance records | Kalillac's database | As in section 6 |
| Application logs | Kalillac's server | [D-11] |
| Backups | [D-11: where] | [D-11: schedule] |
| Provider-held data | OpenAI, Brave, Tavily, Stripe, Resend, Cloudflare, DigitalOcean | Their policies |

**Deleting your account.** From the account page you can request deletion with your password. Sign-in to
the account is closed at once and every session is signed out. A deletion request does not cancel a paid
subscription: cancel it first in Manage billing. [D-8: what is erased, within what period, what is kept
(for example billing records required by law) and for how long, whether the request can be withdrawn,
and whether you receive a confirmation.]

## 12. Your choices and rights

- You choose what to type, attach and save. Do not submit information you cannot allow OpenAI, Brave,
  Tavily, Cloudflare, DigitalOcean, Stripe or Resend to process.
- You can use Kalillac without an account, and you can use temporary conversations while signed in.
- On the account page you can see your plan and billing status, change your password, sign out sessions,
  manage or cancel your subscription through Stripe, and request deletion.
- You can delete saved Spaces, conversations and files yourself at any time.
- [D-2, D-12: rights under the laws that apply (for example access, correction, deletion, portability,
  objection), how to make a request, how identity is checked, and response times.]

## 13. Security

No online service can promise that it is completely secure. Kalillac stores passwords only as Argon2id
hashes, stores only hashes of session and link tokens, keeps each account's files separate, opens
uploaded files and images in an isolated process, and encrypts pending registration addresses. Keep your
Kalillac password private and do not reuse it elsewhere. Do not send other passwords, API keys,
credentials, payment-card numbers or other highly sensitive secrets in messages or files; ordinary use of
Kalillac does not require them.

## 14. Age

Kalillac is for adults. You must be at least 18 [D-17]. [Current "Age" section otherwise unchanged.]

## 15. Changes

Kalillac is under active development. If the handling of personal information changes in a way that
matters, this page will be updated before the change takes effect, with a new date. [D-20: how account
holders are notified.]

## 16. About this page and contact

Statements about Kalillac's own application describe its source code and design. Statements about
providers are limited to what Kalillac sends them. This page is an operator description, not an
independent privacy or security audit.

Questions, requests and corrections: [D-1, D-12: privacy contact; today contact@arogetech.com].
