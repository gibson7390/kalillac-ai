# File generation and upload intake

Kalillac must hand a customer real files, not text to copy, and must be able to read the files a
customer uploads. This package holds the two cores and the isolated worker that runs them:

* `kalillac_files.generate(spec, format)` turns a validated specification into a file.
* `kalillac_files.intake.inspect(data, declared_name)` validates one uploaded file and extracts
  bounded text from it.
* `kalillac_files.editing.outline(data, name)` and `edit(data, name, operations)` turn an uploaded
  Word, PowerPoint or Excel file into an edited copy; the upload itself is never changed.
* `kalillac_files.worker.run_generate`, `run_inspect`, `run_outline` and `run_edit` do the same
  work in a separate, limited process. This is the only way the application should ever call the
  cores.

Status: **cores, worker, file assistant, storage, routes and chat delivery implemented and
development verified; off by default (`KALILLAC_FILES_ENABLED`). Not owner accepted, not release
ready** (open audit findings; see the roadmap). With the flag off the application imports nothing from
`kalillac_files`, `/api/files` does not exist and `/api/chat` is unchanged. The production dependency
lock is unchanged.

## Design system

Documents share one design system across PDF and Word: the Kalillac palette (navy, blue, quiet
greys), Inter for text and headings (Source Serif 4 for the optional "editorial" theme), JetBrains
Mono for code, and Noto faces for Arabic, Hebrew, Devanagari, Thai, Chinese, Japanese and Korean.
Every font is SIL Open Font License, pinned to its publisher's release and verified by SHA-256
(`kalillac_files/fonts/manifest.json`, licenses beside it). Chinese, Japanese and Korean faces are
too large to commit and are fetched and verified by `tools/fetch_document_fonts.py`.

A document can carry: a cover page; a contents page; a summary panel; three heading levels;
paragraphs and list items with bold, italic and inline code; tables with captions, right-aligned
quantities and an emphasised total row; callouts in five tones; rows of metric cards with trend
marks; charts (column, bar, stacked column, line, area, donut); pull quotes; code blocks;
numbered steps; side-by-side comparisons; and page breaks.

**PDF** is typeset by Typst (`typeset.py`, `templates/document.typ`): running header with the
current section, "Page X of Y" footer, bookmarks and a tagged, accessible PDF, subsetted embedded
fonts, the Unicode bidirectional algorithm with mirrored layout for right-to-left documents, and
charts as vector art with alternative text. The document arrives as JSON data, never as markup.

**Word** (`docx_design.py`) uses real named styles, a refreshable table-of-contents field, a
section header that follows the current Heading 1, page-number fields, tables that repeat their
header and do not split rows, charts as images with alternative text and numbered captions, and the
Inter faces embedded in the file so it looks the same on any machine. Right-to-left documents are
mirrored and carry their own complex-script font, also embedded.

**Charts** (`charts.py`) are drawn deterministically: round axis steps that include zero for bars
and areas (a line whose values sit in a narrow band far from zero gets an axis fitted to the band,
so its changes show; PowerPoint charts follow the same rule), compact values ("$4.2M"), labels measured with the real font so they are shortened rather than overlapping,
and a bounds check that every mark stays on its canvas.

**Layout inspection** (`quality.py`) reads every generated PDF back with PDFium and refuses one with
a visible glyph past a margin, a blank page in the middle or missing glyphs. In justified text,
punctuation and hyphens may hang up to 3 pt into the margin (optical margin alignment, which Typst
applies on purpose); any other glyph over the 1.5 pt tolerance is a defect. Hostile content (unbreakable URLs
and words, twelve-column tables, very long numbers and labels) is made to fit: long tokens get
invisible break points, tables and metric values step their type down, and code shrinks to fit its
longest line before anything wraps.

## Spreadsheets

`xlsx_design.py` writes workbooks with XlsxWriter, after `formula_engine.py` has evaluated every
formula:

* **Computed values in the file.** Every formula is stored with its result, so mail clients, phones
  and cloud previews that do not recalculate still show the numbers. Excel recalculates on opening.
