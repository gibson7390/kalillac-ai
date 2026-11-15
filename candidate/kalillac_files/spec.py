"""The bounded, validated description of a document or a workbook.

validate_document() and validate_workbook() take untrusted plain data (as a
model tool call or an API body would supply it) and return immutable objects
the renderers can trust. Anything outside the grammar is refused with a
SpecError whose code is stable and whose message never echoes content.

Document:  {"title": str, "blocks": [block, ...],
            optional: "subtitle", "summary", "author", "organization", "date" (shown as given),
            "language" (BCP 47, default "en"), "theme" ("modern" | "editorial"), "page" ("a4" | "letter"),
            "cover" (bool), "contents" (bool)}
  rich:    str | [{"text": str, "bold"?: bool, "italic"?: bool, "code"?: bool}, ...]   (text is never markup)
  block:   {"type": "heading", "level": 1 | 2 | 3, "text": str}
           {"type": "paragraph", "text": rich}
           {"type": "list", "ordered": bool, "items": [rich, ...]}
           {"type": "table", "header": [str, ...], "rows": [[str, ...], ...],
            "caption"?: str, "align"?: ["left" | "right" | "center", ...], "total_row"?: bool}
           {"type": "callout", "text": rich, "tone"?: "info" | "success" | "warning" | "danger" | "note", "title"?: str}
           {"type": "metrics", "items": [{"label": str, "value": str, "detail"?: str, "trend"?: "up" | "down" | "flat"}]}
           {"type": "chart", "kind": "column" | "bar" | "stacked" | "line" | "area" | "donut",
            "categories": [str, ...], "series": [{"name": str, "values": [number | null, ...]}, ...],
            "title"?: str, "caption"?: str, "prefix"?: str, "suffix"?: str}
           {"type": "quote", "text": str, "attribution"?: str}
           {"type": "code", "text": str, "language"?: str}
           {"type": "steps", "items": [{"title": str, "text"?: str}, ...]}
           {"type": "comparison", "columns": [{"title": str, "items": [str, ...]}, ...]}
           {"type": "page_break"}

Workbook:  {"title": str, "sheets": [sheet, ...]}
  sheet:   {"name": str, "columns": [column, ...], "rows": [[cell, ...], ...]}
  column:  {"header": str, "format": "text" | "number" | "integer" | "currency" | "percent" | "date",
            "width": 4..80 (optional)}
  cell:    None | str | int | float | bool | {"formula": "=..."}   (dates are ISO "YYYY-MM-DD" strings)
"""
from dataclasses import dataclass
from datetime import date
import math
import re

from .formulas import is_safe_formula

MAX_TITLE = 200
MAX_BLOCKS = 2_000
MAX_BLOCK_TEXT = 20_000
MAX_DOCUMENT_TEXT = 200_000
MAX_LIST_ITEMS = 500
MAX_TABLE_COLUMNS = 12
MAX_TABLE_ROWS = 500
MAX_TABLE_CELL = 2_000
MAX_DOCUMENT_TABLE_CELLS = 6_000  # Across all tables of one document, headers included.
MAX_SHEETS = 20
MAX_SHEET_COLUMNS = 100
MAX_SHEET_ROWS = 10_000
MAX_WORKBOOK_CELLS = 50_000
MAX_CELL_TEXT = 2_000
MAX_NUMBER = 10**15
COLUMN_FORMATS = ("text", "number", "integer", "currency", "percent", "date")

# Characters XML 1.0 (DOCX, XLSX) cannot carry, and every other control
# character except tab and newline.
_FORBIDDEN = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff￾￿]")
_SHEET_NAME_FORBIDDEN = re.compile(r"[\[\]:*?/\\']")
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)


