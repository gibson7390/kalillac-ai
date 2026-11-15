"""File generation core: every generated artifact is opened again with an
independent reader and checked, not just produced.

Needs the document libraries in requirements-files.txt and pypdf. Content is
synthetic. Nothing here touches the application, a database or the network.
"""
from __future__ import annotations

import copy
import csv
from datetime import date
import io
import json
from pathlib import Path
import zipfile

import pytest

pypdf = pytest.importorskip("pypdf")
docx = pytest.importorskip("docx")
openpyxl = pytest.importorskip("openpyxl")
pytest.importorskip("typst")
pytest.importorskip("pypdfium2")

from kalillac_files import FORMATS, GeneratedFile, SpecError, generate  # noqa: E402
from kalillac_files import generation as generate_module, spec as spec_module  # noqa: E402
from kalillac_files.formulas import FUNCTIONS, is_safe_formula  # noqa: E402
from kalillac_files.generation import safe_filename  # noqa: E402

CANDIDATE = Path(__file__).resolve().parents[1]

DOCUMENT = {
    "title": "Quarterly Review — Café Operations",
    "blocks": [
        {"type": "heading", "level": 1, "text": "Summary"},
        {"type": "paragraph", "text": "Revenue grew 12% while costs held steady.\nSecond line of the summary."},
        {"type": "heading", "level": 2, "text": "Priorities"},
        {"type": "list", "ordered": False, "items": ["Open the second site", "Renegotiate the lease"]},
        {"type": "list", "ordered": True, "items": ["Hire a manager", "Train the team", "Launch"]},
        {"type": "heading", "level": 3, "text": "Figures"},
        {"type": "table", "header": ["Item", "Q1", "Q2"],
         "rows": [["Coffee", "1,200", "1,450"], ["Pastry", "640", "<b>710</b> & more"]]},
        {"type": "page_break"},
        {"type": "paragraph", "text": "Closing remarks on the following page."},
    ],
}
WORKBOOK = {
    "title": "Loan Model",
    "sheets": [
        {"name": "Inputs",
         "columns": [{"header": "Name", "format": "text", "width": 24}, {"header": "Value", "format": "number"},
                     {"header": "Share", "format": "percent"}, {"header": "Starts", "format": "date"},
                     {"header": "Units", "format": "integer"}, {"header": "Cost", "format": "currency"}],
         "rows": [["Principal", 250000, 0.8, "2031-01-15", 12, 1999.5],
                  ["Rate", 0.045, None, "2031-02-28", 360, 0.25],
                  ["=not a formula", 3.5, 0.125, "2032-02-29", -4, 10],
                  ["Total", {"formula": "=SUM(B2:B4)"}, {"formula": "=AVERAGE(C2:C4)"}, None, True,
                   {"formula": "=ROUND(PMT(B3/12,E3,-B2),2)"}]]},
        {"name": "Notes & Sources",
         "columns": [{"header": "Note", "format": "text"}],
         "rows": [["Synthetic figures."], ["+1 555 0100"], ["@mention"], ["日本語のメモ"]]},
    ],
}


def make(spec, format):
    result = generate(copy.deepcopy(spec), format)
    assert type(result) is GeneratedFile and type(result.data) is bytes and result.data
    return result


# --- PDF ------------------------------------------------------------------------------------


def _pdf_text(data):
    reader = pypdf.PdfReader(io.BytesIO(data))
    return reader, [page.extract_text() for page in reader.pages]


def test_pdf_is_a_readable_pdf_with_every_part_of_the_document():
    result = make(DOCUMENT, "pdf")
    assert (result.filename, result.media_type) == ("quarterly-review-cafe-operations.pdf", "application/pdf")
    assert result.data.startswith(b"%PDF-") and result.data.rstrip().endswith(b"%%EOF")
    reader, pages = _pdf_text(result.data)
    assert len(pages) == 2                                   # The page break produced a second page.
    assert reader.metadata.title == DOCUMENT["title"] and reader.metadata.author == "Kalillac AI"
    first = " ".join(pages[0].split())
    for expected in ("Quarterly Review — Café Operations", "Summary", "Revenue grew 12% while costs held steady.",
                     "Second line of the summary.", "Priorities", "Open the second site", "Renegotiate the lease",
                     "Hire a manager", "Launch", "Figures", "ITEM", "Coffee", "1,450", "Pastry"):
        assert expected in first, expected
    assert "Closing remarks on the following page." in " ".join(pages[1].split())
    assert "Closing remarks" not in first