* **Correctness against Excel.** The engine implements every function the allowlist accepts (about
  a hundred), with Excel's semantics for blanks, text, booleans, errors, dates and rounding. 147 edge
  cases were recalculated by Microsoft Excel and recorded (`tests/formula_zoo.py`,
  `tools/record_formula_zoo.py`); the engine matches all of them. The benchmark forecast's 438
  formulas match Excel's recalculation and an independent computation in Python.
* **Broken models are refused**, not delivered: a reference to a sheet that does not exist, an
  unknown function, or a circular chain (`formula_reference_error`, `formula_circular`). Value
  errors such as `#DIV/0!` are kept, because they can be legitimate before inputs are filled in.
* **Financial-model conventions.** Typed constants in blue, formulas in black, formulas reading
  another sheet in green, editable inputs on a pale yellow fill, a bold total row between a rule and
  a double rule, negatives in red parentheses, whole amounts without cents, years without
  separators, readable dates.
* **Usable sheets.** Widths measured from the content, frozen header and label column, filters,
  print setup one page wide with the header repeated and "Page X of Y", gridlines on screen only.
* **Features.** Per-cell formats for mixed input sheets, conditional formatting (negatives, data
  bars, colour scales, thresholds, top and bottom), drop-down and range validation, native Excel
  charts linked to the cells (date axes for time series), dashboard sheets with metric tiles and
  charts, and an optional About sheet with links to each sheet and a colour legend.
* **Future functions** (IFS, XLOOKUP, CONCAT, DAYS, ...) are stored with the `_xlfn.` prefix Excel
  requires; without it Excel shows `#NAME?`. Found by the Excel comparison, fixed and tested.

## Presentations

`deck.py` validates a deck and `pptx_design.py` writes it as a 16:9 PowerPoint file in the same
design system (dark title, section and closing slides with the brand artwork; light content slides
with a running section label, a fitted title, an accent rule and a numbered footer). There is no free
positioning: each slide uses one of eleven layouts, which carry the design.

| Layout | Holds |
|---|---|
| `title`, `closing` | Title and subtitle beside the artwork; the title slide adds organization, author and date |
| `section` | Numbered section divider; its title becomes the running label of the slides after it |
| `points` | Up to six points, with an optional lead sentence |
| `two_columns` | Two cards, each a heading and up to five points |
| `metrics` | Two to four metric cards with trend marks, and an optional note |
| `chart` | A native chart (column, bar, stacked, line, area, donut) with an optional key takeaway |
| `table` | Up to six columns and eight rows, numbers right-aligned, optional total row |
| `quote`, `statement`, `timeline` | A pull quote; one large sentence; three to six numbered steps |

Every slide may carry speaker notes. Everything is a native, editable PowerPoint object: text boxes,
shapes, tables, and charts with their data embedded as a workbook (Edit Data works).

