# Corrections for the Codex audits: evidence for independent verification

**Checkpoints (provenance).** Six different commits have been handed to Codex; they are not the same:

| Checkpoint | Full SHA | What it is |
|---|---|---|
| Tag `codex-reverify-2026-11-16` (historical, published, unchanged) | `d3081a96f4fc20d711dbc8d094cab8c576b1bdc1` | The first reverification checkpoint, on `feat/account-security-d2-change`. It predates the corrections for findings A to D. An earlier version of this header said the tag marks "the commit that contains this file in this form"; that was wrong after this file changed, and the tag was never moved. |
| Second reverification handoff | `53869d73a509df07643e91dde7dff18ba6bbf984` (`feat/account-security-d2-quarantine`) | The checkpoint Codex reviewed on 2026-11-16: A to D corrected, migration 0022's 19 PostgreSQL tests passed independently; the confirmation lifecycle and the prose-total check had defects (below). |
| Third reverification handoff | `c5291b4a732ec87a72fdbe3c39caebb339ce0452` (`fix/codex-reverification-3`) | Codex verified the confirmation lifecycle and the provenance corrections there, and found regression R1 in the amount matcher (below). |
| Fourth reverification handoff | `856d9a53c25f6b3424326cb8d68532112d8b39e7` (`fix/codex-reverification-4`, unchanged) | The R1 correction. Codex verified its original examples and its 30-fail/33-pass comparison, and found that repeated horizontal spaces still escaped the prose matcher (below). |
| Fifth reverification handoff | `0ea08a8e40bd1c22be4e5166c275640ec7a6d495` (`fix/codex-reverification-5`, unchanged) | The repeated-space correction. Codex verified the 15 repeated-space cases fixed, and found two text-extraction gaps: Word tabs and soft line breaks (below). |
| Sixth reverification handoff (audited; the correction cycle is closed) | `78b7a055994b69aa0462ed47bd9db23de82f9bea` (`fix/codex-reverification-6`, unchanged) | The correction of the two extraction gaps, stacked on `0ea08a8`. Codex independently verified both findings resolved and found no new actionable issues. This record is updated on a separate branch so the audited commit stays as Codex reviewed it. |

Nothing in any checkpoint was changed to make CI pass without investigation.

Branch `fix/codex-audit-corrections`, stacked on `feat/account-security-d2` (which is on
`feat/file-controls-v1`). The audited snapshot `6191b53` (`feat/document-quality-v1`) is unchanged.

**Nothing here is independently resolved unless a section records Codex's verification against a
named commit.** Until then each item is "corrected, awaiting Codex verification". Green CI is listed
as supporting evidence only, and no item is owner accepted.

How each regression was shown to catch the original defect: the new tests were run against the
pre-correction source of the changed module (restored from the parent commit, then put back), and
they failed; they pass on the correction. The counts are recorded per item.

## Status after Codex's reverification (handoff of 2026-11-16)

Codex independently reverified the corrections against `6337c12c81780bda28166854fd5a6c956c0c030c`
(and also inspected `9c9a80d3033b74a5dee57efb412168080d7980eb`).

| Item | Implemented | Our CI | **Independently verified by Codex** | Owner accepted |
|---|---|---|---|---|
| CODEX-D1 financial migration safety | `f582696` | yes | **Resolved** (original failure sequence prevented) | n/a |
| CODEX-D2 file-worker cleanup | `ad983ed` | yes | **Resolved** | n/a |
| CODEX-D3 reconciliation consistency | `da2321c` | yes | **Resolved** | n/a |
| CODEX-D4 formula validation | `ea5e081` | yes | **Resolved** | n/a |
| 1904 date system | `3126abe` | yes | **Resolved** | n/a |
| Stale chart caches | `adf9da8` | yes | **Resolved** | n/a |
| Financial report reconciliation (Q1) | `e92583d` | yes | **Resolved** | no |
| About-sheet numbers | `c0851b6` | yes | **Resolved** | no |
| PowerPoint edit fitting | `7137da2` | yes | Remaining defect B (cumulative table height) | no |
| Total rows after edits (Q4) | `888293e` | yes | Remaining defects A (tracked edits) and section 3 (exact authorization) | no |
| Rounding policy | `e92583d` | yes | Remaining defect D | no |
| Benchmark restatement | `e92583d` | yes | Remaining defect C (outlook chart) | no |
| Word pagination and contents (finding 4, Q3) | not adopted (decision 12) | n/a | **Unresolved** | no |
| Arabic PDF extraction (finding 5) | `dee4831` partial | yes | **Partially resolved** | no |
| General semantic consistency | partial | yes | **Incomplete** | no |

### Corrections for the reverification findings (branch `fix/codex-reverification-2`)

Branch HEAD: `ca29074114136c738ba6595a371c2c09267263c2`. Source locations are at that commit. "Original
failure" means the new regression tests were run against the pre-correction module (restored from the
parent commit, then put back) and failed, or, where stated, the failure was reproduced directly.

