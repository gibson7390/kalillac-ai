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