* **Text never overflows.** Each piece of text is wrapped and measured with Carlito, which has
  Calibri's metrics (the deck uses Calibri, present wherever Office or Google Slides is), and set at
  the largest size in its range that fits its box; what follows a two-line title moves down. The
  prediction was compared with PowerPoint's own line count for every text box and table cell
  (through PowerPoint's object model): 235 of 235 agree on a deck with every field at its limit, 123
  of 123 on the benchmark deck. Table cells keep 0.05 in in hand, because PowerPoint needs up to
  0.03 in more than text and margins there (measured).
* **Limits that fit.** The text limits in `deck.py` are set so that every layout, with every field at
  its limit in ordinary prose, fits; the tests prove it layout by layout. Only a table dense in
  every cell is refused (`slide_text_too_long`), so the author shortens it.
* **Even lines.** Titles, subtitles, statements, quotes, leads and takeaways are balanced: set at
  the narrowest width that keeps their line count, so no line ends with a stray word.
* **Charts** have no repeated title (the slide title names them), axis ticks as precise as their
  step (`$6M`, not `$6.0M`), value labels on single-series bars instead of an axis, and the deck
  palette.
* **Right-to-left decks** (Arabic, Hebrew, ...) are mirrored: the title column, cards, metric
  order, table columns, timeline, chart and takeaway, footer and the artwork itself.
* **Chinese, Japanese and Korean** text wraps between characters and names the language's own
  East Asian face (Yu Gothic, Microsoft YaHei, Microsoft JhengHei, Malgun Gothic).
* **Rendered and inspected in Microsoft PowerPoint**: the benchmark deck, the at-the-limits deck
  and an Arabic deck, exported to PDF and reviewed slide by slide (development check, not CI).

## Package hygiene

python-docx and python-pptx start every file from a starter template whose own record would travel
with it: a "generated by" description, its author, 2013 dates, an application record naming a
Macintosh Office release and a 4:3 page, an empty-page thumbnail, a bibliography part and printer
settings. Word and PowerPoint files carry none of that (`package.py`): their core properties name
this file's author and summary, the application record is Kalillac's, and every ZIP entry is dated
at the ZIP epoch, so the same specification always produces the same bytes (tested across seconds
and across processes). Workbooks keep XlsxWriter's record, which carries the real creation time.

## Formats

| Format | From | Supported |
|---|---|---|
| PDF | document | The whole design system above. Text in any bundled script; a character no bundled font has (emoji, historic scripts) is refused as `pdf_unsupported_characters` |
| DOCX | document | The whole design system above, editable, any Unicode text |
| XLSX | workbook | Designed workbooks with computed values (see "Spreadsheets") |
| CSV | workbook | One sheet, UTF-8 with a byte order mark, formula-injection guard |
| Markdown, TXT | document | Plain renderings of the same document |
| JSON | workbook | Each sheet as records keyed by column header |
| PPTX | deck | Designed presentations (see "Presentations") |

## The specification

Plain data, as a model tool call or an API body would supply it. Nothing in it is executed,
evaluated, fetched or used as a path; there is no template language and no HTML, so text is always
text. The grammar is in `kalillac_files/spec.py`.

```json
{"title": "Quarterly Review",
 "blocks": [{"type": "heading", "level": 1, "text": "Summary"},
            {"type": "paragraph", "text": "Revenue grew 12%."},
            {"type": "list", "ordered": false, "items": ["First", "Second"]},
            {"type": "table", "header": ["Item", "Q1"], "rows": [["Coffee", "1,200"]]},
            {"type": "page_break"}]}
```

```json
{"title": "Loan Model",
 "sheets": [{"name": "Inputs",
             "columns": [{"header": "Name", "format": "text"}, {"header": "Value", "format": "number"}],
             "rows": [["Principal", 250000], ["Total", {"formula": "=SUM(B2:B2)"}]]}]}
```

A refused specification raises `SpecError` with a stable code (`invalid_table`, `invalid_formula`,
`document_too_large`, ...). The message is the code; content is never echoed.

## Safety properties, each with a test

* **Valid files.** Every PDF, DOCX, XLSX and PPTX produced in the tests is opened again with an
  independent reader (PDFium, pypdf, python-docx, openpyxl, python-pptx) and its content,
  structure, styles, formulas, number formats and archive integrity are checked.
* **No markup or code execution.** Document text is passed to the typesetter as data, so `<b>` or
  `#set page(...)` appears literally. A spreadsheet
  text cell is stored as text whatever it starts with; only an explicit `{"formula": ...}` becomes a
  formula.
* **Formula allowlist.** A formula may use cell references, operators and about a hundred functions
  that read this workbook only. External workbook references, DDE, `WEBSERVICE`, `HYPERLINK`,
  `INDIRECT`, `CALL`, `RTD` and anything unlisted are refused.
* **CSV injection.** Text starting with `=`, `+`, `-`, `@`, tab or carriage return is prefixed with
  an apostrophe.
* **No path from input.** The file name is built from the title by an allowlist (ASCII letters and
  digits joined by hyphens, at most 60 characters, never a reserved device name). `../../etc/passwd`
  becomes `etc-passwd.pdf`.
* **Bounded work.** Sizes are checked before rendering, a workbook over the cell limit is refused by
  counting before any cell is examined, and the result is capped at 10 MB.
* **Faithful or refused.** A document a format cannot represent is refused, never rendered wrongly.

## Limits

| Limit | Value |
|---|---|
| Document text | 200,000 characters; 2,000 blocks; 20,000 characters per paragraph |
| Tables in a document | 12 columns, 500 rows each, 6,000 cells in total |
| Workbook | 20 sheets, 100 columns, 10,000 rows per sheet, 50,000 cells in total |
| Cell text / formula | 2,000 / 500 characters |
| Generated file | 10 MB |

A six-page illustrated report typesets in about one second; the Word version takes about two.
Earlier, looser limits allowed a 71-second PDF and a 96 MB workbook build, which is why they are
this tight. Word files are about 1.2 MB because the Inter faces are embedded in full, so that text
the reader adds later still uses them.

Known format limits:

* **Emoji and rare scripts** have no bundled font and are refused in PDF.
* **Text layers of Arabic, Devanagari and Thai PDFs** do not survive copy-and-paste or search
  letter-for-letter (the page itself is correct). Word's own PDF exports of the same text have the
  same problem. Hebrew, Latin, Greek, Cyrillic and CJK extract correctly. The Word file carries the
  exact text.
* **Word layout** is computed by the program that opens the file, so pagination differs a little
  between Word, LibreOffice and others. It has been rendered and inspected in Microsoft Word.
* **Formulas are stored, not calculated.** The spreadsheet program computes them on opening.
* **Deck fonts are not embedded.** Calibri is present wherever Office or Google Slides is;
  elsewhere (LibreOffice on Linux) Carlito, its metric-compatible twin, takes its place without
  changing a line break.
* **Scripts Calibri lacks** (Arabic, Hebrew, ...) are measured at a conservative 0.6 em per
  character (Arial, which PowerPoint uses for them, averages 0.50 to 0.55 em), so their boxes err
  roomy, never short.
* No images supplied by the customer, no hyperlinks.

## Editing an uploaded file

`editing.py` edits the customer's own Word, PowerPoint or Excel file and returns a new file named
`<name>-edited.<ext>`. The upload is never modified, so the original stays available beside the
result. The workflow is: intake checks the upload, `outline()` lists what can be changed, each
piece with an address (`p4`, `t1.r2.c3`; `s2.sh3`, `s2.notes`; `Inputs!B5`), the model chooses
operations, and `edit()` applies them.

| Operation | Word | PowerPoint | Excel |
|---|---|---|---|
| `replace_text` (optionally case-insensitive, within an address, with an expected count) | yes | yes, notes included | text cells only, never formulas |
| `set_text` | paragraph, cell | paragraph, shape, table cell, notes | |
| `insert_paragraph` (after or before, optional style), `delete` (paragraph, row), `add_row` | yes | | |
| `set_cell` (number, text, true/false, empty, or an allowlisted formula) | | | yes |

What makes an edit faithful:

* **The customer's formatting is kept.** A replacement takes the formatting of the text it
  replaces, even when the match spans differently formatted runs; inserted paragraphs and rows
  copy their neighbour's formatting; everything not addressed is untouched (byte for byte in the
  tests).