| Finding | Correction (full SHA) | Source location | Original failure | Corrected behaviour and regression coverage |
|---|---|---|---|---|
| A. Tracked Word edits bypassed the total check | `e7abd44c1e0b66890529df877a8eb043568aeae3` | `kalillac_files/editing.py`: `_accepted_text` (1713), `_word_tables_accepted` (1720), `_check_totals_kept` (1773) | 4 of the 7 new tests fail on the previous editor (a tracked replacement and tracked row changes passed) | Tables are read with every revision accepted (inserted text and rows count, deleted do not, merged cells aligned); the file keeps its real revisions. `test_file_editing.py`: tracked `set_text`/`replace_text`, tracked restatement accepting to the new and rejecting to the original figures, tracked row insertion and deletion, a previously revised upload, formatting kept |
| B. Rows that each fit pushed the table off the slide | `7a5466c6b739102d6e6e36104540fbe28d53ca4e` | `editing.py`: `_check_table_fit` (890) | `test_rows_that_each_fit_cannot_together_push_the_table_off_the_slide` fails on the previous editor | The whole final table is laid out (every row at its tallest cell, cells spanning rows, bottom compared with the slide, pre-existing overflow never charged to the edit). Also: merged cell, Arabic table, other cells unchanged (these pass on both versions; they guard the new code) |
| C. Restatement left the outlook chart's 2026 value undisclosed | `b742ff6f0624c5377a1310c841d810e91643ca54` | `tests/quality_benchmark.py`: `RESTATEMENT_CHARTS`, `RESTATEMENT_NOTE` (410); the report fixture's line chart is titled | The dependency test finds an untitled, unnamed dependent chart in the previous fixture | Every chart whose data shows 2026 revenue is found from the report's data and named in the disclosure (`test_every_chart_that_shows_the_restated_revenue_is_named_in_the_restatement`). Charts are images in the Word file; a text edit cannot redraw them, so they are disclosed, not regenerated |
| D. Displayed parts did not reconcile (`[33333.4, 33333.4, 33333.2]`) | `61618c259ee449071cb75ed1ad1473af6ede3531` | `kalillac_files/charts.py`: `breakdown` (111) | Reproduced directly: the previous labels were 33.333K three times under 100.000K | Within two extra decimals the shown parts add up exactly, otherwise the chart prints "Rounded; parts may not add to the total."; shares total 100%; source values unchanged. `test_document_design.py`: three non-reconcilable cases, note shown only when needed |
| Section 3. The model's `exact=True` alone skipped the check | `c274f905d841f8251a3aba29f6e2a1d14e9a422d` | `kalillac_files/assistant.py`: `edit_digest` (208), its use in `edit_file` (317); `kalillac_files/router.py`: `FileService._offer` (123), `_presented` (137), `assist` (155); `/api/chat` handoff | The previous service applied the model's exact edit with no customer involvement | `exact` is honoured only with a single-use, 30-minute confirmation the service issued, bound to account, file and the exact operations; never shown to the model. `test_files_router.py` at `/api/files/assist`: model flag alone ignored, confirmation applies once, bound to operations and account, expires, malformed refused |
| Prose total left stated elsewhere (found while correcting A) | `87a1f363a5ca95895c2b0217a654a8d9c68b3b8a` | `editing.py`: `_document_text` (1791), `_check_restated_totals` (1807) | The proposal's summary kept "$186,500" after a table-only restatement | Refused with `dependent_figure_unchanged` unless the old total is restated everywhere or the customer confirmed the exact edit (`test_a_restated_total_must_be_restated_wherever_the_document_states_it`) |

| Finding | Implemented | CI passed (run, exact tested SHA) | Independently verified | Owner accepted |
|---|---|---|---|---|
| A, B, C, D, section 3 | yes | `file-generation-verification` `43801588596`, `subscription-foundation-verification` `43801592242`, `safety-helpfulness-regression` `43801595873`, all on `c274f905d841f8251a3aba29f6e2a1d14e9a422d` | no (awaiting Codex) | no |
| Prose total | yes | `file-generation-verification` `43802564474` on `ca29074114136c738ba6595a371c2c09267263c2` (836 tests, none skipped) | no (awaiting Codex) | no |

**Exact-edit confirmation is backend-only.** The service issues and honours confirmations, and both
`/api/files/assist` and `/api/chat` return and accept them, but the chat interface has no control that
shows the offer and sends the confirmation. Until it exists, an edit that would break a total is refused
for the customer, never applied silently. **The customer flow is incomplete.**

**Still open (status unchanged by new evidence):** Word pagination and contents finalization (decision 12;
prototype evaluated in `docs/DECISION-12-WORD-FINALIZATION.md`, not adopted); Arabic PDF logical text
(ligature order, diacritics, mixed-direction reading order, search and word splitting); native and
cross-viewer presentation checks (only Microsoft Word and PowerPoint were available here); broader
semantic dependencies (derived figures such as growth rates and margins stated in prose and metrics are
not checked; totals and table totals are).

### Codex's review of `53869d73a509df07643e91dde7dff18ba6bbf984` and the corrections (branch `fix/codex-reverification-3`)

**Independently verified by Codex at `53869d7`:** the original failures of findings A, B, C and D are
corrected, and migration 0022's 19 PostgreSQL tests passed in Codex's own run. These are not changed here.

**New findings at `53869d7`, now corrected (implemented; awaiting Codex):**

| Finding | Source location (at the current checkpoint) | Original failure (new tests run against the `53869d7` modules, then restored) | Corrected behaviour and regression coverage |
|---|---|---|---|
| 1. A confirmation was copied, not claimed, and consumed only after the assistant finished: two concurrent requests could both apply it, and an edit stored before a provider failure (HTTP 502) left it usable for a second edit | `kalillac_files/router.py`: `FileService._claimer` (137), used by `assist` (180); `kalillac_files/assistant.py`: `FileRequest.claim` (186), the claim in `edit_file` (321). The post-run consumption block is removed | Fail on `53869d7`: concurrent replay (both requests applied the edit), provider failure after persistence (the same confirmation applied a second edit), cancellation (worker killed at its time limit; the confirmation stayed usable). Pass on both: storage failure (`53869d7` already consumed it after the worker returned), tracked-mode binding | The service atomically claims (under its lock, deleting it) one presented confirmation bound to the same account, file, operations, tracked mode and an unexpired lifetime, **before** the edit runs; it is never given back. `test_files_router.py`: `test_two_concurrent_requests_cannot_both_use_one_confirmation` (319), `test_a_provider_failure_after_the_edit_is_stored_does_not_free_the_confirmation` (336), `test_a_failed_attempt_consumes_the_confirmation_and_a_retry_needs_a_new_one[cancelled, storage]` (349), `test_a_confirmation_stays_bound_to_tracked_mode` (368); the existing tests for operations, account, expiry and malformed values (246 to 290) are kept |
| 2. The prose check counted substrings: "$300" was found inside "$3000", so restating a $300 total refused with `dependent_figure_unchanged` | `kalillac_files/editing.py`: `_FIGURE` (1810), `_figure_key` (1814), `_figures` (1825), `_check_restated_totals` (1830) | Codex's case and 4 other boundary cases (`$300.5`, `$3005`, `$300K`, `A$300`) refused on `53869d7`; `$ 300` (a true stale total with a space) was accepted there; a correctly restated second table was refused because "$3000" remained | Figures are matched as complete tokens (never inside a longer number, decimal, thousands group, scaled figure or other currency) and compared by value, currency and scale, so `$300.00` and `$ 300` are the old total and `300 seats` is not. Nothing is changed automatically; the customer's confirmed exact edit still bypasses it. `test_file_editing.py`: `test_an_unrelated_figure_that_contains_the_old_total_does_not_block_the_restatement` (10 cases, 1053), `test_the_old_total_left_in_prose_is_still_refused` (6 cases, tracked and untracked, 1067), `test_the_old_total_left_in_another_table_is_still_refused` (1074) |
| 3. The evidence header said the tag marks this file's current form | This file's header | The tag resolves to `d3081a96f4fc20d711dbc8d094cab8c576b1bdc1` | The header separates the historical tag, the reviewed checkpoint and the current checkpoint; the published tag is unchanged |

