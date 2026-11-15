"""Excel workbooks in the Kalillac design system, with computed values.

write_xlsx(workbook) builds the file with XlsxWriter:

  * every formula is evaluated by formula_engine first; a model with a
    reference to a missing sheet, an unknown function or a circular chain is
    refused, and every other formula is written together with its computed
    value, so previews that do not recalculate still show the numbers (Excel
    recalculates on opening as well);
  * financial-model conventions: typed constants in blue, formulas in black,
    formulas that read another sheet in green, editable input columns on a
    pale yellow fill, a bold total row between a rule and a double rule;
  * number formats per column (thousands separators, currency symbol,
    negatives in red parentheses, percentages, dates), widths measured from
    the content, a frozen header row, filters, print setup that fits the page
    width and repeats the header;
  * conditional formatting, drop-down and range validation, native Excel
    charts that stay linked to the cells, dashboard sheets with metric tiles,
    and an optional About sheet that explains the workbook and its colours.

Text is always written as text: no string is ever turned into a formula, a
number or a link.
"""
from datetime import date, datetime
import io

from . import formula_engine as fe
from .spec import Formula, SpecError, Typed

NAVY, BLUE, BLUE_DEEP, INK, MUTED, HAIR, PAPER = "#0B1737", "#0069FF", "#0053CF", "#0E1526", "#5C677E", "#E2E9F4", \
    "#F6F8FC"
INPUT_FONT, INPUT_FILL, LINK_FONT = "#0047B3", "#FFF7DB", "#0B7A44"
SERIES = ("#0069FF", "#0B1737", "#12A15C", "#F59F00", "#7C5CFF", "#E8590C")
FONT = "Calibri"
CHART_ROWS = 18


def _number_format(column_format, symbol="$", decimals=None):
    if column_format == "text":
        return "@"
    if column_format == "date":
        return "d mmm yyyy"
    if column_format == "integer":
        return "#,##0;[Red]-#,##0"
    if column_format == "percent":
        places = 1 if decimals is None else decimals
        return "0" + ("." + "0" * places if places else "") + "%"
    places = 2 if decimals is None else decimals
    body = "#,##0" + ("." + "0" * places if places else "")
    if column_format == "currency":
        quoted = '"' + symbol + '"' if len(symbol) == 1 else '"' + symbol + ' "'
        return f"{quoted}{body}_);[Red]({quoted}{body})"
    return f"{body};[Red]-{body}"


