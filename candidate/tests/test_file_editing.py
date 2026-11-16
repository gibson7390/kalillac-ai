"""Editing uploaded Word, PowerPoint and Excel files: addresses, faithful edits, tracked changes,
preserved content, recalculated workbooks and refusals. Every fixture is built in the test;
nothing touches the application or the network.
"""
from __future__ import annotations

import copy
import io
import re
import zipfile

import pytest

docx = pytest.importorskip("docx")
pptx = pytest.importorskip("pptx")
openpyxl = pytest.importorskip("openpyxl")
xlsxwriter = pytest.importorskip("xlsxwriter")
pytest.importorskip("typst")

from kalillac_files import generate  # noqa: E402
from kalillac_files.editing import MAX_OPERATIONS, edit, outline  # noqa: E402
from kalillac_files.spec import SpecError  # noqa: E402
from quality_benchmark import EXECUTIVE_DECK, FORECAST_WORKBOOK  # noqa: E402
from test_document_design import REPORT  # noqa: E402
from test_file_intake import rezip  # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore::UserWarning")  # openpyxl cannot read x14 extensions.
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


@pytest.fixture(scope="module")
def word():
    return generate(copy.deepcopy(REPORT), "docx").data


@pytest.fixture(scope="module")
def deck():
    return generate(copy.deepcopy(EXECUTIVE_DECK), "pptx").data


@pytest.fixture(scope="module")
def book():
    return generate(copy.deepcopy(FORECAST_WORKBOOK), "xlsx").data


def refused(data, name, operations, **options):
    with pytest.raises(SpecError) as error:
        edit(data, name, operations, **options)
    return error.value.code


def body_paragraphs(data):
    return list(docx.Document(io.BytesIO(data)).paragraphs)


def paragraph_xml(data):
    xml = zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml").decode("utf-8")
    return re.findall(r"<w:p[ >].*?</w:p>", xml, re.S)


def unit(found, address):
    return next(u for u in found.units if u.address == address)


def address_of(found, starts):
    return next(u for u in found.units if u.kind == "paragraph" and u.text.startswith(starts)
                and "\n" not in u.text).address


# --- outlines --------------------------------------------------------------------------------


def test_a_word_outline_addresses_paragraphs_tables_and_cells(word):
    found = outline(word, "report.docx")
    assert found.kind == "docx" and not found.truncated
    heading = next(u for u in found.units if u.text == "The year at a glance")
    assert heading.kind == "paragraph" and heading.detail.lower() == "heading 1"
    assert re.fullmatch(r"p\d+", heading.address)
    tables = [u for u in found.units if u.kind == "table"]
    assert tables and all(re.fullmatch(r"t\d+", u.address) for u in tables)
    line_item = next(u for u in found.units if u.kind == "cell" and u.text == "Line item")
    assert re.fullmatch(r"t\d+\.r1\.c1", line_item.address)


def test_a_deck_outline_addresses_slides_shapes_paragraphs_cells_and_notes(deck):
    found = outline(deck, "board.pptx")
    assert found.kind == "pptx"
    assert unit(found, "s1").kind == "slide"
    title = next(u for u in found.units if u.text == EXECUTIVE_DECK["title"] and u.kind == "shape")
    assert title.address.startswith("s1.sh") and title.detail == "Title"
    assert unit(found, "s1.notes").text == EXECUTIVE_DECK["slides"][0]["notes"]
    assert any(u.kind == "cell" and u.text == "Contractual" for u in found.units)
    assert not any(u.kind == "shape" and not u.text.strip() for u in found.units)   # Empty shapes are not listed.


def test_a_workbook_outline_addresses_sheets_and_cells_with_their_formulas(book):
    found = outline(book, "model.xlsx")
    assert found.kind == "xlsx"
    assert [u.address for u in found.units if u.kind == "sheet"] == ["About", "Dashboard", "Inputs", "Forecast",
                                                                      "Summary", "Loan"]
    assert unit(found, "Inputs!A2").text == "Customers at launch" and unit(found, "Inputs!B2").text == "1200"
    assert any(u.detail.startswith("=") and u.address.startswith("Forecast!") for u in found.units)


