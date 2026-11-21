"""Workbooks: the formula engine against Microsoft Excel, and the designed XLSX output.

The engine's results are compared with values Microsoft Excel itself computed for the same
formulas (tests/formula_zoo.py, recorded by tools/record_formula_zoo.py). The benchmark forecast
is checked against an independent computation in plain Python, and every file is read back with
openpyxl and as raw package XML. Nothing touches the application or the network.
"""
from __future__ import annotations

import copy
import io
import re
import zipfile

import pytest

openpyxl = pytest.importorskip("openpyxl")
pytest.importorskip("xlsxwriter")

import formula_zoo  # noqa: E402
from kalillac_files import formula_engine as fe, generate, spec, xlsx_design  # noqa: E402
from kalillac_files.formulas import FUNCTIONS  # noqa: E402
from kalillac_files.spec import SpecError  # noqa: E402
from quality_benchmark import FORECAST_WORKBOOK, expected_forecast, expected_loan  # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore::UserWarning")  # openpyxl cannot read x14 extensions.


def evaluation(raw):
    return xlsx_design.evaluate(spec.validate_workbook(copy.deepcopy(raw)))


def book_of(raw, data_only=False):
    data = generate(copy.deepcopy(raw), "xlsx").data
    return data, openpyxl.load_workbook(io.BytesIO(data), data_only=data_only)


# --- the engine against Excel ---------------------------------------------------------------


def test_every_allowed_function_is_implemented_and_nothing_else():
    assert set(fe.FUNCTIONS) == set(FUNCTIONS)


def test_every_zoo_case_was_recorded_from_excel():
    assert set(formula_zoo.EXCEL_RESULTS) == set(formula_zoo.CASES) and len(formula_zoo.CASES) >= 140


@pytest.mark.parametrize("row, case", list(enumerate(formula_zoo.CASES, start=2)))
def test_engine_matches_excel(row, case):
    mine = ZOO.values[("Zoo", row, 2)]
    want = formula_zoo.EXCEL_RESULTS[case]
    if isinstance(mine, fe.ExcelError):
        mine = mine.code
    if isinstance(want, bool) or isinstance(mine, bool):
        assert type(mine) is type(want) and mine == want
    elif isinstance(want, (int, float)):
        assert isinstance(mine, float) and abs(mine - want) <= 1e-9 * max(1.0, abs(want)), (mine, want)
    else:
        assert mine == want


ZOO = evaluation(formula_zoo.workbook())


def test_newer_functions_are_stored_with_their_prefix():
    data = generate(copy.deepcopy(formula_zoo.workbook()), "xlsx").data
    sheet = zipfile.ZipFile(io.BytesIO(data)).read("xl/worksheets/sheet1.xml").decode()
    for name in ("IFS", "IFNA", "XOR", "CONCAT", "DAYS", "XLOOKUP"):
        assert f"_xlfn.{name}(" in sheet, name                    # Otherwise Excel shows #NAME?.
    assert "_xlfn.SUM(" not in sheet


@pytest.mark.parametrize("formula, code", [
    ("=Missing!A1", "formula_reference_error"),
    ("=SUM(B2:B3)", "formula_circular"),
])
def test_broken_models_are_refused(formula, code):
    raw = {"title": "T", "sheets": [{"name": "S", "columns": [{"header": "A", "format": "number"},
                                                             {"header": "B", "format": "number"}],
                                     "rows": [[1, {"formula": formula}], [2, 3]]}]}
    with pytest.raises(SpecError) as refused:
        generate(raw, "xlsx")
    assert refused.value.code == code


def test_a_long_circular_chain_across_sheets_is_found():
    raw = {"title": "T", "sheets": [
        {"name": "A", "columns": [{"header": "x", "format": "number"}], "rows": [[{"formula": "=B!A2+1"}]]},
        {"name": "B", "columns": [{"header": "x", "format": "number"}], "rows": [[{"formula": "=C!A2+1"}]]},
        {"name": "C", "columns": [{"header": "x", "format": "number"}], "rows": [[{"formula": "=A!A2+1"}]]}]}
    with pytest.raises(SpecError) as refused:
        generate(raw, "xlsx")
    assert refused.value.code == "formula_circular"


