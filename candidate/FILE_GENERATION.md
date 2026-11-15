# File generation

Kalillac must hand a customer real files, not text to copy. This is the generation core:
`kalillac_files.generate(spec, format)` turns a validated specification into a file.

Status: **core implemented and development verified. Not reachable from the application.** Nothing
imports `kalillac_files`, no route exposes it, and the production dependency lock is unchanged.
Uploads, editing, storage, downloads and quotas are not built (see "Not built").

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

## Architecture requirements for wiring it in

Rendering is synchronous CPU work lasting up to seconds. It must not run inside a request handler
or on the chat-serving path. The intended shape, not yet built:

1. The model produces a specification through a tool call. It is validated exactly as above.
2. A bounded worker pool renders it with its own time limit and concurrency cap, separate from the
   chat workers.
3. The file is stored under a random identifier owned by the customer's account, with a per-account
   storage quota and an expiry, and removed by a cleanup job.
4. Download is an authenticated route that checks ownership in the backend on every request and
   serves with `Content-Disposition: attachment` and the recorded media type.
5. Generation is a Premium capability, checked from the verified entitlement.

Items 3 to 5 need customer accounts. No uploaded content is ever executed, and no customer-written
code runs anywhere in this design.

## Dependencies

`requirements-files.txt` pins reportlab 5.1.0 (BSD), python-docx 1.2.0 (MIT), openpyxl 3.1.5 (MIT)
and their two extra dependencies, lxml and et_xmlfile. `requirements-files-test.txt` pins pypdf for
the tests only. Shipping this needs a new production Python environment built from the lock plus
`requirements-files.txt`; the current release procedure keeps the environment unchanged.

## Not built

* File uploads and analysis (validation, type and size limits, decompression-bomb and malformed-file
  defences, quotas, isolation).
* Editing an uploaded document into a new file.
* Storage, download authorization, cleanup, quotas and cost accounting.
* The model tool and the interface for requesting and downloading a file.
* PowerPoint.

## Tests

`tests/test_file_generation.py` (155), run by `.github/workflows/file-generation-verification.yml`,
which requires all 155 to run with none skipped.