* **No silent edits.** An operation that matches nothing, or a different number of times than
  `expect`, is refused (`edit_no_match`, `edit_match_count`). Addresses always refer to the
  uploaded file, so the operations of one edit cannot shift one another; touching something an
  earlier operation deleted is refused (`edit_conflict`).
* **Tracked changes in Word.** With `track_changes`, every insertion and deletion (text,
  paragraphs, rows, paragraph marks) is a revision by "Kalillac AI" that the reader accepts or
  rejects in Word. The tests prove that accepting everything gives exactly the plain edit and
  rejecting everything gives exactly the upload; Microsoft Word listed the 9 revisions of a test
  edit and, once they were accepted, showed the edited text.
* **Excel edited in its XML**, not loaded and re-saved by a spreadsheet library (which drops
  charts, shapes and unknown features): only the parts an edit changes are rewritten; charts,
  drawings, styles and shared strings stay byte for byte. Shared formulas are written out in full
  before a cell changes; a cell inside an array formula or a merged range (other than its first
  cell) is refused; renaming an Excel table's header updates the table, as Excel requires; a
  stale calculation chain is removed.
* **No stale numbers.** After an Excel edit every formula's stored value is recomputed with the
  formula engine. Changing an input of the benchmark forecast gives exactly the values of a
  workbook generated fresh with that input, and Microsoft Excel's own recalculation of the edited
  file agreed with all 438. When a workbook holds formulas the engine does not compute (array
  formulas, defined names, functions outside the allowlist), stored values are cleared instead of
  left stale, and Excel is told to recalculate on opening in every case.