def test_value_errors_are_kept_and_reported_not_refused():
    raw = {"title": "T", "sheets": [{"name": "S", "columns": [{"header": "A", "format": "number"}],
                                     "rows": [[0], [{"formula": "=1/A2"}], [{"formula": "=IFERROR(1/A2,0)"}]]}]}
    result = evaluation(raw)
    assert result.errors == [("S", "A3", "#DIV/0!")] and result.structural == []
    _data, book = book_of(raw, data_only=True)
    assert book["S"]["A3"].value == "#DIV/0!" and book["S"]["A4"].value == 0


def test_runaway_work_is_bounded(monkeypatch):
    monkeypatch.setattr(fe, "MAX_ITERATIONS", 500)
    raw = {"title": "T", "sheets": [{"name": "S", "columns": [{"header": "A", "format": "number"}],
                                     "rows": [[n] for n in range(300)] + [[{"formula": "=SUM(A2:A301)*2"}]] * 5}]}
    with pytest.raises(SpecError) as refused:
        generate(raw, "xlsx")
    assert refused.value.code == "workbook_too_large"


# --- the benchmark forecast ---------------------------------------------------------------


def test_forecast_values_match_an_independent_computation():
    result = evaluation(FORECAST_WORKBOOK)
    assert result.errors == []
    months, years = expected_forecast()
    for index, month in enumerate(months):
        row = index + 2
        assert result.values[("Forecast", row, 2)] == month["customers"]
        assert result.values[("Forecast", row, 3)] == pytest.approx(month["revenue"], rel=1e-12)
        assert result.values[("Forecast", row, 7)] == pytest.approx(month["ebitda"], rel=1e-9, abs=1e-6)
    for offset, year in enumerate(sorted(years)):
        assert result.values[("Summary", offset + 2, 2)] == pytest.approx(years[year]["revenue"], rel=1e-12)
        assert result.values[("Summary", offset + 2, 4)] == pytest.approx(years[year]["ebitda"], rel=1e-9)
    payment, interest = expected_loan()
    assert result.values[("Loan", 2, 3)] == pytest.approx(payment, rel=1e-12)
    assert result.values[("Loan", 26, 4)] == pytest.approx(interest, rel=1e-9)
    assert result.values[("Loan", 25, 6)] == 0                    # Fully repaid.


def test_forecast_file_carries_its_computed_values():
    _data, cached = book_of(FORECAST_WORKBOOK, data_only=True)
    _data, formulas = book_of(FORECAST_WORKBOOK)
    result = evaluation(FORECAST_WORKBOOK)
    for (sheet, row, column), value in result.values.items():
        cell = cached[sheet].cell(row=row, column=column)
        assert cell.value == pytest.approx(value, rel=1e-12, abs=1e-9) if isinstance(value, float) else value, \
            (sheet, row, column)
        assert str(formulas[sheet].cell(row=row, column=column).value).startswith("="), (sheet, row, column)


def test_forecast_is_built_to_financial_model_conventions():
    data, book = book_of(FORECAST_WORKBOOK)
    assert book.sheetnames == ["About", "Dashboard", "Inputs", "Forecast", "Summary", "Loan"]
    assert book.properties.title == FORECAST_WORKBOOK["title"] and book.properties.creator == "Finance"
    inputs, forecast, summary = book["Inputs"], book["Forecast"], book["Summary"]
    # Inputs: blue values on a yellow fill; formulas reading another sheet in green; local ones in black.
    assert inputs["B3"].font.color.rgb.endswith("0047B3") and inputs["B3"].fill.fgColor.rgb.endswith("FFF7DB")
    assert inputs["B3"].number_format == "0.0%" and inputs["B4"].number_format.startswith('"$"#,##0.00')
    assert inputs["B7"].number_format.startswith('"$"#,##0_)')       # Whole amounts without cents.
    assert forecast["C2"].font.color.rgb.endswith("0B7A44")          # =B2*Inputs!$B$4
    assert forecast["E2"].font.color.rgb.endswith("0E1526")          # =C2+D2
    assert forecast["I2"].number_format == "0"                       # A year, not 2,027.
    assert forecast["A2"].number_format == "d mmm yyyy" and forecast["A2"].is_date
    assert forecast.freeze_panes == "B2" and forecast.auto_filter.ref == "A1:I37"
    total = summary["B5"]
    assert total.font.b and total.border.bottom.style == "double" and total.border.top.style == "thin"
    sheets = {name: zipfile.ZipFile(io.BytesIO(data)).read(f"xl/worksheets/sheet{i}.xml").decode()
              for i, name in enumerate(book.sheetnames, start=1)}
    # One page wide (fitToWidth defaults to 1), as many pages tall as needed.
    assert 'fitToPage="1"' in sheets["Forecast"] and 'fitToHeight="0"' in sheets["Forecast"]
    assert 'orientation="landscape"' in sheets["Forecast"] and "printOptions gridLines" not in sheets["Forecast"]
    assert "&amp;P of &amp;N" in sheets["Forecast"]                   # "Page X of Y" in the footer.
    assert sheets["Forecast"].count("<conditionalFormatting") == 3
    assert '<dataValidation type="decimal" operator="greaterThanOrEqual"' in sheets["Inputs"]