**Retry and idempotency (defined).** One confirmation authorizes exactly one exact-edit attempt. It is
consumed when the attempt starts, whatever happens next: success, a worker failure or time-limit kill,
a storage refusal, a provider failure after the edited file was stored, or a concurrent request
presenting the same confirmation (which then runs as an unconfirmed edit and is refused by the total
checks, with a fresh offer). A retry therefore never replays the edit silently: it is checked again
from the start, and if the edit still breaks a total the customer gets a new confirmation to accept.
If a file was stored before a later failure, it stays in the customer's files (it counts against
storage and is visible in the file list); the service does not deduplicate or roll it back. Exact
requests are not idempotent by request key; there is no request key.

CI on `c5291b4a732ec87a72fdbe3c39caebb339ce0452`: `file-generation-verification` `43809914449` (858
tests), `subscription-foundation-verification` `43809917574`, `safety-helpfulness-regression`
`43809920980`, all success (Codex inspected the job logs). They did not cover R1.

**Still open after these corrections, unchanged:**

- **The exact-confirmation interface.** The backend only; the chat interface has no control to accept an offer.
- **Word finalization.** Pagination and contents (decision 12).
- **Arabic PDF interoperability.** Ligature order, diacritics, mixed-direction reading order, search and word splitting.
- **Broader semantic dependencies.** Derived figures such as growth rates and margins stated in prose are not checked; totals and table totals are.
- **Native and cross-viewer presentation checks.**

### Codex's review of `c5291b4a732ec87a72fdbe3c39caebb339ce0452` and the R1 correction (branch `fix/codex-reverification-4`)

**Independently verified by Codex at `c5291b4`:**
- F1, the confirmation lifecycle: one attempt per confirmation, consumed atomically before the edit.
- F3, provenance.
- The original `$300`/`$3000` false refusal.

None of these is changed here: `router.py` and `assistant.py` are untouched, and the earlier boundary regressions are kept and pass.

**R1, now corrected (implemented; awaiting Codex).** The token matcher introduced in `c5291b4` missed stale totals written as:
- signed amounts (`-$300`);
- accounting negatives (`($300)`);
- ISO-currency amounts (`USD 300`).

Ordinary Word (plain and tracked) and PowerPoint edits were therefore accepted with the old total left in the summary beside a correctly restated table.

| | |
|---|---|
| Source location (at the current checkpoint) | `kalillac_files/editing.py`: `_FIGURE`, `_figure_keys`, `_figure_key`, `_figures`, `_check_restated_totals` (from line 1807) |
| Correction | Prose amounts are recognized in every form the table total check reads (`consistency._QUANTITY`): an opening parenthesis, a sign (`-`, `−`, `–`), a currency sign or a three-letter ISO code followed by an ordinary or no-break space, thousands separators, decimals, a K/M/B or bn scale, and a closing parenthesis. A table cell is keyed exactly as the total check reads it (`_quantity`), without removing spaces. Whole-token boundaries, currency and scale distinctions are kept. Where prose is ambiguous, the amount counts as both signs, so a stale total is never missed. This covers parentheses (accounting notation or punctuation: `The total fee ($300) is fixed.` still refuses a `$300` total), a spaced hyphen, and an en dash. An adjacent hyphen or `−` is negative. `$100-$300` is a range. Rewriting a total as the same amount in another form is not a change. Nothing is changed automatically; the customer's confirmed exact edit still bypasses the check |
| Regressions (`tests/test_file_editing.py`, each in DOCX plain, DOCX tracked and PPTX) | `test_a_stale_signed_accounting_or_iso_total_is_refused_and_a_full_restatement_accepted`: 5 styles (`-$`, `−$`, `($)`, `USD `, `USD` + no-break space) × 3 formats. Each refuses the table-only edit, accepts the restatement made everywhere (the summary then states only the new total), and accepts the confirmed exact edit. `test_an_amount_that_may_be_the_old_total_is_refused`: 8 cases × 3 formats (a negative written another way, `−`, parentheses, en dash, positive parenthetical, dash, range, `USD 300.00`). `test_an_unrelated_signed_or_iso_amount_does_not_block_the_restatement`: 8 cases × 3 formats (opposite sign, larger amounts, another currency, another scale, a count, a decimal); the unrelated text is unchanged |
| Original failure | Run against the `c5291b4` editor (restored from that commit, then put back): **30 of the 63 new tests fail**. These are all 15 stale-total reproductions (5 styles × DOCX plain, DOCX tracked, PPTX) and the 15 equivalent-representation cases (negative written as `-`, `−`, `()` and `–`, and `USD 300.00`). The 33 others pass on both versions: the positive parenthetical, dash and range cases (which `c5291b4` already matched as `$300`) and the 24 unrelated-amount cases. They guard the new matcher against false refusals |
| Corrected | All 63 pass. All 12 file suites locally: 920 passed, 1 skipped (`test_memory_limit_stops_the_child`, POSIX-only; Linux CI runs it), 1 expected openpyxl warning |

