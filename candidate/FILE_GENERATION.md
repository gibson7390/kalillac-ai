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

## Formats

| Format | From | Supported |
|---|---|---|
| PDF | document | Title, three heading levels, paragraphs, bulleted and numbered lists, tables that repeat their header across pages, page breaks. **Western European text only** (see limits) |
| DOCX | document | The same structure as real Word styles (Title, Heading 1 to 3, List Bullet, List Number, Table Grid), editable, any Unicode text |
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
  independent reader (pypdf, python-docx, openpyxl) and its content, structure, styles, formulas,
  number formats and archive integrity are checked.
* **No markup or code execution.** PDF text is escaped, so `<b>` appears literally. A spreadsheet
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

Measured on the development machine at these maxima: PDF 1.6 s, DOCX 2.0 s, XLSX 2.1 s. Earlier,
looser limits allowed a 71-second PDF and a 96 MB workbook build, which is why they are this tight.

Known format limits:

* **PDF text coverage.** The only fonts available without adding a font file to the repository are
  the Bitstream Vera faces inside reportlab (about 280 characters: Western European). Any other
  character is refused as `pdf_unsupported_characters`; the same document generates as DOCX.
  Supporting more scripts means bundling a Unicode font, which is a licensing decision for the owner.
* **PDF table cells** cannot continue on the next page. A cell taller than a page is refused as
  `pdf_layout_failed`.
* **Formulas are stored, not calculated.** The spreadsheet program computes them on opening.
* No images, charts, hyperlinks, text styling within a paragraph, or page headers and footers.

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

`requirements-files.txt` pins reportlab 5.1.0 (BSD), python-docx 1.2.0 (MIT), openpyxl 3.1.5 (MIT),
pypdf 7.1.0 (BSD, reads uploaded PDFs) and two further dependencies, lxml and et_xmlfile. Shipping
this needs a new production Python environment built from the lock plus
`requirements-files.txt`; the current release procedure keeps the environment unchanged.

## Not built

* The upload route and per-account upload quotas and isolation.
* Editing an uploaded document into a new file. The two cores are the halves of it: extracted text
  in, a new specification out. The original is never modified.
* Storage, download authorization, cleanup, quotas and cost accounting.
* The model tool and the interface for requesting, uploading and downloading a file.
* PowerPoint.

## Tests

`tests/test_file_generation.py` (155), `tests/test_file_intake.py` (63) and
`tests/test_file_worker.py` (31, with real child processes and real kills), run by
`.github/workflows/file-generation-verification.yml`, which requires all 249 to run with none
skipped.