def test_forecast_charts_are_native_and_linked_to_the_cells():
    data, _book = book_of(FORECAST_WORKBOOK)
    archive = zipfile.ZipFile(io.BytesIO(data))
    charts = sorted(name for name in archive.namelist() if re.match(r"xl/charts/chart\d+\.xml", name))
    assert len(charts) == 4                                          # Two on the dashboard, one each elsewhere.
    xml = [archive.read(name).decode() for name in charts]
    assert any("<c:f>Summary!$B$2:$B$4</c:f>" in chart for chart in xml)     # Totals never join a chart.
    assert any("<c:f>Forecast!$C$2:$C$37</c:f>" in chart and "<c:dateAx>" in chart for chart in xml)
    assert all("<c:numCache>" in chart for chart in xml)             # Previews draw without recalculating.


def test_dashboard_tiles_show_live_formulas():
    _data, book = book_of(FORECAST_WORKBOOK)
    _data, cached = book_of(FORECAST_WORKBOOK, data_only=True)
    dashboard = book["Dashboard"]
    assert dashboard["B2"].value == "Operating plan 2027–2029"
    positions = xlsx_design._dashboard_positions(spec.validate_workbook(copy.deepcopy(FORECAST_WORKBOOK)).sheets[0])
    formulas = [dashboard.cell(row=r, column=c).value for r, c in positions]
    assert formulas[0] == "=Summary!B5" and formulas[5].startswith("=NPV(")
    assert cached["Dashboard"].cell(*positions[0]).value == pytest.approx(expected_forecast()[1][2027]["revenue"]
                                                                          + expected_forecast()[1][2028]["revenue"]
                                                                          + expected_forecast()[1][2029]["revenue"])
    assert dashboard.sheet_view.showGridLines is False


# --- specification ------------------------------------------------------------------------


@pytest.mark.parametrize("change, code", [
    (lambda w: w["sheets"][1]["columns"][1].update(role="owner"), "invalid_column"),
    (lambda w: w["sheets"][1]["columns"][1].update(symbol="BTC"), "invalid_column"),
    (lambda w: w["sheets"][2]["conditional"].append({"column": "Month", "rule": "data_bar"}), "invalid_conditional"),
    (lambda w: w["sheets"][2]["conditional"].append({"column": "Revenue", "rule": "above"}), "invalid_conditional"),
    (lambda w: w["sheets"][2]["conditional"].append({"column": "Nope", "rule": "negative"}), "invalid_conditional"),
    (lambda w: w["sheets"][1]["validation"].append({"column": "Note", "min": 1}), "invalid_validation"),
    (lambda w: w["sheets"][1]["validation"].append({"column": "Assumption", "list": ["a,b"]}), "invalid_validation"),
    (lambda w: w["sheets"][2]["charts"].append({"kind": "radar", "category": "Month", "series": ["Revenue"]}),
     "invalid_sheet_chart"),
    (lambda w: w["sheets"][2]["charts"].append({"kind": "line", "category": "Month", "series": ["Month"]}),
     "invalid_sheet_chart"),
    (lambda w: w["sheets"][2]["charts"].append({"kind": "pie", "category": "Month", "series": ["Revenue",
                                                                                              "EBITDA"]}),
     "invalid_sheet_chart"),
    (lambda w: w["sheets"][2]["charts"].append({"kind": "line", "sheet": "Summary", "category": "Year",
                                                "series": ["Revenue"]}), "invalid_sheet_chart"),
    (lambda w: w["sheets"][0]["metrics"].append({"label": "x", "formula": "=WEBSERVICE(\"x\")",
                                                 "format": "number"}), "invalid_dashboard"),
    (lambda w: w["sheets"][0]["charts"].append({"kind": "line", "category": "Month", "series": ["Revenue"]}),
     "invalid_sheet_chart"),
    (lambda w: w["sheets"][1]["rows"][0].__setitem__(1, {"value": 5}), "invalid_cell"),
    (lambda w: w["sheets"][1]["rows"][0].__setitem__(1, {"value": "x", "format": "number"}), "invalid_cell"),
    (lambda w: w["sheets"][1]["rows"][0].__setitem__(1, {"value": 5, "format": "money"}), "invalid_cell"),
    (lambda w: w["sheets"][4]["rows"][0].__setitem__(1, "text in a money column"), "invalid_cell"),
])
def test_invalid_workbook_features_are_refused(change, code):
    raw = copy.deepcopy(FORECAST_WORKBOOK)
    change(raw)
    with pytest.raises(SpecError) as refused:
        spec.validate_workbook(raw)
    assert refused.value.code == code