CI on `856d9a53c25f6b3424326cb8d68532112d8b39e7`: `file-generation-verification` `43818179290` (921
tests), `subscription-foundation-verification` `43818183401`, `safety-helpfulness-regression`
`43818187472`, all success (Codex inspected the job logs). They did not cover repeated spaces.

**Still open after this correction, unchanged:**
- **The exact-confirmation interface:** the backend only.
- **Word finalization.**
- **Arabic PDF interoperability.**
- **Broader semantic dependencies:** growth rates and margins in prose.
- **Native and cross-viewer presentation checks.**
- **The one-attempt contract:** an output stored before a provider failure stays in the account, without rollback or deduplication.

### Codex's review of `856d9a53c25f6b3424326cb8d68532112d8b39e7` and the repeated-space correction (branch `fix/codex-reverification-5`)

**Independently verified by Codex at `856d9a5`:**
- The original `-$300`, `($300)` and `USD 300` cases: refused in DOCX plain and tracked, and in PPTX.
- The 63 R1 regressions: 30 fail and 33 pass on the parent, and all pass on the correction.
- The `$3000` false-refusal correction and the `$300`, `$300.00` and `$ 300` controls.
- The confirmation lifecycle: identical source blobs.
- Provenance.

None of these is changed here: `router.py` and `assistant.py` are untouched, and all 63 R1 cases and the earlier boundary cases are kept and pass.

**Finding (residual R1), now corrected (implemented; awaiting Codex).** The prose matcher allowed one optional space between the parts of an amount, while the table check (`consistency._QUANTITY`) allows any run of whitespace. A total written with repeated spaces lost its currency, sign or scale in prose. Codex's cases, in DOCX plain, DOCX tracked and PPTX:
- `USD  300`
- `USD` followed by two U+00A0, then `300`
- `-  $300`
- `$  300`
- `$300  K`

In each, an ordinary edit was accepted with the stale summary left beside the restated table: 15 inconsistent files.

| | |
|---|---|
| Source location (at the current checkpoint) | `kalillac_files/editing.py`: the comment from line 1807, `_GAP` (1816), `_FIGURE` (1817) |
| Correction | Between the parts of an amount (after an opening parenthesis, after a sign, after a currency sign or ISO code, before a scale, before a closing parenthesis), the matcher accepts any run of horizontal whitespace the table check accepts there, in any number and mix: ordinary, no-break, narrow and thin spaces, tabs. That is, every character Python's `\s` matches except line and paragraph separators (`\n`, `\r`, `\v`, `\f`, U+001C to U+001F, U+0085, U+2028, U+2029). Document text joins paragraphs, cells and text bodies with line breaks, so an amount still never spans two lines or two containers. An ISO code still needs at least one space. A spaced sign is still ambiguous (both signs). Ranges, ambiguous parentheses and dashes, whole-token boundaries, and currency, sign and scale distinctions are unchanged. A token can no longer begin with whitespace (the gap after a currency is now inside the optional currency group). Spacing is never stripped or rewritten, nothing is changed automatically, and the confirmation checks are untouched |
| Regressions (`tests/test_file_editing.py`, each in DOCX plain, DOCX tracked and PPTX) | `test_a_stale_total_with_repeated_spaces_is_refused_and_a_full_restatement_accepted` (1197): Codex's five styles and three mixed runs (`USD` + space, no-break, narrow no-break and space; `(` + space, no-break, `$300`, no-break, space, `)`; `−` + no-break, space, `$`, space, no-break). Each refuses the stale summary, accepts the full restatement and accepts the confirmed exact edit: 24 tests. `test_the_old_total_spaced_differently_in_the_summary_is_refused` (1222): mixed representations between table and summary, 24 tests. `test_a_restatement_in_another_spacing_is_accepted` (1236): 9 tests. `test_respacing_the_total_alone_is_not_a_change_of_total` (1252): formatting-only equivalence, 15 tests. `test_an_unrelated_spaced_amount_does_not_block_the_restatement` (1275): another currency, a larger amount, another scale, no scale, the opposite sign, a count; the text is unchanged; 21 tests. `test_an_amount_is_never_joined_across_paragraphs_points_or_cells` (1285): 3 tests. `test_horizontal_space_runs_never_cross_a_line` (1306): unit test, 1 |
| Original failure | Run against the `856d9a5` editor (restored from that commit, then put back): **34 of the 97 new tests fail**. All 24 repeated-space reproductions fail (Codex's 15 among them). 9 cases fail where the summary itself uses a repeated or mixed run (`-` + no-break, space, no-break, `$300`; `USD` + space, no-break, space, `300`; `(  $300  )`). The unit test fails. 63 pass on both: summaries with a single space beside a repeated-space table (the parent already read the table cell correctly), restatements, formatting-only changes, unrelated amounts, and the container tests. They guard the change against false refusals and joined containers |
| Corrected | All 97 pass. All 12 file suites locally: 1017 passed, 1 skipped (`test_memory_limit_stops_the_child`, POSIX-only; Linux CI runs it), 1 expected openpyxl warning. Ruff passes |

CI on `0ea08a8e40bd1c22be4e5166c275640ec7a6d495`: `file-generation-verification` `43824203820` (1018
tests), `subscription-foundation-verification` `43824208279`, `safety-helpfulness-regression`
`43824213718` (820 tests), all success.

**Still open after this correction, unchanged:**
- **The exact-confirmation interface:** the backend only; there is no chat acceptance control.
- **Word finalization:** page numbers and contents.
- **Arabic PDF interoperability:** ligatures, diacritics, mixed-direction reading order and search.
- **Broader semantic dependencies:** growth rates and margins in prose.
- **Native and cross-viewer presentation checks.**
- **The one-attempt confirmation contract:** a stored output survives a later provider failure, without rollback or request-key deduplication.
- **Line breaks inside a paragraph (as of `0ea08a8`):** a Word or PowerPoint line break inside a paragraph contributed no character to the document text, so an amount split by one read as `USD300`. Superseded by the extraction correction on `fix/codex-reverification-6` (below): the break now reads as a line break, and a split amount is the known limitation recorded there.

### Codex's review of `0ea08a8e40bd1c22be4e5166c275640ec7a6d495` and the text-extraction correction (branch `fix/codex-reverification-6`)

**Independently verified by Codex at `0ea08a8`:** the original 15 repeated-space cases are fixed. That correction and its 97 tests are kept unchanged; the amount matcher (`_GAP`, `_FIGURE`) is not modified here. `router.py` and `assistant.py` are untouched, so the exact-edit confirmation flow is unchanged.

**Findings at `0ea08a8`, corrected at `78b7a05`, and independently verified resolved by Codex (see the status table below).** Both are in how the editor extracts text for the prose-total check, not in the matcher.

**1. Word tabs were dropped.**
- Problem: the accepted-revisions view read only `w:t`. A `w:tab` between `USD` and `300` disappeared, so the checker saw `USD300`, and a table-only restatement to `USD 400` was accepted with the summary still showing `USD<TAB>300`.
- Source location: `kalillac_files/editing.py`: `_RUN_SPACING` (1718), `_accepted_text` (1721).
- Correction: a run's `w:tab` and `w:ptab` read as a tab, and `w:br` and `w:cr` as a line break, as Word shows them (the same mapping as the editor's run reader `_w_text`). Tab stops defined in paragraph properties (`w:pPr/w:tabs/w:tab`) are not characters, and deleted runs (`w:del`, `w:moveFrom`) are still dropped.