def _auto_decimals(column, values):
    """Whole amounts of 100 or more read best without cents; otherwise the column's default."""
    if column.decimals is not None or column.format not in ("currency", "number"):
        return column.decimals
    numbers = [float(v) for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if not numbers:
        return None
    # Whole amounts, or amounts of 100 and more where cents add only noise. A column that also holds
    # small fractional values (a rate, a unit price) keeps its decimals, or they would display as 0.
    if max(abs(v) for v in numbers) >= 100 and all(abs(v) >= 100 or abs(v - round(v)) < 1e-6 for v in numbers):
        return 0
    return None


def _cell_decimals(cell_format, value):
    if cell_format in ("currency", "number") and isinstance(value, (int, float)) and not isinstance(value, bool) \
            and float(value).is_integer() and abs(value) >= 100:
        return 0
    return None


def _is_year_column(column, values):
    """Integer columns holding years read 2027, not 2,027."""
    numbers = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return column.format == "integer" and bool(numbers) and all(
        float(v).is_integer() and 1800 <= v <= 2200 for v in numbers)


def _display_width(value, number_format):
    if value is None:
        return 0
    if isinstance(value, bool):
        return 5
    if isinstance(value, (int, float)):
        magnitude = f"{abs(value):,.2f}" if "." in number_format else f"{abs(value):,.0f}"
        return len(magnitude) + 3
    if isinstance(value, (date, datetime)):
        return 11
    return max((len(line) for line in str(value).split("\n")), default=0)


class _Formats:
    def __init__(self, book):
        self.book, self.cache = book, {}

    def get(self, **properties):
        key = tuple(sorted(properties.items()))
        if key not in self.cache:
            base = {"font_name": FONT, "font_size": 10, "font_color": INK, "valign": "vcenter"}
            self.cache[key] = self.book.add_format({**base, **properties})
        return self.cache[key]


def _engine_value(value):
    if isinstance(value, Formula):
        return value.text
    if isinstance(value, Typed):
        return _engine_value(value.value)
    if isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    return value


def _build_model(workbook, positions):
    """The formula engine's view of the workbook, cell for cell as it will be written."""
    model = fe.Workbook()
    for sheet in workbook.sheets:
        model.add_sheet(sheet.name)
        if sheet.kind == "table":
            for column_index, column in enumerate(sheet.columns, start=1):
                model.set(sheet.name, 1, column_index, column.header)
            for row_index, row in enumerate(sheet.rows, start=2):
                for column_index, value in enumerate(row, start=1):
                    if value is not None:
                        model.set(sheet.name, row_index, column_index, _engine_value(value))
    for (sheet_name, row, column), formula in positions.items():
        model.set(sheet_name, row, column, formula)
    return model


def _evaluate(workbook, positions):
    model = _build_model(workbook, positions)
    try:
        evaluation = fe.evaluate_workbook(model)
    except fe.CircularReference:
        raise SpecError("formula_circular") from None
    except fe.WorkTooLarge:
        raise SpecError("workbook_too_large") from None
    if evaluation.structural:
        raise SpecError("formula_reference_error")
    return evaluation


def _cached(value):
    if isinstance(value, fe.ExcelError):
        return value.code
    if value is None:
        return 0
    return value


def _dashboard_positions(sheet):
    """Cell (1-based row, column) of each metric value on a dashboard."""
    positions = []
    for index in range(len(sheet.metrics)):
        row = 6 + (index // 4) * 4
        column = 2 + (index % 4) * 3
        positions.append((row, column))
    return positions


class _Writer:
    def __init__(self, workbook):
        import xlsxwriter
        self.spec = workbook
        self.buffer = io.BytesIO()
        self.book = xlsxwriter.Workbook(self.buffer, {
            "in_memory": True, "strings_to_numbers": False, "strings_to_formulas": False, "strings_to_urls": False,
            "nan_inf_to_errors": False,
            # Functions added after Excel 2007 (IFS, XLOOKUP, CONCAT, DAYS, ...) must be stored with their
            # "_xlfn." prefix, or Excel shows #NAME? where the formula should be.
            "use_future_functions": True})
        self.book.set_properties({"title": workbook.title, "author": workbook.author or "Kalillac AI",
                                  "creator": "Kalillac AI", "comments": workbook.summary or ""})
        self.formats = _Formats(self.book)
        positions = {}
        for sheet in workbook.sheets:
            if sheet.kind == "dashboard":
                for metric, (row, column) in zip(sheet.metrics, _dashboard_positions(sheet)):
                    positions[(sheet.name, row, column)] = metric.formula.text
        self.evaluation = _evaluate(workbook, positions)

    def value_of(self, sheet, row, column):
        return self.evaluation.values.get((sheet, row, column))

    # --- table sheets -----------------------------------------------------------------------

    def table(self, sheet):
        page = self.book.add_worksheet(sheet.name)
        page.hide_gridlines(1)  # Gridlines on screen, never on paper.
        columns = sheet.columns
        count, last_data = len(sheet.rows), len(sheet.rows) + 1
        has_inputs = any(column.role == "input" for column in columns)
        if has_inputs:
            page.set_tab_color("#F5C542")
        page.set_row(0, 30)
        header = self.formats.get(bold=True, font_color="#FFFFFF", bg_color=NAVY, text_wrap=True, border=0,
                                  bottom=2, bottom_color=BLUE)
        def plain(cell, r, i):
            if isinstance(cell, Formula):
                return None if cell.format else self.value_of(sheet.name, r, i + 1)
            return None if isinstance(cell, Typed) else cell
        decimals = [_auto_decimals(column, [plain(row[i], r, i) for r, row in enumerate(sheet.rows, start=2)])
                    for i, column in enumerate(columns)]
        # Money columns of one table share one precision: whole amounts beside cents look like an error.
        money = [i for i, column in enumerate(columns) if column.format == "currency" and column.decimals is None]
        if money and sum(decimals[i] == 0 for i in money) * 2 >= len(money):
            for i in money:
                values = [plain(row[i], r, i) for r, row in enumerate(sheet.rows, start=2)]
                if all(abs(v) >= 1 or v == 0 for v in values
                       if isinstance(v, (int, float)) and not isinstance(v, bool)):
                    decimals[i] = 0
        number_formats = [_number_format(column.format, column.symbol, decimals[i]) for i, column in enumerate(columns)]
        for i, column in enumerate(columns):
            computed = [self.value_of(sheet.name, r, i + 1) if isinstance(row[i], Formula) else row[i]
                        for r, row in enumerate(sheet.rows, start=2)]
            if _is_year_column(column, computed):
                number_formats[i] = "0"
        widths = []
        for index, column in enumerate(columns):
            page.write_string(0, index, column.header, self.formats.get(
                **{**dict(bold=True, font_color="#FFFFFF", bg_color=NAVY, text_wrap=True, bottom=2,
                          bottom_color=BLUE), "align": "right" if column.format not in ("text",) else "left"}))
            shown = [self.value_of(sheet.name, r, index + 1) if isinstance(row[index], Formula) else
                     (row[index].value if isinstance(row[index], Typed) else row[index])
                     for r, row in enumerate(sheet.rows, start=2)]
            longest = max([_display_width(v, number_formats[index]) for v in shown] + [0])
            header_need = min(len(column.header), 24) + 2
            widths.append(column.width or max(9, min(48, max(longest + 2, header_need))))
            page.set_column(index, index, widths[-1])
        del header
        for offset, row in enumerate(sheet.rows):
            row_index = offset + 1
            is_total = sheet.total_row and offset == count - 1
            for index, value in enumerate(row):
                column = columns[index]
                properties = {"num_format": number_formats[index], "bottom": 1, "bottom_color": HAIR}
                override = value.format if isinstance(value, (Formula, Typed)) else None
                if override:
                    shown = value.value if isinstance(value, Typed) else self.value_of(sheet.name, row_index + 1,
                                                                                       index + 1)
                    properties["num_format"] = _number_format(override, value.symbol or column.symbol,
                                                              _cell_decimals(override, shown))
                if isinstance(value, Typed):
                    value = value.value
                if column.format == "text":
                    properties.update(text_wrap=True, valign="top")
                if column.role == "input" and not is_total:
                    properties.update(bg_color=INPUT_FILL)
                if isinstance(value, Formula):
                    properties["font_color"] = LINK_FONT if "!" in value.text else INK
                elif isinstance(value, (int, float)) and not isinstance(value, bool):
                    properties["font_color"] = INPUT_FONT
                if is_total:
                    properties.update(bold=True, top=1, top_color=NAVY, bottom=6, bottom_color=NAVY, bg_color=PAPER)
                cell_format = self.formats.get(**properties)
                if value is None:
                    page.write_blank(row_index, index, None, cell_format)
                elif isinstance(value, Formula):
                    page.write_formula(row_index, index, value.text, cell_format,
                                       _cached(self.value_of(sheet.name, row_index + 1, index + 1)))
                elif isinstance(value, bool):
                    page.write_boolean(row_index, index, value, cell_format)
                elif isinstance(value, (int, float)):
                    page.write_number(row_index, index, value, cell_format)
                elif isinstance(value, date):
                    page.write_datetime(row_index, index, datetime(value.year, value.month, value.day), cell_format)
                else:
                    page.write_string(row_index, index, value, cell_format)
        data_bottom = last_data - (1 if sheet.total_row else 0)  # 1-based last row of ordinary data
        self.conditional(page, sheet, data_bottom)
        self.validation(page, sheet, data_bottom)
        labels = columns[0].format in ("text", "date")   # The column that names each row.
        page.freeze_panes(1, 1 if labels and len(columns) > 5 else 0)
        if count >= 5:
            page.autofilter(0, 0, data_bottom - 1, len(columns) - 1)
        self.print_setup(page, len(columns), sheet.name)
        if sheet.description:
            page.write_comment(0, 0, sheet.description, {"author": "Kalillac AI", "x_scale": 2.2, "y_scale": 1.6})
        anchor_column = len(columns) + 1
        for index, chart in enumerate(sheet.charts):
            page.insert_chart(1 + index * CHART_ROWS, anchor_column, self.chart(chart, sheet), {"x_offset": 8,
                                                                                               "y_offset": 4})

    def conditional(self, page, sheet, data_bottom):
        if data_bottom < 2:
            return
        for rule in sheet.rules:
            target = (1, rule.column, data_bottom - 1, rule.column)
            if rule.rule == "negative":
                page.conditional_format(*target, {"type": "cell", "criteria": "<", "value": 0,
                                                  "format": self.formats.get(font_color="#B42318")})
            elif rule.rule == "data_bar":
                page.conditional_format(*target, {"type": "data_bar", "bar_color": "#D3E5FF", "bar_solid": True,
                                                  "bar_border_color": "#D3E5FF"})
            elif rule.rule == "color_scale":
                page.conditional_format(*target, {"type": "3_color_scale", "min_color": "#F8C9C4",
                                                  "mid_color": "#FFFFFF", "max_color": "#BFE6CF"})
            elif rule.rule in ("above", "below"):
                page.conditional_format(*target, {
                    "type": "cell", "criteria": ">" if rule.rule == "above" else "<", "value": rule.value,
                    "format": self.formats.get(bg_color="#E7F7EE" if rule.rule == "above" else "#FDEEEC",
                                               font_color="#0B7A44" if rule.rule == "above" else "#A1221A")})
            else:
                page.conditional_format(*target, {
                    "type": "top" if rule.rule == "top" else "bottom", "value": int(rule.value),
                    "format": self.formats.get(bold=True, bg_color="#EAF3FF", font_color=BLUE_DEEP)})

    def validation(self, page, sheet, data_bottom):
        bottom = max(data_bottom, 1) + 200  # Room for rows the customer adds below.
        if sheet.total_row:
            bottom = data_bottom
        for rule in sheet.validations:
            target = (1, rule.column, max(bottom - 1, 1), rule.column)
            if rule.options is not None:
                page.data_validation(*target, {"validate": "list", "source": list(rule.options),
                                               "error_title": "Choose from the list",
                                               "error_message": "Pick one of the values in the drop-down."})
                continue
            kind = "integer" if sheet.columns[rule.column].format == "integer" else "decimal"
            if rule.minimum is not None and rule.maximum is not None:
                criteria = {"criteria": "between", "minimum": rule.minimum, "maximum": rule.maximum}
                message = f"Enter a value from {rule.minimum:g} to {rule.maximum:g}."
            elif rule.minimum is not None:
                criteria = {"criteria": ">=", "value": rule.minimum}
                message = f"Enter a value of at least {rule.minimum:g}."
            else:
                criteria = {"criteria": "<=", "value": rule.maximum}
                message = f"Enter a value of at most {rule.maximum:g}."
            page.data_validation(*target, {"validate": kind, **criteria, "error_title": "Value out of range",
                                           "error_message": message})

    def print_setup(self, page, columns, name):
        if columns > 6:
            page.set_landscape()
        page.set_paper(9)  # A4; fits Letter as well at fit-to-width.
        page.fit_to_pages(1, 0)
        page.repeat_rows(0)
        page.set_margins(left=0.5, right=0.5, top=0.75, bottom=0.75)
        title = self.spec.title.replace("&", "&&")[:60]
        page.set_header(f'&L&"{FONT},Bold"&9{title}&R&"{FONT},Regular"&9&A')
        page.set_footer(f'&L&"{FONT},Regular"&8Kalillac AI&R&"{FONT},Regular"&8Page &P of &N')

    # --- charts -----------------------------------------------------------------------------

    def chart(self, spec_chart, own_sheet):
        source = own_sheet if spec_chart.sheet is None else next(
            sheet for sheet in self.spec.sheets if sheet.name.lower() == spec_chart.sheet.lower())
        kind = spec_chart.kind
        options = {"type": {"stacked": "column"}.get(kind, kind)}
        if kind == "stacked":
            options["subtype"] = "stacked"
        chart = self.book.add_chart(options)
        last = len(source.rows) - (1 if source.total_row else 0)  # Totals never join a chart.
        if last < 1:
            last = len(source.rows)
        pie = kind in ("pie", "doughnut")
        for position, column in enumerate(spec_chart.series):
            color = SERIES[position % len(SERIES)]
            series = {"name": [source.name, 0, column], "categories": [source.name, 1, spec_chart.category, last,
                                                                       spec_chart.category],
                      "values": [source.name, 1, column, last, column]}
            if pie:
                series["points"] = [{"fill": {"color": SERIES[i % len(SERIES)]}, "border": {"color": "#FFFFFF"}}
                                    for i in range(last)]
                series["data_labels"] = {"percentage": True, "font": {"name": FONT, "size": 9}}
            elif kind in ("line", "area"):
                series["line"] = {"color": color, "width": 2.25}
                if kind == "area":
                    series["fill"] = {"color": color, "transparency": 70}
                else:
                    series["marker"] = {"type": "circle", "size": 5, "fill": {"color": "#FFFFFF"},
                                        "border": {"color": color, "width": 1.5}}
            else:
                series["fill"] = {"color": color}
                series["border"] = {"none": True}
                series["gap"] = 60
            if not pie and len(spec_chart.series) == 1 and last <= 12:
                series["data_labels"] = {"value": True, "font": {"name": FONT, "size": 8, "color": INK},
                                         "num_format": _number_format(source.columns[column].format,
                                                                      source.columns[column].symbol, 0)}
            chart.add_series(series)
        title = spec_chart.title or (source.columns[spec_chart.series[0]].header if len(spec_chart.series) == 1
                                     else None)
        if title:
            chart.set_title({"name": title, "name_font": {"name": FONT, "size": 12, "bold": True, "color": NAVY}})
        else:
            chart.set_title({"none": True})
        chart.set_legend({"position": "bottom", "font": {"name": FONT, "size": 9, "color": MUTED}}
                         if pie or len(spec_chart.series) > 1 else {"none": True})
        if not pie:
            first = source.columns[spec_chart.series[0]]
            value_axis = {"num_format": _number_format(first.format, first.symbol, 0),
                          "num_font": {"name": FONT, "size": 9, "color": MUTED},
                          "major_gridlines": {"visible": True, "line": {"color": HAIR}},
                          "line": {"none": True}}
            if spec_chart.y_title:
                value_axis["name"] = spec_chart.y_title
                value_axis["name_font"] = {"name": FONT, "size": 9, "color": MUTED, "bold": False}
            category_axis = {"num_font": {"name": FONT, "size": 9, "color": MUTED}, "line": {"color": "#C3CEE0"},
                             "major_tick_mark": "none", "label_position": "low"}
            if source.columns[spec_chart.category].format == "date":
                # A time axis: one label per quarter for long monthly series, not one per point.
                category_axis.update(date_axis=True, num_format="mmm yy", major_unit_type="months",
                                     major_unit=3 if last > 18 else 1, base_unit="months")
            if kind == "bar":
                chart.set_x_axis(value_axis)
                chart.set_y_axis({**category_axis, "reverse": True})
            else:
                chart.set_y_axis(value_axis)
                chart.set_x_axis(category_axis)
        chart.set_chartarea({"border": {"color": HAIR}, "fill": {"color": "#FFFFFF"}})
        chart.set_plotarea({"fill": {"none": True}})
        chart.set_size({"width": 620, "height": 330})
        return chart

    # --- dashboards and the About sheet -----------------------------------------------------

    def dashboard(self, sheet):
        page = self.book.add_worksheet(sheet.name)
        page.hide_gridlines(2)
        page.set_tab_color(BLUE)
        page.set_column(0, 0, 3)
        for column in range(1, 13):
            page.set_column(column, column, 3.5 if column % 3 == 0 else 16)
        page.set_row(1, 34)
        page.write_string(1, 1, sheet.title, self.formats.get(font_size=20, bold=True, font_color=NAVY))
        if sheet.description:
            page.write_string(2, 1, sheet.description, self.formats.get(font_size=10, font_color=MUTED))
        for metric, (row, column) in zip(sheet.metrics, _dashboard_positions(sheet)):
            label_row, value_row = row - 2, row - 1  # 0-based
            edge = {"left": 1, "left_color": HAIR, "right": 1, "right_color": HAIR}
            page.merge_range(label_row, column - 1, label_row, column, metric.label.upper(), self.formats.get(
                font_size=8, bold=True, font_color=MUTED, bg_color="#FFFFFF", top=1, top_color=HAIR, indent=1,
                **edge))
            page.set_row(value_row, 36)
            value_format = self.formats.get(font_size=20, bold=True, font_color=NAVY, bg_color="#FFFFFF", bottom=5,
                                            bottom_color=BLUE, indent=1,
                                            num_format=_number_format(metric.format, metric.symbol, 0
                                                                      if metric.format in ("currency", "integer")
                                                                      else None), **edge)
            page.merge_range(value_row, column - 1, value_row, column, "", value_format)
            page.write_formula(value_row, column - 1, metric.formula.text, value_format,
                               _cached(self.value_of(sheet.name, row, column)))
        top = 4 + 4 * ((len(sheet.metrics) + 3) // 4) if sheet.metrics else 4
        for index, spec_chart in enumerate(sheet.charts):
            row = top + (index // 2) * CHART_ROWS
            column = 1 if index % 2 == 0 else 7
            chart = self.chart(spec_chart, None)
            chart.set_size({"width": 560 if len(sheet.charts) > 1 else 760, "height": 320})
            page.insert_chart(row, column, chart, {"x_offset": 4, "y_offset": 6})
        self.print_setup(page, 12, sheet.name)
        page.set_landscape()

    def about(self):
        names = {sheet.name.lower() for sheet in self.spec.sheets}
        name = "About" if "about" not in names else "Read me"
        page = self.book.add_worksheet(name)
        page.hide_gridlines(2)
        page.set_column(0, 0, 3)
        page.set_column(1, 1, 26)
        page.set_column(2, 2, 72)
        page.set_row(1, 34)
        page.write_string(1, 1, self.spec.title, self.formats.get(font_size=20, bold=True, font_color=NAVY))
        row = 3
        if self.spec.summary:
            page.set_row(row, 15 * max(2, min(10, len(self.spec.summary) // 90 + 1)))
            page.merge_range(row, 1, row, 2, self.spec.summary, self.formats.get(text_wrap=True, valign="top",
                                                                                   font_size=11))
            row += 2
        heading = self.formats.get(bold=True, font_color=NAVY, font_size=11, bottom=1, bottom_color=HAIR)
        page.write_string(row, 1, "Sheet", heading)
        page.write_string(row, 2, "What it contains", heading)
        row += 1
        for sheet in self.spec.sheets:
            page.write_url(row, 1, f"internal:'{sheet.name.replace(chr(39), chr(39) * 2)}'!A1",
                           self.formats.get(font_color=BLUE_DEEP, underline=1), string=sheet.name)
            page.write_string(row, 2, sheet.description or ("Dashboard" if sheet.kind == "dashboard" else ""),
                              self.formats.get(text_wrap=True, font_color=INK))
            row += 1
        row += 1
        page.write_string(row, 1, "Colour conventions", heading)
        page.write_string(row, 2, "", heading)
        legend = (("12,500", self.formats.get(font_color=INPUT_FONT, num_format="#,##0"), "Typed values"),
                  ("12,500", self.formats.get(font_color=INPUT_FONT, bg_color=INPUT_FILL, num_format="#,##0"),
                   "Inputs meant to be changed"),
                  ("12,500", self.formats.get(font_color=INK), "Formulas on the same sheet"),
                  ("12,500", self.formats.get(font_color=LINK_FONT), "Formulas that read another sheet"))
        for sample, cell_format, meaning in legend:
            row += 1
            page.write_string(row, 1, sample, cell_format)
            page.write_string(row, 2, meaning, self.formats.get(font_color=INK))
        self.print_setup(page, 3, name)
        page.activate()

    def build(self):
        if self.spec.summary:
            self.about()
        for sheet in self.spec.sheets:
            if sheet.kind == "dashboard":
                self.dashboard(sheet)
            else:
                self.table(sheet)
        if not self.spec.summary:
            self.book.worksheets()[0].activate()
        self.book.close()
        return self.buffer.getvalue()


def write_xlsx(workbook):
    return _Writer(workbook).build()


def evaluate(workbook):
    """The formula engine's evaluation of a validated workbook (values and errors), without a file."""
    positions = {}
    for sheet in workbook.sheets:
        if sheet.kind == "dashboard":
            for metric, (row, column) in zip(sheet.metrics, _dashboard_positions(sheet)):
                positions[(sheet.name, row, column)] = metric.formula.text
    return _evaluate(workbook, positions)
