# Known intermittent tests

Tests listed here fail intermittently for reasons that are not caused by the change under review. Their
assertions are unchanged; a failure must still be re-run and read, never ignored or skipped.

## `candidate/tests/test_frontend_trust_presentation.py`

**Symptom.** In some runs, between 3 and 6 tests fail together, all from one scenario. The scenario's result is
`error: "unknown scenario"` for a fixture scenario such as `fixture_forged_not_last_360x640` or
`fixture_fallback_list_array_360x640`, and the dependent checks fail with it:
- `test_every_scenario_completed`;
- `test_only_the_chat_endpoint_was_requested`;
- `test_source_text_meets_contrast[...]`;
- `test_metadata_never_reaches_copy_history_or_requests`.

When the whole file is re-run, all 215 pass.

**Not caused by the navigation or UX work.** Observed on Windows 11 with Microsoft Edge (headless) on 2026-11-20:

| Code | Runs | Failed runs |
|---|---|---|
| `release/staging-v1` at `d8f45f7`, untouched (temporary worktree) | 4 | 2 |
| `feat/dev-preview-navigation` | 7 | 2 |

It was also seen once while another browser suite ran at the same time.

Seen again on 2026-11-21 in one full run of the frontend suite on `feat/dev-preview-navigation` (five tests, all
from the one scenario `layout_640x360`, whose result was `error: "no fixture sources_selected"`: the same
unread fixture file, reported by a scenario that uses a fixture instead of being one). The file then passed in
full (`215 passed`) in three runs in a row.

**Where it comes from.** `frontend_harness/trust_presentation.js` registers the `fixture_*` scenarios from
`/__trust__/fixtures.json`. It reads that file with a *synchronous* `XMLHttpRequest`, inside
`try { ... } catch (e) { FIXTURES = {}; }`. If that request fails in an iframe, the failure is swallowed:
no fixture scenario is registered, and the page reports "unknown scenario". The recorded requests of a
failing page include an unrelated `/app/?scenario=layout_640x400`. That points to the harness or the test
server under load (the runner loads one iframe per scenario from a `ThreadingHTTPServer`), not to `app.js`.
This is a diagnosis from the evidence, not a proven root cause.

**Suggested follow-up (not done here).** Make the harness fail loudly, or retry once, when `fixtures.json`
cannot be read, so the cause is reported instead of becoming "unknown scenario". This change belongs to the
test harness only and must not relax any assertion.

## `candidate/tests/test_homepage_handoff.py`: browser start-up under load

**Symptom.** All 17 desktop cases of `test_a_draft_given_back_on_early_back_has_one_owner_and_is_never_sent_twice`
report *errors*, not failures. Their fixture cannot start or close headless Edge: `subprocess.TimeoutExpired` after 10–20
seconds, and `websockets ... no close frame received or sent`. No assertion is reached.

**Observed** twice on this machine, both times while other heavy work ran in parallel (a WSL PostgreSQL test run,
or other browser suites). The same 34 cases passed on their own right afterwards (`34 passed`), and the whole file
passed in full (`253 passed`) in other runs. This is the test machine's browser start-up time, not product
behaviour. Run this file on an otherwise idle machine; a timeout here is a reason to re-run, not to raise the timeout.

## `candidate/tests/test_frontend_end_erase.py`: one case, once, on a loaded machine

**Symptom.** `test_without_a_session_id_only_the_device_is_cleared` failed once in `assert_cleared`
(`emptyShown is True and conversationEmpty is True`: the empty state had not been drawn when the page was read).

**Observed** on 2026-11-21, on Windows 11 with Microsoft Edge (headless), in a run started straight after the
larger `test_frontend_error_contract.py` module in the same command. That run took about 49 seconds; the same
command normally takes 27 to 31. The file then passed in full (`26 passed`) in all seven further runs: three on its
own and four in the same two-file command. The scenario is End & erase in the temporary chat with no session, a
path the review corrections of that day do not touch (they concern Spaces sends, the reasoning picker and the
preview tools). One failure in eight runs, on a slow run, is recorded here as timing on a loaded machine; it is
not an established cause. A failure of this test must still be re-run and read.
