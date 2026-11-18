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
    kept = [name for name in before.namelist() if name.startswith(("xl/drawings/", "xl/styles",
                                                                     "xl/theme/", "xl/sharedStrings"))]
    assert len(kept) >= 4 and all(before.read(name) == after.read(name) for name in kept)
    # Charts plot the changed results, so their cached copies change (tested below); a label edit
    # that no chart plots leaves every chart part byte for byte.
    label = edit(book, "forecast.xlsx", [{"op": "replace_text", "find": "launch", "replace": "day one",
                                          "in": "Inputs!A2"}]).data
    charts = [name for name in before.namelist() if name.startswith("xl/charts/")]
    labelled = zipfile.ZipFile(io.BytesIO(label))
    assert charts and all(before.read(name) == labelled.read(name) for name in charts)
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
    assert "strongest and most profitable" in text and "18,600" in text
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



# --- the workbook's date system (Codex document audit, finding 1) -----------------------------------

def _dated_book(date1904):
    """Equivalent workbooks in the 1900 and 1904 date systems: the same dates, date formulas and an
    unrelated input (D1) that an edit will change."""
    from datetime import datetime
    from openpyxl.utils.datetime import CALENDAR_MAC_1904, CALENDAR_WINDOWS_1900
    book = openpyxl.Workbook()
    book.epoch = CALENDAR_MAC_1904 if date1904 else CALENDAR_WINDOWS_1900
    sheet = book.active
    sheet.title = "Dates"
    sheet["A1"], sheet["A2"], sheet["D1"] = datetime(2027, 1, 1), datetime(2027, 3, 31), 5
    for row, formula in enumerate(DATE_FORMULAS, start=1):
        sheet.cell(row=row, column=2, value=formula)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


DATE_FORMULAS = ["=YEAR(A1)", "=MONTH(A1)", "=DAY(A1)", "=DATE(2027,3,15)", "=EDATE(A1,1)", "=EOMONTH(A1,0)",
                 "=A1+30", "=DAYS(A2,A1)", "=WEEKDAY(A1)", "=TEXT(A1,\"yyyy-mm-dd\")", "=NETWORKDAYS(A1,A2)",
                 "=YEAR(EDATE(A1,12))", "=DATEDIF(A1,A2,\"d\")", "=PMT(0.05/12,DATEDIF(A1,A2,\"m\"),-1000)",
                 "=D1*2"]


def _expected(date1904):
    from datetime import date, timedelta
    epoch = date(1904, 1, 1) if date1904 else date(1899, 12, 30)

    def serial(day):
        return float((day - epoch).days)
    start, end = date(2027, 1, 1), date(2027, 3, 31)
    workdays = sum(1 for n in range((end - start).days + 1) if (start + timedelta(days=n)).weekday() < 5)
    months = 2
    payment = 1000 * (0.05 / 12) / (1 - (1 + 0.05 / 12) ** -months)
    return [2027, 1, 1, serial(date(2027, 3, 15)), serial(date(2027, 2, 1)), serial(date(2027, 1, 31)),
            serial(date(2027, 1, 31)), 89, 6, "2027-01-01", workdays, 2028, 89, payment, 12]


@pytest.mark.parametrize("date1904", [False, True])
def test_an_unrelated_edit_recalculates_dates_in_the_workbooks_own_date_system(date1904):
    """A 1904 workbook's YEAR(2027-01-01) once became 2022 after an unrelated edit."""
    edited = edit(_dated_book(date1904), "dates.xlsx", [{"op": "set_cell", "at": "Dates!D1", "value": 6}]).data
    sheet = openpyxl.load_workbook(io.BytesIO(edited), data_only=True)["Dates"]
    assert openpyxl.load_workbook(io.BytesIO(edited)).epoch.year == (1904 if date1904 else 1899)
    found = [sheet.cell(row=row, column=2).value for row in range(1, len(DATE_FORMULAS) + 1)]
    for formula, mine, want in zip(DATE_FORMULAS, found, _expected(date1904)):
        if isinstance(want, float):
            assert isinstance(mine, (int, float)) and abs(mine - want) < 1e-9, (formula, mine, want)
        else:
            assert mine == want, (formula, mine, want)
    # The dates themselves are untouched, and still read as the same calendar dates.
    dates = openpyxl.load_workbook(io.BytesIO(edited))["Dates"]
    assert (dates["A1"].value.date().isoformat(), dates["A2"].value.date().isoformat()) == ("2027-01-01", "2027-03-31")


def test_equivalent_1900_and_1904_workbooks_differ_only_by_their_epoch():
    results = []
    for date1904 in (False, True):
        edited = edit(_dated_book(date1904), "dates.xlsx", [{"op": "set_cell", "at": "Dates!D1", "value": 7}]).data
        sheet = openpyxl.load_workbook(io.BytesIO(edited), data_only=True)["Dates"]
        results.append([sheet.cell(row=row, column=2).value for row in range(1, len(DATE_FORMULAS) + 1)])
    serial_rows = {4, 5, 6, 7}                       # DATE, EDATE, EOMONTH and A1+30 return serials.
    for row, (windows, mac) in enumerate(zip(*results), start=1):
        if row in serial_rows:
            assert windows - mac == 1462, row        # The two systems' serials are 1,462 days apart.
        else:
            assert windows == mac, row



# --- chart caches after an edit (Codex document audit, finding 2) -------------------------------------

def chart_caches(data):
    """{(chart part, reference): cached values} for every numRef and strRef of every chart."""
    from lxml import etree
    archive, found = zipfile.ZipFile(io.BytesIO(data)), {}
    c = "{http://schemas.openxmlformats.org/drawingml/2006/chart}"
    for name in sorted(n for n in archive.namelist() if n.startswith("xl/charts/chart") and n.endswith(".xml")):
        root = etree.fromstring(archive.read(name))
        for reference in [*root.iter(c + "numRef"), *root.iter(c + "strRef")]:
            cache = reference.find(c + "numCache") if reference.tag == c + "numRef" else reference.find(c + "strCache")
            points = None if cache is None else {
                "count": int(cache.find(c + "ptCount").get("val")),
                "points": {int(p.get("idx")): p.findtext(c + "v") for p in cache.findall(c + "pt")}}
            found[(name, reference.findtext(c + "f"), reference.tag.endswith("numRef"))] = points
    return found


def _same(a, b):
    try:
        return abs(float(a) - float(b)) <= 1e-9 * max(1.0, abs(float(b)))
    except (TypeError, ValueError):
        return a == b


