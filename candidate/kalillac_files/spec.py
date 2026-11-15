"""The bounded, validated description of a document or a workbook.

validate_document() and validate_workbook() take untrusted plain data (as a
model tool call or an API body would supply it) and return immutable objects
the renderers can trust. Anything outside the grammar is refused with a
SpecError whose code is stable and whose message never echoes content.

Document:  {"title": str, "blocks": [block, ...]}
  block:   {"type": "heading", "level": 1 | 2 | 3, "text": str}
           {"type": "paragraph", "text": str}
           {"type": "list", "ordered": bool, "items": [str, ...]}
           {"type": "table", "header": [str, ...], "rows": [[str, ...], ...]}
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


@dataclass(frozen=True)
class Heading:
    level: int
    text: str


@dataclass(frozen=True)
class Paragraph:
    text: str


@dataclass(frozen=True)
class ListBlock:
    ordered: bool
    items: tuple


@dataclass(frozen=True)
class Table:
    header: tuple
    rows: tuple


@dataclass(frozen=True)
class PageBreak:
    pass


@dataclass(frozen=True)
class Document:
    title: str
    blocks: tuple


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
        return Paragraph(_text(value["text"], MAX_BLOCK_TEXT, "invalid_paragraph"))
    if kind == "list":
        _keys(value, ("type", "ordered", "items"))
        if type(value["ordered"]) is not bool:
            raise SpecError("invalid_list")
        items = _list(value["items"], MAX_LIST_ITEMS, "invalid_list", minimum=1)
        return ListBlock(value["ordered"], tuple(_text(item, MAX_TABLE_CELL, "invalid_list") for item in items))
    if kind == "table":
        _keys(value, ("type", "header", "rows"))
        header = _list(value["header"], MAX_TABLE_COLUMNS, "invalid_table", minimum=1)
        rows = _list(value["rows"], MAX_TABLE_ROWS, "invalid_table")
        width = len(header)
        if any(type(row) is not list or len(row) != width for row in rows):
            raise SpecError("invalid_table")
        return Table(tuple(_text(cell, 200, "invalid_table", single_line=True) for cell in header),
                     tuple(tuple(_text(cell, MAX_TABLE_CELL, "invalid_table", allow_empty=True) for cell in row)
                           for row in rows))
    if kind == "page_break":
        _keys(value, ("type",))
        return PageBreak()
    raise SpecError("unknown_block")


def _document_size(blocks):
    total = 0
    for block in blocks:
        if type(block) in (Heading, Paragraph):
            total += len(block.text)
        elif type(block) is ListBlock:
            total += sum(len(item) for item in block.items)
        elif type(block) is Table:
            total += sum(len(cell) for cell in block.header) + sum(len(cell) for row in block.rows for cell in row)
    return total


def validate_document(value):
    _keys(value, ("title", "blocks"))
    title = _text(value["title"], MAX_TITLE, "invalid_title", single_line=True)
    blocks = tuple(_block(item) for item in _list(value["blocks"], MAX_BLOCKS, "invalid_blocks", minimum=1))
    if _document_size(blocks) > MAX_DOCUMENT_TEXT:
        raise SpecError("document_too_large")
    if sum(len(block.header) * (len(block.rows) + 1) for block in blocks if type(block) is Table) \
            > MAX_DOCUMENT_TABLE_CELLS:
        raise SpecError("document_too_large")
    return Document(title, blocks)


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
