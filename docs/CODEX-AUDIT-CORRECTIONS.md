# Corrections for the Codex audits: evidence for independent verification

**Checkpoints (provenance).** Four different commits have been handed to Codex; they are not the same:

| Checkpoint | Full SHA | What it is |
|---|---|---|
| Tag `codex-reverify-2026-11-16` (historical, published, unchanged) | `d3081a96f4fc20d711dbc8d094cab8c576b1bdc1` | The first reverification checkpoint, on `feat/account-security-d2-change`. It predates the corrections for findings A to D. An earlier version of this header said the tag marks "the commit that contains this file in this form"; that was wrong after this file changed, and the tag was never moved. |
| Second reverification handoff | `53869d73a509df07643e91dde7dff18ba6bbf984` (`feat/account-security-d2-quarantine`) | The checkpoint Codex reviewed on 2026-11-16: A to D corrected, migration 0022's 19 PostgreSQL tests passed independently; the confirmation lifecycle and the prose-total check had defects (below). |
| Third reverification handoff | `c5291b4a732ec87a72fdbe3c39caebb339ce0452` (`fix/codex-reverification-3`) | Codex verified the confirmation lifecycle and the provenance corrections there, and found regression R1 in the amount matcher (below). |
| **Current correction checkpoint** | The HEAD of `fix/codex-reverification-4` (a commit cannot name its own SHA; the full SHA and its CI runs are given in the handoff) | The bounded correction of R1, stacked on `c5291b4`. |

Nothing in any checkpoint was changed to make CI pass without investigation.

Branch `fix/codex-audit-corrections`, stacked on `feat/account-security-d2` (which is on
`feat/file-controls-v1`). The audited snapshot `6191b53` (`feat/document-quality-v1`) is unchanged.

**Nothing here is independently resolved.** Each item is "corrected, awaiting Codex verification"
until Codex verifies it against the commit named. Green CI is listed as supporting evidence only.

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

The CI runs for the current checkpoint are listed in the handoff.

**Still open after this correction, unchanged:**
- **The exact-confirmation interface:** the backend only.
- **Word finalization.**
- **Arabic PDF interoperability.**
- **Broader semantic dependencies:** growth rates and margins in prose.
- **Native and cross-viewer presentation checks.**
- **The one-attempt contract:** an output stored before a provider failure stays in the account, without rollback or deduplication.

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

## Reproducing locally

Windows, from `candidate/`, with the worktree's `.venv`:

    ..\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_workbook_design.py
    ..\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_file_editing.py
    ..\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_file_worker.py
    ..\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_document_design.py
    ..\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_finance_budgets.py

The PostgreSQL regressions (D1 offline script, D3 snapshot) run only in the disposable GitHub Actions
job: `gh workflow run provider-ceilings-postgres --ref fix/codex-audit-corrections`.