def test_only_word_powerpoint_and_excel_files_can_be_edited(word):
    pdf = generate(copy.deepcopy(REPORT), "pdf").data
    for data, name in ((pdf, "report.pdf"), (b"plain text", "notes.txt")):
        with pytest.raises(SpecError) as error:
            outline(data, name)
        assert error.value.code == "edit_unsupported_type"
    operation = [{"op": "replace_text", "find": "a", "replace": "b"}]
    assert refused(pdf, "report.pdf", operation) == "edit_unsupported_type"
    assert refused(rezip(word, add={"word/vbaProject.bin": b"\xd0\xcf"}), "x.docx", operation) == "macros_not_allowed"
    assert refused(word, "x.xlsx", operation) == "type_mismatch"


# --- Word ------------------------------------------------------------------------------------


def test_replacing_text_keeps_the_formatting_of_the_text_it_replaces(word):
    result = edit(word, "Annual review.docx", [{"op": "replace_text", "find": "$18.4M", "replace": "$18.6M"}])
    assert result.filename == "annual-review-edited.docx"
    paragraphs = body_paragraphs(result.data)
    text = "".join(re.findall(r"<w:t(?: [^>]*)?>([^<]*)</w:t>", document_xml(result.data)))  # Tables too.
    assert "$18.4M" not in text and text.count("$18.6M") >= 3
    reached = next(p for p in paragraphs if "Revenue reached" in p.text)
    assert "".join(run.text for run in reached.runs if run.bold) == "Revenue reached $18.6M"  # Still bold.


def test_a_match_spanning_runs_is_replaced_and_formatting_elsewhere_is_untouched(word):
    result = edit(word, "x.docx", [{"op": "replace_text", "find": "strongest year. Revenue",
                                    "replace": "best year yet. Sales", "expect": 1}])
    paragraph = next(p for p in body_paragraphs(result.data) if "best year yet" in p.text)
    assert paragraph.text == "2026 was the best year yet. Sales reached $18.4M."
    assert [run.text for run in paragraph.runs if run.bold] == [" reached $18.4M"]


def test_untouched_paragraphs_are_byte_for_byte_the_same(word):
    before = paragraph_xml(word)
    after = paragraph_xml(edit(word, "x.docx", [{"op": "replace_text", "find": "Financial performance",
                                                  "replace": "Results"}]).data)
    assert len(before) == len(after)
    changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
    assert changed and all("Results" in after[i] for i in changed)


def test_case_insensitive_matching_and_counts_that_guard_against_surprises(word):
    assert edit(word, "x.docx", [{"op": "replace_text", "find": "THE PLAN", "replace": "The roadmap",
                                  "match_case": False}]).data
    assert refused(word, "x.docx", [{"op": "replace_text", "find": "THE PLAN", "replace": "x"}]) == "edit_no_match"
    assert refused(word, "x.docx", [{"op": "replace_text", "find": "$18.4M", "replace": "x", "expect": 1}]) \
        == "edit_match_count"


def test_set_insert_and_delete_paragraphs_by_address(word):
    found = outline(word, "x.docx")
    heading = address_of(found, "The year at a glance")
    first = address_of(found, "2026 was the strongest year")
    result = edit(word, "x.docx", [
        {"op": "set_text", "at": first, "text": "A record year."},
        {"op": "insert_paragraph", "after": first, "text": "Restated after the audit."},
        {"op": "insert_paragraph", "before": heading, "text": "Restated figures", "style": "Heading 2"},
    ])
    paragraphs = body_paragraphs(result.data)
    texts = [p.text for p in paragraphs]
    at = texts.index("A record year.")
    assert texts[at + 1] == "Restated after the audit."
    assert texts[texts.index("The year at a glance") - 1] == "Restated figures"
    assert paragraphs[texts.index("Restated figures")].style.name == "Heading 2"
    # The inserted paragraph takes its neighbour's paragraph formatting.
    assert paragraphs[at + 1].paragraph_format.space_after == paragraphs[at].paragraph_format.space_after
    removed = edit(word, "x.docx", [{"op": "delete", "at": first}])
    assert not any(p.text.startswith("2026 was the strongest year") for p in body_paragraphs(removed.data))
    assert refused(word, "x.docx", [{"op": "insert_paragraph", "after": first, "text": "x", "style": "Nope"}]) \
        == "edit_unknown_style"


