"""The spreadsheet formulas a generated workbook may contain.

A formula is data the customer's spreadsheet program will evaluate, so only a
closed set is accepted:

  * it starts with "=" and is at most MAX_FORMULA characters;
  * outside string literals, only cell references, numbers, the listed
    operators and separators, and sheet references ('Sheet name'!A1) appear;
  * every function name is in FUNCTIONS: arithmetic, statistics, logic, text,
    date, lookup and finance functions that read cells of this workbook only.

That excludes everything that reaches outside the workbook or runs something:
external workbook references ("[book.xlsx]"), DDE ("=cmd|..."), structured or
array syntax, and functions such as WEBSERVICE, HYPERLINK, RTD, CALL, INDIRECT
and FILTERXML.
"""
import re

MAX_FORMULA = 500

FUNCTIONS = frozenset({
    # arithmetic and statistics
    "SUM", "SUMIF", "SUMIFS", "SUMPRODUCT", "AVERAGE", "AVERAGEIF", "AVERAGEIFS", "MIN", "MAX", "COUNT", "COUNTA",
    "COUNTBLANK", "COUNTIF", "COUNTIFS", "MEDIAN", "MODE", "STDEV", "VAR", "ABS", "ROUND", "ROUNDUP", "ROUNDDOWN",
    "INT", "TRUNC", "MOD", "POWER", "SQRT", "EXP", "LN", "LOG", "LOG10", "PRODUCT", "QUOTIENT", "CEILING", "FLOOR",
    "LARGE", "SMALL", "RANK", "PERCENTILE",
    # logic
    "IF", "IFS", "IFERROR", "IFNA", "AND", "OR", "NOT", "XOR", "TRUE", "FALSE", "ISBLANK", "ISNUMBER", "ISTEXT",
    "ISERROR", "NA",
    # text
    "CONCATENATE", "CONCAT", "TEXT", "LEFT", "RIGHT", "MID", "LEN", "TRIM", "UPPER", "LOWER", "PROPER", "VALUE",
    "SUBSTITUTE", "FIND", "SEARCH", "REPT",
    # dates
    "DATE", "YEAR", "MONTH", "DAY", "TODAY", "NOW", "EDATE", "EOMONTH", "DATEDIF", "WEEKDAY", "DAYS", "NETWORKDAYS",
    # lookup within the workbook
    "VLOOKUP", "HLOOKUP", "XLOOKUP", "INDEX", "MATCH", "CHOOSE", "ROW", "COLUMN", "ROWS", "COLUMNS",
    # finance
    "PMT", "IPMT", "PPMT", "PV", "FV", "NPV", "IRR", "RATE", "NPER", "SLN",
})

_STRING = re.compile(r'"(?:[^"]|"")*"')
_SHEET = re.compile(r"'(?:[^'\[\]:*?/\\]|''){1,31}'!")
_ALLOWED = re.compile(r"[A-Za-z0-9_.$:,()+\-*/^&=<>% !]*")
_NAME_CALL = re.compile(r"([A-Za-z_][A-Za-z0-9_.]*)\s*\(")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")
_REFERENCE = re.compile(r"\$?[A-Za-z]{1,3}\$?[0-9]{1,7}|\$?[A-Za-z]{1,3}|[A-Za-z_][A-Za-z0-9_]{0,30}!")


def is_safe_formula(text):
    """Whether text is a formula from the closed set described above."""
    if type(text) is not str or not 2 <= len(text) <= MAX_FORMULA or not text.startswith("="):
        return False
    body = text[1:]
    if body.count('"') % 2:
        return False
    body = _STRING.sub(" ", body)          # String literals may hold any text.
    body = _SHEET.sub(" ", body)           # Quoted sheet references.
    if '"' in body or "'" in body or _ALLOWED.fullmatch(body) is None:
        return False
    if body.count("(") != body.count(")"):
        return False
    calls = {name.upper() for name in _NAME_CALL.findall(body)}
    if not calls <= FUNCTIONS:
        return False
    # Every remaining word must be a function just checked, a cell or column
    # reference, an unquoted sheet name before "!", or TRUE / FALSE.
    remainder = _NAME_CALL.sub("(", body)
    for word in _NAME.findall(remainder):
        if word.upper() in ("TRUE", "FALSE"):
            continue
        if _REFERENCE.fullmatch(word) is None and _REFERENCE.fullmatch(word + "!") is None:
            return False
        if _REFERENCE.fullmatch(word) is None and (word + "!") not in remainder:
            return False
    return True