class SpecError(ValueError):
    """A refused specification or an unrenderable document. `code` is stable;
    the message is fixed text and never contains the offending content."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _text(value, maximum, code, allow_empty=False, single_line=False):
    if type(value) is not str or len(value) > maximum or _FORBIDDEN.search(value):
        raise SpecError(code)
    if single_line and ("\n" in value or "\r" in value or "\t" in value):
        raise SpecError(code)
    if not allow_empty and not value.strip():
        raise SpecError(code)
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _keys(value, required, optional=()):
    if type(value) is not dict or not set(required) <= set(value) <= set(required) | set(optional):
        raise SpecError("invalid_structure")


def _list(value, maximum, code, minimum=0):
    if type(value) is not list or not minimum <= len(value) <= maximum:
        raise SpecError(code)
    return value


# --- documents ------------------------------------------------------------------------------

TONES = ("info", "success", "warning", "danger", "note")
CHART_KINDS = ("column", "bar", "stacked", "line", "area", "donut")
THEMES = ("modern", "editorial")
PAGE_SIZES = ("a4", "letter")
TRENDS = ("up", "down", "flat")
ALIGNMENTS = ("left", "right", "center")
MAX_RUNS = 200
MAX_CHARTS = 40
MAX_CHART_CATEGORIES = 40
MAX_CHART_SERIES = 6
_LANGUAGE = re.compile(r"[a-z]{2,3}(-[A-Za-z0-9]{2,8}){0,3}", re.ASCII)
# A cell that reads as a quantity: digits with optional sign, currency, separators, percent or unit suffix.
_NUMERIC = re.compile(r"[\s(+\-−]*[$€£¥]?\s*[0-9][0-9.,   ]*\s*(%|[kKmMbB]|bn|pp|x)?\)?\s*")


@dataclass(frozen=True)
class Run:
    """A stretch of text with emphasis. Text is never markup."""
    text: str
    bold: bool = False
    italic: bool = False
    code: bool = False


def plain(runs):
    return "".join(run.text for run in runs)


@dataclass(frozen=True)
class Heading:
    level: int
    text: str


@dataclass(frozen=True)
class Paragraph:
    runs: tuple

    @property
    def text(self):
        return plain(self.runs)


@dataclass(frozen=True)
class ListBlock:
    ordered: bool
    items: tuple  # of tuples of Run

    @property
    def texts(self):
        return tuple(plain(item) for item in self.items)


@dataclass(frozen=True)
class Table:
    header: tuple
    rows: tuple
    caption: str | None = None
    align: tuple = ()
    total_row: bool = False


@dataclass(frozen=True)
class PageBreak:
    pass


@dataclass(frozen=True)
class Callout:
    tone: str
    title: str | None
    runs: tuple


@dataclass(frozen=True)
class Metric:
    label: str
    value: str
    detail: str | None = None
    trend: str | None = None


@dataclass(frozen=True)
class Metrics:
    items: tuple


@dataclass(frozen=True)
class Series:
    name: str
    values: tuple  # of float, or None for a missing point


@dataclass(frozen=True)
class Chart:
    kind: str
    categories: tuple
    series: tuple
    title: str | None = None
    caption: str | None = None
    prefix: str = ""
    suffix: str = ""


@dataclass(frozen=True)
class Quote:
    text: str
    attribution: str | None = None


@dataclass(frozen=True)
class Code:
    text: str
    language: str | None = None


@dataclass(frozen=True)
class Step:
    title: str
    text: str | None = None


@dataclass(frozen=True)
class Steps:
    items: tuple


@dataclass(frozen=True)
class ComparisonColumn:
    title: str
    items: tuple


@dataclass(frozen=True)
class Comparison:
    columns: tuple


@dataclass(frozen=True)
class Document:
    title: str
    blocks: tuple
    subtitle: str | None = None
    summary: str | None = None
    author: str | None = None
    organization: str | None = None
    date: str | None = None
    language: str = "en"
    theme: str = "modern"
    page: str = "a4"
    cover: bool = False
    contents: bool = False


def _optional(value, key, maximum, code, single_line=True):
    found = value.get(key)
    return None if found is None else _text(found, maximum, code, single_line=single_line)


def _flag(value, key, code, default=False):
    found = value.get(key, default)
    if type(found) is not bool:
        raise SpecError(code)
    return found


def _choice(value, key, choices, code, default):
    found = value.get(key, default)
    if type(found) is not str or found not in choices:
        raise SpecError(code)
    return found


def _runs(value, maximum, code):
    """Rich text: a string, or a list of {"text", "bold"?, "italic"?, "code"?} runs."""
    if type(value) is str:
        return (Run(_text(value, maximum, code)),)
    runs, total = [], 0
    for item in _list(value, MAX_RUNS, code, minimum=1):
        if type(item) is not dict or not {"text"} <= set(item) <= {"text", "bold", "italic", "code"}:
            raise SpecError(code)
        text = _text(item["text"], maximum, code, allow_empty=True)
        flags = [item.get(name, False) for name in ("bold", "italic", "code")]
        if any(type(flag) is not bool for flag in flags):
            raise SpecError(code)
        total += len(text)
        if text:
            runs.append(Run(text, *flags))
    if total > maximum or not plain(runs).strip():
        raise SpecError(code)
    return tuple(runs)


def _number(value, code):
    if type(value) is bool or type(value) not in (int, float) or not math.isfinite(value) or abs(value) > MAX_NUMBER:
        raise SpecError(code)
    return float(value)


def _auto_align(header, rows):
    align = []
    for index in range(len(header)):
        cells = [row[index] for row in rows if row[index].strip()]
        align.append("right" if cells and all(_NUMERIC.fullmatch(cell) for cell in cells) else "left")
    return tuple(align)


def _table(value):
    _keys(value, ("type", "header", "rows"), ("caption", "align", "total_row"))
    header = _list(value["header"], MAX_TABLE_COLUMNS, "invalid_table", minimum=1)
    rows = _list(value["rows"], MAX_TABLE_ROWS, "invalid_table")
    width = len(header)
    if any(type(row) is not list or len(row) != width for row in rows):
        raise SpecError("invalid_table")
    header = tuple(_text(cell, 200, "invalid_table", single_line=True) for cell in header)
    rows = tuple(tuple(_text(cell, MAX_TABLE_CELL, "invalid_table", allow_empty=True) for cell in row)
                 for row in rows)
    align = value.get("align")
    if align is None:
        align = _auto_align(header, rows)
    elif type(align) is not list or len(align) != width or any(a not in ALIGNMENTS for a in align):
        raise SpecError("invalid_table")
    total_row = _flag(value, "total_row", "invalid_table")
    if total_row and not rows:
        raise SpecError("invalid_table")
    return Table(header, rows, _optional(value, "caption", 300, "invalid_table"), tuple(align), total_row)


def _chart(value):
    _keys(value, ("type", "kind", "categories", "series"), ("title", "caption", "prefix", "suffix"))
    if type(value["kind"]) is not str or value["kind"] not in CHART_KINDS:
        raise SpecError("invalid_chart")
    categories = tuple(_text(c, 60, "invalid_chart", single_line=True)
                       for c in _list(value["categories"], MAX_CHART_CATEGORIES, "invalid_chart", minimum=1))
    series = []
    for item in _list(value["series"], MAX_CHART_SERIES, "invalid_chart", minimum=1):
        _keys(item, ("name", "values"))
        values = _list(item["values"], len(categories), "invalid_chart", minimum=len(categories))
        series.append(Series(_text(item["name"], 60, "invalid_chart", single_line=True),
                             tuple(None if v is None else _number(v, "invalid_chart") for v in values)))
    known = [v for s in series for v in s.values if v is not None]
    if not known:
        raise SpecError("invalid_chart")
    kind = value["kind"]
    if kind == "donut" and (len(series) != 1 or any(v is None or v < 0 for v in series[0].values)
                            or sum(series[0].values) <= 0 or len(categories) > 8):
        raise SpecError("invalid_chart")
    if kind in ("stacked", "area") and any(v is not None and v < 0 for v in known):
        raise SpecError("invalid_chart")
    if kind in ("line", "area") and len(categories) < 2:
        raise SpecError("invalid_chart")
    affixes = []
    for key in ("prefix", "suffix"):
        found = value.get(key, "")
        if type(found) is not str or len(found) > 6 or _FORBIDDEN.search(found) or "\n" in found:
            raise SpecError("invalid_chart")
        affixes.append(found)
    return Chart(kind, categories, tuple(series), _optional(value, "title", 200, "invalid_chart"),
                 _optional(value, "caption", 300, "invalid_chart"), *affixes)


def _block(value):
    if type(value) is not dict or type(value.get("type")) is not str:
        raise SpecError("invalid_structure")
    kind = value["type"]
    if kind == "heading":
        _keys(value, ("type", "level", "text"))
        if type(value["level"]) is not int or value["level"] not in (1, 2, 3):
            raise SpecError("invalid_heading")
        return Heading(value["level"], _text(value["text"], 500, "invalid_heading", single_line=True))
    if kind == "paragraph":
        _keys(value, ("type", "text"))
        return Paragraph(_runs(value["text"], MAX_BLOCK_TEXT, "invalid_paragraph"))
    if kind == "list":
        _keys(value, ("type", "ordered", "items"))
        if type(value["ordered"]) is not bool:
            raise SpecError("invalid_list")
        items = _list(value["items"], MAX_LIST_ITEMS, "invalid_list", minimum=1)
        return ListBlock(value["ordered"], tuple(_runs(item, MAX_TABLE_CELL, "invalid_list") for item in items))
    if kind == "table":
        return _table(value)
    if kind == "page_break":
        _keys(value, ("type",))
        return PageBreak()
    if kind == "callout":
        _keys(value, ("type", "text"), ("tone", "title"))
        return Callout(_choice(value, "tone", TONES, "invalid_callout", "info"),
                       _optional(value, "title", 200, "invalid_callout"),
                       _runs(value["text"], 4_000, "invalid_callout"))
    if kind == "metrics":
        _keys(value, ("type", "items"))
        items = []
        for item in _list(value["items"], 4, "invalid_metrics", minimum=1):
            _keys(item, ("label", "value"), ("detail", "trend"))
            trend = item.get("trend")
            if trend is not None and trend not in TRENDS:
                raise SpecError("invalid_metrics")
            items.append(Metric(_text(item["label"], 60, "invalid_metrics", single_line=True),
                                _text(item["value"], 24, "invalid_metrics", single_line=True),
                                _optional(item, "detail", 80, "invalid_metrics"), trend))
        return Metrics(tuple(items))
    if kind == "chart":
        return _chart(value)
    if kind == "quote":
        _keys(value, ("type", "text"), ("attribution",))
        return Quote(_text(value["text"], 2_000, "invalid_quote"), _optional(value, "attribution", 200, "invalid_quote"))
    if kind == "code":
        _keys(value, ("type", "text"), ("language",))
        language = value.get("language")
        if language is not None and (type(language) is not str or not re.fullmatch(r"[A-Za-z0-9+#._-]{1,24}", language)):
            raise SpecError("invalid_code")
        return Code(_text(value["text"], 20_000, "invalid_code"), language)
    if kind == "steps":
        _keys(value, ("type", "items"))
        items = []
        for item in _list(value["items"], 8, "invalid_steps", minimum=2):
            _keys(item, ("title",), ("text",))
            items.append(Step(_text(item["title"], 120, "invalid_steps", single_line=True),
                              _optional(item, "text", 600, "invalid_steps", single_line=False)))
        return Steps(tuple(items))
    if kind == "comparison":
        _keys(value, ("type", "columns"))
        columns = []
        for column in _list(value["columns"], 3, "invalid_comparison", minimum=2):
            _keys(column, ("title", "items"))
            columns.append(ComparisonColumn(
                _text(column["title"], 120, "invalid_comparison", single_line=True),
                tuple(_text(item, 400, "invalid_comparison")
                      for item in _list(column["items"], 12, "invalid_comparison", minimum=1))))
        return Comparison(tuple(columns))
    raise SpecError("unknown_block")


def _document_size(blocks):
    total = 0
    for block in blocks:
        kind = type(block)
        if kind is Heading:
            total += len(block.text)
        elif kind in (Paragraph, Callout):
            total += len(plain(block.runs))
        elif kind is ListBlock:
            total += sum(len(plain(item)) for item in block.items)
        elif kind is Table:
            total += sum(len(cell) for cell in block.header) + sum(len(cell) for row in block.rows for cell in row)
        elif kind in (Quote, Code):
            total += len(block.text)
        elif kind is Steps:
            total += sum(len(item.title) + len(item.text or "") for item in block.items)
        elif kind is Comparison:
            total += sum(len(column.title) + sum(len(item) for item in column.items) for column in block.columns)
    return total


def validate_document(value):
    _keys(value, ("title", "blocks"), ("subtitle", "summary", "author", "organization", "date", "language", "theme",
                                       "page", "cover", "contents"))
    title = _text(value["title"], MAX_TITLE, "invalid_title", single_line=True)
    blocks = tuple(_block(item) for item in _list(value["blocks"], MAX_BLOCKS, "invalid_blocks", minimum=1))
    if _document_size(blocks) > MAX_DOCUMENT_TEXT:
        raise SpecError("document_too_large")
    if sum(len(block.header) * (len(block.rows) + 1) for block in blocks if type(block) is Table) \
            > MAX_DOCUMENT_TABLE_CELLS:
        raise SpecError("document_too_large")
    if sum(1 for block in blocks if type(block) is Chart) > MAX_CHARTS:
        raise SpecError("document_too_large")
    language = value.get("language", "en")
    if type(language) is not str or _LANGUAGE.fullmatch(language) is None:
        raise SpecError("invalid_options")
    return Document(
        title, blocks,
        subtitle=_optional(value, "subtitle", 300, "invalid_options"),
        summary=_optional(value, "summary", 1_500, "invalid_options", single_line=False),
        author=_optional(value, "author", 120, "invalid_options"),
        organization=_optional(value, "organization", 120, "invalid_options"),
        date=_optional(value, "date", 60, "invalid_options"),
        language=language,
        theme=_choice(value, "theme", THEMES, "invalid_options", "modern"),
        page=_choice(value, "page", PAGE_SIZES, "invalid_options", "a4"),
        cover=_flag(value, "cover", "invalid_options"),
        contents=_flag(value, "contents", "invalid_options"),
    )


# --- workbooks ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Formula:
    text: str


@dataclass(frozen=True)
class Column:
    header: str
    format: str
    width: int | None


@dataclass(frozen=True)
class Sheet:
    name: str
    columns: tuple
    rows: tuple  # of tuples of None | str | int | float | bool | date | Formula


@dataclass(frozen=True)
class Workbook:
    title: str
    sheets: tuple


def _column(value):
    _keys(value, ("header", "format"), ("width",))
    if type(value["format"]) is not str or value["format"] not in COLUMN_FORMATS:
        raise SpecError("invalid_column")
    width = value.get("width")
    if width is not None and (type(width) is not int or not 4 <= width <= 80):
        raise SpecError("invalid_column")
    return Column(_text(value["header"], 200, "invalid_column", single_line=True), value["format"], width)


def _cell(value, column):
    if value is None or type(value) is bool:
        return value
    if type(value) is dict:
        if set(value) != {"formula"} or not is_safe_formula(value["formula"]):
            raise SpecError("invalid_formula")
        return Formula(value["formula"])
    if type(value) in (int, float):
        if type(value) is float and not math.isfinite(value) or abs(value) > MAX_NUMBER:
            raise SpecError("invalid_cell")
        if column.format in ("text", "date"):
            raise SpecError("invalid_cell")
        if column.format == "integer" and type(value) is float and not value.is_integer():
            raise SpecError("invalid_cell")
        return value
    if type(value) is str:
        text = _text(value, MAX_CELL_TEXT, "invalid_cell", allow_empty=True)
        if column.format == "date":
            if _ISO_DATE.fullmatch(text) is None:
                raise SpecError("invalid_date")
            try:
                return date.fromisoformat(text)
            except ValueError:
                raise SpecError("invalid_date") from None
        if column.format != "text":
            raise SpecError("invalid_cell")  # A numeric column never silently holds text.
        return text
    raise SpecError("invalid_cell")


def _sheet(value):
    _keys(value, ("name", "columns", "rows"))
    name = _text(value["name"], 31, "invalid_sheet_name", single_line=True)
    if _SHEET_NAME_FORBIDDEN.search(name) or name != name.strip():
        raise SpecError("invalid_sheet_name")
    columns = tuple(_column(item) for item in _list(value["columns"], MAX_SHEET_COLUMNS, "invalid_column", minimum=1))
    rows = _list(value["rows"], MAX_SHEET_ROWS, "invalid_rows")
    width = len(columns)
    if any(type(row) is not list or len(row) != width for row in rows):
        raise SpecError("invalid_rows")
    return Sheet(name, columns, tuple(tuple(_cell(cell, columns[i]) for i, cell in enumerate(row)) for row in rows))


def validate_workbook(value):
    _keys(value, ("title", "sheets"))
    title = _text(value["title"], MAX_TITLE, "invalid_title", single_line=True)
    raw = _list(value["sheets"], MAX_SHEETS, "invalid_sheets", minimum=1)
    cells = 0
    for item in raw:  # Counted before any cell is validated or built.
        if type(item) is dict and type(item.get("rows")) is list and type(item.get("columns")) is list:
            cells += len(item["rows"]) * max(1, len(item["columns"]))
    if cells > MAX_WORKBOOK_CELLS:
        raise SpecError("workbook_too_large")
    sheets = tuple(_sheet(item) for item in raw)
    names = [sheet.name.lower() for sheet in sheets]
    if len(set(names)) != len(names):
        raise SpecError("invalid_sheet_name")
    return Workbook(title, sheets)