**2. Soft line breaks were dropped (Word and PowerPoint).**
- Problem: the PowerPoint text reader (`_body_text`) ignored `a:br`, and the Word view ignored `w:br`, so the end of one line was joined to the start of the next. Codex's false refusal: a line ending `$` followed by `300 licenses were issued.` read as `$300`.
- Source location: `editing.py`: `_body_text` (765) and `_accepted_text`.
- Correction: a line break inside a paragraph reads as a line break, which the amount matcher never crosses. PowerPoint tabs were already characters inside `a:t`; their behaviour is unchanged.

**Effect on table reading (stated so it is not missed):** `_accepted_text` also reads Word table cells for the total check (`_word_tables_accepted`). A Word cell containing a tab or a line break now reads with that whitespace instead of with its parts joined. That matches PowerPoint cells, which python-pptx already reads with a line break as `\v`, and what Word shows. No existing test changed. Generated documents contain no such cells.

**Regressions.** All in `tests/test_file_editing.py`, on real DOCX and PPTX files: generated, then the summary's marker is replaced in the file's XML by a real `w:tab`, `w:br`, `a:br` or tracked-revision run. Results are read back with python-docx and python-pptx, not the editor's extraction.

| Test (line) | Cases | What it asserts | On `0ea08a8` |
|---|---|---|---|
| `test_a_word_tab_inside_a_stale_total_keeps_the_total_recognized` (1357) | Word plain, Word tracked | A table-only `USD 300` → `USD 400` restatement is refused with `dependent_figure_unchanged` while the summary reads `USD<TAB>300`. Updating the summary too is accepted (plain: read back as `USD\t400`; tracked: accepted view, tab kept). The confirmed exact edit is accepted | **fail** (`DID NOT RAISE`: the inconsistent edit was accepted) |
| `test_a_powerpoint_tab_inside_a_stale_total_keeps_the_total_recognized` (1377) | PowerPoint | The same, for a tab inside `a:t` | pass (existing behaviour preserved) |
| `test_a_line_break_inside_a_paragraph_separates_the_lines` (1390) | Word plain, Word tracked, PowerPoint | Line `…quoted in $`, break, `300 licenses were issued.`: restating the `$300` total is accepted, and the unrelated text is intact in the returned file (`\n` in Word, `\v` in PowerPoint) | **fail** (false `dependent_figure_unchanged`) |
| `test_a_tracked_line_break_counts_as_word_shows_it_once_accepted` (1407) | Inserted break and deleted break (earlier tracked revisions) × plain and tracked edit | An inserted break separates the lines, so the restatement is accepted. A deleted break does not, so the accepted text reads `$300` and the restatement is refused | inserted: **fail** (false refusal); deleted: pass (refused on both) |
| `test_word_tabs_and_breaks_read_as_word_shows_them_and_tab_stops_do_not` (1420) | Unit | `USD`, tab, `300`, `w:br`, `w:cr`, a deleted tab and a `w:pPr` tab stop read as `USD\t300\nend\nx` | **fail** |
| `test_a_powerpoint_line_break_reads_as_a_line_break` (1433) | Unit | `a:br` reads as `\n`, and a tab in `a:t` is kept | **fail** |

**Original failure and correction.**
- Run against the `0ea08a8` editor (restored from that commit, then put back): **9 of the 12 new tests fail**, for the reasons shown, and **3 pass**: the PowerPoint tab case and the two deleted-break cases.
- On the correction, all 12 pass.
- All 12 file suites locally: 1029 passed, 1 skipped (`test_memory_limit_stops_the_child`, POSIX-only; Linux CI runs it), 1 expected openpyxl warning. Ruff passes.
- The earlier paragraph, cell and container boundary tests, the 97 repeated-space tests and the 63 R1 tests are unchanged and pass.

**Known limitation kept for this pass (deliberate).** An amount deliberately split across a line break, such as `USD`, a break, then `300`, now reads as `USD\n300` and is not recognized as `USD 300`, because the matcher never crosses a line. A stale total written that way would not be found. Recognizing amounts split across a line, without joining unrelated lines (the false refusal above), is open.

