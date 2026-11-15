# File generation and upload intake

Kalillac must hand a customer real files, not text to copy, and must be able to read the files a
customer uploads. This package holds the two cores and the isolated worker that runs them:

* `kalillac_files.generate(spec, format)` turns a validated specification into a file.
* `kalillac_files.intake.inspect(data, declared_name)` validates one uploaded file and extracts
  bounded text from it.
* `kalillac_files.worker.run_generate(...)` and `run_inspect(...)` do the same work in a separate,
  limited process. This is the only way the application should ever call the cores.

Status: **cores and worker implemented and development verified. Not reachable from the application.**
Nothing imports `kalillac_files`, no route exposes it, and the production dependency lock is
unchanged. Storage, downloads, quotas, editing and the interface are not built (see "Not built").

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

**Charts** (`charts.py`) are drawn deterministically: round axis steps that include zero, compact
values ("$4.2M"), labels measured with the real font so they are shortened rather than overlapping,
and a bounds check that every mark stays on its canvas.

**Layout inspection** (`quality.py`) reads every generated PDF back with PDFium and refuses one with
text past a margin, a blank page in the middle or missing glyphs. Hostile content (unbreakable URLs
and words, twelve-column tables, very long numbers and labels) is made to fit: long tokens get
invisible break points, tables and metric values step their type down, and code shrinks to fit its
longest line before anything wraps.

## Formats

| Format | From | Supported |
|---|---|---|
| PDF | document | The whole design system above. Text in any bundled script; a character no bundled font has (emoji, historic scripts) is refused as `pdf_unsupported_characters` |
| DOCX | document | The whole design system above, editable, any Unicode text |
| XLSX | workbook | Several sheets, bold frozen header row, column widths, number formats (number, integer, currency, percent, date, text), real dates, formulas from an allowlist |
| CSV | workbook | One sheet, UTF-8 with a byte order mark, formula-injection guard |
| Markdown, TXT | document | Plain renderings of the same document |
| JSON | workbook | Each sheet as records keyed by column header |

PowerPoint is not supported and is refused as `unsupported_format`.

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

* **Valid files.** Every PDF, DOCX and XLSX produced in the tests is opened again with an
  independent reader (PDFium, pypdf, python-docx, openpyxl) and its content, structure, styles,
  formulas, number formats and archive integrity are checked.
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
* No images supplied by the customer, no hyperlinks.

## Upload intake

`inspect(data, declared_name)` accepts PDF, DOCX, XLSX and UTF-8 text (`.txt`, `.md`, `.csv`,
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

Spreadsheet formulas in an upload are reported as text and never evaluated.

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
| Memory (default 768 MB) and CPU time | Set by the child on itself before it reads any input. **POSIX only:** on Windows the time limit is the only bound; production runs on Linux | `out_of_memory`, `worker_failed` |
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

## Architecture requirements for wiring it in

Rendering is synchronous CPU work lasting up to seconds. It must not run inside a request handler
or on the chat-serving path. The intended shape; step 2 exists, the rest is not built:

1. The model produces a specification through a tool call. It is validated exactly as above.
2. The isolated worker renders it (or inspects an upload) under its time, memory and concurrency
   limits, separate from the chat workers. The calling route must run the blocking call off the
   event loop.
3. The file is stored under a random identifier owned by the customer's account, with a per-account
   storage quota and an expiry, and removed by a cleanup job.
4. Download is an authenticated route that checks ownership in the backend on every request and
   serves with `Content-Disposition: attachment` and the recorded media type.
5. Generation is a Premium capability, checked from the verified entitlement.

Items 3 to 5 need customer accounts. No uploaded content is ever executed, and no customer-written
code runs anywhere in this design.

## Dependencies

`requirements-files.txt` pins Typst 0.15.2 (Apache-2.0), fontTools 4.68.0 (MIT), pypdfium2 5.15.2
(Apache-2.0/BSD-3, layout inspection and PDF reading), python-docx 1.2.0 (MIT), openpyxl 3.1.5 (MIT),
pypdf 7.1.0 (BSD) and two further dependencies, lxml and et_xmlfile. The worker starts the
typesetter's two-thread pool on a constant before it reads any input and only then forbids itself
new threads. Shipping this needs a new production Python environment built from the lock plus
`requirements-files.txt`; the current release procedure keeps the environment unchanged.

## Not built

* The upload route and per-account upload quotas and isolation.
* Editing an uploaded document into a new file. The two cores are the halves of it: extracted text
  in, a new specification out. The original is never modified.
* Storage, download authorization, cleanup, quotas and cost accounting.
* The model tool and the interface for requesting, uploading and downloading a file.
* PowerPoint.
* Spreadsheets in the design system (styled workbooks, charts, conditional formatting, validation):
  the XLSX writer is still the plain one described above.

## Tests

`tests/test_file_generation.py` (164), `tests/test_file_intake.py` (63),
`tests/test_file_worker.py` (31, with real child processes and real kills) and
`tests/test_document_design.py` (98: specification, charts, font integrity and licensing, layout
soundness on benchmark and hostile content, multilingual and right-to-left output, Word structure),
run by `.github/workflows/file-generation-verification.yml`, which fetches and verifies the fonts
and requires all 356 to run with none skipped.

Rendering through Microsoft Word itself is a manual development check (Word is not available in
CI): the benchmark report and the Arabic document were opened, exported to PDF and inspected.