* **Checked on the way out.** The result must pass intake again and stay within 10 MB.

Limits of editing: `replace_text` in Word covers the body (paragraphs and tables), not headers,
footers, footnotes, text boxes or document properties; numbers drawn inside chart pictures do not
change; PowerPoint has no tracked changes; Excel edits do not add rows or sheets.

## Upload intake

`inspect(data, declared_name)` accepts PDF, DOCX, XLSX, PPTX and UTF-8 text (`.txt`, `.md`, `.csv`,
`.json`), at most 10 MB, and returns the detected kind and up to 400,000 characters of text, with a
flag when it was cut short. Nothing is written to disk and nothing in the file is executed,
evaluated or followed. A refusal is an `IntakeError` with a stable code.

| Threat | Defence | Code |
|---|---|---|
| Wrong type under a trusted name | The kind is decided from the content; a declared extension that disagrees is refused | `type_mismatch`, `unsupported_type` |
| Decompression bomb | Declared sizes, the total and the expansion ratio are checked from the headers before any part is read; every part is then read through a hard byte cap | `archive_too_large` |
| Path traversal in an archive | Absolute, drive, backslash, `..` and duplicate entry names are refused (the stored name is checked, since Windows rewrites backslashes). Nothing is extracted to disk in any case | `archive_unsafe_entry` |
| Too many entries | 2,000 entries at most | `archive_too_many_entries` |
| Macros | `vbaProject.bin`, macro sheets and macro-enabled content types are refused | `macros_not_allowed` |
| Encrypted files | Encrypted archive entries and encrypted PDFs are refused | `encrypted_file` |
| XML external entities, entity expansion | Any XML part with a DOCTYPE or entity declaration is refused before parsing; parts must be UTF-8 so another encoding cannot hide one; the parser resolves no entity and makes no network access | `xml_declaration_not_allowed`, `xml_encoding_not_allowed` |
| Malformed files | Parser errors become a refusal, never an unhandled exception | `unreadable_archive`, `unreadable_document` |
| Resource exhaustion | Size, entry, page (500), cell (200,000) and text caps | `file_too_large`, truncation flag |
| Embedded objects, external links, nested archives | Never opened or followed | |

Intake reports spreadsheet formulas as text and never evaluates them. Editing recomputes a
workbook's formulas only when every one of them is in the allowlist, with Kalillac's own engine,
which reads cells of that workbook and nothing else. A deck's text is read slide by slide in the
presentation's order, with its speaker notes.

A hostile PDF can still make any PDF parser spend unbounded time or memory, which is why intake
must be called through the isolated worker below, never directly. There is no malware scanning;
refusing macros, encryption and executables by type is the control in place. Scanning uploaded
files with an external service would be a new paid service and needs approval.

## Isolated worker

`worker.run_generate(spec, format)` and `worker.run_inspect(data, name)` return exactly what the
cores return, from a child Python process:

