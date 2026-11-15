"""Arithmetic a reader will check: a table's total row must add up.

A language model writes figures well and adds them up badly. A report whose total row does not equal
its rows is a financial inconsistency a customer will find and never forgive, so a document or deck
whose table is marked with a total row, labelled as a total ("Total", "Grand total", "Subtotal",
"Sum", or the same words in Spanish, French, German and Portuguese), is checked column by column:

  * a column is checked when every body cell and its total cell read as plain quantities in the same
    unit: digits with thousands separators, an optional currency sign, a minus sign or accounting
    parentheses for negatives, and an optional K/M/B scale;
  * percentages, ratios and empty or textual cells are never summed (a share, a margin, a growth rate
    or a note column is not additive), and neither is a total row labelled otherwise ("Average",
    "Gross profit", "All months");
  * a sum within the rounding of the figures shown passes; anything else is refused with
    table_total_mismatch, naming the table and column so the author can correct it.

Workbooks are not checked here: their totals are formulas, computed by the formula engine.
"""
import re

from .spec import SpecError

_TOTAL_LABEL = re.compile(r"\s*(grand\s+total|sub-?total|total|sum|totale?s?|gesamt(summe)?|summe|somme)\b",
                          re.IGNORECASE)
_QUANTITY = re.compile(r"(?P<open>\()?\s*(?P<sign>[-−–])?\s*(?P<currency>[$€£¥₹]|[A-Z]{3}\s)?\s*"
                       r"(?P<number>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*(?P<scale>[KMB]|bn)?\s*(?P<close>\))?")


class TotalMismatch(SpecError):
    def __init__(self, table, column):
        super().__init__("table_total_mismatch")
        self.table, self.column = table, column


def _quantity(cell):
    """(value, unit, decimals) for a plain quantity, or None for anything else."""
    text = cell.strip()
    if not text or "%" in text:
        return None
    match = _QUANTITY.fullmatch(text)
    if match is None or bool(match.group("open")) != bool(match.group("close")):
        return None
    number = match.group("number")
    value = float(number.replace(",", ""))
    if match.group("open") or match.group("sign"):
        value = -value
    decimals = len(number.split(".")[1]) if "." in number else 0
    return value, ((match.group("currency") or "").strip(), match.group("scale") or ""), decimals


def check_table(header, rows, number=1):
    """Raise TotalMismatch when the last row is labelled a total and a quantity column does not add up."""
    if len(rows) < 3 or not rows[-1] or _TOTAL_LABEL.match(rows[-1][0] or "") is None:
        return
    body, total = rows[:-1], rows[-1]
    for column in range(1, len(header)):
        if re.search(r"%|percent|share|margin|rate|growth|change|ratio|average|mean|median", header[column],
                     re.IGNORECASE):
            continue
        parsed = [_quantity(row[column]) for row in body]
        expected = _quantity(total[column])
        if expected is None or any(item is None for item in parsed):
            continue
        if len({item[1] for item in parsed} | {expected[1]}) != 1:
            continue                     # Mixed units or scales: not a simple sum.
        places = max([item[2] for item in parsed] + [expected[2]])
        tolerance = 0.5 * 10 ** -places * (len(parsed) + 1) + 1e-9
        if abs(sum(item[0] for item in parsed) - expected[0]) > tolerance:
            raise TotalMismatch(number, column + 1)


def check_document(document):
    from .spec import Table
    number = 0
    for block in document.blocks:
        if type(block) is Table:
            number += 1
            if block.total_row:
                check_table(block.header, block.rows, number)


def check_deck(deck):
    number = 0
    for slide in deck.slides:
        if slide.layout == "table":
            number += 1
            if slide.total_row:
                check_table(slide.header, slide.rows, number)