def test_an_edit_refreshes_every_chart_cache_exactly_as_a_fresh_build(book):
    """WORKBOOK_EDIT moved Summary!B2 from 872,739 to 1,090,495 while every chart kept 872,739."""
    edited = edit(book, "forecast.xlsx", [{"op": "set_cell", "at": "Inputs!B2", "value": 1500}]).data
    spec = copy.deepcopy(FORECAST_WORKBOOK)
    spec["sheets"][1]["rows"][0][1]["value"] = 1500
    found, expected = chart_caches(edited), chart_caches(generate(spec, "xlsx").data)
    assert found.keys() == expected.keys() and len(found) >= 12
    for key, want in expected.items():
        mine = found[key]
        assert mine is not None and mine["count"] == want["count"], key
        assert mine["points"].keys() == want["points"].keys(), key
        assert all(_same(mine["points"][i], want["points"][i]) for i in want["points"]), key
    assert found != chart_caches(book)                                  # The charts did change.


def test_every_cached_chart_point_agrees_with_the_cells_it_plots(book):
    """Category and value caches, including the date axis, against the edited file's own cell values."""
    edited = edit(book, "forecast.xlsx", [{"op": "set_cell", "at": "Inputs!B3", "value": 0.07},
                                          {"op": "set_cell", "at": "Inputs!B2", "value": 1800}]).data
    values = openpyxl.load_workbook(io.BytesIO(edited), data_only=True)
    from openpyxl.utils.cell import range_boundaries
    checked = 0
    for (part, reference, numeric), cache in chart_caches(edited).items():
        sheet, area = reference.split("!")
        left, top, right, bottom = range_boundaries(area.replace("$", ""))
        cells = [values[sheet.strip("'")].cell(row=r, column=col).value
                 for r in range(top, bottom + 1) for col in range(left, right + 1)]
        assert cache["count"] == len(cells), (part, reference)
        for index, value in enumerate(cells):
            if hasattr(value, "toordinal"):                              # A date cell: its serial.
                from openpyxl.utils.datetime import to_excel
                value = to_excel(value)
            if value is None:
                assert index not in cache["points"], (part, reference, index)
            else:
                assert _same(cache["points"][index], value), (part, reference, index, cache["points"][index], value)
            checked += 1
    assert checked > 100


def _with_chart_reference(book, old, new):
    archive = zipfile.ZipFile(io.BytesIO(book))
    part = next(n for n in archive.namelist() if n.startswith("xl/charts/chart") and old.encode() in archive.read(n))
    return rezip(book, change={part: lambda content: content.replace(old.encode(), new.encode(), 1)}), part


def test_a_chart_reference_this_editor_cannot_recompute_loses_its_stale_cache(book):
    """A two-dimensional area is not a series this editor can rebuild: its copy is removed, so no
    viewer shows an old number, and Excel rebuilds it when the file opens."""
    changed, part = _with_chart_reference(book, "<c:f>Summary!$B$2:$B$4</c:f>", "<c:f>Summary!$B$2:$C$4</c:f>")
    edited = edit(changed, "forecast.xlsx", [{"op": "set_cell", "at": "Inputs!B2", "value": 1500}]).data
    caches = chart_caches(edited)
    assert caches[(part, "Summary!$B$2:$C$4", True)] is None
    assert all(cache is not None for (name, reference, _n), cache in caches.items() if reference != "Summary!$B$2:$C$4")


def test_charts_over_formulas_the_editor_cannot_compute_lose_their_caches(book):
    """When the workbook holds a formula outside the supported set, no result is recomputed: every
    chart copy that depends on a formula is removed instead of left stale."""
    archive = zipfile.ZipFile(io.BytesIO(book))
    target = next(n for n in archive.namelist()
                  if n.startswith("xl/worksheets/sheet") and re.search(rb"<f>[^<]+</f>", archive.read(n)))
    broken = rezip(book, change={target: lambda content: re.sub(rb"<f>[^<]+</f>", b"<f>INDIRECT(\"A1\")</f>",
                                                                content, count=1)})
    edited = edit(broken, "forecast.xlsx", [{"op": "set_cell", "at": "Inputs!B2", "value": 1500}]).data
    caches = chart_caches(edited)
    revenue = [cache for (name, reference, numeric), cache in caches.items() if reference.endswith("$C$2:$C$37")]
    assert revenue and all(cache is None for cache in revenue)



# --- presentation edits must still fit (Codex document audit, finding 6) ----------------------------

LONG = " ".join(["Revenue grew across every region as procurement savings offset higher freight costs."] * 40)


def test_a_replacement_that_cannot_fit_its_shape_is_refused(deck):
    """The audit replaced s4.sh2 with 3,329 characters: the edit succeeded at 32 points in a 0.55-inch box."""
    assert len(LONG) > 3300
    assert refused(deck, "x.pptx", [{"op": "set_text", "at": "s4.sh2", "text": LONG}]) == "slide_text_too_long"
    assert refused(deck, "x.pptx", [{"op": "set_text", "at": "s4.sh2.p1", "text": LONG}]) == "slide_text_too_long"
    short = edit(deck, "x.pptx", [{"op": "set_text", "at": "s4.sh2", "text": "Two roasteries"}]).data
    assert "Two roasteries" in [u.text for u in outline(short, "x.pptx").units]


def test_a_replace_text_that_overflows_is_refused_and_one_that_fits_is_kept(deck):
    found = outline(deck, "x.pptx")
    title = next(u for u in found.units if u.address == "s4.sh2")
    word = title.text.split()[0]
    assert refused(deck, "x.pptx", [{"op": "replace_text", "find": word, "replace": LONG, "in": "s4.sh2"}]) \
        == "slide_text_too_long"
    kept = edit(deck, "x.pptx", [{"op": "replace_text", "find": word, "replace": word, "in": "s4.sh2"}]).data
    assert [u.text for u in outline(kept, "x.pptx").units if u.address == "s4.sh2"] == [title.text]


def test_a_table_cell_that_would_push_the_table_off_the_slide_is_refused(deck):
    cell = next(u for u in outline(deck, "x.pptx").units if u.kind == "cell")
    assert refused(deck, "x.pptx", [{"op": "set_text", "at": cell.address, "text": LONG}]) == "slide_text_too_long"
    edit(deck, "x.pptx", [{"op": "set_text", "at": cell.address, "text": "Third party"}])


def test_right_to_left_text_is_measured_too():
    from test_deck_design import ARABIC_DECK
    arabic = generate(copy.deepcopy(ARABIC_DECK), "pptx").data
    title = next(u for u in outline(arabic, "a.pptx").units if u.kind == "shape" and u.text.strip())
    long = " ".join(["نمت الإيرادات في كل المناطق مع تحسن كفاءة سلاسل الإمداد وارتفاع تكاليف الشحن."] * 40)
    assert refused(arabic, "a.pptx", [{"op": "set_text", "at": title.address, "text": long}]) == "slide_text_too_long"
    edit(arabic, "a.pptx", [{"op": "set_text", "at": title.address, "text": "نظرة عامة"}])