**Status after Codex's independent verification of `78b7a055994b69aa0462ed47bd9db23de82f9bea` (2026-11-16).** Codex reran the 12 targeted cases: 9 failed on the parent `0ea08a8` and all 12 passed on the fix. Codex also confirmed with independently built Word and PowerPoint files that the original failures are corrected, and found no new actionable issues. The correction cycle for the prose-total check is closed.

| Finding | Implemented | CI passed (run, exact tested SHA) | Independently verified | Owner accepted |
|---|---|---|---|---|
| 1. Word tabs dropped | yes | `file-generation-verification` `43830666258` (1030 tests, none skipped), `subscription-foundation-verification` `43830672198`, `safety-helpfulness-regression` `43830677857` (820 tests), all success on `78b7a055994b69aa0462ed47bd9db23de82f9bea` | **Resolved** (Codex, at `78b7a05`) | no (pending) |
| 2. Soft line breaks dropped (Word and PowerPoint) | yes | the same three runs | **Resolved** (Codex, at `78b7a05`) | no (pending) |
| Amounts split across a line break | not addressed (deliberate) | n/a | documented limitation | no |

**Still open after this correction:**
- **The exact-confirmation interface:** the backend only; there is no chat acceptance control.
- **Word finalization:** page numbers and contents.
- **Arabic PDF interoperability:** ligatures, diacritics, mixed-direction reading order and search.
- **Broader semantic dependencies:** growth rates and margins in prose.
- **Native and cross-viewer presentation checks.**
- **The one-attempt confirmation contract:** a stored output survives a later provider failure, without rollback or request-key deduplication.
- **Amounts split across a line break:** above.

## Findings from the first audit (CODEX-D1 to CODEX-D4)

These are defect identifiers, unrelated to the D1/D2 implementation stages of the V1-02D/E
account-security plan.

| Finding | Correction | Original failure reproduced by | Verified by | CI |
|---|---|---|---|---|
| **CODEX-D1** 0016 skipped its empty-ledger guard in offline SQL | `f582696` | `test_0016_rendered_offline_carries_its_empty_ledger_refusal` fails on the original migration (no guard in the rendered SQL) | The same unit test; the PostgreSQL test now applies the offline-rendered 0015→0016 script, as the owner, to a ledger with a reservation and requires the refusal, no table created, revision still 0015 | `provider-ceilings-postgres` `43782671168` success (on `da2321c`, which contains it) |
| **CODEX-D2** a thread-start failure after `Popen` left a child running | `ad983ed` | `test_a_thread_that_cannot_start_never_leaves_a_child_running[1]` and `[2]` fail on the original worker (a raw `RuntimeError` escapes and the child is never reaped) | Both positions: `worker_failed` well before the time limit, child reaped, both pipes closed, slot released; an interrupted wait also reaps | `file-generation-verification` `43782674608` success |
| **CODEX-D3** reconciliation reads saw different committed states | `da2321c` | `test_finance_report_reads_one_snapshot_while_spending_commits` reproduces the false violations inside the test: one complete request commits after the reservations are summed and before the buckets are read, and the same reads under READ COMMITTED disagree | `verify()`/`summary()` refuse outside REPEATABLE READ, READ ONLY on PostgreSQL; the operator report run across the same schedule reports no violation; the snapshot rejects writes | `provider-ceilings-postgres` `43782671168` success |
| **CODEX-D4** malformed formulas passed (`=1+`, `=)1+2(`, `=A0`) | `ea5e081` | 63 of the 83 new tests fail on the original engine (the other 20 are well-formed formulas and legitimate errors, which pass on both) | `FormulaSyntaxError` / `invalid_formula` for 21 malformed cases in XLSX, CSV and JSON and in the editing API; 17 well-formed formulas accepted; `#DIV/0!`, `#NUM!`, `#N/A` kept; missing sheets keep `formula_reference_error`; an uploaded malformed formula leaves no computed cache | `file-generation-verification` `43783458552` success |

Also found and fixed while correcting D4: the engine treated any text cell starting with "=" as a
formula (now constants are literal), and a trailing omitted argument (`SUM(A1,)`) was refused.

## Findings from the document-quality audit of `6191b53`

| # | Finding | Correction | Original failure reproduced by | Verified by | CI |
|---|---|---|---|---|---|
| 1 | 1904 date system: an unrelated edit turned `YEAR(2027-01-01)` into 2022 | `3126abe` | Both new tests fail on the original code with `('=YEAR(A1)', 2022, 2027)` | Equivalent 1900/1904 workbooks, an unrelated edit, 15 date formulas (YEAR, MONTH, DAY, DATE, EDATE, EOMONTH, arithmetic, DAYS, WEEKDAY, TEXT, NETWORKDAYS, DATEDIF, a date-driven PMT) against independently computed values; serial results differ by exactly 1,462 | `file-generation-verification` `43784121071` success (contains it) |
| 2 | Edited workbook charts kept stale caches | `adf9da8` | All four new tests fail on the original editor | After the benchmark edit every chart cache equals a fresh build with the same input; after two input edits every cached point (date axis included) equals its cell; unsupported references and charts over uncomputable formulas lose their caches; a label edit leaves chart parts byte for byte | `43784121071` success |
| 3 | D4 still reproducible | see CODEX-D4 | | | |
| 4 | Word page-total caches hard-coded to 1 | **Not corrected: owner decision** (below) | Reproduced the mechanism; see "Word fields" | | |
| 5 | Arabic PDF text extraction and search failed | `dee4831` (partial) | `test_arabic_pdf_text_keeps_every_letter_of_every_word` fails with the original font | Every source word's exact letters come back from PDFium as one run, the heading is found by search, no invented letter | `file-generation-verification` `43788006737` success |
| 6 | PowerPoint edits bypassed fitting | `7137da2` (+ `76edf37`, lint only) | Five of the six new tests fail on the original editor | The audit's 3,329-character replacement is refused (`set_text` on shape and paragraph, `replace_text`); a table cell that would push its table off the slide is refused; Arabic measured; notes unconstrained; every shape and cell of the generated benchmark and Arabic decks, treated as edited, is accepted (the editor agrees with generation) | `43784906776` success |
| 7 | About-sheet numbers stored as text | `c0851b6` | New test fails on the original writer | Samples are numbers formatted `#,##0`; no numeric-looking string cell on the sheet | `43788006737` success (contains it) |
| 8 | Arabic Word callout accent on the left | **Not reproduced in Word** (below) | | | |

