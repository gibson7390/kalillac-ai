"""One renderer per format. Each takes a validated spec object and returns bytes.

The document libraries are imported inside the renderer that needs them, so
importing this package costs nothing until a file is actually generated.

PDF is typeset by Typst through the Kalillac design template (typeset.py) and
DOCX is written by docx_design.py; both carry the full document vocabulary
(cover, contents, tables, callouts, metric cards, charts, steps and so on).
Markdown and plain text are faithful, simpler renderings of the same
document: a chart becomes its data table, a callout a quoted note.
"""
import csv
from datetime import date
import io
import json

from . import charts
from .spec import (Callout, Chart, Code, Comparison, Formula, Heading, ListBlock, Metrics, PageBreak, Paragraph,
                   Quote, SpecError, Steps, Table, Typed, plain)

# CSV: a text cell starting with one of these would be read as a formula by a
# spreadsheet program; it is prefixed with an apostrophe (OWASP CSV injection).
_CSV_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")
NUMBER_FORMATS = {"text": "@", "number": "#,##0.00", "integer": "#,##0", "currency": "#,##0.00",
                  "percent": "0.00%", "date": "yyyy-mm-dd"}


def render_pdf(document):
    from .typeset import typeset
    return typeset(document)


def render_docx(document):
    from .docx_design import write_docx
    return write_docx(document)


# --- XLSX -----------------------------------------------------------------------------------


def render_pptx(deck):
    from .pptx_design import write_pptx
    return write_pptx(deck)


def render_xlsx(workbook):
    from .xlsx_design import write_xlsx
    return write_xlsx(workbook)


# --- text formats ---------------------------------------------------------------------------


def _csv_value(value):
    if isinstance(value, Typed):
        value = value.value
    if value is None:
        return ""
    if type(value) is Formula:
        return value.text
    if type(value) is bool:
        return "TRUE" if value else "FALSE"
    if type(value) is date:
        return value.isoformat()
    if type(value) is str:
        return "'" + value if value.startswith(_CSV_TRIGGERS) else value
    return repr(value) if type(value) is float else str(value)


def render_csv(workbook):
    """The first sheet only: CSV has no sheets. UTF-8 with a byte order mark,
    so spreadsheet programs read non-ASCII text correctly."""
    if len(workbook.sheets) != 1:
        raise SpecError("csv_requires_one_sheet")
    from .xlsx_design import check_formulas
    check_formulas(workbook)       # The same refusals as the XLSX: no broken model in any format.
    sheet = workbook.sheets[0]
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow([_csv_value(column.header) for column in sheet.columns])
    for row in sheet.rows:
        writer.writerow([_csv_value(value) for value in row])
    return b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8")


def _markdown_cell(text):
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")


def _markdown_runs(runs):
    out = []
    for run in runs:
        text = run.text
        if run.code:
            out.append("`" + text.replace("`", "'") + "`")
            continue
        for mark in ("\\", "*", "_", "`"):
            text = text.replace(mark, "\\" + mark)
        if run.bold and run.italic:
            text = "***" + text + "***"
        elif run.bold:
            text = "**" + text + "**"
        elif run.italic:
            text = "*" + text + "*"
        out.append(text)
    return "".join(out)


def chart_table(chart):
    """The data of a chart as (header, rows): what a format without graphics can still say exactly."""
    header = ["", *(series.name for series in chart.series)]
    rows = [[name, *("" if series.values[index] is None
                     else charts.format_value(series.values[index], chart.prefix, chart.suffix)
                     for series in chart.series)] for index, name in enumerate(chart.categories)]
    return header, rows


def _markdown_table(header, rows):
    lines = ["| " + " | ".join(_markdown_cell(value) for value in header) + " |", "|" + " --- |" * len(header)]
    lines += ["| " + " | ".join(_markdown_cell(value) for value in row) + " |" for row in rows]
    return lines