def test_pdf_text_is_never_interpreted_as_markup():
    _reader, pages = _pdf_text(make(DOCUMENT, "pdf").data)
    assert "<b>710</b> & more" in " ".join(pages[0].split())    # Shown literally, not rendered bold.
    hostile = {"title": "<para>&amp;</para>", "blocks": [
        {"type": "paragraph", "text": "<font size='99'>x</font> <a href='http://example.test'>link</a> <br/> &lt;"}]}
    _reader, pages = _pdf_text(make(hostile, "pdf").data)
    text = " ".join(pages[0].split())
    assert "<para>&amp;</para>" in text and "<font size='99'>x</font>" in text and "<br/> &lt;" in text


def test_pdf_is_reproducible():
    assert make(DOCUMENT, "pdf").data == make(DOCUMENT, "pdf").data


@pytest.mark.parametrize("text", ["日本語のテキスト", "中文文本", "한국어 텍스트", "Привет, мир", "Łódź i Kraków",
                                  "Ελληνικά", "Tiếng Việt", "العربية", "עברית", "हिन्दी", "ภาษาไทย"])
def test_pdf_carries_every_bundled_script(text):
    for spec in ({"title": text, "blocks": [{"type": "paragraph", "text": "plain"}]},
                 {"title": "Plain", "blocks": [{"type": "paragraph", "text": text}]},
                 {"title": "Plain", "blocks": [{"type": "table", "header": ["A"], "rows": [[text]]}]},
                 {"title": "Plain", "blocks": [{"type": "list", "ordered": True, "items": [text]}]}):
        _reader, pages = _pdf_text(make(spec, "pdf").data)
        assert "\ufffd" not in pages[0]
    # The words come back out of the text layer: the glyphs are real text, not pictures or boxes.
    _reader, pages = _pdf_text(make({"title": "Plain", "blocks": [{"type": "paragraph", "text": text}]}, "pdf").data)
    assert all(word in pages[0] for word in text.split() if word.isalpha() and word.isascii())


@pytest.mark.parametrize("text", ["emoji 😀", "\U0001f680 launch", "cuneiform \U00012000"])
def test_pdf_refuses_text_no_bundled_font_can_show(text):
    for spec in ({"title": text, "blocks": [{"type": "paragraph", "text": "plain"}]},
                 {"title": "Plain", "blocks": [{"type": "table", "header": ["A"], "rows": [[text]]}]}):
        with pytest.raises(SpecError) as refused:
            generate(spec, "pdf")
        assert refused.value.code == "pdf_unsupported_characters" and text not in str(refused.value)
    # The same document is fully supported where the format can carry it.
    assert make({"title": "Plain", "blocks": [{"type": "paragraph", "text": text}]}, "docx").data


def test_pdf_handles_a_long_table_across_pages():
    rows = [[f"Row {n}", "value " * 6] for n in range(400)]
    result = make({"title": "Long", "blocks": [{"type": "table", "header": ["Name", "Detail"], "rows": rows}]}, "pdf")
    reader, pages = _pdf_text(result.data)
    assert len(pages) > 5 and "Row 0" in pages[0] and "Row 399" in pages[-1]
    assert all("NAME" in page for page in pages)             # The header repeats on every page.


def test_pdf_breaks_a_table_cell_taller_than_a_page_across_pages():
    from kalillac_files import quality
    for cell in ("wordy " * 330, "x" * 2000):
        spec = {"title": "Wide", "blocks": [{"type": "table", "header": ["h"] * 12, "rows": [[cell] * 12]}]}
        result = make(spec, "pdf")
        report = quality.inspect_pdf(result.data)
        assert report.sound and report.pages > 1, report       # Continued, never clipped past the margin.
        assert make(spec, "docx").data


# --- DOCX -----------------------------------------------------------------------------------