### Finding 5: what is and is not resolved

Root cause, verified at glyph level in the generated PDF: Noto Sans Arabic draws ب ت ث ن ي from one
shared dotless body glyph plus separate dot glyphs, so a glyph-to-text map cannot name the letter
and each dot repeats it. The PDF now sets Arabic in IBM Plex Sans Arabic (OFL-1.1, pinned and hashed),
which has a glyph per letter per joining form. Measured with PDFium on the Arabic benchmark:

| | Noto (before) | Plex (after) |
|---|---|---|
| Heading "لمحة عن العام" found by PDFium search | no | yes |
| Of the document's 27 Arabic words: returned with exactly their letters, as one run | 12 | 27 |
| Of the same 27: returned exactly as written (letter order too) | 11 | 19 (the other 8 contain a ligature pair) |
| Invented or doubled letters | yes ("ققريير") | none |

**Still open:** PDFium and pypdf reverse the two letters of a required two-letter ligature glyph
(lam-alef and Plex's other `rlig` pairs) inside right-to-left runs ("األداء" for "الأداء"), and
PDFium can split a word where a letter's tail leaves a gap. Typst's `/ActualText` spans are not used by
PDFium (the Noto PDF had 43 and still extracted wrongly). For comparison, Microsoft Word's own PDF
export of the same document extracts worse in PDFium (no probe word found). Clipboard and
screen-reader behaviour are not yet verified. This item is not resolved.

**Measured again 2026-11-17 (narrows the finding; resolves nothing).** The generated Arabic benchmark PDF
(Linux, typst 0.15.2, pypdfium2, pypdf 7.1.0; the same 27 source words as the test) carries seven
multi-letter glyphs in its ToUnicode maps: لا لأ لإ (regular and bold) and سي شر (regular). Words found
exactly in the extracted text:

| | PDFium text | PDFium search | pypdf text |
|---|---|---|---|
| As generated | 20 of 27 | 19 | **27 of 27** |
| Those seven entries' letters reversed in ToUnicode | 27 | 26 | 20 |

So pypdf already extracts every word exactly; the reversal is PDFium's (Chrome's viewer) alone, and the
statement above that pypdf reverses them no longer holds for this pypdf. The obvious ToUnicode
workaround only moves the defect from PDFium to pypdf (and to any extractor that reads ToUnicode in
logical order), so it is **not adopted**. All seven pairs come from the font's required-ligature
lookup (`rlig`, 241 pairs; checked in the GSUB table, whose only `liga` entry is the Latin "fi"), so
turning ligatures off is not a typographic option. A real correction must be honoured by both extractors,
for example the ligature's letters carried by a mechanism PDFium reads; none is measured yet.

### Finding 4 and Q3: Word fields

Verified locally in Microsoft Word 16 through automation, on the generated benchmark documents:

* Word desktop computes the footer itself: Word's own PDF export shows "Page 1 of 3", "Page 2 of 3",
  "Page 3 of 3" for the report and research documents. The stale cached "1" is what a viewer that does
  not recalculate fields shows.
* **The update prompt (Q3) is caused by the dirty TOC field.** Two copies of the report differing only
  in that flag: the copy without it opened with alerts on and no prompt; the copy with it blocked on a
  modal dialog (Word was then stopped).
* Word's pagination cannot be reproduced on the server. Word and the PDF engine paginate the same
  content differently (research document: Word's first body page ends inside "Method"; the PDF's
  holds the results table). So no cached page number written at generation is guaranteed to match
  Word.

Owner decision 12 (2026-11-15): page-number accuracy is never traded for the prompt, so the accurate
Word refresh stays. A LibreOffice finalization prototype, run in isolation, is evaluated in
`docs/DECISION-12-WORD-FINALIZATION.md`: it reproduced the owner's "Page 2 of 1" (LibreOffice does not
compute `SECTIONPAGES`), paginated every benchmark heading like Word, and its finalized files opened in
Word with no prompt and with contents equal to Word's own recomputation. Not adopted until the owner
accepts a sandboxed design. Finding 4 therefore stays open.

### Owner decision 13: Arabic in IBM Plex Sans Arabic in Word (`9c9a80d`)

Word documents in Arabic-script languages embed Plex; Arabic inside documents of other languages now
gets Plex too (it was Arial). Verified in Word 16 with Plex not installed: Word renders from the
embedded faces and the result matches the PDF. Found and fixed on the way: combining marks (tanween,
shadda) were split into runs of their own. PowerPoint keeps Arial: decks cannot embed a font today. `file-generation-verification` `43793462261` success (818 tests, none skipped).

### Finding 8: Arabic Word mirroring

Word's own rendering of the generated Arabic document (its PDF export, compared side by side with
the Typst PDF) shows the callout accent on the right, headings, paragraphs and the table mirrored
and right-aligned. In a `bidiVisual` table Word treats the `left` border as the leading edge. Other
viewers (LibreOffice, Google Docs) were not available here, so cross-viewer behaviour is unverified
and the Word output was not changed.

### Content findings (Q1, Q4, Q7, Q8, Q10)

Corrected in `e92583d` (`file-generation-verification` `43788689617` success, 810 tests, none skipped), with tests that reconcile each item from the fixture data: the revenue mix now
sums to the income statement's $18.4M; a breakdown's shown parts always add up to its shown total
(one rounding policy, nothing nudged); the research report is dated after its complete follow-up;
the proposal states its start date, and its offer expires before that date; the executive review has
no code; the tracked restatement changes every dependent figure and states which charts show
pre-restatement figures.