def test_text_is_never_turned_into_a_formula_number_or_link():
    raw = {"title": "T", "sheets": [{"name": "S", "columns": [{"header": "A", "format": "text"}],
                                     "rows": [["=1+1"], ["123"], ["https://example.test"], ["@SUM(A1)"]]}]}
    data, book = book_of(raw)
    values = [(book["S"][f"A{r}"].value, book["S"][f"A{r}"].data_type) for r in range(2, 6)]
    assert values == [("=1+1", "s"), ("123", "s"), ("https://example.test", "s"), ("@SUM(A1)", "s")]
    assert "hyperlink" not in zipfile.ZipFile(io.BytesIO(data)).read("xl/worksheets/sheet1.xml").decode()



# --- Codex audit D4: syntax is validated apart from calculation -----------------------------------

MALFORMED = [
    "=1+", "=)1+2(", "=A0", "=-", "=1*/2", "=(1+2", "=1+2)", "=SUM(1,2", "=SUM((1,2)", "=SUM(1;2)",
    "=A1048577", "=XFE1", "=ZZZ1", "=SUM(A0:A3)", "=SUM(A1:B)", "=SUM(A:1)", "=ROUND(1)", "=IF()",
    "=SUM()", "=NOT(TRUE,FALSE)", "=1 2",
]


@pytest.mark.parametrize("formula", MALFORMED)
def test_a_malformed_formula_is_refused_in_every_workbook_format(formula):
    raw = {"title": "T", "sheets": [{"name": "S", "columns": [{"header": "A", "format": "number"}],
                                     "rows": [[1], [{"formula": formula}]]}]}
    for kind in ("xlsx", "csv", "json"):
        with pytest.raises(SpecError) as refused:
            generate(copy.deepcopy(raw), kind)
        assert refused.value.code == "invalid_formula", (formula, kind)


@pytest.mark.parametrize("formula", MALFORMED)
def test_check_syntax_raises_only_its_stable_error(formula):
    with pytest.raises(fe.FormulaSyntaxError) as refused:
        fe.check_syntax(formula)
    assert refused.value.code == "invalid_formula" and str(refused.value) == "invalid_formula"


@pytest.mark.parametrize("formula", [
    "=1/0", "=SUM(A1:B2)", "=SUM(A:A)", "=SUM(1:1)", "=XFD1048576", "=-A1", "=1%", "=(1+2)*3",
    "=SUM(A1,)", "=IF(A1,,2)", "=SUM(,)", "=IF(A1>0,\"y\",\"n\")", "=ROUND(PMT(B3/12,E3,-B2),2)",
    "='Data sheet'!B2*2", "=TODAY()", "=1--2", "=2^-1",
])
def test_well_formed_formulas_pass_the_syntax_check(formula):
    fe.check_syntax(formula)


def test_legitimate_calculation_errors_are_kept_not_refused():
    raw = {"title": "T", "sheets": [{"name": "S", "columns": [{"header": "A", "format": "number"}],
                                     "rows": [[0], [{"formula": "=1/A2"}], [{"formula": "=SQRT(-1)"}],
                                              [{"formula": "=VLOOKUP(99,A2:A2,1,FALSE)"}]]}]}
    result = evaluation(raw)
    assert [result.values[("S", row, 1)] for row in (3, 4, 5)] == [fe.DIV0, fe.NUM, fe.NA]
    for kind in ("xlsx", "csv", "json"):
        generate(copy.deepcopy(raw), kind)                   # Valid models, whose results are errors.