def test_speaker_notes_have_no_box_and_are_never_refused_for_length(deck):
    notes = next((u for u in outline(deck, "x.pptx").units if u.kind == "notes"), None)
    if notes is None:
        pytest.skip("the benchmark deck has no notes")
    edit(deck, "x.pptx", [{"op": "set_text", "at": notes.address, "text": LONG}])


def test_every_generated_shape_and_cell_is_judged_to_fit_as_generation_made_it(deck):
    """The editor's measurement agrees with generation's: nothing a fresh deck contains is refused."""
    import pptx
    from kalillac_files import editing
    from test_deck_design import ARABIC_DECK
    for data in (deck, generate(copy.deepcopy(ARABIC_DECK), "pptx").data):
        presentation = pptx.Presentation(io.BytesIO(data))
        containers = editing._text_containers(presentation)
        for key, _kind, _element, _slide in containers:
            before = {}
            for other, kind, element, _s in containers:
                body = element.text_frame._txBody if kind == "shape" else element[3]._tc.find(editing.A + "txBody")
                if body is not None:
                    before[other] = ("<changed>" if other == key else editing._body_text(body), copy.deepcopy(body))
            editing._check_deck_fit(presentation, before)              # Raises if it would refuse.
        assert len(containers) > 50



def test_the_benchmark_restatement_is_internally_consistent(word):
    """Q4: the tracked edit restated revenue to $18.6M but left growth, gross profit and margin at
    their old values. Now every dependent figure the text states moves with it, the old figures are
    gone, and the charts that cannot be restated are named."""
    import quality_benchmark
    text = accepted(document_xml(edit(word, "report.docx", quality_benchmark.report_edit(outline(word, "report.docx")),
                                      track_changes=True).data))
    revenue, cost, previous_revenue, previous_gross = 18_600, 10_746, 15_000, 5_925
    gross = revenue - cost
    assert f"{gross:,}" in text and f"{revenue:,}" in text
    assert f"+{(revenue / previous_revenue - 1) * 100:.1f}%" in text                    # +24.0%
    assert f"grew {round((revenue / previous_revenue - 1) * 100)}%" in text            # grew 24%
    assert f"+{(gross / previous_gross - 1) * 100:.1f}%" in text                       # +32.6%
    assert f"{gross / revenue * 100:.1f}%" in text                                       # 42.2%
    assert f"{(gross / revenue - previous_gross / previous_revenue) * 100:.1f} points" in text  # 2.7 points
    for old, _new in quality_benchmark.RESTATED.values():
        assert old not in text, old
    assert quality_benchmark.RESTATEMENT_NOTE in text


def test_every_chart_that_shows_the_restated_revenue_is_named_in_the_restatement():
    """Codex reverification, finding C: the line chart's 2026 point is the $18.4M revenue, and the
    restatement did not say it was stale. Dependants are found from the report's own data: a chart
    whose values sum to the 2026 revenue, or that plots it as a point (in dollars or millions)."""
    import quality_benchmark
    revenue = 18_400_000
    dependent = []
    for block in REPORT["blocks"]:
        if block.get("type") != "chart":
            continue
        for series in block["series"]:
            values = [value for value in series["values"] if value is not None]
            if sum(values) == revenue or any(value in (revenue, revenue / 1_000_000) for value in values):
                dependent.append(block.get("title"))
                break
    assert None not in dependent and len(dependent) == 3
    assert set(dependent) == set(quality_benchmark.RESTATEMENT_CHARTS)
    for title in dependent:
        assert f"\u201c{title}\u201d" in quality_benchmark.RESTATEMENT_NOTE



# --- a changed figure carries its dependants (Codex document audit, Q4) -----------------------------

@pytest.fixture(scope="module")
def proposal():
    import quality_benchmark
    return generate(copy.deepcopy(quality_benchmark.WORD_PROPOSAL), "docx").data


def _summary(old, new):
    """The proposal's summary states the total fee: a restatement changes it too."""
    return {"op": "replace_text", "find": old, "replace": new, "expect": 1}


def _fee_cell(found, text):
    return next(u.address for u in found.units if u.kind == "cell" and u.text == text)


def test_an_edit_that_breaks_a_total_row_is_refused_unless_asked_for_exactly(proposal):
    found = outline(proposal, "p.docx")
    permits, total = _fee_cell(found, "$11,000"), _fee_cell(found, "$186,500")
    one = [{"op": "set_text", "at": permits, "text": "$12,000"}]
    assert refused(proposal, "p.docx", one) == "table_total_mismatch"
    assert refused(proposal, "p.docx", [{"op": "replace_text", "find": "$11,000", "replace": "$12,000"}]) \
        == "table_total_mismatch"
    # With its dependant, the same change is accepted.
    both = edit(proposal, "p.docx", one + [{"op": "set_text", "at": total, "text": "$187,500"},
                                           _summary("$186,500", "$187,500")]).data
    assert "$187,500" in document_xml(both)
    # The customer's explicit, limited change is still possible.
    exact = edit(proposal, "p.docx", one, exact=True).data
    assert "$12,000" in document_xml(exact) and "$186,500" in document_xml(exact)
    assert refused(proposal, "p.docx", one, exact="yes") == "invalid_edit"


def test_a_table_already_inconsistent_in_the_upload_never_blocks_an_edit(proposal):
    found = outline(proposal, "p.docx")
    broken = edit(proposal, "p.docx", [{"op": "set_text", "at": _fee_cell(found, "$11,000"), "text": "$12,000"}],
                  exact=True).data
    later = outline(broken, "p.docx")
    edit(broken, "p.docx", [{"op": "set_text", "at": _fee_cell(later, "$13,000"), "text": "$14,000"}])


def test_a_deck_table_total_is_kept_too():
    deck = generate({"title": "Fees", "slides": [
        {"layout": "table", "title": "Fees by stage", "total_row": True, "header": ["Stage", "Fee"],
         "rows": [["Concept", "$24,500"], ["Permits", "$11,000"], ["Build", "$138,000"], ["Total", "$173,500"]]}]},
        "pptx").data
    cell = next(u.address for u in outline(deck, "d.pptx").units if u.kind == "cell" and u.text == "$11,000")
    assert refused(deck, "d.pptx", [{"op": "set_text", "at": cell, "text": "$12,000"}]) == "table_total_mismatch"
    edit(deck, "d.pptx", [{"op": "set_text", "at": cell, "text": "$12,000"}], exact=True)