Semantic editing, first part (`888293e`, on `feat/account-security-d2-change`, stacked on this branch):
a Word or PowerPoint edit may not leave a total row that added up no longer adding up; it is refused
with `table_total_mismatch` unless the customer explicitly asked for exactly that change
(`edit(..., exact=True)`, the assistant's `edit_file` "exact" flag). Tables already inconsistent in the
upload never block an edit. Regression tests in `test_file_editing.py` and `test_file_worker.py`. `file-generation-verification` `43790147001` success (814 tests, none skipped).

Still open, from Codex's architecture recommendation: dependency checks for figures stated in prose
and metrics, and shared typed facts feeding tables, metrics, charts and prose.

## Premium audit of 2026-11-18: six confirmed findings (F1 to F6)

Codex's independent audit of Kalillac Premium confirmed six findings, besides the account-erasure and
retention launch gaps. Each original failure was reproduced before the correction and is kept as a test.
**None is marked resolved here:** each awaits Codex's independent verification at the SHA below.

| Finding | Correction (branch, SHA) | Original failure reproduced | Corrected behaviour and regression coverage |
|---|---|---|---|
| F1. Local reset could delete any directory | `fix/local-reset-ownership` `afa247a` (fast-forwarded into `feat/local-staging`) | On `8760bfc`, `KALILLAC_LOCAL_ROOT=<unrelated dir> reset --yes` (as root) stopped the process groups named in its `run/` folder and deleted it | Every command checks the root first: absolute, normal, named `kalillac-local-staging`, no symlink, a root-owned parent nobody else can write, and the ownership marker naming this deployment, the path, the device and the inode. `reset` never adopts an unmarked directory and re-checks after stopping; `stop` signals only local staging's own processes. `test_local_reset.py` (29, real directories through the real command). Also: the web root is no longer world-writable |
| F2. Expired files | `fix/premium-audit-corrections` `8de030b` | On `4a36dd7`: an expired file could not be deleted (404); quotas ignored expired files, so storage grew past the quota (six files under a three-file quota); expired files were deleted only at application startup | Quotas count every file on disk; owners delete expired files; the application runs the retention cleanup at startup and hourly under the operator command's lock, period `KALILLAC_FILES_DELETE_AFTER_EXPIRY_DAYS` (0 unless set, as before; D-9). `test_files_retention_access.py` |
| F3. Failed requests could consume provider spending without limit | `a12bf14` (cherry-picked from `3a82e0a`) | On `ac1f2a9`: six failing requests from one account all reached the provider, no account count changed (kept as `test_without_the_account_limit_failing_requests_reach_the_provider_every_time`) | Each provider attempt of a signed-in account's request is counted on its UTC day (migration 0026) in the pool's transaction and refused at the account's limit before dispatch; refunds for never-dispatched attempts; Free/paid limits required, no default (owner values). `test_account_attempts.py` (11), `test_account_attempts_postgres.py` (4) |
| F4. End & erase during a file request | `4a36dd7` | On `ac1f2a9`: the end reported no active request; the file was created after the end and the late reply delivered (200, the ended session's id) | The file request is registered work of its chat session, its budget is cancelled by the end, the assistant stops before the next model call or tool, and the late result is replaced by 409 `session_ended`. `test_files_session_and_modes.py` (deterministic: the model call is held until the end) |
| F5. Reasoning modes on file requests | `4a36dd7` | On `ac1f2a9`: Deep reached the provider as the default effort; invalid and unauthorized modes were not refused | The chat resolves the mode before the file handoff and carries it into the worker thread; `/api/files/assist` accepts and checks `reasoning_mode`. The provider payload is asserted on both paths |
| F6. Files after Premium ends | `8de030b` | Every `/api/files` route required the paid tier | List, download and delete need only the signed-in owner; upload and the assistant keep the tier; `can_add` drives the chat; "Your files" on the account page. Real PostgreSQL journey continued past cancellation |

| Finding | Implemented | CI passed (run, exact tested SHA) | Independently verified | Owner accepted |
|---|---|---|---|---|
| F1 | yes | `subscription-foundation-verification` `44410273831` success on `afa247a` (job 697 tests, including the 29 reset tests) | pending (Codex) | no (pending) |
| F2 | yes | on `a12bf14`: file generation `44424864471` (1,093), frontend `44424873844` (1,933 / 1,914 passed), anonymous allowance `44424887336` (646), provider ceilings `44424878148`, safety `44424900568`: success; after the test-stub follow-up `52b2408`: subscription `44435167879` (suites 1,583; PostgreSQL job 657) and Spaces `44435172942`: success (on `a12bf14` they had failed on three stubs of the old signature, one stalling the PostgreSQL job until cancelled) | pending (Codex) | no (pending) |
| F3 | yes | the same runs | pending (Codex) | no (pending) |
| F4 | yes | file generation `44414037930` success on `4a36dd7`; and the runs above | pending (Codex) | no (pending) |
| F5 | yes | the same as F4 | pending (Codex) | no (pending) |
| F6 | yes | the runs above | pending (Codex) | no (pending) |

**Open questions for Codex:**
1. F1: is adopting an unmarked root at the default path (non-destructive commands only, and only when it holds this deployment's record) acceptable, or should the owner mark it by hand?
2. F2: the default deletion period stays 0 days when unset (the release's behaviour since `36e1053`). Should a files deployment instead refuse to start without an explicit period?
3. F3: attempts are counted for every provider (OpenAI, Brave, Tavily) against one per-account number. Is one combined limit right, or should each provider have its own?
4. F4: files already stored before the end stay in the account (account files are not the conversation's). Is that the right boundary?
5. F6: a downgraded account keeps download access to images it uploaded. Can that path be used for any analysis without the tier?


## Reproducing locally

Windows, from `candidate/`, with the worktree's `.venv`:

    ..\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_workbook_design.py
    ..\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_file_editing.py
    ..\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_file_worker.py
    ..\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_document_design.py
    ..\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_finance_budgets.py

The PostgreSQL regressions (D1 offline script, D3 snapshot) run only in the disposable GitHub Actions
job: `gh workflow run provider-ceilings-postgres --ref fix/codex-audit-corrections`.