def test_a_missing_sheet_keeps_its_own_code_in_every_workbook_format():
    raw = {"title": "T", "sheets": [{"name": "S", "columns": [{"header": "A", "format": "number"}],
                                     "rows": [[1], [{"formula": "='Missing Sheet'!A1"}]]}]}
    for kind in ("xlsx", "csv", "json"):
        with pytest.raises(SpecError) as refused:
            generate(copy.deepcopy(raw), kind)
        assert refused.value.code == "formula_reference_error", kind


def test_text_that_starts_with_an_equals_sign_is_text_to_the_engine():
    raw = {"title": "T", "sheets": [{"name": "S", "columns": [{"header": "A", "format": "text"},
                                                             {"header": "B", "format": "number"}],
                                     "rows": [["=not a formula", {"formula": "=LEN(A2)"}]]}]}
    result = evaluation(raw)
    assert result.values[("S", 2, 2)] == 14.0 and result.errors == []



def test_the_about_sheet_shows_its_colour_samples_as_numbers_not_text():
    """Codex document audit, finding 7: About!B14:B17 held the text "12,500", which Excel flags as a
    number stored as text. Every numeric-looking cell of the About sheet is a real number."""
    data = generate(copy.deepcopy(FORECAST_WORKBOOK), "xlsx").data
    about = openpyxl.load_workbook(io.BytesIO(data))["About"]
    samples = [row for row in about.iter_rows() if row[2].value in (
        "Typed values", "Inputs meant to be changed", "Formulas on the same sheet", "Formulas that read another sheet")]
    assert len(samples) == 4
    for row in samples:
        assert (row[1].value, row[1].data_type, row[1].number_format) == (12500, "n", "#,##0")
    looks_numeric = re.compile(r"[-+]?[0-9][0-9,]*(\.[0-9]+)?%?")
    assert not [cell.coordinate for row in about.iter_rows() for cell in row
                if cell.data_type == "s" and looks_numeric.fullmatch(str(cell.value or "").strip())]


# --- dashboard layout, headings and printing (document-quality benchmark, 2026-11-21) ---------
# In Microsoft Excel the benchmark budget showed dashboard charts covering each other, a doughnut whose
# colours repeated and whose labels collided, headings hidden under filter buttons, axis labels running
# together, and a 19-column sheet printed too small to read. Each defect has its test here.

_AMOUNTS = [185588, 140070, 15408, 38400, 7800, 21836, 14193, 4560, 3480, 3000, 3600]
_WIDE = ["Month"] + [f"Operating cost line {index:02d}" for index in range(1, 19)]
BUDGET = {
    "title": "Budget",
    "sheets": [
        {"name": "Dashboard", "kind": "dashboard", "title": "Budget",
         "metrics": [{"label": f"Tile {index}", "formula": "=Wide!B14", "format": "currency"} for index in range(5)],
         "charts": [
             {"kind": "line", "sheet": "Wide", "title": "By month", "category": "Month", "series": _WIDE[1:3]},
             {"kind": "doughnut", "sheet": "Parts", "title": "Parts", "category": "Category", "series": ["Amount"]},
             {"kind": "column", "sheet": "Wide", "title": "One line", "category": "Month", "series": _WIDE[1:2]},
             {"kind": "area", "sheet": "Wide", "title": "Area", "category": "Month", "series": _WIDE[2:3]}]},
        {"name": "Wide", "total_row": True,
         "columns": [{"header": "Month", "format": "text"}] + [{"header": header, "format": "currency"}
                                                               for header in _WIDE[1:]],
         "rows": [[f"M{month:02d}"] + [40000 + 1000 * month + column for column in range(18)]
                  for month in range(1, 13)]
         + [["Total"] + [{"formula": f"=SUM({chr(66 + index)}2:{chr(66 + index)}13)"} for index in range(18)]]},
        {"name": "Parts", "columns": [{"header": "Category", "format": "text"}, {"header": "Amount", "format": "currency"}],
         "rows": [[f"Part {index:02d}", amount] for index, amount in enumerate(_AMOUNTS)],
         "charts": [{"kind": "bar", "title": "Parts", "category": "Category", "series": ["Amount"]}]},
        {"name": "Small", "columns": [{"header": "Item", "format": "text"}, {"header": "Unit price", "format": "currency"}],
         "rows": [["A", 12.5], ["B", 8.25], ["C", 3]]},
    ],
}