def test_addresses_refer_to_the_upload_whatever_the_order_of_operations(word):
    found = outline(word, "x.docx")
    heading = address_of(found, "The year at a glance")
    first = address_of(found, "2026 was the strongest year")
    one = edit(word, "x.docx", [{"op": "insert_paragraph", "before": heading, "text": "New"},
                                {"op": "set_text", "at": first, "text": "Changed"}]).data
    two = edit(word, "x.docx", [{"op": "set_text", "at": first, "text": "Changed"},
                                {"op": "insert_paragraph", "before": heading, "text": "New"}]).data
    assert [p.text for p in body_paragraphs(one)] == [p.text for p in body_paragraphs(two)]
    assert refused(word, "x.docx", [{"op": "delete", "at": first}, {"op": "set_text", "at": first, "text": "x"}]) \
        == "edit_conflict"


def line_items(data):
    return next(t for t in docx.Document(io.BytesIO(data)).tables if t.rows[0].cells[0].text in ("Line item", "Item"))


def test_table_cells_and_rows(word):
    found = outline(word, "x.docx")
    cell = next(u for u in found.units if u.kind == "cell" and u.text == "Line item").address
    table = cell.split(".")[0]
    width = len(line_items(word).rows[1].cells)
    cells = ["Freight"] + ["1.1"] * (width - 1)
    result = edit(word, "x.docx", [{"op": "set_text", "at": cell, "text": "Item"},
                                   {"op": "add_row", "after": f"{table}.r2", "cells": cells}])
    target = line_items(result.data)
    assert target.rows[0].cells[0].text == "Item" and [c.text for c in target.rows[2].cells] == cells
    # The new row copies its neighbour's formatting: numbers stay aligned as before.
    assert target.rows[2].cells[1].paragraphs[0].alignment == target.rows[1].cells[1].paragraphs[0].alignment
    assert refused(word, "x.docx", [{"op": "add_row", "after": f"{table}.r2", "cells": ["one"]}]) == "edit_row_width"
    deleted = edit(word, "x.docx", [{"op": "delete", "at": f"{table}.r2"}])
    assert len(line_items(deleted.data).rows) == len(line_items(word).rows) - 1
    assert refused(word, "x.docx", [{"op": "set_text", "at": table, "text": "x"}]) == "edit_address_wrong_kind"
    assert refused(word, "x.docx", [{"op": "set_text", "at": "p9999", "text": "x"}]) == "edit_address_not_found"


def accepted(xml):
    """Document text with every revision accepted: deletions dropped, insertions kept."""
    xml = re.sub(r"<w:del [^>]*>.*?</w:del>", "", xml, flags=re.S)
    return "".join(re.findall(r"<w:t(?: [^>]*)?>([^<]*)</w:t>", xml))


def rejected(xml):
    """Document text with every revision rejected: insertions dropped, deletions restored."""
    xml = re.sub(r"<w:ins [^>]*>.*?</w:ins>", "", xml, flags=re.S)
    return "".join(re.findall(r"<w:(?:t|delText)(?: [^>]*)?>([^<]*)</w:(?:t|delText)>", xml))


def document_xml(data):
    return zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml").decode("utf-8")


