# Files outlive temporary conversations: customer-facing disclosure

Branch `fix/files-storage-disclosure` (from `release/staging-v1` fed4a6c). Prepared for owner and
Codex review. It is **not** merged into the release branch and does not touch the production Privacy
Policy or Terms of Use. Those stay as they are until the Premium release is approved (see
`docs/legal/` on `docs/legal-review-v1`).

## The behaviour being disclosed

A signed-in account with file access stores every file it attaches, and every file Kalillac creates or
edits, in the account's file store (`candidate/kalillac_files/store.py`). This happens even though the
chat itself is a temporary session that is never saved. Each file stays available for 30 days from
when it was stored (`expires_at`). After that it is no longer listed or downloadable.

Removing the stored bytes after expiry is a separate step. `tools/data_lifecycle.py files` (branch
`feat/data-lifecycle`) can do it, but it is not scheduled anywhere, and its deletion period is owner
decision D-9. So the copy below says files are **stored and stay available for 30 days**. It does not
say they are deleted after 30 days. That would be a retention promise nobody has made yet.

Before this branch, the chat said only that "This conversation uses temporary session memory. It is
not saved to an account or a chat history". A customer could reasonably have read that to mean their
attachments were not kept either.

## Exact changes

| Where | Before | After |
|---|---|---|
| Sign-in page side note (`site/src/account/account-pages.tsx`, `NOTE_SIGN_IN`) | Temporary chat remains available without an account. Signing in doesn’t attach the conversation you already have open. | …the same, then: **While you’re signed in, files you attach or Kalillac creates are stored in your account and stay available for 30 days.** |
| Create-account page side note (`NOTE_CREATE`) | Creating an account doesn’t attach the conversation you already have open, and temporary chat remains available. You decide what becomes part of your account. | Creating an account doesn’t attach the conversation you already have open, and temporary chat remains available. You decide what becomes part of your account; **files you attach or Kalillac creates while signed in are stored in it and stay available for 30 days.** |
| Account overview note (`NOTE_OVERVIEW`) | Your account currently holds your sign-in email, verification status, plan and billing settings. Temporary chat remains available, and you decide what becomes part of your account. | Your account holds your sign-in email, verification status, plan and billing settings **and, where they are offered, your saved Spaces and your files. Files you attach or Kalillac creates are stored in your account and stay available for 30 days, even when the conversation itself is temporary.** Temporary chat remains available. |
| Chat session description (the `title` of `#session-status`, `frontend/app.js`), added only when `GET /api/files` answers 200 | This conversation uses temporary session memory. It is not saved to an account or a chat history, and reloading starts a new session. | …the same, then: **Files you attach or Kalillac creates are stored separately in your account and stay available for 30 days.** Accounts without file access see the original sentence unchanged. |
| Chat attachment row (`#attach-note`, `frontend/index.html`), shown only while a file is attached | *(nothing)* | **Attached files are stored in your account and stay available for 30 days, even though this conversation isn't saved. Removing one here doesn't delete it.** |

The overview note previously said the account held only sign-in, verification, plan and billing data.
That was already out of date once Spaces and files shipped. The account UI's guard test listed
"spaces" and "space for" as forbidden (unimplemented) claims. Those two entries were removed because
Spaces now exist.

The "×" on an attachment only removes the file from the message being written. The stored file stays.
The attachment note says so: "Removing one here doesn't delete it."

## Tests

- `tests/test_account_ui.py`: the overview sentence is asserted exactly, plus a new test (run for both
  the sign-in and create pages) that the side note states the files are stored and stay available
  for 30 days. That adds two tests.
- `tests/test_frontend_error_contract.py`: on a files deployment with access, the session description
  ends with the files sentence, and the attachment note appears exactly while a file is attached
  (absent before attaching and after sending). Without access, the description is unchanged. These are
  new assertions inside existing tests, so no test was added.
- The browser harness (`tests/frontend_harness/harness.js`) snapshot gains `keptNote` and
  `sessionTitle`.
- `code-structure-frontend-verification.yml`: 1929 collected, 1910 passed, 19 skipped (was 1927 /
  1908 / 19). The anonymous-allowance count (645) is unchanged.

## Gap found: customers cannot see or delete their stored files

The file API supports listing and deleting files (`GET /api/files`, `DELETE /api/files/{id}`), but
**no customer-facing page does either**. The chat shows links only for files in the conversation that
is open. Once that temporary chat ends, a customer has no way to see which files their account holds,
or to delete one before it expires.

Consequences:

- The Premium Privacy draft (`docs/legal-review-v1`, sections 7 and 12, and the retention table in section 11) says "You can delete a
  file sooner; it is removed from storage at once" and "You can delete saved Spaces, conversations and
  files yourself at any time". Both are **not true for files today**. They must either wait for a files
  page or be reworded before the draft is used.
- The disclosure above deliberately makes no claim that customers can delete files.

The fix needs no owner decision on retention: a "Your files" section on the account page that lists
the account's files (name, size, when it stops being available), with download and delete, built on
the existing API and its per-account isolation. It is proposed as the next slice. Whether it is
required before Premium launch is an owner decision.

## Owner decisions this does not make

- **D-9**: when expired files are removed from storage (the copy deliberately avoids saying).
- Whether a files page is required for launch, and whether the Premium Privacy draft's deletion
  sentences wait for it.
- The final legal wording. These are product copy and notes, prepared for review; the Privacy Policy
  and Terms of Use are not replaced.