def test_docx_is_an_editable_word_document_with_real_structure():
    result = make(DOCUMENT, "docx")
    assert result.filename == "quarterly-review-cafe-operations.docx"
    assert result.media_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    with zipfile.ZipFile(io.BytesIO(result.data)) as archive:
        assert archive.testzip() is None
        assert {"[Content_Types].xml", "word/document.xml", "word/styles.xml"} <= set(archive.namelist())
    document = docx.Document(io.BytesIO(result.data))
    assert document.core_properties.title == DOCUMENT["title"] and document.core_properties.author == "Kalillac AI"
    styled = [(p.style.name, p.text) for p in document.paragraphs if p.text]
    assert styled[0] == ("Title", "Quarterly Review — Café Operations")
    assert ("Heading 1", "Summary") in styled and ("Heading 2", "Priorities") in styled
    assert ("Heading 3", "Figures") in styled
    assert ("Normal", "Revenue grew 12% while costs held steady.\nSecond line of the summary.") in styled
    assert [text for style, text in styled if style == "List Bullet"] == ["Open the second site",
                                                                          "Renegotiate the lease"]
    assert [text for style, text in styled if style == "List Number"] == ["Hire a manager", "Train the team", "Launch"]
    [table] = document.tables
    assert [[cell.text for cell in row.cells] for row in table.rows] == [
        ["Item", "Q1", "Q2"], ["Coffee", "1,200", "1,450"], ["Pastry", "640", "<b>710</b> & more"]]
    # Headers display in capitals through formatting; the stored text stays as written.
    assert all(run.font.all_caps for cell in table.rows[0].cells for run in cell.paragraphs[0].runs)
    # The header row is set in the semibold face of the design system.
    assert all(run._r.rPr.rFonts.get(
        "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}ascii") == "Inter SemiBold"
        for cell in table.rows[0].cells for run in cell.paragraphs[0].runs)
    assert "w:br w:type=\"page\"" in document.element.xml    # A real page break, not blank lines.


def test_docx_carries_any_unicode_text():
    spec = {"title": "多言語", "blocks": [{"type": "paragraph", "text": "日本語 Привет Łódź 😀 العربية"},
                                       {"type": "table", "header": ["列"], "rows": [["値"]]}]}
    result = make(spec, "docx")
    assert result.filename == "kalillac-document.docx"        # No ASCII letters in the title.
    document = docx.Document(io.BytesIO(result.data))
    assert "日本語 Привет Łódź 😀 العربية" in [p.text for p in document.paragraphs]
    assert document.tables[0].rows[1].cells[0].text == "値"


# --- XLSX -----------------------------------------------------------------------------------


def test_xlsx_is_a_real_workbook_with_formulas_formats_and_sheets():
    result = make(WORKBOOK, "xlsx")
    assert result.filename == "loan-model.xlsx"
    assert result.media_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    with zipfile.ZipFile(io.BytesIO(result.data)) as archive:
        assert archive.testzip() is None
        assert {"[Content_Types].xml", "xl/workbook.xml", "xl/worksheets/sheet1.xml",
                "xl/worksheets/sheet2.xml"} <= set(archive.namelist())
    book = openpyxl.load_workbook(io.BytesIO(result.data))    # Formulas as written, not cached values.
    assert book.sheetnames == ["Inputs", "Notes & Sources"]
    assert book.properties.title == "Loan Model" and book.properties.creator == "Kalillac AI"
    page = book["Inputs"]
    assert [cell.value for cell in page[1]] == ["Name", "Value", "Share", "Starts", "Units", "Cost"]
    # Header frozen, and the label column too once a table is wide enough to scroll sideways.
    assert all(cell.font.bold for cell in page[1]) and page.freeze_panes == "B2"
    assert int(page.column_dimensions["A"].width) == 24      # Stored with Excel's padding (24.71).

    # Formulas are formulas.
    for address, formula in (("B5", "=SUM(B2:B4)"), ("C5", "=AVERAGE(C2:C4)"), ("F5", "=ROUND(PMT(B3/12,E3,-B2),2)")):
        assert (page[address].value, page[address].data_type) == (formula, "f"), address
    # Numbers are numbers, with the column's number format.
    # Two decimals, because the same column holds 0.045: whole-number display would show it as 0.
    assert (page["B2"].value, page["B2"].data_type) == (250000, "n")
    assert page["B2"].number_format.startswith("#,##0.00") and page["B3"].number_format.startswith("#,##0.00")
    assert (page["C2"].value, page["C2"].number_format) == (0.8, "0.0%")
    assert (page["E3"].value, page["E3"].number_format) == (360, "#,##0;[Red]-#,##0")       # Negatives in red.
    assert (page["F2"].value, page["F2"].number_format) == (1999.5, '"$"#,##0.00_);[Red]("$"#,##0.00)')
    assert page["E5"].value is True and page["C3"].value is None and page["D5"].value is None
    # Dates are dates.
    assert page["D2"].value.date() == date(2031, 1, 15) and page["D4"].value.date() == date(2032, 2, 29)
    assert page["D2"].is_date and page["D2"].number_format == "d mmm yyyy"
    # Text is text, whatever it starts with: never a formula.
    assert (page["A4"].value, page["A4"].data_type) == ("=not a formula", "s")
    notes = book["Notes & Sources"]
    assert [(notes[f"A{n}"].value, notes[f"A{n}"].data_type) for n in (2, 3, 4, 5)] == [
        ("Synthetic figures.", "s"), ("+1 555 0100", "s"), ("@mention", "s"), ("日本語のメモ", "s")]

    # A spreadsheet program computes the formulas; here the raw XML proves they are stored as formulas.
    with zipfile.ZipFile(io.BytesIO(result.data)) as archive:
        sheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert "<f>SUM(B2:B4)</f>" in sheet and "<f>ROUND(PMT(B3/12,E3,-B2),2)</f>" in sheet
    assert "not a formula</f>" not in sheet