def test_tracked_changes_accept_to_the_edit_and_reject_to_the_original(word):
    first = address_of(outline(word, "x.docx"), "2026 was the strongest year")
    operations = [{"op": "replace_text", "find": "$18.4M", "replace": "$18.6M"},
                  {"op": "set_text", "at": first, "text": "A record year."},
                  {"op": "insert_paragraph", "after": first, "text": "Restated after the audit."}]
    plain = document_xml(edit(word, "x.docx", operations).data)
    xml = document_xml(edit(word, "x.docx", operations, track_changes=True,
                            revision_date="2026-11-15T09:00:00Z").data)
    assert 'w:author="Kalillac AI"' in xml and 'w:date="2026-11-15T09:00:00Z"' in xml
    assert "<w:delText" in xml and "<w:ins " in xml
    assert accepted(xml) == accepted(plain)                        # Accept all: exactly the plain edit.
    assert rejected(xml) == rejected(document_xml(word))           # Reject all: exactly the upload.
    ids = re.findall(r'<w:(?:ins|del) w:id="(\d+)"', xml)
    assert len(ids) == len(set(ids))                               # Every revision has its own id.
    assert re.search(r"<w:pPr>(?:(?!</w:pPr>).)*<w:rPr><w:ins ", xml, re.S)   # The new paragraph mark too.


def test_tracked_deletions_of_rows_and_paragraphs_are_marked_not_removed(word):
    found = outline(word, "x.docx")
    first = address_of(found, "2026 was the strongest year")
    table = next(u for u in found.units if u.kind == "cell" and u.text == "Line item").address.split(".")[0]
    tracked = edit(word, "x.docx", [{"op": "delete", "at": first}, {"op": "delete", "at": f"{table}.r2"}],
                   track_changes=True).data
    xml = document_xml(tracked)
    assert "2026 was the strongest year" in rejected(xml) and "2026 was the strongest year" not in accepted(xml)
    assert re.search(r"<w:trPr>(?:(?!</w:trPr>).)*<w:del ", xml, re.S)
    assert len(paragraph_xml(tracked)) == len(paragraph_xml(word))  # Marked, not removed.


def test_a_section_break_paragraph_cannot_be_deleted(word):
    body = docx.Document(io.BytesIO(word)).element.body
    blocks = [child for child in body if child.tag in (W + "p", W + "tbl")]
    paragraphs = [child for child in blocks if child.tag == W + "p"]
    with_break = [index for index, p in enumerate(paragraphs, start=1)
                  if p.find(W + "pPr") is not None and p.find(W + "pPr").find(W + "sectPr") is not None]
    assert with_break                                            # Cover and contents end in section breaks.
    assert refused(word, "x.docx", [{"op": "delete", "at": f"p{with_break[0]}"}]) == "edit_not_allowed"


# --- PowerPoint ------------------------------------------------------------------------------


def subtitle_run(data):
    shape = next(shape for shape in pptx.Presentation(io.BytesIO(data)).slides[-1].shapes if shape.name == "Subtitle")
    return shape.text_frame.paragraphs[0].runs[0]


def test_deck_text_is_replaced_everywhere_including_notes_keeping_formatting(deck):
    result = edit(deck, "Board deck.pptx", [{"op": "replace_text", "find": "Riverside", "replace": "Harbourside"}])
    assert result.filename == "board-deck-edited.pptx"
    presentation = pptx.Presentation(io.BytesIO(result.data))
    texts = [shape.text_frame.text for slide in presentation.slides for shape in slide.shapes if shape.has_text_frame]
    assert not any("Riverside" in text for text in texts) and any("Harbourside lease" in text for text in texts)
    assert "Harbourside" in presentation.slides[0].notes_slide.notes_text_frame.text
    run, original = subtitle_run(result.data), subtitle_run(deck)
    assert (run.font.size, run.font.color.rgb, run.font.name) == (original.font.size, original.font.color.rgb,
                                                                   original.font.name)