# --- tracked edits are judged as they read once accepted (Codex reverification, finding A) ----------

def _accepted_fees(data):
    from kalillac_files.editing import _word_tables_accepted
    return next(rows for header, rows in _word_tables_accepted(data) if header and header[0] == "Stage")


def test_a_tracked_replacement_that_breaks_a_total_is_refused(proposal):
    cell = _fee_cell(outline(proposal, "p.docx"), "$11,000")
    one = [{"op": "set_text", "at": cell, "text": "$12,000"}]
    assert refused(proposal, "p.docx", one, track_changes=True) == "table_total_mismatch"
    assert refused(proposal, "p.docx", [{"op": "replace_text", "find": "$11,000", "replace": "$12,000"}],
                   track_changes=True) == "table_total_mismatch"


def test_a_tracked_restatement_with_its_total_accepts_consistent_and_rejects_to_the_original(proposal):
    found = outline(proposal, "p.docx")
    operations = [{"op": "set_text", "at": _fee_cell(found, "$11,000"), "text": "$12,000"},
                  {"op": "set_text", "at": _fee_cell(found, "$186,500"), "text": "$187,500"},
                  _summary("$186,500", "$187,500")]
    tracked = edit(proposal, "p.docx", operations, track_changes=True).data
    xml = document_xml(tracked)
    assert "<w:ins " in xml and "<w:del " in xml                       # Real revisions are delivered.
    fees = _accepted_fees(tracked)
    assert ["Permits", "Approval lodged", "$12,000"] in fees and fees[-1][-1] == "$187,500"
    assert "$187,500" in accepted(xml) and "$12,000" in accepted(xml) and "$11,000" not in accepted(xml)
    assert "$11,000" in rejected(xml) and "$12,000" not in rejected(xml) and "$187,500" not in rejected(xml)


def test_tracked_row_insertions_and_deletions_count_toward_the_total(proposal):
    found = outline(proposal, "p.docx")
    row = _fee_cell(found, "$11,000").rsplit(".", 1)[0]
    added = [{"op": "add_row", "after": row, "cells": ["Survey", "Signing", "$2,000"]}]
    assert refused(proposal, "p.docx", added, track_changes=True) == "table_total_mismatch"
    fixed = added + [{"op": "set_text", "at": _fee_cell(found, "$186,500"), "text": "$188,500"},
                     _summary("$186,500", "$188,500")]
    assert _accepted_fees(edit(proposal, "p.docx", fixed, track_changes=True).data)[-1][-1] == "$188,500"
    removed = [{"op": "delete", "at": row}]
    assert refused(proposal, "p.docx", removed, track_changes=True) == "table_total_mismatch"
    fixed = removed + [{"op": "set_text", "at": _fee_cell(found, "$186,500"), "text": "$175,500"},
                       _summary("$186,500", "$175,500")]
    after = _accepted_fees(edit(proposal, "p.docx", fixed, track_changes=True).data)
    assert not any(cells[0] == "Permits" for cells in after) and after[-1][-1] == "$175,500"


def test_a_previously_revised_upload_is_judged_by_its_accepted_figures(proposal):
    found = outline(proposal, "p.docx")
    revised = edit(proposal, "p.docx", [{"op": "set_text", "at": _fee_cell(found, "$11,000"), "text": "$12,000"},
                                        {"op": "set_text", "at": _fee_cell(found, "$186,500"), "text": "$187,500"},
                                        _summary("$186,500", "$187,500")],
                   track_changes=True).data
    later = outline(revised, "p.docx")
    concept = next(u.address for u in later.units if u.kind == "cell" and u.text == "$24,500")
    assert refused(revised, "p.docx", [{"op": "set_text", "at": concept, "text": "$25,500"}]) == "table_total_mismatch"
    total = next(u.address for u in later.units if u.kind == "cell" and "187,500" in u.text)
    edit(revised, "p.docx", [{"op": "set_text", "at": concept, "text": "$25,500"},
                             {"op": "set_text", "at": total, "text": "$188,500"},
                             _summary("$187,500", "$188,500")], track_changes=True)


def test_a_tracked_fee_edit_keeps_the_cells_formatting(proposal):
    import re as _re
    cell = _fee_cell(outline(proposal, "p.docx"), "$11,000")
    operations = [{"op": "set_text", "at": cell, "text": "$12,000"}]
    exact = edit(proposal, "p.docx", operations, track_changes=True, exact=True).data
    original = _re.search(r"<w:r>(<w:rPr>(?:(?!</w:rPr>).)*</w:rPr>)<w:t[^>]*>\$11,000</w:t>", document_xml(proposal), _re.S)
    assert original is not None and original.group(1) in document_xml(exact)


# --- the whole table must still fit (Codex reverification, finding B) --------------------------------

PHRASE = "Margin falls on overflow volume and quality depends on a third party. "


def _table_cells(data, name):
    found = outline(data, name)
    return [u for u in found.units if u.kind == "cell"]


def _first_column_cells_by_row(data, name):
    rows = {}
    for unit in _table_cells(data, name):
        table, row, column = unit.address.rsplit(".", 2)
        if column == "c1" and row != "r1":
            rows.setdefault(table, []).append(unit.address)
    return next(iter(rows.values()))


def test_rows_that_each_fit_cannot_together_push_the_table_off_the_slide(deck):
    first, second = _first_column_cells_by_row(deck, "x.pptx")[:2]
    for count in range(1, 40):
        text = PHRASE * count
        both = [{"op": "set_text", "at": first, "text": text}, {"op": "set_text", "at": second, "text": text}]
        try:
            edit(deck, "x.pptx", both)
        except SpecError as error:
            assert error.code == "slide_text_too_long"
            edit(deck, "x.pptx", both[:1])                                # One of them alone still fits.
            edit(deck, "x.pptx", both[1:])
            return
    pytest.fail("no length made two rows overflow")


def _merged_and_rtl_decks():
    import pptx
    from test_deck_design import ARABIC_DECK
    spec = {"title": "Fees", "slides": [
        {"layout": "table", "title": "Fees by stage", "header": ["Stage", "Paid on", "Fee"],
         "rows": [["Concept", "Signing", "$24,500"], ["Permits", "Lodged", "$11,000"],
                  ["Build", "Weeks 6 to 12", "$138,000"], ["Opening", "Opening day", "$13,000"]]}]}
    merged = pptx.Presentation(io.BytesIO(generate(spec, "pptx").data))
    table = next(shape for shape in merged.slides[0].shapes if getattr(shape, "has_table", False)).table
    table.cell(1, 1).merge(table.cell(2, 1))                               # A cell spanning two rows.
    out = io.BytesIO()
    merged.save(out)
    return out.getvalue(), generate(copy.deepcopy(ARABIC_DECK), "pptx").data