| Limit | Behaviour | Code |
|---|---|---|
| Wall-clock time (default 20 s) | The child is killed and reaped | `timeout` |
| Memory (default 768 MB) and CPU time | Set by the child on itself before it reads any input. **POSIX only:** on Windows the time limit is the only bound; production runs on Linux. A starved child usually fails at once; once, on a GitHub runner, it stalled and was killed at the time limit instead | `out_of_memory`, `worker_failed`, `timeout` |
| Concurrency (2) | A further request is refused immediately, never queued | `busy` |
| Output size | The parent stops reading and kills the child | `output_too_large` |
| Environment | The child inherits no variable except the few Python needs to start, and runs as an isolated interpreter (`-I`). No provider key, database credential or other secret exists in the process that parses customer files | |
| Child failure | A crash, a malformed reply or a reply that does not match the request (wrong media type, wrong extension, invalid text) | `worker_failed` |

A refusal by a core (`pdf_unsupported_characters`, `macros_not_allowed`, ...) passes through with
its own code. The child's stderr is discarded, so no document content reaches a log.

On POSIX the child also forbids itself from starting any process or thread. The spreadsheet
library would import numpy when it is installed, and numpy starts threads on import, so the child
blocks that import; nothing in the cores needs it. A future dependency that starts threads would
fail the Linux tests rather than slip through.

Not provided by the worker: operating-system sandboxing (a separate user, filesystem or network
namespace). The child has no network use and writes no file, but nothing at the OS level prevents
it. That is a deployment decision for when uploads are exposed.

## The file assistant and delivery

`assistant.py`, `store.py`, `router.py`, `intent.py`, `account_access.py` and `consistency.py`
connect the cores to customers. The shape:

1. **Understanding the request.** In `/api/chat`, a message with attachments, or one that asks for
   a file (a request to make something plus a file deliverable: a named format, a spreadsheet,
   workbook, deck, slides, presentation or "file"; programming and how-to questions excluded,
   `intent.py`), from a signed-in account with file access goes to the file assistant. Everything
   else, including every anonymous request, takes the ordinary chat path unchanged. The same
   assistant is also reachable directly at `POST /api/files/assist`.
2. **Planning and generating.** GPT-6 Luna receives the file instructions (`FILE_INSTRUCTIONS`:
   when to make a file and in which format, how to plan it, accuracy rules, the complete grammar)
   and three tools: `create_file`, `read_file` (the outline of an attached file) and `edit_file`.
   Every tool request is validated; files are rendered and edited in the isolated worker.
3. **Validating the result.** Everything the cores check applies (layout inspection for PDF,
   formulas that compute, text that fits each slide), plus `consistency.py`: a table whose last row
   is labelled a total must add up, column by column, or the file is refused with
   `table_total_mismatch` and the table and column named. A refusal returns to the model as data
   with its code and a hint; the model corrects the specification and tries again, within the
   loop's bounds (6 tool rounds, 10 tool calls, 4 files per request).
4. **Storing.** `FileStore` keeps each file under a random 128-bit identifier in a directory named
   by a hash of the owning account; quotas (250 MB and 300 files per account by default) are
   checked before writing; files expire after 30 days and are purged at startup; writes are
   atomic. Another account's file and a missing file are indistinguishable.
5. **Delivering.** The chat reply carries `files` (identifier, name, type, size). The chat page
   shows them as download links (`/api/files/{id}`: authenticated, owner-checked, served as an
   attachment with `nosniff`, a sandboxing content security policy and `no-store`). A deployment
   with files enabled marks the page (`<html data-kalillac-files="on">`); only then does the page
   ask whether the account may attach files and show the attach control.

Configuration, all required to be explicit when the flag is on (startup fails otherwise):
`KALILLAC_FILES_ENABLED`, `KALILLAC_FILES_ROOT` (an absolute directory), `KALILLAC_FILES_TIER`
(`paid` by default: Premium only; `any` admits Free accounts. Which tier may use files is an owner
decision; the default is fail-closed). Files need accounts and the database.

Security properties, each tested:

* Every `/api/files` route needs a signed-in account (the V1 security authority when configured,
  otherwise the session lookup with verified email when verification is on) of the configured tier.