def test_deck_shapes_cells_and_notes_are_set_by_address(deck):
    found = outline(deck, "x.pptx")
    subtitle = next(u for u in found.units
                    if u.kind == "shape" and u.detail == "Subtitle" and u.address.startswith("s1."))
    cell = next(u for u in found.units if u.kind == "cell" and u.text == "Contractual")
    result = edit(deck, "x.pptx", [{"op": "set_text", "at": subtitle.address, "text": "Why we double capacity"},
                                   {"op": "set_text", "at": cell.address, "text": "Third party"},
                                   {"op": "set_text", "at": "s1.notes", "text": "Lead with the decision."}])
    again = outline(result.data, "x.pptx")
    assert unit(again, subtitle.address).text == "Why we double capacity"
    assert unit(again, cell.address).text == "Third party"
    assert unit(again, "s1.notes").text == "Lead with the decision."
    assert refused(deck, "x.pptx", [{"op": "set_text", "at": "s1", "text": "x"}]) == "edit_address_wrong_kind"
    assert refused(deck, "x.pptx", [{"op": "insert_paragraph", "after": "s1.sh1.p1", "text": "x"}]) == "invalid_edit"
    assert refused(deck, "x.pptx", [{"op": "replace_text", "find": "x", "replace": "y"}], track_changes=True) \
        == "invalid_edit"


# --- Excel -----------------------------------------------------------------------------------


def cached(data):
    values = {}
    workbook = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    formulas = openpyxl.load_workbook(io.BytesIO(data), data_only=False)
    for sheet in formulas.worksheets:
        for row in sheet.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and cell.value.startswith("="):
                    values[(sheet.title, cell.coordinate)] = workbook[sheet.title][cell.coordinate].value
    return values


def test_changing_an_input_recomputes_every_formula_exactly_as_a_fresh_build(book):
    edited = edit(book, "forecast.xlsx", [{"op": "set_cell", "at": "Inputs!B2", "value": 1500}]).data
    spec = copy.deepcopy(FORECAST_WORKBOOK)
    spec["sheets"][1]["rows"][0][1]["value"] = 1500
    expected, found = cached(generate(spec, "xlsx").data), cached(edited)
    assert found.keys() == expected.keys() and len(found) > 400
    for key, value in expected.items():
        if isinstance(value, float):
            assert found[key] == pytest.approx(value, rel=1e-12, abs=1e-9), key
        else:
            assert found[key] == value, key
    assert found != cached(book)                                    # The change did reach the results.
    assert 'fullCalcOnLoad="1"' in zipfile.ZipFile(io.BytesIO(edited)).read("xl/workbook.xml").decode()


def test_everything_the_edit_does_not_touch_is_kept_byte_for_byte(book):
    edited = edit(book, "forecast.xlsx", [{"op": "set_cell", "at": "Inputs!B2", "value": 1500}]).data
    before, after = zipfile.ZipFile(io.BytesIO(book)), zipfile.ZipFile(io.BytesIO(edited))
    kept = [name for name in before.namelist() if name.startswith(("xl/charts/", "xl/drawings/", "xl/styles",
                                                                     "xl/theme/", "xl/sharedStrings"))]
    assert len(kept) >= 4 and all(before.read(name) == after.read(name) for name in kept)
    original = {cell.coordinate: cell.number_format for cell in openpyxl.load_workbook(io.BytesIO(book))["Inputs"]["B"]}
    assert {cell.coordinate: cell.number_format
            for cell in openpyxl.load_workbook(io.BytesIO(edited))["Inputs"]["B"]} == original


def test_text_and_formulas_are_set_safely(book):
    edited = edit(book, "f.xlsx", [
        {"op": "set_cell", "at": "Inputs!C2", "value": "=HYPERLINK(\"http://x\")"},     # Text, never a formula.
        {"op": "set_cell", "at": "Inputs!D2", "formula": "=IFS(B2>1000,\"large\",TRUE,\"small\")"},
        {"op": "set_cell", "at": "Inputs!D3", "value": True},
        {"op": "set_cell", "at": "Inputs!D4", "value": None},
    ]).data
    sheet = openpyxl.load_workbook(io.BytesIO(edited))["Inputs"]
    values = openpyxl.load_workbook(io.BytesIO(edited), data_only=True)["Inputs"]
    assert sheet["C2"].value == "=HYPERLINK(\"http://x\")" and sheet["C2"].data_type == "s"
    archive = zipfile.ZipFile(io.BytesIO(edited))
    sheets = "".join(archive.read(name).decode() for name in archive.namelist() if name.startswith("xl/worksheets/s"))
    assert "<f>_xlfn.IFS(" in sheets                                  # Or Excel would show #NAME?.
    assert values["D2"].value == "large" and values["D3"].value is True and sheet["D4"].value is None
    for operation, code in (({"at": "Inputs!D2", "formula": "=WEBSERVICE(\"x\")"}, "invalid_formula"),
                            ({"at": "Inputs!D2", "formula": "=Nowhere!A1"}, "formula_reference_error"),
                            ({"at": "Nowhere!A1", "value": 1}, "edit_address_not_found"),
                            ({"at": "Inputs!ZZZZ1", "value": 1}, "edit_address_not_found"),
                            ({"at": "Inputs!A1", "value": float("nan")}, "invalid_edit")):
        assert refused(book, "f.xlsx", [{"op": "set_cell", **operation}]) == code, operation