def test_merged_and_right_to_left_tables_are_measured_whole():
    merged, arabic = _merged_and_rtl_decks()
    spanning = next(u.address for u in _table_cells(merged, "m.pptx") if u.text.startswith("Signing"))
    assert refused(merged, "m.pptx", [{"op": "set_text", "at": spanning, "text": PHRASE * 40}]) == "slide_text_too_long"
    edit(merged, "m.pptx", [{"op": "set_text", "at": spanning, "text": "On signing"}])
    cells = _table_cells(arabic, "a.pptx")
    if not cells:
        pytest.skip("the Arabic deck has no table")
    long = " ".join(["نمت الإيرادات في كل المناطق مع تحسن كفاءة سلاسل الإمداد."] * 30)
    rows = sorted({u.address.rsplit(".", 1)[0] for u in cells})[1:3]
    targets = [next(u.address for u in cells if u.address.startswith(row + ".")) for row in rows]
    assert refused(arabic, "a.pptx", [{"op": "set_text", "at": a, "text": long} for a in targets]) \
        == "slide_text_too_long"


def test_an_edit_that_fits_keeps_every_other_cell_unchanged(deck):
    before = {u.address: u.text for u in _table_cells(deck, "x.pptx")}
    target = _first_column_cells_by_row(deck, "x.pptx")[0]
    after = {u.address: u.text for u in _table_cells(edit(deck, "x.pptx", [{"op": "set_text", "at": target,
                                                                              "text": "Capital"}]).data, "x.pptx")}
    assert after[target] == "Capital"
    assert {a: t for a, t in after.items() if a != target} == {a: t for a, t in before.items() if a != target}


def test_a_restated_total_must_be_restated_wherever_the_document_states_it(proposal):
    """The proposal's summary states the total fee: changing the fee table's total (with its rows) but
    not the summary would leave the document stating two totals."""
    found = outline(proposal, "p.docx")
    table_only = [{"op": "set_text", "at": _fee_cell(found, "$11,000"), "text": "$12,000"},
                  {"op": "set_text", "at": _fee_cell(found, "$186,500"), "text": "$187,500"}]
    assert refused(proposal, "p.docx", table_only) == "dependent_figure_unchanged"
    assert refused(proposal, "p.docx", table_only, track_changes=True) == "dependent_figure_unchanged"
    everywhere = table_only[:1] + [{"op": "replace_text", "find": "$186,500", "replace": "$187,500"}]
    text = accepted(document_xml(edit(proposal, "p.docx", everywhere, track_changes=True).data))
    assert "$186,500" not in text and text.count("$187,500") >= 2
    edit(proposal, "p.docx", table_only, exact=True)                       # The customer's confirmed choice.


# --- a stated total is matched as a whole figure (Codex reverification 3, finding 2) ----------------

def _insurance(prose, extra=()):
    """Codex's reproduction: a sentence with an unrelated figure, and a fee table A $100, B $200, Total $300."""
    return generate({"title": "Cover", "blocks": [
        {"type": "paragraph", "text": prose},
        {"type": "table", "caption": "Fees", "total_row": True, "header": ["Item", "Fee"],
         "rows": [["A", "$100"], ["B", "$200"], ["Total", "$300"]]}, *extra]}, "docx").data


def _restate(data):
    """Edit A to $200 and the total to $400, leaving the prose alone."""
    found = outline(data, "c.docx")
    return [{"op": "set_text", "at": _fee_cell(found, "$100"), "text": "$200"},
            {"op": "set_text", "at": _fee_cell(found, "$300"), "text": "$400"}]


@pytest.mark.parametrize("prose", [
    "Separate insurance coverage is $3000.",                 # Codex's case: "$300" is inside "$3000".
    "Separate insurance coverage is $3,000.",
    "Separate insurance coverage is $1,300.",
    "Separate insurance coverage is $300.5 a month.",
    "Separate insurance coverage is $3005.",
    "Separate insurance coverage is $300K.",
    "Separate insurance coverage is A$300.",                 # Another currency.
    "Separate insurance coverage is €300.",
    "The venue holds 300 seats.",                            # A count, not money.
    "Reference 300-B applies.",
])
def test_an_unrelated_figure_that_contains_the_old_total_does_not_block_the_restatement(prose):
    data = _insurance(prose)
    text = document_xml(edit(data, "c.docx", _restate(data)).data)
    assert "$400" in text and prose.replace("€", "€") in text      # Nothing else changed automatically.


@pytest.mark.parametrize("prose", [
    "The total fee is $300.",
    "The total fee is $300, payable on signing.",
    "The total fee is $300.00.",                             # The same figure, formatted differently.
    "The total fee is $ 300.",
    "The total fee ($300) is fixed.",
    "Fees: $300; insurance: $3000.",                         # The stale total beside an unrelated figure.
])
def test_the_old_total_left_in_prose_is_still_refused(prose):
    data = _insurance(prose)
    assert refused(data, "c.docx", _restate(data)) == "dependent_figure_unchanged"
    assert refused(data, "c.docx", _restate(data), track_changes=True) == "dependent_figure_unchanged"
    edit(data, "c.docx", _restate(data), exact=True)                       # The customer's confirmed choice.


def test_the_old_total_left_in_another_table_is_still_refused():
    data = _insurance("Separate insurance coverage is $3000.", extra=[
        {"type": "table", "caption": "Summary", "header": ["Measure", "Value"],
         "rows": [["Fees", "$300"], ["Insurance", "$3000"]]}])
    assert refused(data, "c.docx", _restate(data)) == "dependent_figure_unchanged"
    found = outline(data, "c.docx")
    summary = next(u.address for u in found.units if u.kind == "cell" and u.text == "$300"
                   and u.address != _fee_cell(found, "$300"))
    edit(data, "c.docx", _restate(data) + [{"op": "set_text", "at": summary, "text": "$400"}])


# --- signed, accounting and ISO-currency totals (Codex reverification of c5291b4, R1) ---------------

STYLES = {"signed": lambda n: f"-${n}", "minus": lambda n: f"\u2212${n}", "accounting": lambda n: f"(${n})",
          "iso": lambda n: f"USD {n}", "iso_no_break": lambda n: f"USD\u00a0{n}", "positive": lambda n: f"${n}"}