def test_xlsx_header_starting_with_equals_is_text():
    spec = {"title": "T", "sheets": [{"name": "S", "columns": [{"header": "=SUM(A2:A3)", "format": "number"}],
                                     "rows": [[1], [2]]}]}
    page = openpyxl.load_workbook(io.BytesIO(make(spec, "xlsx").data))["S"]
    assert (page["A1"].value, page["A1"].data_type) == ("=SUM(A2:A3)", "s")


# --- CSV, Markdown, text, JSON --------------------------------------------------------------


def test_csv_neutralises_formula_injection_and_keeps_real_formulas():
    spec = {"title": "Export", "sheets": [copy.deepcopy(WORKBOOK["sheets"][0])]}
    spec["sheets"][0]["rows"] += [['=HYPERLINK("http://example.test","x")', None, None, None, None, None],
                                  ["-2+3", None, None, None, None, None], ['say "hi", ok', None, None, None, None, None]]
    result = make(spec, "csv")
    assert (result.filename, result.media_type) == ("export.csv", "text/csv; charset=utf-8")
    assert result.data.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(result.data[3:].decode("utf-8"), newline="")))
    assert rows[0] == ["Name", "Value", "Share", "Starts", "Units", "Cost"]
    assert rows[1] == ["Principal", "250000", "0.8", "2031-01-15", "12", "1999.5"]
    assert rows[3][0] == "'=not a formula"                    # Text that looks like a formula is disarmed.
    assert rows[4] == ["Total", "=SUM(B2:B4)", "=AVERAGE(C2:C4)", "", "TRUE", "=ROUND(PMT(B3/12,E3,-B2),2)"]
    assert rows[5][0] == "'=HYPERLINK(\"http://example.test\",\"x\")" and rows[6][0] == "'-2+3"
    assert rows[7][0] == 'say "hi", ok'
    with pytest.raises(SpecError) as refused:
        generate(copy.deepcopy(WORKBOOK), "csv")
    assert refused.value.code == "csv_requires_one_sheet"


def test_markdown_text_and_json_outputs():
    markdown = make(DOCUMENT, "md")
    assert (markdown.filename, markdown.media_type) == ("quarterly-review-cafe-operations.md",
                                                        "text/markdown; charset=utf-8")
    text = markdown.data.decode("utf-8")
    assert text.startswith("# Quarterly Review — Café Operations\n\n## Summary\n")
    assert "- Open the second site\n- Renegotiate the lease\n" in text and "1. Hire a manager\n2. Train the team\n" in text
    assert "| Item | Q1 | Q2 |\n| --- | --- | --- |\n| Coffee | 1,200 | 1,450 |\n" in text and "\n---\n" in text
    assert "a \\| b" in make({"title": "T", "blocks": [{"type": "table", "header": ["h"], "rows": [["a | b"]]}]},
                             "md").data.decode("utf-8")

    plain = make(DOCUMENT, "txt")
    assert plain.media_type == "text/plain; charset=utf-8"
    assert plain.data.decode("utf-8").startswith("Quarterly Review — Café Operations\n==========")
    assert "* Open the second site" in plain.data.decode("utf-8")

    exported = make(WORKBOOK, "json")
    assert (exported.filename, exported.media_type) == ("loan-model.json", "application/json")
    parsed = json.loads(exported.data)
    assert parsed["title"] == "Loan Model" and [s["name"] for s in parsed["sheets"]] == ["Inputs", "Notes & Sources"]
    assert parsed["sheets"][0]["rows"][0] == {"Name": "Principal", "Value": 250000, "Share": 0.8,
                                             "Starts": "2031-01-15", "Units": 12, "Cost": 1999.5}
    assert parsed["sheets"][0]["rows"][3]["Value"] == {"formula": "=SUM(B2:B4)"}
    assert parsed["sheets"][1]["rows"][3] == {"Note": "日本語のメモ"}
    duplicate = {"title": "T", "sheets": [{"name": "S", "columns": [{"header": "A", "format": "text"}] * 2,
                                           "rows": [["x", "y"]]}]}
    with pytest.raises(SpecError) as refused:
        generate(duplicate, "json")
    assert refused.value.code == "json_requires_unique_headers"