def test_text_replacement_in_a_workbook_never_touches_formulas_or_other_cells(book):
    edited = edit(book, "f.xlsx", [{"op": "replace_text", "find": "customers", "replace": "clients",
                                    "match_case": False, "in": "Inputs"}]).data
    assert openpyxl.load_workbook(io.BytesIO(edited))["Inputs"]["A2"].value == "clients at launch"
    forecast = openpyxl.load_workbook(io.BytesIO(edited))["Forecast"]
    original = openpyxl.load_workbook(io.BytesIO(book))["Forecast"]
    def values(sheet):
        return [cell.value for row in sheet.iter_rows() for cell in row]
    assert values(forecast) == values(original)
    one = edit(book, "f.xlsx", [{"op": "replace_text", "find": "launch", "replace": "day one", "in": "Inputs!A2"}]).data
    assert openpyxl.load_workbook(io.BytesIO(one))["Inputs"]["A2"].value == "Customers at day one"


def foreign_workbook():
    """A workbook as other software writes it: a table, a merged range, an array formula, a defined
    name, a chart, and a shared formula (written in by hand, as Excel does)."""
    buffer = io.BytesIO()
    workbook = xlsxwriter.Workbook(buffer, {"in_memory": True})
    sheet = workbook.add_worksheet("Data")
    sheet.add_table("A1:C4", {"columns": [{"header": "Item"}, {"header": "Units"}, {"header": "Price"}],
                              "data": [["Beans", 10, 4.5], ["Milk", 20, 1.5], ["Cups", 300, 0.05]]})
    sheet.merge_range("E1:F1", "Merged", workbook.add_format({"bold": True}))
    sheet.write_array_formula("E3:E4", "{=B2:B3*2}")
    workbook.define_name("Rate", "=Data!$C$2")
    for row in (2, 3, 4):
        sheet.write_formula(f"G{row}", f"=B{row}*C{row}")
    sheet.write_formula("H2", "=SUM(G2:G4)")
    chart = workbook.add_chart({"type": "column"})
    chart.add_series({"values": "=Data!$B$2:$B$4", "categories": "=Data!$A$2:$A$4"})
    sheet.insert_chart("J2", chart)
    workbook.close()

    def shared(xml):
        xml = xml.decode("utf-8")
        xml = re.sub(r'(<c r="G2"[^>]*>)<f>B2\*C2</f>', r'\1<f t="shared" ref="G2:G4" si="0">B2*C2</f>', xml)
        xml = re.sub(r'(<c r="G3"[^>]*>)<f>B3\*C3</f>', r'\1<f t="shared" si="0"/>', xml)
        xml = re.sub(r'(<c r="G4"[^>]*>)<f>B4\*C4</f>', r'\1<f t="shared" si="0"/>', xml)
        return xml.encode("utf-8")
    return rezip(buffer.getvalue(), change={"xl/worksheets/sheet1.xml": shared})


def computable_foreign_workbook():
    """The same workbook without what the engine cannot compute (the array formula)."""
    return rezip(foreign_workbook(), change={"xl/worksheets/sheet1.xml": lambda xml: re.sub(
        rb'<f t="array"[^>]*>[^<]*</f>', b"", xml)})