def _parts(raw):
    data, book = book_of(raw)
    archive = zipfile.ZipFile(io.BytesIO(data))
    sheets = {name: archive.read(f"xl/worksheets/sheet{index}.xml").decode()
              for index, name in enumerate(book.sheetnames, start=1)}
    return book, archive, sheets


def _width(sheet, index):
    """Width of a column (1-based). XlsxWriter writes neighbours of one width as a single range."""
    for dimension in sheet.column_dimensions.values():
        if dimension.min <= index <= dimension.max:
            return dimension.width
    raise AssertionError(index)


def _anchors(archive, charts):
    """((from column, offset, row, offset), (to column, offset, row, offset)) of every chart on the drawing
    that holds `charts` charts."""
    for name in archive.namelist():
        if re.fullmatch(r"xl/drawings/drawing\d+\.xml", name):
            xml = archive.read(name).decode()
            found = re.findall(r"<xdr:twoCellAnchor[^>]*>.*?</xdr:twoCellAnchor>", xml, re.S)
            if len(found) == charts:
                corner = r"<xdr:col>(\d+)</xdr:col><xdr:colOff>(-?\d+)</xdr:colOff><xdr:row>(\d+)</xdr:row>" \
                         r"<xdr:rowOff>(-?\d+)</xdr:rowOff>"
                return [tuple(tuple(int(v) for v in re.search(f"<xdr:{end}>{corner}</xdr:{end}>", anchor).groups())
                              for end in ("from", "to")) for anchor in found], found
    raise AssertionError("no drawing with that many charts")


def test_dashboard_charts_stand_on_the_tile_grid_and_never_overlap():
    _book, archive, _sheets = _parts(BUDGET)
    anchors, raw = _anchors(archive, 4)
    for index, (start, end) in enumerate(anchors):
        left = index % 2 == 0
        # Left charts span the first two tiles (columns B to F), right charts the last two (H to L); the gap
        # column G lies between them. Before: a 560-pixel chart in 528 pixels of columns, 36 pixels under
        # its neighbour.
        assert start[0] == (1 if left else 7) and start[1] == 0, anchors
        assert (end[0], end[1]) <= ((6, 0) if left else (12, 0)), anchors
    for upper, lower in ((anchors[0], anchors[2]), (anchors[1], anchors[3])):
        assert (lower[0][2], lower[0][3]) >= (upper[1][2], upper[1][3]), anchors     # No vertical overlap either.
    # Tied to the cells they stand on, so they keep to their columns in any program and at any zoom.
    assert all('editAs="oneCell"' not in anchor and 'editAs="absolute"' not in anchor for anchor in raw)
    single = copy.deepcopy(BUDGET)
    single["sheets"][0]["charts"] = single["sheets"][0]["charts"][:1]
    (start, end), = _anchors(_parts(single)[1], 1)[0][:1]
    assert start[0] == 1 and (end[0], end[1]) <= (12, 0) and end[0] >= 11          # One chart under all four tiles.
    half = sum(xlsx_design._pixels(xlsx_design._dashboard_width(column)) for column in range(1, 6))
    assert half == 4 * xlsx_design._pixels(16) + xlsx_design._pixels(3.5) < 528


def _chart_xml(archive, title):
    for name in archive.namelist():
        if re.fullmatch(r"xl/charts/chart\d+\.xml", name):
            xml = archive.read(name).decode()
            if f"<a:t>{title}</a:t>" in xml:
                return xml
    raise AssertionError(title)