def test_formats_are_exactly_the_supported_set_and_a_spec_fits_its_kind():
    assert sorted(FORMATS) == ["csv", "docx", "json", "md", "pdf", "pptx", "txt", "xlsx"]
    for format in ("ppt", "PDF", "html", "exe", "", None, 3, ["pdf"]):
        with pytest.raises(SpecError) as refused:
            generate(copy.deepcopy(DOCUMENT), format)
        assert refused.value.code == "unsupported_format"
    for spec, format in ((WORKBOOK, "pdf"), (WORKBOOK, "docx"), (DOCUMENT, "xlsx"), (DOCUMENT, "csv"),
                         (DOCUMENT, "pptx"), (WORKBOOK, "pptx")):
        with pytest.raises(SpecError) as refused:
            generate(copy.deepcopy(spec), format)
        assert refused.value.code == "invalid_structure"


def test_generation_never_changes_the_callers_specification():
    for spec, format in ((DOCUMENT, "pdf"), (DOCUMENT, "docx"), (WORKBOOK, "xlsx"), (WORKBOOK, "json")):
        given = copy.deepcopy(spec)
        generate(given, format)
        assert given == spec


# --- refused specifications -----------------------------------------------------------------


def _doc(*blocks, title="T"):
    return {"title": title, "blocks": list(blocks)}


def _book(columns, rows, name="S"):
    return {"title": "T", "sheets": [{"name": name, "columns": columns, "rows": rows}]}


TEXT = {"header": "A", "format": "text"}
NUMBER = {"header": "A", "format": "number"}


@pytest.mark.parametrize("spec, code", [
    (None, "invalid_structure"), ([], "invalid_structure"), ("{}", "invalid_structure"),
    ({"title": "T"}, "invalid_structure"),
    ({"title": "T", "blocks": [], "extra": 1}, "invalid_structure"),
    ({"title": "T", "blocks": []}, "invalid_blocks"),
    ({"title": "T", "blocks": "x"}, "invalid_blocks"),
    (_doc({"type": "paragraph", "text": "x"}, title=""), "invalid_title"),
    (_doc({"type": "paragraph", "text": "x"}, title="a\nb"), "invalid_title"),
    (_doc({"type": "paragraph", "text": "x"}, title="x" * 201), "invalid_title"),
    (_doc({"type": "paragraph", "text": "x"}, title=7), "invalid_title"),
    (_doc({"type": "script", "text": "x"}), "unknown_block"),
    (_doc({"type": "image", "url": "http://example.test/x.png"}), "unknown_block"),
    (_doc({"text": "x"}), "invalid_structure"),
    (_doc("paragraph"), "invalid_structure"),
    (_doc({"type": "paragraph", "text": "x", "style": "red"}), "invalid_structure"),
    (_doc({"type": "paragraph", "text": ""}), "invalid_paragraph"),
    (_doc({"type": "paragraph", "text": "x" * 20_001}), "invalid_paragraph"),
    (_doc({"type": "paragraph", "text": "bell \x07"}), "invalid_paragraph"),
    (_doc({"type": "paragraph", "text": "null \x00"}), "invalid_paragraph"),
    (_doc({"type": "paragraph", "text": b"bytes"}), "invalid_paragraph"),
    (_doc({"type": "heading", "level": 4, "text": "x"}), "invalid_heading"),
    (_doc({"type": "heading", "level": True, "text": "x"}), "invalid_heading"),
    (_doc({"type": "heading", "level": 1, "text": "a\nb"}), "invalid_heading"),
    (_doc({"type": "list", "ordered": "yes", "items": ["a"]}), "invalid_list"),
    (_doc({"type": "list", "ordered": True, "items": []}), "invalid_list"),
    (_doc({"type": "list", "ordered": True, "items": ["a", 2]}), "invalid_list"),
    (_doc({"type": "list", "ordered": True, "items": ["a"] * 501}), "invalid_list"),
    (_doc({"type": "table", "header": [], "rows": []}), "invalid_table"),
    (_doc({"type": "table", "header": ["a"] * 13, "rows": []}), "invalid_table"),
    (_doc({"type": "table", "header": ["a", "b"], "rows": [["only one"]]}), "invalid_table"),
    (_doc({"type": "table", "header": ["a"], "rows": [["x"]] * 501}), "invalid_table"),
    (_doc({"type": "table", "header": ["a"], "rows": [[1]]}), "invalid_table"),
    (_doc({"type": "page_break", "count": 3}), "invalid_structure"),
])
def test_invalid_documents_are_refused_with_a_stable_code(spec, code):
    for format in ("pdf", "docx", "md", "txt"):
        with pytest.raises(SpecError) as refused:
            generate(spec, format)
        assert refused.value.code == code and str(refused.value) == code