def test_shared_formulas_survive_an_edit_and_are_recomputed():
    data = computable_foreign_workbook()
    assert b'si="0"/>' in zipfile.ZipFile(io.BytesIO(data)).read("xl/worksheets/sheet1.xml")
    edited = edit(data, "d.xlsx", [{"op": "set_cell", "at": "Data!B3", "value": 25}]).data
    sheet = openpyxl.load_workbook(io.BytesIO(edited))["Data"]
    values = openpyxl.load_workbook(io.BytesIO(edited), data_only=True)["Data"]
    assert [sheet[f"G{r}"].value for r in (2, 3, 4)] == ["=B2*C2", "=B3*C3", "=B4*C4"]
    assert values["G3"].value == pytest.approx(37.5) and values["H2"].value == pytest.approx(45 + 37.5 + 15)


def test_table_headers_merged_cells_and_array_formulas_are_kept_consistent():
    data = foreign_workbook()
    renamed = edit(data, "d.xlsx", [{"op": "set_cell", "at": "Data!B1", "value": "Quantity"}]).data
    table = zipfile.ZipFile(io.BytesIO(renamed)).read("xl/tables/table1.xml").decode("utf-8")
    assert 'name="Quantity"' in table and 'name="Units"' not in table   # Or Excel would repair the file.
    replaced = edit(data, "d.xlsx", [{"op": "replace_text", "find": "Price", "replace": "Unit price"}]).data
    assert 'name="Unit price"' in zipfile.ZipFile(io.BytesIO(replaced)).read("xl/tables/table1.xml").decode()
    for operation in ({"at": "Data!B1", "value": 5},                  # A table header is a name: text.
                      {"at": "Data!B1", "value": "Item"},             # Names are unique.
                      {"at": "Data!B1", "formula": "=1+1"},
                      {"at": "Data!F1", "value": "x"},                # Inside a merged range.
                      {"at": "Data!E4", "value": 1}):                 # Inside an array formula.
        assert refused(data, "d.xlsx", [{"op": "set_cell", **operation}]) == "edit_not_allowed", operation
    assert edit(data, "d.xlsx", [{"op": "set_cell", "at": "Data!E1", "value": "The merge's first cell"}]).data


def test_a_workbook_the_engine_cannot_compute_gets_no_stale_values():
    data = foreign_workbook()       # Its array formula is outside what the engine computes.
    edited = edit(data, "d.xlsx", [{"op": "set_cell", "at": "Data!B3", "value": 25}]).data
    xml = zipfile.ZipFile(io.BytesIO(edited)).read("xl/worksheets/sheet1.xml").decode("utf-8")
    formula_cells = re.findall(r"<c [^>]*>(?:(?!</c>).)*?<f[ >/](?:(?!</c>).)*</c>", xml, re.S)
    assert formula_cells and not any("<v>" in cell for cell in formula_cells)   # Cleared, not stale.
    assert 'fullCalcOnLoad="1"' in zipfile.ZipFile(io.BytesIO(edited)).read("xl/workbook.xml").decode()