def test_a_doughnut_gives_every_slice_its_own_colour_and_labels_only_slices_with_room():
    _book, archive, _sheets = _parts(BUDGET)
    xml = next(archive.read(name).decode() for name in archive.namelist()
               if re.fullmatch(r"xl/charts/chart\d+\.xml", name) and "<c:doughnutChart>" in archive.read(name).decode())
    points = re.findall(r"<c:dPt>.*?</c:dPt>", xml, re.S)
    colours = [re.search(r'<a:srgbClr val="([0-9A-F]{6})"', point).group(1) for point in points]
    assert len(points) == 11 and len(set(colours)) == 11        # Before: six colours for eleven slices.
    small = sum(amount / sum(_AMOUNTS) < xlsx_design.SLICE_LABEL_SHARE for amount in _AMOUNTS)
    labels = re.search(r"<c:dLbls>.*?</c:dLbls>", xml, re.S).group(0)
    assert small == 7 and labels.count('<c:delete val="1"/>') == small   # Before: eleven labels in a heap.
    assert '<c:legendPos val="r"/>' in xml and '<c:holeSize val="58"/>' in xml
    # The chart still reads its cells: the layout decisions change no reference.
    assert "<c:f>Parts!$B$2:$B$12</c:f>" in xml and "<c:f>Parts!$A$2:$A$12</c:f>" in xml
    assert len(set(xlsx_design.SLICES)) == 12


def test_axis_labels_of_large_amounts_are_compact_and_small_ones_are_exact():
    _book, archive, _sheets = _parts(BUDGET)
    bar = next(archive.read(name).decode() for name in archive.namelist()
               if re.fullmatch(r"xl/charts/chart\d+\.xml", name) and 'barDir val="bar"' in archive.read(name).decode())
    axis = re.search(r"<c:valAx>.*?</c:valAx>", bar, re.S).group(0)
    assert "&quot;K&quot;" in axis                                   # $200K, not $200,000 run into $180,000.
    assert "&quot;K&quot;" not in re.search(r"<c:dLbls>.*?</c:dLbls>", bar, re.S).group(0)   # Data labels: full figures.
    column = spec.validate_workbook(copy.deepcopy(BUDGET)).sheets[3].columns[1]
    assert xlsx_design._axis_format(column, 12.5) == xlsx_design._number_format("currency", "$", 0)
    assert '"M"' in xlsx_design._axis_format(column, 25_000_000)
    area = next(archive.read(name).decode() for name in archive.namelist()
                if re.fullmatch(r"xl/charts/chart\d+\.xml", name) and "<c:areaChart>" in archive.read(name).decode())
    assert "<c:dLbls>" not in area                                   # Before: twelve labels over the boundary line.


def test_filter_buttons_never_cover_a_heading():
    book, _archive, _sheets = _parts(BUDGET)
    wide, small = book["Wide"], book["Small"]
    assert wide.auto_filter.ref == "A1:S13" and not small.auto_filter.ref
    for index, header in enumerate(_WIDE[1:], start=2):
        cell = wide.cell(row=1, column=index)
        # Right-aligned, indented clear of the button, in a column wide enough for both.
        assert cell.alignment.horizontal == "right" and cell.alignment.indent == xlsx_design.FILTER_BUTTON - 1
        assert _width(wide, index) >= min(len(header), 24) + 2 + xlsx_design.FILTER_BUTTON
    assert wide["A1"].alignment.horizontal == "left" and wide["A1"].alignment.indent == 1
    assert _width(wide, 1) >= len("Month") + 2 + xlsx_design.FILTER_BUTTON
    assert small["B1"].alignment.indent == 0                      # No filter, nothing to clear.
    assert wide["A2"].alignment.indent == 1 and wide["B2"].alignment.indent == 0
    # An explicit width that is too narrow for the heading and its button is widened; a generous one is kept.
    narrow = copy.deepcopy(BUDGET)
    narrow["sheets"][1]["columns"][0]["width"] = 5
    narrow["sheets"][1]["columns"][1]["width"] = 60
    again = book_of(narrow)[1]["Wide"]
    assert _width(again, 1) >= 10 and _width(again, 2) >= 60