@pytest.mark.parametrize("spec, code", [
    ({"title": "T", "sheets": []}, "invalid_sheets"),
    ({"title": "T", "sheets": [{"name": "S", "columns": [TEXT], "rows": []}] * 21}, "invalid_sheets"),
    (_book([TEXT], [], name="a/b"), "invalid_sheet_name"),
    (_book([TEXT], [], name="[book]"), "invalid_sheet_name"),
    (_book([TEXT], [], name="x" * 32), "invalid_sheet_name"),
    (_book([TEXT], [], name=" padded "), "invalid_sheet_name"),
    (_book([TEXT], [], name="it's"), "invalid_sheet_name"),
    ({"title": "T", "sheets": [{"name": "Same", "columns": [TEXT], "rows": []},
                               {"name": "SAME", "columns": [TEXT], "rows": []}]}, "invalid_sheet_name"),
    (_book([], []), "invalid_column"),
    (_book([{"header": "A", "format": "money"}], []), "invalid_column"),
    (_book([{"header": "A", "format": "text", "width": 500}], []), "invalid_column"),
    (_book([{"header": "A", "format": "text", "colour": "red"}], []), "invalid_structure"),
    (_book([TEXT], [["a", "b"]]), "invalid_rows"),
    (_book([TEXT], ["not a row"]), "invalid_rows"),
    (_book([TEXT], [[1]]), "invalid_cell"),                       # A number in a text column.
    (_book([NUMBER], [["12"]]), "invalid_cell"),                  # Text in a numeric column.
    (_book([NUMBER], [[float("nan")]]), "invalid_cell"),
    (_book([NUMBER], [[float("inf")]]), "invalid_cell"),
    (_book([NUMBER], [[10**16]]), "invalid_cell"),
    (_book([{"header": "A", "format": "integer"}], [[1.5]]), "invalid_cell"),
    (_book([TEXT], [["x" * 2001]]), "invalid_cell"),
    (_book([TEXT], [[["nested"]]]), "invalid_cell"),
    (_book([{"header": "A", "format": "date"}], [["15/01/2031"]]), "invalid_date"),
    (_book([{"header": "A", "format": "date"}], [["2031-02-30"]]), "invalid_date"),
    (_book([NUMBER], [[{"formula": "=WEBSERVICE(\"http://example.test\")"}]]), "invalid_formula"),
    (_book([NUMBER], [[{"formula": "SUM(A1:A2)"}]]), "invalid_formula"),
    (_book([NUMBER], [[{"formula": "=SUM(A1:A2)", "cached": 3}]]), "invalid_formula"),
    (_book([NUMBER], [[{"value": 3}]]), "invalid_cell"),           # A typed value needs its format.
])
def test_invalid_workbooks_are_refused_with_a_stable_code(spec, code):
    with pytest.raises(SpecError) as refused:
        generate(spec, "xlsx")
    assert refused.value.code == code