def test_a_stale_calculation_chain_is_dropped():
    chain = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<calcChain xmlns="http://schemas.'
             'openxmlformats.org/spreadsheetml/2006/main"><c r="G2" i="1"/></calcChain>')
    relation = (b'<Relationship Id="rId99" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                b'relationships/calcChain" Target="calcChain.xml"/></Relationships>')
    override = (b'<Override PartName="/xl/calcChain.xml" ContentType="application/vnd.openxmlformats-'
                b'officedocument.spreadsheetml.calcChain+xml"/></Types>')
    with_chain = rezip(computable_foreign_workbook(), add={"xl/calcChain.xml": chain}, change={
        "xl/_rels/workbook.xml.rels": lambda xml: xml.replace(b"</Relationships>", relation),
        "[Content_Types].xml": lambda xml: xml.replace(b"</Types>", override)})
    edited = zipfile.ZipFile(io.BytesIO(edit(with_chain, "d.xlsx",
                                             [{"op": "set_cell", "at": "Data!B3", "value": 5}]).data))
    assert "xl/calcChain.xml" not in edited.namelist()
    assert b"calcChain" not in edited.read("xl/_rels/workbook.xml.rels")
    assert b"calcChain" not in edited.read("[Content_Types].xml")


def test_the_benchmark_complex_edit_applies_whole(word, book):
    import quality_benchmark
    operations = quality_benchmark.report_edit(outline(word, "report.docx"))
    plain = document_xml(edit(word, "report.docx", operations).data)
    tracked = document_xml(edit(word, "report.docx", operations, track_changes=True).data)
    assert accepted(tracked) == accepted(plain) and rejected(tracked) == rejected(document_xml(word))
    text = accepted(plain)
    assert "strongest and most profitable" in text and "Audit adjustment" in text and "18,600" in text
    edited = edit(book, "forecast.xlsx", quality_benchmark.WORKBOOK_EDIT).data
    assert openpyxl.load_workbook(io.BytesIO(edited))["Inputs"]["B2"].value == 1500


# --- requests --------------------------------------------------------------------------------


@pytest.mark.parametrize("operations", [
    [], "replace", [{"op": "replace_text", "find": "", "replace": "x"}],
    [{"op": "replace_text", "find": "a", "replace": "b", "extra": 1}],
    [{"op": "replace_text", "find": "a", "replace": 3}],
    [{"op": "replace_text", "find": "a\x00", "replace": "b"}],
    [{"op": "replace_text", "find": "a", "replace": "b", "expect": 0}],
    [{"op": "set_text", "at": "p1"}],
    [{"op": "insert_paragraph", "after": "p1", "before": "p2", "text": "x"}],
    [{"op": "add_row", "after": "t1.r1", "cells": []}],
    [{"op": "set_cell", "at": "A1", "value": 1}],                 # Not an operation for Word.
    [{"op": "run_macro"}],
    [{"op": "replace_text", "find": "a", "replace": "b"}] * (MAX_OPERATIONS + 1),
])
def test_malformed_requests_are_refused(word, operations):
    assert refused(word, "x.docx", operations) == "invalid_edit"


def test_the_upload_is_never_modified_and_the_result_is_reproducible(word):
    original = bytes(word)
    operation = [{"op": "replace_text", "find": "$18.4M", "replace": "$18.6M"}]
    first = edit(word, "x.docx", operation)
    assert word == original and edit(word, "x.docx", operation).data == first.data
    assert refused(word, "x.docx", operation, revision_date="yesterday") == "invalid_edit"



@pytest.mark.parametrize("formula", ["=1+", "=)1+2(", "=A0", "=XFE1", "=SUM(A1:B)", "=ROUND(1)", "=(1+2"])
def test_editing_refuses_a_malformed_formula_before_writing(book, formula):
    """Codex audit D4, editing: the same syntax check as generation, before any change."""
    assert refused(book, "f.xlsx", [{"op": "set_cell", "at": "Inputs!D2", "formula": formula}]) == "invalid_formula"


def test_an_uploaded_malformed_formula_leaves_no_computed_value(book):
    """A customer's own malformed formula is never given a computed cache: every cache is cleared and
    Excel recalculates on opening."""
    archive = zipfile.ZipFile(io.BytesIO(book))
    target = next(name for name in archive.namelist()
                  if name.startswith("xl/worksheets/sheet") and re.search(rb"<f>[^<]+</f>", archive.read(name)))

    def break_one(content):
        return re.sub(rb"<f>[^<]+</f>", b"<f>1+</f>", content, count=1)
    broken = rezip(book, change={target: break_one})
    edited = edit(broken, "f.xlsx", [{"op": "set_cell", "at": "Inputs!B2", "value": 1500}]).data
    formulas = openpyxl.load_workbook(io.BytesIO(edited))
    values = openpyxl.load_workbook(io.BytesIO(edited), data_only=True)
    cached = [values[sheet.title][cell.coordinate].value for sheet in formulas.worksheets
              for row in sheet.iter_rows() for cell in row if cell.data_type == "f"]
    assert cached and all(value is None for value in cached)