def _styled(kind, cell, prose):
    """A document (docx) or deck (pptx) stating `prose`, with a total-row table A 100, B 200, Total 300
    written in the cell style."""
    rows = [["A", cell(100)], ["B", cell(200)], ["Total", cell(300)]]
    if kind == "docx":
        raw = {"title": "Total test", "blocks": [{"type": "paragraph", "text": prose},
               {"type": "table", "header": ["Item", "Amount"], "total_row": True, "rows": rows}]}
    else:
        raw = {"title": "Total test", "slides": [{"layout": "points", "title": "Summary", "points": [prose]},
               {"layout": "table", "title": "Breakdown", "total_row": True, "header": ["Item", "Amount"],
                "rows": rows}]}
    return generate(raw, kind).data


def _restated(data, name, cell):
    """Set A to 200 and the total to 400: the table still adds up."""
    found = outline(data, name)
    return [{"op": "set_text", "at": next(u.address for u in found.units if u.kind == "cell" and u.text == old),
             "text": new} for old, new in [(cell(100), cell(200)), (cell(300), cell(400))]]


def _text(data, kind):
    if kind == "docx":
        return accepted(document_xml(data))
    from pptx import Presentation
    deck = Presentation(io.BytesIO(data))
    return "\n".join(shape.text_frame.text for slide in deck.slides for shape in slide.shapes
                     if shape.has_text_frame)


FORMATS = [("docx", False), ("docx", True), ("pptx", False)]


@pytest.mark.parametrize("kind,tracked", FORMATS)
@pytest.mark.parametrize("style", ["signed", "minus", "accounting", "iso", "iso_no_break"])
def test_a_stale_signed_accounting_or_iso_total_is_refused_and_a_full_restatement_accepted(kind, tracked, style):
    cell = STYLES[style]
    name = f"t.{kind}"
    options = {"track_changes": True} if tracked else {}
    data = _styled(kind, cell, f"The total is {cell(300)}.")
    table_only = _restated(data, name, cell)
    assert refused(data, name, table_only, **options) == "dependent_figure_unchanged"
    # Restated everywhere, the same edit is accepted and the summary states the new total only.
    everywhere = table_only + [{"op": "replace_text", "find": f"The total is {cell(300)}.",
                                "replace": f"The total is {cell(400)}.", "expect": 1}]
    text = _text(edit(data, name, everywhere, **options).data, kind)
    assert f"The total is {cell(400)}." in text and f"The total is {cell(300)}." not in text
    edit(data, name, table_only, exact=True, **options)                    # The customer's confirmed choice.


@pytest.mark.parametrize("kind,tracked", FORMATS)
@pytest.mark.parametrize("cell_style,prose", [
    ("accounting", "The net result was -$300."),      # The same negative amount, written another way.
    ("accounting", "The net result was \u2212$300."),
    ("signed", "The net result was ($300)."),         # Parentheses in prose may be accounting notation.
    ("signed", "The net result \u2013$300 is final."),  # An en dash may be a minus sign.
    ("positive", "The total fee ($300) is fixed."),   # ... or punctuation around a positive amount.
    ("positive", "Fees \u2013 $300 in all."),         # A dash before a positive amount.
    ("positive", "Quotes ranged $100-$300."),         # A range, not a negative "-$300".
    ("iso", "The total is USD 300.00."),
])
def test_an_amount_that_may_be_the_old_total_is_refused(kind, tracked, cell_style, prose):
    cell = STYLES[cell_style]
    name = f"t.{kind}"
    data = _styled(kind, cell, prose)
    options = {"track_changes": True} if tracked else {}
    assert refused(data, name, _restated(data, name, cell), **options) == "dependent_figure_unchanged"


@pytest.mark.parametrize("kind,tracked", FORMATS)
@pytest.mark.parametrize("cell_style,prose", [
    ("signed", "A separate deposit of $300 is refundable."),   # Positive, not the negative total.
    ("signed", "The prior year was -$3000."),
    ("accounting", "Insurance is ($3,000) a year."),
    ("iso", "The fee is EUR 300."),                           # Another currency.
    ("iso", "The fee is USD 3000."),
    ("iso", "The fee is USD 300K."),                          # Another scale.
    ("iso", "The venue holds 300 seats."),                    # A count.
    ("minus", "The loss was \u2212$300.5 per unit."),
])
def test_an_unrelated_signed_or_iso_amount_does_not_block_the_restatement(kind, tracked, cell_style, prose):
    cell = STYLES[cell_style]
    name = f"t.{kind}"
    data = _styled(kind, cell, prose)
    options = {"track_changes": True} if tracked else {}
    text = _text(edit(data, name, _restated(data, name, cell), **options).data, kind)
    assert prose in text                                               # Nothing else changed automatically.


# --- repeated and mixed horizontal spaces (Codex reverification of 856d9a5) -------------------------

NB, THIN = "\u00a0", "\u202f"
SPACED = {
    "iso_two_spaces": lambda n: f"USD  {n}",                 # Codex's five reproductions.
    "iso_two_no_break": lambda n: f"USD{NB}{NB}{n}",
    "signed_two_spaces": lambda n: f"-  ${n}",
    "currency_two_spaces": lambda n: f"$  {n}",
    "scale_two_spaces": lambda n: f"${n}  K",
    "iso_mixed_run": lambda n: f"USD {NB}{THIN} {n}",         # Mixed runs.
    "accounting_mixed_run": lambda n: f"( {NB}${n}{NB} )",
    "minus_mixed_run": lambda n: f"\u2212{NB} $ {NB}{n}",
}


@pytest.mark.parametrize("kind,tracked", FORMATS)
@pytest.mark.parametrize("style", list(SPACED))
def test_a_stale_total_with_repeated_spaces_is_refused_and_a_full_restatement_accepted(kind, tracked, style):
    cell = SPACED[style]
    name = f"t.{kind}"
    options = {"track_changes": True} if tracked else {}
    data = _styled(kind, cell, f"The total is {cell(300)}.")
    table_only = _restated(data, name, cell)
    assert refused(data, name, table_only, **options) == "dependent_figure_unchanged"
    everywhere = table_only + [{"op": "replace_text", "find": f"The total is {cell(300)}.",
                                "replace": f"The total is {cell(400)}.", "expect": 1}]
    text = _text(edit(data, name, everywhere, **options).data, kind)
    assert f"The total is {cell(400)}." in text and f"The total is {cell(300)}." not in text
    edit(data, name, table_only, exact=True, **options)                    # The customer's confirmed choice.