def render_markdown(document):
    lines = ["# " + document.title, ""]
    if document.subtitle:
        lines += ["*" + document.subtitle + "*", ""]
    meta = [value for value in (document.organization, document.author, document.date) if value]
    if meta:
        lines += [" \u00b7 ".join(meta), ""]
    if document.summary:
        lines += ["> " + document.summary.replace("\n", "\n> "), ""]
    for block in document.blocks:
        kind = type(block)
        if kind is Heading:
            lines += ["#" * (block.level + 1) + " " + block.text, ""]
        elif kind is Paragraph:
            lines += [_markdown_runs(block.runs), ""]
        elif kind is ListBlock:
            lines += [(f"{index}. " if block.ordered else "- ") + _markdown_runs(item).replace("\n", " ")
                      for index, item in enumerate(block.items, start=1)] + [""]
        elif kind is Table:
            if block.caption:
                lines += ["*" + block.caption + "*", ""]
            lines += _markdown_table(block.header, block.rows) + [""]
        elif kind is Callout:
            title = f"**{block.title}** " if block.title else ""
            lines += ["> " + title + _markdown_runs(block.runs).replace("\n", "\n> "), ""]
        elif kind is Metrics:
            lines += _markdown_table(["Metric", "Value", "Detail"],
                                     [[m.label, m.value, m.detail or ""] for m in block.items]) + [""]
        elif kind is Chart:
            if block.title:
                lines += ["**" + block.title + "**", ""]
            lines += _markdown_table(*chart_table(block)) + [""]
            if block.caption:
                lines += ["*" + block.caption + "*", ""]
        elif kind is Quote:
            lines += ["> " + block.text.replace("\n", "\n> ")]
            lines += (["> ", "> \u2014 " + block.attribution] if block.attribution else []) + [""]
        elif kind is Code:
            fence = "````" if "```" in block.text else "```"
            lines += [fence + (block.language or ""), block.text, fence, ""]
        elif kind is Steps:
            lines += [f"{index}. **{step.title}**" + (f" {step.text}" if step.text else "")
                      for index, step in enumerate(block.items, start=1)] + [""]
        elif kind is Comparison:
            for column in block.columns:
                lines += ["**" + column.title + "**", ""] + ["- " + item for item in column.items] + [""]
        elif kind is PageBreak:
            lines += ["---", ""]
    return "\n".join(lines).rstrip("\n").encode("utf-8") + b"\n"


def _text_table(header, rows):
    return ["\t".join(header)] + ["\t".join(value.replace("\n", " ").replace("\t", " ") for value in row)
                                  for row in rows]


def render_text(document):
    lines = [document.title, "=" * min(len(document.title), 78), ""]
    if document.subtitle:
        lines += [document.subtitle, ""]
    if document.summary:
        lines += [document.summary, ""]
    for block in document.blocks:
        kind = type(block)
        if kind is Heading:
            lines += [block.text, ("-" if block.level > 1 else "=") * min(len(block.text), 78), ""]
        elif kind is Paragraph:
            lines += [block.text, ""]
        elif kind is ListBlock:
            lines += [(f"{index}. " if block.ordered else "* ") + item.replace("\n", " ")
                      for index, item in enumerate(block.texts, start=1)] + [""]
        elif kind is Table:
            lines += ([block.caption] if block.caption else []) + _text_table(block.header, block.rows) + [""]
        elif kind is Callout:
            lines += [(block.title + ": " if block.title else "") + plain(block.runs), ""]
        elif kind is Metrics:
            lines += [f"{m.label}: {m.value}" + (f" ({m.detail})" if m.detail else "") for m in block.items] + [""]
        elif kind is Chart:
            lines += ([block.title] if block.title else []) + _text_table(*chart_table(block))
            lines += ([block.caption] if block.caption else []) + [""]
        elif kind is Quote:
            lines += ['"' + block.text + '"' + (f" \u2014 {block.attribution}" if block.attribution else ""), ""]
        elif kind is Code:
            lines += [block.text, ""]
        elif kind is Steps:
            lines += [f"{index}. {step.title}" + (f": {step.text}" if step.text else "")
                      for index, step in enumerate(block.items, start=1)] + [""]
        elif kind is Comparison:
            for column in block.columns:
                lines += [column.title] + ["* " + item for item in column.items] + [""]
        elif kind is PageBreak:
            lines += ["", ""]
    return "\n".join(lines).rstrip("\n").encode("utf-8") + b"\n"


def render_json(workbook):
    """Each sheet as a list of records keyed by column header."""
    from .xlsx_design import check_formulas
    check_formulas(workbook)       # The same refusals as the XLSX: no broken model in any format.

    def plain(value):
        if type(value) is Typed:
            value = value.value
        if type(value) is Formula:
            return {"formula": value.text}
        return value.isoformat() if type(value) is date else value
    sheets = []
    for sheet in (sheet for sheet in workbook.sheets if sheet.kind == "table"):
        headers = [column.header for column in sheet.columns]
        if len(set(headers)) != len(headers):
            raise SpecError("json_requires_unique_headers")
        sheets.append({"name": sheet.name, "rows": [dict(zip(headers, (plain(v) for v in row))) for row in sheet.rows]})
    return json.dumps({"title": workbook.title, "sheets": sheets}, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