def test_size_limits_are_enforced_before_rendering(monkeypatch):
    paragraph = {"type": "paragraph", "text": "x"}
    with pytest.raises(SpecError) as refused:
        generate({"title": "T", "blocks": [paragraph] * (spec_module.MAX_BLOCKS + 1)}, "txt")
    assert refused.value.code == "invalid_blocks"
    big = {"type": "paragraph", "text": "x" * 20_000}
    with pytest.raises(SpecError) as refused:
        generate({"title": "T", "blocks": [big] * 11}, "txt")     # 220,000 characters.
    assert refused.value.code == "document_too_large"
    assert make({"title": "T", "blocks": [big] * 10}, "txt").data  # 200,000 are accepted.
    table = {"type": "table", "header": ["a", "b", "c"], "rows": [["1", "2", "3"]] * 499}   # 1,500 cells.
    assert make({"title": "T", "blocks": [table] * 4}, "txt").data
    with pytest.raises(SpecError) as refused:
        generate({"title": "T", "blocks": [table] * 5}, "txt")     # 7,500 table cells.
    assert refused.value.code == "document_too_large"

    # A workbook over the cell limit is refused by counting, before any cell is examined.
    examined = []
    monkeypatch.setattr(spec_module, "_cell", lambda value, column: examined.append(1) or value)
    row = ["x"] * 100
    oversized = _book([TEXT] * 100, [row] * 501)                  # 50,100 cells.
    with pytest.raises(SpecError) as refused:
        generate(oversized, "xlsx")
    assert refused.value.code == "workbook_too_large" and examined == []
    with pytest.raises(SpecError) as refused:
        generate(_book([TEXT], [["x"]] * (spec_module.MAX_SHEET_ROWS + 1)), "csv")
    assert refused.value.code == "invalid_rows"


def test_an_oversized_result_is_refused(monkeypatch):
    monkeypatch.setattr(generate_module, "MAX_FILE_BYTES", 200)
    with pytest.raises(SpecError) as refused:
        generate(copy.deepcopy(DOCUMENT), "docx")
    assert refused.value.code == "file_too_large"
    assert make({"title": "T", "blocks": [{"type": "paragraph", "text": "x"}]}, "txt").data == b"T\n=\n\nx\n"


def test_the_largest_accepted_workbook_generates(monkeypatch):
    rows = [[f"row {n}", n, n / 7] for n in range(5000)]
    spec = _book([TEXT, {"header": "N", "format": "integer"}, {"header": "R", "format": "number"}], rows)
    spec["sheets"][0]["rows"].append(["Total", {"formula": "=SUM(B2:B5001)"}, {"formula": "=AVERAGE(C2:C5001)"}])
    page = openpyxl.load_workbook(io.BytesIO(make(spec, "xlsx").data))["S"]
    assert page.max_row == 5002 and page["B5002"].value == "=SUM(B2:B5001)" and page["A5001"].value == "row 4999"


# --- formulas -------------------------------------------------------------------------------


@pytest.mark.parametrize("formula", [
    "=SUM(B2:B4)", "=A1+B1", "=-A1*2", "=$A$1/B$2", "=ROUND(PMT(B3/12,E3,-B2),2)", "=IF(A1>0,\"yes\",\"no\")",
    "=IFERROR(VLOOKUP(A2,D:E,2,FALSE),0)", "=SUMIF(A:A,\">=10\",B:B)", "=A1&\" units\"", "=A1^2", "=B2*15%",
    "=Sheet2!A1", "='Notes & Sources'!A2", "=SUM(Inputs!B2:B4)", "=AND(A1<>B1,A1<=10)", "=NPV(0.05,B2:B6)+B1",
    "=IF(A1=\"=cmd|x\",1,0)", "=TODAY()", "=sum(a1:a3)", "=TRUE", "=COUNTIF(A:A,\"*x*\")",
])
def test_safe_formulas_are_accepted(formula):
    assert is_safe_formula(formula)
    # The formula sits in column F, outside every range it reads, beside the sheets it names.
    numbers = [{"header": name, "format": "number"} for name in "ABCDE"]
    workbook = {"title": "T", "sheets": [
        {"name": "S", "columns": numbers + [{"header": "F", "format": "number"}],
         "rows": [[1, 2, 3, 4, 5, {"formula": formula}]] + [[n, n, n, n, n, None] for n in range(6, 10)]},
        {"name": "Sheet2", "columns": [TEXT], "rows": [["x"]]},
        {"name": "Inputs", "columns": [TEXT, NUMBER], "rows": [["a", 1], ["b", 2], ["c", 3]]},
        {"name": "Notes & Sources", "columns": [TEXT], "rows": [["note"]]}]}
    make(workbook, "xlsx")


@pytest.mark.parametrize("formula, code", [
    ("=Nowhere!A1", "formula_reference_error"),      # A sheet that does not exist.
    ("=SUM(A2:A3)", "formula_circular"),             # Reads its own cell.
])
def test_structurally_broken_models_are_refused(formula, code):
    with pytest.raises(SpecError) as refused:
        generate(_book([NUMBER], [[{"formula": formula}], [1]]), "xlsx")
    assert refused.value.code == code