@pytest.mark.parametrize("kind,tracked", FORMATS)
@pytest.mark.parametrize("table,summary", [
    ("iso_two_spaces", f"The total is USD{NB}300."),               # The same amount spaced differently.
    ("iso_two_no_break", "The total is USD 300.00."),
    ("scale_two_spaces", "The total is $300K."),
    ("currency_two_spaces", "The total is $300."),
    ("signed_two_spaces", "The total is -$300."),
    ("signed", f"The total is -{NB} {NB}$300."),
    ("iso", f"The total is USD {NB} 300."),
    ("accounting", "The total is (  $300  )."),
])
def test_the_old_total_spaced_differently_in_the_summary_is_refused(kind, tracked, table, summary):
    cell = {**STYLES, **SPACED}[table]
    name = f"t.{kind}"
    data = _styled(kind, cell, summary)
    options = {"track_changes": True} if tracked else {}
    assert refused(data, name, _restated(data, name, cell), **options) == "dependent_figure_unchanged"


@pytest.mark.parametrize("kind,tracked", FORMATS)
@pytest.mark.parametrize("table,old_summary,new_summary", [
    ("iso_two_spaces", "The total is USD 300.", f"The total is USD{NB}400."),   # Restated in another spacing.
    ("scale_two_spaces", "The total is $300K.", "The total is $400 K."),
    ("signed_two_spaces", "The total is -$300.", "The total is -  $400."),
])
def test_a_restatement_in_another_spacing_is_accepted(kind, tracked, table, old_summary, new_summary):
    cell = SPACED[table]
    name = f"t.{kind}"
    data = _styled(kind, cell, old_summary)
    options = {"track_changes": True} if tracked else {}
    everywhere = _restated(data, name, cell) + [{"op": "replace_text", "find": old_summary,
                                                 "replace": new_summary, "expect": 1}]
    text = _text(edit(data, name, everywhere, **options).data, kind)
    assert new_summary in text and old_summary not in text


@pytest.mark.parametrize("kind,tracked", FORMATS)
@pytest.mark.parametrize("table,respaced", [
    ("iso_two_spaces", "USD 300"), ("iso_two_no_break", f"USD{NB}300.00"), ("scale_two_spaces", "$300K"),
    ("signed_two_spaces", "-$300"), ("accounting_mixed_run", "($300)"),
])
def test_respacing_the_total_alone_is_not_a_change_of_total(kind, tracked, table, respaced):
    """Formatting only: the summary still states the same amount, so nothing is stale."""
    cell = SPACED[table]
    name = f"t.{kind}"
    summary = f"The total is {cell(300)}."
    data = _styled(kind, cell, summary)
    options = {"track_changes": True} if tracked else {}
    found = outline(data, name)
    total = next(u.address for u in found.units if u.kind == "cell" and u.text == cell(300))
    text = _text(edit(data, name, [{"op": "set_text", "at": total, "text": respaced}], **options).data, kind)
    assert summary in text


@pytest.mark.parametrize("kind,tracked", FORMATS)
@pytest.mark.parametrize("table,prose", [
    ("iso_two_spaces", "The fee is EUR  300."),                    # Another currency.
    ("iso_two_spaces", "The fee is USD  3000."),
    ("scale_two_spaces", "The fee is $300  M."),                   # Another scale.
    ("scale_two_spaces", "The fee is $  300."),                    # No scale: another amount.
    ("signed_two_spaces", f"A deposit of ${NB} 300 is refundable."),  # Positive, not the negative total.
    ("currency_two_spaces", "The venue holds  300  seats."),       # A count.
    ("currency_two_spaces", "Insurance is $  3,000  a year."),
])
def test_an_unrelated_spaced_amount_does_not_block_the_restatement(kind, tracked, table, prose):
    cell = SPACED[table]
    name = f"t.{kind}"
    data = _styled(kind, cell, prose)
    options = {"track_changes": True} if tracked else {}
    text = _text(edit(data, name, _restated(data, name, cell), **options).data, kind)
    assert prose in text                                               # Nothing else changed automatically.


@pytest.mark.parametrize("kind,tracked", FORMATS)
def test_an_amount_is_never_joined_across_paragraphs_points_or_cells(kind, tracked):
    """A currency code ending one paragraph (or point) and a number starting the next are not "USD 300"."""
    cell = SPACED["iso_two_spaces"]
    name = f"t.{kind}"
    rows = [["A", cell(100)], ["B", cell(200)], ["Total", cell(300)]]
    first, second = "All prices are in USD", "300 seats remain."
    if kind == "docx":
        raw = {"title": "Total test", "blocks": [{"type": "paragraph", "text": first},
               {"type": "paragraph", "text": second},
               {"type": "table", "header": ["Item", "Amount"], "total_row": True, "rows": rows}]}
    else:
        raw = {"title": "Total test", "slides": [
            {"layout": "points", "title": "Summary", "points": [first, second]},
            {"layout": "table", "title": "Breakdown", "total_row": True, "header": ["Item", "Amount"],
             "rows": rows}]}
    data = generate(raw, kind).data
    options = {"track_changes": True} if tracked else {}
    text = _text(edit(data, name, _restated(data, name, cell), **options).data, kind)
    assert first in text and second in text


def test_horizontal_space_runs_never_cross_a_line():
    from kalillac_files.editing import _figures
    assert _figures("USD  300\u00a0\u00a0and\t$ \u00a0300  K") == {(300.0, "USD", ""): 1, (300.0, "$", "K"): 1}
    # Broken by a line or paragraph break, the parts are separate: a bare number, or a figure without
    # the currency or scale on the other line.
    for broken, read in [("USD\n300", (300.0, "", "")), ("USD \n 300", (300.0, "", "")),
                         ("USD\u2028300", (300.0, "", "")), ("USD\r\n300", (300.0, "", "")),
                         ("$\n300", (300.0, "", "")), ("$300\nK", (300.0, "$", "")),
                         ("-\n$300", (300.0, "$", ""))]:
        assert _figures(broken) == {read: 1}, broken


# --- tabs and line breaks inside a paragraph (Codex reverification of 0ea08a8) ----------------------

MARK = "QQMARKQQ"
WORD_TAB = '</w:t></w:r><w:r><w:tab/></w:r><w:r><w:t xml:space="preserve">'
WORD_BREAK = '</w:t></w:r><w:r><w:br/></w:r><w:r><w:t xml:space="preserve">'
REVISION = 'w:id="{}" w:author="Reviewer" w:date="2026-11-01T00:00:00Z"'
WORD_INSERTED_BREAK = (f'</w:t></w:r><w:ins {REVISION.format(9001)}><w:r><w:br/></w:r></w:ins>'
                       '<w:r><w:t xml:space="preserve">')
WORD_DELETED_BREAK = (f'</w:t></w:r><w:del {REVISION.format(9002)}><w:r><w:br/></w:r></w:del>'
                      '<w:r><w:t xml:space="preserve">')