* `POST /api/files`, `POST /api/files/assist` and `DELETE /api/files/{id}` are in the unsafe-request
  guard's inventory: a cross-site or query-carrying request is refused before any work.
* Uploads are the raw body (no multipart parser), bounded while reading, and pass intake in the
  worker before anything is stored; the name is reduced to an inert label.
* The model can address only files attached to the request or made in it, never a path or another
  account's file; text read from a file is labelled untrusted data.
* Model calls go through the same controls as every chat call (reasoning mode, financial
  reservation, provider protection, usage observation), with a 240-second timeout for long
  specifications. At GPT-6 Luna's published output price ($0.50 per million tokens) a full
  32,000-token answer costs at most $0.016; a request makes at most 7 model calls (6 tool rounds and
  the reply), so about $0.11 of output at most, plus input tokens.
* At most two assistant requests run at once per process; more are refused with `busy`.

Not yet done, and required before release: the request budget (`KALILLAC_REQUEST_BUDGET`) does not
yet bound the file path's deadlines (the assistant has its own concurrency cap and timeouts);
web search is not available inside the file assistant; the reply is not streamed, so a long file
takes up to a few minutes with only the thinking indicator; the chat's capability statement for
accounts without file access still says files cannot be made; a browser review of the attach and
download flow against a real server; a release of `app.js` must bump the asset cache key.

## Dependencies

`requirements-files.txt` pins Typst 0.15.2 (Apache-2.0), fontTools 4.68.0 (MIT), pypdfium2 5.15.2
(Apache-2.0/BSD-3, layout inspection and PDF reading), python-docx 1.2.0 (MIT), python-pptx 1.0.2
(MIT), openpyxl 3.1.5 (MIT),
pypdf 7.1.0 (BSD) and two further dependencies, lxml and et_xmlfile. The worker starts the
typesetter's two-thread pool on a constant before it reads any input and only then forbids itself
new threads (for PDF, Word and decks, which all use it: Word and decks for their cover artwork). The
Carlito faces in `fonts/metrics/` (OFL) only measure slide text; they are never embedded. Shipping this needs a new production Python environment built from the lock plus
`requirements-files.txt`; the current release procedure keeps the environment unchanged.

## Not built

* Per-account cost accounting for file requests beyond the existing financial controls.
* Object storage or a database record of files (the store is a local directory, sized for one server).
* The release items listed at the end of "The file assistant and delivery".

## Tests

`tests/test_workbook_design.py` (178: the engine against Excel, the forecast benchmark, conventions,
charts, refusals), `tests/test_file_generation.py` (166), `tests/test_file_intake.py` (65),
`tests/test_file_worker.py` (33, with real child processes and real kills), `tests/test_file_editing.py`
(39: outlines, faithful edits, tracked changes, preserved parts, recomputed workbooks, refusals),
`tests/test_document_design.py` (109: specification, charts, font integrity and licensing, layout
soundness on benchmark and hostile content, multilingual and right-to-left output, Word structure
and package record) and `tests/test_deck_design.py` (63: the deck specification and its refusals,
package record and reproducibility, native charts and their number formats, every text box
re-measured from the file, no overlaps, nothing below the footer, every layout at its limits,
right-to-left mirroring, East Asian text, measurement against the font tables), run by
`.github/workflows/file-generation-verification.yml`, which fetches and verifies the fonts and
requires all 653 to run with none skipped.

`tools/render_benchmark.py OUTPUT_FOLDER` writes the whole quality benchmark (business report,
research document, financial workbook, Word proposal, executive presentation, a tracked complex
edit, a multilingual document, Arabic report and deck) with a README, for a person to open and
judge. It produces files, not a score.

Rendering through Microsoft Word itself is a manual development check (Word is not available in
CI): the benchmark report and the Arabic document were opened, exported to PDF and inspected.
The forecast workbook was recalculated and exported by Microsoft Excel the same way, and the decks
by Microsoft PowerPoint, whose line counts were also compared with the measurement.