@pytest.mark.parametrize("formula", [
    "=cmd|' /C calc'!A0", "=MSEXCEL|'\\..\\..\\Windows\\System32\\cmd.exe'!''", "=DDE(\"cmd\",\"/C calc\",\"x\")",
    "=WEBSERVICE(\"http://example.test/\"&A1)", "=HYPERLINK(\"http://example.test\",\"click\")",
    "=IMPORTXML(\"http://example.test\",\"//a\")", "=INDIRECT(\"A1\")", "=CALL(\"kernel32\",\"x\",\"J\")",
    "=RTD(\"prog\",,\"x\")", "=FILTERXML(A1,\"//x\")", "=EXEC(\"calc\")", "=[book.xlsx]Sheet1!A1",
    "='C:\\[book.xlsx]Sheet1'!A1", "=SUM(A1:A2", "=SUM(A1:A2))", "=\"unterminated", "SUM(A1:A2)", "=", "",
    "=A1;B1", "=A1 #", "={1,2}", "=@SUM(A1)", "=Total*2", "=SUM(A1:A2)\n", "=" + "1+" * 300 + "1",
    "=SUM (A1:A2)",
    None, 7, ["=SUM(A1)"], "=LAMBDA(x,x)(1)", "=STDEV.S(A1:A3)", "=A1`", "=A1\\B1",
])
def test_unsafe_formulas_are_refused(formula):
    assert not is_safe_formula(formula)
    with pytest.raises(SpecError):
        generate(_book([NUMBER], [[{"formula": formula}]]), "xlsx")


def test_function_allowlist_has_no_function_that_leaves_the_workbook():
    assert not FUNCTIONS & {"WEBSERVICE", "HYPERLINK", "INDIRECT", "RTD", "CALL", "REGISTER", "EXEC", "DDE",
                            "FILTERXML", "IMPORTXML", "IMPORTDATA", "IMPORTRANGE", "IMAGE", "ENCODEURL", "OFFSET",
                            "INFO", "CELL", "EVALUATE", "LAMBDA", "LET"}
    assert all(name.isupper() and name.isalnum() for name in FUNCTIONS)


# --- file names -----------------------------------------------------------------------------


@pytest.mark.parametrize("title, expected", [
    ("Quarterly Report", "quarterly-report.pdf"),
    ("../../etc/passwd", "etc-passwd.pdf"),
    ("..\\..\\Windows\\system32\\config", "windows-system32-config.pdf"),
    ("C:\\autoexec.bat", "c-autoexec-bat.pdf"),
    ("report.exe", "report-exe.pdf"),
    ("  spaced   out  ", "spaced-out.pdf"),
    ("Café Münster 2031", "cafe-munster-2031.pdf"),
    ("日本語", "kalillac-document.pdf"),
    ("///", "kalillac-document.pdf"),
    ("CON", "kalillac-document.pdf"),
    ("nul", "kalillac-document.pdf"),
    ("COM1", "kalillac-document.pdf"),
    ("a" * 200, "a" * 60 + ".pdf"),
    ("line\u2028separator", "lineseparator.pdf"),
    (".hidden", "hidden.pdf"),
])
def test_file_names_are_built_by_allowlist(title, expected):
    name = safe_filename(title, "pdf")
    assert name == expected
    assert "/" not in name and "\\" not in name and ".." not in name and not name.startswith(".")
    assert name.isascii() and len(name) <= 64


def _app_outside_the_files_flag(source):
    """The application source outside its KALILLAC_FILES_ENABLED block, where kalillac_files is used."""
    start = source.index('if os.getenv("KALILLAC_FILES_ENABLED"')
    end = source.index('\n@api.get("/api/health")', start)
    return (source[:start] + source[end:]).replace("nothing in kalillac_files is imported", "")


def test_the_application_reaches_the_generation_core_only_behind_the_files_flag():
    source = (CANDIDATE / "app_fastapi_candidate.py").read_text(encoding="utf-8")
    assert "kalillac_files" not in _app_outside_the_files_flag(source)
    for package in ("kalillac_routing", "kalillac_accounts", "kalillac_billing", "kalillac_spaces", "kalillac_db"):
        for path in (CANDIDATE / package).rglob("*.py"):
            assert "kalillac_files" not in path.read_text(encoding="utf-8"), path.name
    # Importing the package loads no document library.
    import subprocess
    import sys
    probe = ("import sys, kalillac_files; "
             "print(sorted(m for m in ('typst', 'docx', 'openpyxl', 'pypdfium2') if m in sys.modules))")
    result = subprocess.run([sys.executable, "-c", probe], cwd=CANDIDATE, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0 and result.stdout.strip() == "[]", result.stderr[-500:]