DECK_BREAK = "</a:t></a:r><a:br/><a:r><a:t>"


def _marked(kind, cell, prose, replacement):
    """A real file from _styled whose summary has MARK replaced, in the file's XML, by a tab or break
    run (or a tab character), so the regression exercises the editor's text extraction too."""
    data = _styled(kind, cell, prose)
    with zipfile.ZipFile(io.BytesIO(data)) as source:
        parts = [name for name in source.namelist()
                 if name.endswith(".xml") and MARK.encode() in source.read(name)]
    assert len(parts) == 1

    def change(xml):
        return xml.decode("utf-8").replace(MARK, replacement).encode("utf-8")
    return rezip(data, change={parts[0]: change})


def _read_back(data, kind):
    """The file's text read by python-docx or python-pptx, not by the editor: Word shows a tab as "\\t"
    and a line break as "\\n"; PowerPoint a line break as "\\v"."""
    if kind == "docx":
        return "\n".join(paragraph.text for paragraph in docx.Document(io.BytesIO(data)).paragraphs)
    from pptx import Presentation
    deck = Presentation(io.BytesIO(data))
    return "\n".join(shape.text_frame.text for slide in deck.slides for shape in slide.shapes
                     if shape.has_text_frame)


@pytest.mark.parametrize("tracked", [False, True])
def test_a_word_tab_inside_a_stale_total_keeps_the_total_recognized(tracked):
    """"USD", a Word tab, "300": Word shows USD 300, so a table-only restatement to USD 400 is refused,
    and restating the summary too is accepted."""
    cell, name = STYLES["iso"], "t.docx"
    options = {"track_changes": True} if tracked else {}
    data = _marked("docx", cell, f"The total is USD{MARK}300.", WORD_TAB)
    assert "The total is USD\t300." in _read_back(data, "docx")
    table_only = _restated(data, name, cell)
    assert refused(data, name, table_only, **options) == "dependent_figure_unchanged"
    everywhere = table_only + [{"op": "replace_text", "find": "300.", "replace": "400.", "expect": 1}]
    result = edit(data, name, everywhere, **options).data
    if tracked:          # python-docx shows inserted and deleted text alike: read the accepted view.
        text = accepted(document_xml(result))                              # Text runs only, no tab glyph.
        assert "The total is USD400." in text and "USD300." not in text and "<w:tab/>" in document_xml(result)
    else:
        text = _read_back(result, "docx")
        assert "The total is USD\t400." in text and "USD\t300." not in text
    edit(data, name, table_only, exact=True, **options)                    # The customer's confirmed choice.


def test_a_powerpoint_tab_inside_a_stale_total_keeps_the_total_recognized():
    """Unchanged behaviour: a tab is a character inside a:t."""
    cell, name = STYLES["iso"], "t.pptx"
    data = _marked("pptx", cell, f"The total is USD{MARK}300.", "\t")
    assert "The total is USD\t300." in _read_back(data, "pptx")
    table_only = _restated(data, name, cell)
    assert refused(data, name, table_only) == "dependent_figure_unchanged"
    everywhere = table_only + [{"op": "replace_text", "find": "300.", "replace": "400.", "expect": 1}]
    text = _read_back(edit(data, name, everywhere).data, "pptx")
    assert "The total is USD\t400." in text and "USD\t300." not in text


@pytest.mark.parametrize("kind,tracked", FORMATS)
def test_a_line_break_inside_a_paragraph_separates_the_lines(kind, tracked):
    """One line ends with "$", the next begins "300 licenses were issued.": not "$300", so restating a $300
    total is accepted and the unrelated text is left exactly as it was."""
    cell, name = STYLES["positive"], f"t.{kind}"
    options = {"track_changes": True} if tracked else {}
    data = _marked(kind, cell, f"Every fee below is quoted in ${MARK}300 licenses were issued.",
                   WORD_BREAK if kind == "docx" else DECK_BREAK)
    line = "\n" if kind == "docx" else "\v"
    expected = f"Every fee below is quoted in ${line}300 licenses were issued."
    assert expected in _read_back(data, kind)
    text = _read_back(edit(data, name, _restated(data, name, cell), **options).data, kind)
    assert expected in text


@pytest.mark.parametrize("tracked", [False, True])
@pytest.mark.parametrize("revision,stale", [pytest.param(WORD_INSERTED_BREAK, False, id="inserted_break"),
                                            pytest.param(WORD_DELETED_BREAK, True, id="deleted_break")])
def test_a_tracked_line_break_counts_as_word_shows_it_once_accepted(tracked, revision, stale):
    """A break inserted by an earlier tracked revision separates "$" from "300"; a break that revision
    deleted does not, so the accepted text reads "$300", the old total, and the restatement is refused."""
    cell, name = STYLES["positive"], "t.docx"
    options = {"track_changes": True} if tracked else {}
    data = _marked("docx", cell, f"Every fee below is quoted in ${MARK}300 licenses were issued.", revision)
    table_only = _restated(data, name, cell)
    if stale:
        assert refused(data, name, table_only, **options) == "dependent_figure_unchanged"
    else:
        edit(data, name, table_only, **options)


def test_word_tabs_and_breaks_read_as_word_shows_them_and_tab_stops_do_not():
    from lxml import etree

    from kalillac_files.editing import _accepted_text
    namespace = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    paragraph = etree.fromstring(
        f'<w:p {namespace}><w:pPr><w:tabs><w:tab w:val="left" w:pos="720"/></w:tabs></w:pPr>'
        '<w:r><w:t>USD</w:t></w:r><w:r><w:tab/></w:r><w:r><w:t>300</w:t></w:r>'
        '<w:r><w:br/></w:r><w:r><w:t>end</w:t></w:r><w:r><w:cr/></w:r><w:r><w:t>x</w:t></w:r>'
        '<w:del w:id="1" w:author="R" w:date="2026-11-01T00:00:00Z"><w:r><w:tab/></w:r></w:del></w:p>')
    assert _accepted_text(paragraph) == "USD\t300\nend\nx"


def test_a_powerpoint_line_break_reads_as_a_line_break():
    from lxml import etree

    from kalillac_files.editing import _body_text
    namespace = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
    body = etree.fromstring(f'<a:txBody {namespace}><a:p><a:r><a:t>quoted in $</a:t></a:r><a:br/>'
                            '<a:r><a:t>300 licenses</a:t></a:r></a:p><a:p><a:r><a:t>USD\t300</a:t></a:r></a:p>'
                            '</a:txBody>')
    assert _body_text(body) == "quoted in $\n300 licenses\nUSD\t300"
