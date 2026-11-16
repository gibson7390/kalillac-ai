# Corrections for the Codex audits: evidence for independent verification

**Checkpoint for reverification:** the tag `codex-reverify-2026-11-16` marks the exact commit that
contains this file in this form (`feat/account-security-d2-change`). It contains every correction
below and the work stacked after them; nothing in it was changed to make CI pass without investigation.

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

CI on `c274f90` (A to D and section 3): `file-generation-verification` `43801588596`, `subscription-foundation-verification` `43801592242` and `safety-helpfulness-regression` `43801595873`, all successful.

| Finding | Correction | Original failure reproduced by | Verified by |
|---|---|---|---|
| A. Tracked Word edits bypass the total check (`editing.py:1682-1685`) | `e7abd44` | Four of the seven new tests fail on the previous editor | Tables read with every revision accepted: tracked replacements, inserted and deleted rows, previously revised uploads, accept-all and reject-all views, formatting kept |
| B. Table rows that each fit push the table off the slide (`editing.py:892-897`) | `7a5466c` | `test_rows_that_each_fit_cannot_together_push_the_table_off_the_slide` fails on the previous editor | Whole final table measured: cumulative rows, cells spanning rows, Arabic tables, other cells unchanged |
| C. Restatement leaves the outlook chart's 2026 value undisclosed (`quality_benchmark.py:425-428`) | `b742ff6` | The dependency test finds an untitled, unnamed dependent chart in the previous fixture | Every chart whose data shows 2026 revenue is found from the report's data and named in the disclosure |
| D. Displayed parts not reconciling (`charts.py:99-114`, `[33333.4, 33333.4, 33333.2]`) | `61618c2` | The previous labels showed 33.333K three times under 100.000K | Deterministic policy: reconcile within two extra decimals or disclose the rounding on the chart; sources unchanged |
| Sections 2 and 6. A restated total left stated elsewhere (found while correcting A) | `87a1f36` | The proposal's summary kept the old total after a table restatement | Refused with `dependent_figure_unchanged` unless restated everywhere or confirmed exact |
| Section 3. The model's `exact=True` bypassed the check | `c274f90` | The previous service applied the model's exact edit with no customer involvement | Single-use, expiring confirmations bound to account, file and operations, issued only to the interface; tests at `/api/files/assist` |

Still open: Word finalization (decision 12, evaluation in `docs/DECISION-12-WORD-FINALIZATION.md`), Arabic PDF
logical text, the interface control for exact-edit confirmations, and dependency enforcement for figures
in prose and metrics beyond totals. Since `87a1f36`, an edit that changes a table's total is refused while
the old total still appears anywhere else in the document (`dependent_figure_unchanged`); other derived
figures (growth rates, margins) in prose are not yet checked.

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