def test_a_wide_sheet_prints_readably_on_two_pages_instead_of_tiny_on_one():
    book, archive, sheets = _parts(BUDGET)
    workbook = archive.read("xl/workbook.xml").decode()
    # Nineteen columns: several pages across in landscape, each repeating the header row and the Month
    # column. Before: one page across, at a scale nobody could read.
    assert 'fitToHeight="0"' in sheets["Wide"]
    assert 'orientation="landscape"' in sheets["Wide"]
    assert re.search(r"Print_Titles[^>]*>Wide!\$A:\$A,Wide!\$1:\$1<", workbook)
    # A narrow sheet stays portrait on one page across, as before.
    assert "fitToWidth" not in sheets["Small"] and 'fitToPage="1"' in sheets["Small"]
    assert 'orientation="landscape"' not in sheets["Small"]
    pixels = sum(xlsx_design._pixels(int(_width(book["Wide"], index))) for index in range(1, 20))
    across = int(re.search(r'fitToWidth="(\d+)"', sheets["Wide"]).group(1))
    scale = lambda pages: pages * xlsx_design.PRINT_LANDSCAPE / (pixels / 96)  # noqa: E731
    # As few pages as keep the type at 70% or more: one fewer would shrink it below that.
    assert across >= 2 and scale(across) >= xlsx_design.PRINT_MIN_SCALE > scale(across - 1)
    # A chart beside a table prints with it while both fit a page at a readable scale (Parts); beside a
    # table that needs the page to itself, the table is the print area and the chart stays on screen.
    assert re.findall(r"Print_Area[^>]*>([^<!]+)!", workbook) == ["Dashboard"]
    beside = copy.deepcopy(BUDGET)
    beside["sheets"][1]["charts"] = [{"kind": "line", "category": "Month", "series": _WIDE[1:2]}]
    assert re.search(r"Print_Area[^>]*>Wide!\$A\$1:\$S\$14<", _parts(beside)[1].read("xl/workbook.xml").decode())
    # The forecast benchmark is unchanged: nine columns still fit one landscape page.
    forecast = _parts(FORECAST_WORKBOOK)[2]["Forecast"]
    assert "fitToWidth" not in forecast and 'orientation="landscape"' in forecast


def test_a_printed_dashboard_never_cuts_a_chart_in_two():
    _book, archive, sheets = _parts(BUDGET)
    dashboard = sheets["Dashboard"]
    scale = int(re.search(r'<pageSetup[^>]*scale="(\d+)"', dashboard).group(1))
    assert 70 <= scale <= 100 and "fitToPage" not in dashboard and 'orientation="landscape"' in dashboard
    width = sum(xlsx_design._pixels(xlsx_design._dashboard_width(column)) for column in range(13)) / 96
    assert width * scale / 100 <= xlsx_design.PRINT_LANDSCAPE      # The whole grid fits across the page.
    top = 4 + 4 * 2                                               # Five tiles: two rows of tiles.
    breaks = re.search(r'<rowBreaks count="1" manualBreakCount="1"><brk id="(\d+)"', dashboard)
    assert breaks and int(breaks.group(1)) == top + xlsx_design.CHART_ROWS   # A new page before the second chart row.
    assert re.search(r"Print_Area[^>]*>Dashboard!\$A\$1:\$M\$\d+<", archive.read("xl/workbook.xml").decode())
    assert xlsx_design._chart_breaks(12, 1, 40) == [] and xlsx_design._chart_breaks(12, 3, 40) == [30]
    assert xlsx_design._chart_breaks(12, 3, 30) == [30, 48]       # A page that holds one chart row at a time.
    assert xlsx_design._chart_breaks(12, 3, 60) == [48]           # Two chart rows share a page that holds them.
    # Tall title and tile rows count against page one: when it cannot also hold a chart row, the charts
    # start on page two.
    assert xlsx_design._chart_breaks(12, 2, 36, extra=5) == [30]
    assert xlsx_design._chart_breaks(12, 2, 34, extra=5) == [12, 30]


def test_layout_changes_leave_every_formula_and_validation_as_it_was():
    book, archive, sheets = _parts(BUDGET)
    assert book["Wide"]["B14"].value == "=SUM(B2:B13)" and book["Wide"]["S14"].value == "=SUM(S2:S13)"
    values = book_of(BUDGET, data_only=True)[1]
    assert values["Wide"]["B14"].value == sum(40000 + 1000 * month for month in range(1, 13))
    assert values["Dashboard"]["B6"].value == values["Wide"]["B14"].value
    data, forecast = book_of(FORECAST_WORKBOOK)
    assert '<dataValidation type="decimal" operator="greaterThanOrEqual"' in zipfile.ZipFile(
        io.BytesIO(data)).read("xl/worksheets/sheet3.xml").decode()
    assert forecast["Forecast"]["C2"].value == "=B2*Inputs!$B$4"
