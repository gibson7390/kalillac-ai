"""A spreadsheet formula engine for the allowlisted formulas, with Excel's semantics.

Kalillac evaluates every formula of a workbook it generates, for three reasons:

  * the file carries the computed values, so previews that do not recalculate
    (mail clients, phones, cloud file viewers) show numbers instead of blanks;
  * a structurally broken model is refused before it reaches a customer: a
    reference to a sheet that does not exist (#REF!), an unknown function
    (#NAME?), or a circular chain of formulas;
  * the assistant can quote figures from the workbook that match the file.

Semantics follow Excel where it matters for business models: blanks are zero
in arithmetic, numeric text is coerced, text in a range is skipped by SUM and
friends, comparison ranks numbers < text < booleans and is case-insensitive,
errors propagate through arithmetic, and dates are serial numbers in the 1900
system. Every function the formula allowlist accepts is implemented here;
formulas.FUNCTIONS and FUNCTIONS below are kept identical by a test.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import math
import re

from openpyxl.formula.tokenizer import Tokenizer
from openpyxl.utils import column_index_from_string

EPOCH = date(1899, 12, 30)
MAX_ITERATIONS = 2_000_000  # Expressions evaluated plus cells read, per workbook: a bound on runaway work.


class ExcelError(Exception):
    """An Excel error value. It is raised to propagate and stored as the result of a cell."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code

    def __eq__(self, other):
        return isinstance(other, ExcelError) and other.code == self.code

    def __hash__(self):
        return hash(self.code)

    def __repr__(self):
        return self.code


DIV0, VALUE, REF, NAME, NUM, NA = (ExcelError(code) for code in ("#DIV/0!", "#VALUE!", "#REF!", "#NAME?", "#NUM!",
                                                                 "#N/A"))
STRUCTURAL = {REF, NAME}


class CircularReference(Exception):
    pass


class WorkTooLarge(Exception):
    pass


@dataclass(frozen=True)
class Range:
    sheet: str
    top: int
    left: int
    bottom: int
    right: int

    @property
    def single(self):
        return self.top == self.bottom and self.left == self.right


def to_serial(value):
    if isinstance(value, datetime):
        delta = value - datetime(1899, 12, 30)
        return delta.days + delta.seconds / 86400
    return float((value - EPOCH).days)


def from_serial(serial):
    return EPOCH + timedelta(days=int(math.floor(serial)))


# --- parsing ------------------------------------------------------------------------------

_REF = re.compile(r"^(?:(?P<sheet>'(?:[^']|'')+'|[A-Za-z_][A-Za-z0-9_.]*)!)?"
                  r"(?P<a>\$?[A-Za-z]{1,3}\$?[0-9]{1,7}|\$?[A-Za-z]{1,3}|\$?[0-9]{1,7})"
                  r"(?::(?P<b>\$?[A-Za-z]{1,3}\$?[0-9]{1,7}|\$?[A-Za-z]{1,3}|\$?[0-9]{1,7}))?$")
_CELL = re.compile(r"\$?([A-Za-z]{1,3})?\$?([0-9]{1,7})?$")
_PRECEDENCE = {"=": 1, "<>": 1, "<": 1, ">": 1, "<=": 1, ">=": 1, "&": 2, "+": 3, "-": 3, "*": 4, "/": 4, "^": 5}


def _parse_reference(text, sheet):
    match = _REF.match(text)
    if match is None:
        raise ExcelError("#NAME?")
    name = match.group("sheet")
    if name is not None:
        sheet = name[1:-1].replace("''", "'") if name.startswith("'") else name
    first, second = match.group("a"), match.group("b") or match.group("a")
    (c1, r1), (c2, r2) = (_CELL.match(part).groups() for part in (first, second))
    left = column_index_from_string(c1.upper()) if c1 else 1
    right = column_index_from_string(c2.upper()) if c2 else 16384
    top = int(r1) if r1 else 1
    bottom = int(r2) if r2 else 1_048_576
    if not c1 and not r1:
        raise ExcelError("#NAME?")
    return Range(sheet, min(top, bottom), min(left, right), max(top, bottom), max(left, right))


def parse(formula, sheet):
    """An expression tree for formula (with its leading "=") written on sheet."""
    tokens = [t for t in Tokenizer(formula).items if t.type != "WHITE-SPACE"]
    position = [0]

    def peek():
        return tokens[position[0]] if position[0] < len(tokens) else None

    def take():
        token = peek()
        position[0] += 1
        return token

    def primary():
        token = take()
        if token is None:
            raise ExcelError("#VALUE!")
        if token.type == "OPERATOR-PREFIX":
            node = ("neg" if token.value == "-" else "pos", unary())
            return node
        if token.type == "OPERAND":
            if token.subtype == "NUMBER":
                return ("num", float(token.value))
            if token.subtype == "TEXT":
                return ("str", token.value[1:-1].replace('""', '"'))
            if token.subtype == "LOGICAL":
                return ("bool", token.value.upper() == "TRUE")
            if token.subtype == "ERROR":
                return ("err", ExcelError(token.value.upper()))
            return ("ref", _parse_reference(token.value, sheet))
        if token.type == "FUNC" and token.subtype == "OPEN":
            name = token.value[:-1].upper()
            args = []
            if peek() is not None and peek().type == "FUNC" and peek().subtype == "CLOSE":
                take()
                return ("call", name, args)
            while True:
                if peek() is not None and peek().type == "SEP":
                    args.append(("blank",))
                else:
                    args.append(expression(0))
                token = take()
                if token is None:
                    raise ExcelError("#VALUE!")
                if token.type == "FUNC" and token.subtype == "CLOSE":
                    if args and args[-1] is None:
                        args[-1] = ("blank",)
                    return ("call", name, args)
                if token.type != "SEP":
                    raise ExcelError("#VALUE!")
                if peek() is not None and peek().type == "FUNC" and peek().subtype == "CLOSE":
                    args.append(("blank",))
        if token.type == "PAREN" and token.subtype == "OPEN":
            node = expression(0)
            closing = take()
            if closing is None or closing.type != "PAREN":
                raise ExcelError("#VALUE!")
            return node
        raise ExcelError("#NAME?")

    def unary():
        node = primary()
        while peek() is not None and peek().type == "OPERATOR-POSTFIX":
            take()
            node = ("pct", node)
        return node

    def expression(minimum):
        node = unary()
        while True:
            token = peek()
            if token is None or token.type != "OPERATOR-INFIX" or token.value not in _PRECEDENCE:
                return node
            precedence = _PRECEDENCE[token.value]
            if precedence < minimum:
                return node
            take()
            right = expression(precedence + 1)  # Every Excel operator, "^" included, is left-associative.
            node = ("op", token.value, node, right)

    tree = expression(0)
    if position[0] != len(tokens):
        raise ExcelError("#VALUE!")
    return tree


# --- values -------------------------------------------------------------------------------


def number(value):
    """Excel's coercion of one value to a number."""
    if isinstance(value, ExcelError):
        raise value
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, (date, datetime)):
        return to_serial(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        percent = text.endswith("%")
        try:
            result = float(text[:-1] if percent else text)
        except ValueError:
            raise VALUE from None
        return result / 100 if percent else result
    raise VALUE


def text(value):
    if isinstance(value, ExcelError):
        raise value
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        return repr(round(value, 15 - max(0, int(math.log10(abs(value))) + 1))) if value else "0"
    if isinstance(value, (date, datetime)):
        return text(to_serial(value))
    return str(value)


def truth(value):
    if isinstance(value, ExcelError):
        raise value
    if isinstance(value, str):
        if value.upper() in ("TRUE", "FALSE"):
            return value.upper() == "TRUE"
        raise VALUE
    return number(value) != 0


def _rank(value):
    if isinstance(value, bool):
        return 2
    if isinstance(value, str):
        return 1
    return 0


def compare(left, right, operator):
    for value in (left, right):
        if isinstance(value, ExcelError):
            raise value
    left = 0.0 if left is None and not isinstance(right, str) else ("" if left is None else left)
    right = 0.0 if right is None and not isinstance(left, str) else ("" if right is None else right)
    if isinstance(left, (date, datetime)):
        left = to_serial(left)
    if isinstance(right, (date, datetime)):
        right = to_serial(right)
    a, b = _rank(left), _rank(right)
    if a != b:
        order = (a > b) - (a < b)
    elif a == 1:
        x, y = left.lower(), right.lower()
        order = (x > y) - (x < y)
    else:
        x, y = float(left), float(right)
        order = (x > y) - (x < y)
    return {"=": order == 0, "<>": order != 0, "<": order < 0, ">": order > 0, "<=": order <= 0,
            ">=": order >= 0}[operator]


def flatten(value):
    if isinstance(value, list):
        for row in value:
            yield from row
    else:
        yield value


def numbers_of(args, direct_text=True):
    """Numbers from function arguments the way SUM sees them: ranges skip text, booleans and
    blanks; direct arguments are coerced."""
    out = []
    for arg in args:
        if isinstance(arg, list):
            for value in flatten(arg):
                if isinstance(value, ExcelError):
                    raise value
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    out.append(float(value))
                elif isinstance(value, (date, datetime)):
                    out.append(to_serial(value))
        elif arg is not None or direct_text:
            out.append(number(arg))
    return out


def _criterion(criterion):
    """A test for COUNTIF-style criteria: ">=10", "<>x", "a*b?", 5, "" ."""
    if isinstance(criterion, ExcelError):
        raise criterion
    if not isinstance(criterion, str):
        target = criterion
        return lambda value: value is not None and not isinstance(value, ExcelError) and compare(value, target, "=")
    match = re.match(r"^(<=|>=|<>|=|<|>)?(.*)$", criterion, re.S)
    operator, operand = match.group(1) or "=", match.group(2)
    try:
        target = number(operand) if operand.strip() else operand
    except ExcelError:
        target = operand
    if isinstance(target, str) and operator in ("=", "<>") and any(c in target for c in "*?"):
        pattern = re.compile("^" + re.escape(target).replace(r"\*", ".*").replace(r"\?", ".") + "$", re.I | re.S)
        hit = lambda value: isinstance(value, str) and bool(pattern.match(value))  # noqa: E731
        return hit if operator == "=" else (lambda value: not hit(value))
    if isinstance(target, str) and target == "" and operator in ("=", "<>"):
        return (lambda value: value is None or value == "") if operator == "=" else (
            lambda value: value is not None and value != "")

    def test(value):
        if value is None or isinstance(value, ExcelError):
            return operator == "<>"
        if isinstance(target, float) and isinstance(value, str):
            try:
                value = number(value)
            except ExcelError:
                return operator == "<>"
        if isinstance(target, str) and not isinstance(value, str):
            return operator == "<>"
        return compare(value, target, operator)
    return test


def _cells(area):
    return list(flatten(area)) if isinstance(area, list) else [area]


# --- financial helpers --------------------------------------------------------------------


def _pmt(rate, periods, present, future=0.0, when=0.0):
    if periods == 0:
        raise NUM
    if rate == 0:
        return -(present + future) / periods
    factor = (1 + rate) ** periods
    return -(rate * (present * factor + future)) / ((1 + rate * when) * (factor - 1))


def _fv(rate, periods, payment, present=0.0, when=0.0):
    if rate == 0:
        return -(present + payment * periods)
    factor = (1 + rate) ** periods
    return -(present * factor + payment * (1 + rate * when) * (factor - 1) / rate)


def _ipmt(rate, period, periods, present, future=0.0, when=0.0):
    if not 1 <= period <= periods:
        raise NUM
    payment = _pmt(rate, periods, present, future, when)
    if when == 1 and period == 1:
        return 0.0
    balance = _fv(rate, period - 1, payment, present, when)
    interest = balance * rate
    return interest / (1 + rate) if when == 1 else interest


def _irr(values, guess=0.1):
    if not any(v > 0 for v in values) or not any(v < 0 for v in values):
        raise NUM
    rate = guess
    for _ in range(100):
        npv = sum(v / (1 + rate) ** i for i, v in enumerate(values))
        slope = sum(-i * v / (1 + rate) ** (i + 1) for i, v in enumerate(values))
        if slope == 0:
            break
        step = npv / slope
        rate -= step
        if rate <= -1:
            rate = -0.999999
        if abs(step) < 1e-12:
            return rate
    raise NUM


def _rate(periods, payment, present, future=0.0, when=0.0, guess=0.1):
    rate = guess
    for _ in range(100):
        def f(r):
            return _fv(r, periods, payment, present, when) - future
        value = f(rate)
        delta = 1e-7
        slope = (f(rate + delta) - value) / delta
        if slope == 0:
            break
        step = value / slope
        rate -= step
        if abs(step) < 1e-12:
            return rate
    raise NUM


def _nper(rate, payment, present, future=0.0, when=0.0):
    if rate == 0:
        if payment == 0:
            raise NUM
        return -(present + future) / payment
    numerator = payment * (1 + rate * when) - future * rate
    denominator = present * rate + payment * (1 + rate * when)
    if numerator / denominator <= 0:
        raise NUM
    return math.log(numerator / denominator) / math.log(1 + rate)


def _excel_round(value, digits):
    factor = 10 ** digits
    scaled = abs(value) * factor
    rounded = math.floor(scaled + 0.5 + 1e-9)
    return math.copysign(rounded / factor, value)


def _add_months(start, months):
    month = start.month - 1 + months
    year = start.year + month // 12
    month = month % 12 + 1
    last = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)).day
    return date(year, month, min(start.day, last))


def _format_text(value, pattern):
    """TEXT() for the common business formats."""
    if isinstance(value, str):
        return value
    number_value = number(value)
    lowered = pattern.lower()
    if re.fullmatch(r"[ymd/\-. ]+", lowered):
        day = from_serial(number_value)
        mapping = [("yyyy", f"{day.year:04d}"), ("yy", f"{day.year % 100:02d}"), ("mm", f"{day.month:02d}"),
                   ("dd", f"{day.day:02d}"), ("m", str(day.month)), ("d", str(day.day))]
        out = lowered
        for token, replacement in mapping:
            out = out.replace(token, "\x00" + replacement + "\x00" if token not in ("m", "d") else replacement)
        return out.replace("\x00", "")
    percent = pattern.endswith("%")
    core = pattern.rstrip("%")
    if percent:
        number_value *= 100
    prefix = re.match(r"^[^#0.,]*", core).group(0)
    suffix = re.search(r"[^#0.,]*$", core).group(0)
    body = core[len(prefix):len(core) - len(suffix)]
    integer_part, _, fraction_part = body.partition(".")
    decimals = len(fraction_part)
    grouping = "," in integer_part
    padding = integer_part.replace(",", "").count("0")      # "0000" pads to four digits.
    magnitude = abs(_excel_round(number_value, decimals))   # Halves round away from zero, as in Excel.
    whole, _, fraction = f"{magnitude:.{decimals}f}".partition(".")
    whole = whole.rjust(padding, "0") if padding else ("" if whole == "0" and "#" in integer_part else whole)
    if grouping:
        whole = f"{int(whole or 0):,}".rjust(len(whole), "0") if whole else whole
    formatted = whole + ("." + fraction if decimals else "")
    sign = "-" if number_value < 0 and magnitude else ""
    return sign + prefix + formatted + suffix + ("%" if percent else "")


# --- the engine ---------------------------------------------------------------------------


class Workbook:
    """Cells by sheet; formulas evaluated on demand with memoisation."""

    def __init__(self, today=None):
        self.cells = {}          # (sheet key, row, column) -> constant or ("formula", text, tree)
        self.names = {}          # sheet key -> sheet name as written
        self.extent = {}         # sheet key -> (max row, max column)
        self.results = {}
        self.visiting = set()
        self.today = today or date.today()
        self.work = 0
        self.current = None

    def add_sheet(self, name):
        key = name.lower()
        self.names[key] = name
        self.extent.setdefault(key, (0, 0))

    def set(self, sheet, row, column, value):
        key = sheet.lower()
        if isinstance(value, str) and value.startswith("="):
            value = ("formula", value, None)
        self.cells[(key, row, column)] = value
        bottom, right = self.extent.get(key, (0, 0))
        self.extent[key] = (max(bottom, row), max(right, column))

    def value(self, sheet_key, row, column):
        cell = self.cells.get((sheet_key, row, column))
        if not (isinstance(cell, tuple) and cell and cell[0] == "formula"):
            return cell
        address = (sheet_key, row, column)
        if address in self.results:
            return self.results[address]
        if address in self.visiting:
            raise CircularReference(address)
        self.visiting.add(address)
        previous = self.current
        self.current = address
        try:
            tree = cell[2] or parse(cell[1], self.names[sheet_key])
            self.cells[address] = ("formula", cell[1], tree)
            result = self.evaluate(tree)
            if isinstance(result, list):
                result = result[0][0] if result and result[0] else None
            if isinstance(result, Range):
                result = self.resolve(result)
                result = result[0][0] if isinstance(result, list) else result
        except ExcelError as error:
            result = error
        except (ZeroDivisionError,):
            result = DIV0
        except (OverflowError, ValueError):
            result = NUM
        finally:
            self.visiting.discard(address)
            self.current = previous
        self.results[address] = result
        return result

    def resolve(self, reference):
        key = reference.sheet.lower()
        if key not in self.names:
            raise REF
        bottom, right = self.extent[key]
        last_row, last_column = min(reference.bottom, max(bottom, reference.top)), min(reference.right,
                                                                                      max(right, reference.left))
        if reference.single:
            return self.value(key, reference.top, reference.left)
        # Cells read count toward the work bound: a few formulas over large ranges are work too.
        self.work += (last_row - reference.top + 1) * (last_column - reference.left + 1)
        if self.work > MAX_ITERATIONS:
            raise WorkTooLarge()
        return [[self.value(key, r, c) for c in range(reference.left, last_column + 1)]
                for r in range(reference.top, last_row + 1)]

    def evaluate(self, node, keep_reference=False):
        self.work += 1
        if self.work > MAX_ITERATIONS:
            raise WorkTooLarge()
        kind = node[0]
        if kind == "num":
            return node[1]
        if kind == "str":
            return node[1]
        if kind == "bool":
            return node[1]
        if kind == "err":
            raise node[1]
        if kind == "blank":
            return None
        if kind == "ref":
            if keep_reference:
                return node[1]
            return self.resolve(node[1])
        if kind in ("neg", "pos"):
            value = self.scalar(self.evaluate(node[1]))
            return -number(value) if kind == "neg" else number(value)
        if kind == "pct":
            return number(self.scalar(self.evaluate(node[1]))) / 100
        if kind == "op":
            operator = node[1]
            left = self.scalar(self.evaluate(node[2]))
            right = self.scalar(self.evaluate(node[3]))
            if operator in ("=", "<>", "<", ">", "<=", ">="):
                return compare(left, right, operator)
            if operator == "&":
                return text(left) + text(right)
            a, b = number(left), number(right)
            if operator == "+":
                return a + b
            if operator == "-":
                return a - b
            if operator == "*":
                return a * b
            if operator == "/":
                if b == 0:
                    raise DIV0
                return a / b
            if operator == "^":
                if a == 0 and b < 0:
                    raise DIV0
                result = a ** b
                if isinstance(result, complex):
                    raise NUM
                return result
        if kind == "call":
            return self.call(node[1], node[2])
        raise VALUE

    def scalar(self, value):
        """A single value from a range result: Excel's implicit intersection, simplified to the
        first cell for a one-cell range and #VALUE! for anything larger."""
        if isinstance(value, list):
            if len(value) == 1 and len(value[0]) == 1:
                return value[0][0]
            raise VALUE
        return value

    def args(self, nodes):
        return [self.evaluate(node) for node in nodes]

    def call(self, name, nodes):
        handler = FUNCTIONS.get(name)
        if handler is None:
            raise NAME
        return handler(self, nodes)


def _f(arity_min, arity_max=None):
    """Decorator: argument count checking for a function implementation."""
    def wrap(function):
        def checked(engine, nodes):
            if len(nodes) < arity_min or (arity_max is not None and len(nodes) > arity_max):
                raise VALUE
            return function(engine, nodes)
        return checked
    return wrap


def _values(engine, nodes):
    return engine.args(nodes)


def _scalars(engine, nodes):
    return [engine.scalar(v) for v in engine.args(nodes)]


def _num_args(engine, nodes, count):
    values = _scalars(engine, nodes)
    return [number(v) for v in values[:count]] + values[count:]


FUNCTIONS = {}


def register(*names, low=0, high=None):
    def wrap(function):
        checked = _f(low, high)(function)
        for name in names:
            FUNCTIONS[name] = checked
        return function
    return wrap


# Aggregates.
register("SUM", low=1)(lambda e, n: math.fsum(numbers_of(_values(e, n))))
register("PRODUCT", low=1)(lambda e, n: math.prod(numbers_of(_values(e, n))))
register("COUNT", low=1)(lambda e, n: float(sum(
    1 for a in _values(e, n) for v in (flatten(a) if isinstance(a, list) else [a])
    if isinstance(v, (int, float, date, datetime)) and not isinstance(v, bool))))
register("COUNTA", low=1)(lambda e, n: float(sum(
    1 for a in _values(e, n) for v in (flatten(a) if isinstance(a, list) else [a]) if v is not None)))
register("COUNTBLANK", low=1, high=1)(lambda e, n: float(sum(
    1 for v in _cells(e.evaluate(n[0])) if v is None or v == "")))


@register("AVERAGE", low=1)
def _average(engine, nodes):
    values = numbers_of(_values(engine, nodes))
    if not values:
        raise DIV0
    return math.fsum(values) / len(values)


@register("MIN", low=1)
def _min(engine, nodes):
    values = numbers_of(_values(engine, nodes))
    return min(values) if values else 0.0


@register("MAX", low=1)
def _max(engine, nodes):
    values = numbers_of(_values(engine, nodes))
    return max(values) if values else 0.0


@register("MEDIAN", low=1)
def _median(engine, nodes):
    values = sorted(numbers_of(_values(engine, nodes)))
    if not values:
        raise NUM
    middle = len(values) // 2
    return values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2


@register("MODE", low=1)
def _mode(engine, nodes):
    values = numbers_of(_values(engine, nodes))
    counts = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    best = max(counts.values(), default=0)
    if best < 2:
        raise NA
    return next(value for value in values if counts[value] == best)


def _variance(values):
    if len(values) < 2:
        raise DIV0
    mean = math.fsum(values) / len(values)
    return math.fsum((v - mean) ** 2 for v in values) / (len(values) - 1)


register("VAR", low=1)(lambda e, n: _variance(numbers_of(_values(e, n))))
register("STDEV", low=1)(lambda e, n: math.sqrt(_variance(numbers_of(_values(e, n)))))


def _large(engine, nodes, largest):
    values = sorted(numbers_of([engine.evaluate(nodes[0])]), reverse=largest)
    k = int(number(engine.scalar(engine.evaluate(nodes[1]))))
    if not 1 <= k <= len(values):
        raise NUM
    return values[k - 1]


FUNCTIONS["LARGE"] = _f(2, 2)(lambda e, n: _large(e, n, True))
FUNCTIONS["SMALL"] = _f(2, 2)(lambda e, n: _large(e, n, False))


@register("RANK", low=2, high=3)
def _rank_function(engine, nodes):
    value = number(engine.scalar(engine.evaluate(nodes[0])))
    values = numbers_of([engine.evaluate(nodes[1])])
    ascending = len(nodes) > 2 and number(engine.scalar(engine.evaluate(nodes[2]))) != 0
    if value not in values:
        raise NA
    return float(1 + sum(1 for v in values if (v < value if ascending else v > value)))


@register("PERCENTILE", low=2, high=2)
def _percentile(engine, nodes):
    values = sorted(numbers_of([engine.evaluate(nodes[0])]))
    k = number(engine.scalar(engine.evaluate(nodes[1])))
    if not values or not 0 <= k <= 1:
        raise NUM
    position = k * (len(values) - 1)
    low = int(math.floor(position))
    high = min(low + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (position - low)


# Conditional aggregates.
def _pairs(engine, nodes, start):
    tests = []
    for index in range(start, len(nodes), 2):
        area = _cells(engine.evaluate(nodes[index]))
        tests.append((area, _criterion(engine.scalar(engine.evaluate(nodes[index + 1])))))
    return tests


def _matching(tests, size):
    for area, _test in tests:
        if len(area) != size:
            raise VALUE
    return [all(test(area[i]) for area, test in tests) for i in range(size)]


@register("SUMIF", low=2, high=3)
def _sumif(engine, nodes):
    area = _cells(engine.evaluate(nodes[0]))
    test = _criterion(engine.scalar(engine.evaluate(nodes[1])))
    targets = _cells(engine.evaluate(nodes[2])) if len(nodes) > 2 else area
    return math.fsum(number(targets[i]) for i, value in enumerate(area)
                     if i < len(targets) and test(value) and isinstance(targets[i], (int, float))
                     and not isinstance(targets[i], bool))


@register("SUMIFS", low=3)
def _sumifs(engine, nodes):
    if len(nodes) % 2 == 0:
        raise VALUE
    targets = _cells(engine.evaluate(nodes[0]))
    hits = _matching(_pairs(engine, nodes, 1), len(targets))
    return math.fsum(number(v) for v, hit in zip(targets, hits)
                     if hit and isinstance(v, (int, float)) and not isinstance(v, bool))


@register("COUNTIF", low=2, high=2)
def _countif(engine, nodes):
    test = _criterion(engine.scalar(engine.evaluate(nodes[1])))
    return float(sum(1 for value in _cells(engine.evaluate(nodes[0])) if test(value)))


@register("COUNTIFS", low=2)
def _countifs(engine, nodes):
    if len(nodes) % 2:
        raise VALUE
    tests = _pairs(engine, nodes, 0)
    return float(sum(_matching(tests, len(tests[0][0]))))


@register("AVERAGEIF", low=2, high=3)
def _averageif(engine, nodes):
    area = _cells(engine.evaluate(nodes[0]))
    test = _criterion(engine.scalar(engine.evaluate(nodes[1])))
    targets = _cells(engine.evaluate(nodes[2])) if len(nodes) > 2 else area
    values = [number(targets[i]) for i, value in enumerate(area) if i < len(targets) and test(value)
              and isinstance(targets[i], (int, float)) and not isinstance(targets[i], bool)]
    if not values:
        raise DIV0
    return math.fsum(values) / len(values)


@register("AVERAGEIFS", low=3)
def _averageifs(engine, nodes):
    targets = _cells(engine.evaluate(nodes[0]))
    hits = _matching(_pairs(engine, nodes, 1), len(targets))
    values = [number(v) for v, hit in zip(targets, hits) if hit and isinstance(v, (int, float))
              and not isinstance(v, bool)]
    if not values:
        raise DIV0
    return math.fsum(values) / len(values)


@register("SUMPRODUCT", low=1)
def _sumproduct(engine, nodes):
    areas = [_cells(engine.evaluate(node)) for node in nodes]
    if len({len(area) for area in areas}) != 1:
        raise VALUE
    total = 0.0
    for values in zip(*areas):
        product = 1.0
        for value in values:
            if isinstance(value, ExcelError):
                raise value
            product *= value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0
        total += product
    return total


# Maths.
register("ABS", low=1, high=1)(lambda e, n: abs(_num_args(e, n, 1)[0]))
register("INT", low=1, high=1)(lambda e, n: float(math.floor(_num_args(e, n, 1)[0])))
register("SQRT", low=1, high=1)(lambda e, n: (lambda v: math.sqrt(v) if v >= 0 else (_ for _ in ()).throw(NUM))(
    _num_args(e, n, 1)[0]))
register("EXP", low=1, high=1)(lambda e, n: math.exp(_num_args(e, n, 1)[0]))
register("LN", low=1, high=1)(lambda e, n: (lambda v: math.log(v) if v > 0 else (_ for _ in ()).throw(NUM))(
    _num_args(e, n, 1)[0]))
register("LOG10", low=1, high=1)(lambda e, n: (lambda v: math.log10(v) if v > 0 else (_ for _ in ()).throw(NUM))(
    _num_args(e, n, 1)[0]))


@register("LOG", low=1, high=2)
def _log(engine, nodes):
    values = _num_args(engine, nodes, 2)
    base = values[1] if len(values) > 1 else 10.0
    if values[0] <= 0 or base <= 0 or base == 1:
        raise NUM
    return math.log(values[0], base)


@register("POWER", low=2, high=2)
def _power(engine, nodes):
    a, b = _num_args(engine, nodes, 2)
    if a == 0 and b < 0:
        raise DIV0
    result = a ** b
    if isinstance(result, complex):
        raise NUM
    return result


@register("MOD", low=2, high=2)
def _mod(engine, nodes):
    a, b = _num_args(engine, nodes, 2)
    if b == 0:
        raise DIV0
    return a - b * math.floor(a / b)


@register("QUOTIENT", low=2, high=2)
def _quotient(engine, nodes):
    a, b = _num_args(engine, nodes, 2)
    if b == 0:
        raise DIV0
    return float(math.trunc(a / b))


@register("ROUND", low=2, high=2)
def _round(engine, nodes):
    value, digits = _num_args(engine, nodes, 2)
    return _excel_round(value, int(digits))


@register("ROUNDUP", low=2, high=2)
def _roundup(engine, nodes):
    value, digits = _num_args(engine, nodes, 2)
    factor = 10 ** int(digits)
    return math.copysign(math.ceil(abs(value) * factor - 1e-9) / factor, value)


@register("ROUNDDOWN", "TRUNC", low=1, high=2)
def _rounddown(engine, nodes):
    values = _num_args(engine, nodes, 2)
    factor = 10 ** int(values[1] if len(values) > 1 else 0)
    return math.copysign(math.floor(abs(values[0]) * factor + 1e-9) / factor, values[0])


@register("CEILING", low=1, high=2)
def _ceiling(engine, nodes):
    values = _num_args(engine, nodes, 2)
    significance = values[1] if len(values) > 1 else 1.0
    if significance == 0:
        return 0.0
    if values[0] > 0 and significance < 0:
        raise NUM
    return math.ceil(values[0] / significance - 1e-12) * significance


@register("FLOOR", low=1, high=2)
def _floor(engine, nodes):
    values = _num_args(engine, nodes, 2)
    significance = values[1] if len(values) > 1 else 1.0
    if significance == 0:
        raise DIV0
    if values[0] > 0 and significance < 0:
        raise NUM
    return math.floor(values[0] / significance + 1e-12) * significance


# Logic.
@register("IF", low=1, high=3)
def _if(engine, nodes):
    condition = truth(engine.scalar(engine.evaluate(nodes[0])))
    if condition:
        return engine.evaluate(nodes[1]) if len(nodes) > 1 else True
    return engine.evaluate(nodes[2]) if len(nodes) > 2 else False


@register("IFS", low=2)
def _ifs(engine, nodes):
    if len(nodes) % 2:
        raise VALUE
    for index in range(0, len(nodes), 2):
        if truth(engine.scalar(engine.evaluate(nodes[index]))):
            return engine.evaluate(nodes[index + 1])
    raise NA


@register("IFERROR", low=2, high=2)
def _iferror(engine, nodes):
    try:
        value = engine.scalar(engine.evaluate(nodes[0]))
        if isinstance(value, ExcelError):
            raise value
        return value
    except (ExcelError, ZeroDivisionError, OverflowError, ValueError):
        return engine.evaluate(nodes[1])


@register("IFNA", low=2, high=2)
def _ifna(engine, nodes):
    try:
        value = engine.scalar(engine.evaluate(nodes[0]))
        if value == NA:
            raise NA
        return value
    except ExcelError as error:
        if error == NA:
            return engine.evaluate(nodes[1])
        raise


register("AND", low=1)(lambda e, n: all(truth(v) for a in _values(e, n) for v in (
    [x for x in flatten(a) if x is not None and not isinstance(x, str)] if isinstance(a, list) else [a])))
register("OR", low=1)(lambda e, n: any(truth(v) for a in _values(e, n) for v in (
    [x for x in flatten(a) if x is not None and not isinstance(x, str)] if isinstance(a, list) else [a])))
register("XOR", low=1)(lambda e, n: sum(truth(v) for a in _values(e, n) for v in (
    [x for x in flatten(a) if x is not None and not isinstance(x, str)] if isinstance(a, list) else [a])) % 2 == 1)
register("NOT", low=1, high=1)(lambda e, n: not truth(e.scalar(e.evaluate(n[0]))))
register("TRUE", low=0, high=0)(lambda e, n: True)
register("FALSE", low=0, high=0)(lambda e, n: False)
register("NA", low=0, high=0)(lambda e, n: (_ for _ in ()).throw(NA))


def _is(predicate):
    def check(engine, nodes):
        try:
            value = engine.scalar(engine.evaluate(nodes[0]))
        except ExcelError as error:
            value = error
        return predicate(value)
    return check


register("ISBLANK", low=1, high=1)(_is(lambda v: v is None))
register("ISNUMBER", low=1, high=1)(_is(lambda v: isinstance(v, (int, float, date, datetime))
                                        and not isinstance(v, bool)))
register("ISTEXT", low=1, high=1)(_is(lambda v: isinstance(v, str)))
register("ISERROR", low=1, high=1)(_is(lambda v: isinstance(v, ExcelError)))


# Text.
register("CONCATENATE", "CONCAT", low=1)(lambda e, n: "".join(
    text(v) for a in _values(e, n) for v in (flatten(a) if isinstance(a, list) else [a])))
register("LEN", low=1, high=1)(lambda e, n: float(len(text(e.scalar(e.evaluate(n[0]))))))
register("TRIM", low=1, high=1)(lambda e, n: " ".join(text(e.scalar(e.evaluate(n[0]))).split()))
register("UPPER", low=1, high=1)(lambda e, n: text(e.scalar(e.evaluate(n[0]))).upper())
register("LOWER", low=1, high=1)(lambda e, n: text(e.scalar(e.evaluate(n[0]))).lower())
register("PROPER", low=1, high=1)(lambda e, n: re.sub(r"[A-Za-z]+", lambda m: m.group(0).capitalize(),
                                                     text(e.scalar(e.evaluate(n[0]))).lower()))
register("VALUE", low=1, high=1)(lambda e, n: number(e.scalar(e.evaluate(n[0]))))


@register("TEXT", low=2, high=2)
def _text_function(engine, nodes):
    value, pattern = _scalars(engine, nodes)
    return _format_text(value, text(pattern))


def _side(engine, nodes, left):
    values = _scalars(engine, nodes)
    count = int(number(values[1])) if len(values) > 1 else 1
    if count < 0:
        raise VALUE
    string = text(values[0])
    return string[:count] if left else (string[-count:] if count else "")


FUNCTIONS["LEFT"] = _f(1, 2)(lambda e, n: _side(e, n, True))
FUNCTIONS["RIGHT"] = _f(1, 2)(lambda e, n: _side(e, n, False))


@register("MID", low=3, high=3)
def _mid(engine, nodes):
    string, start, count = _scalars(engine, nodes)
    start, count = int(number(start)), int(number(count))
    if start < 1 or count < 0:
        raise VALUE
    return text(string)[start - 1:start - 1 + count]


@register("SUBSTITUTE", low=3, high=4)
def _substitute(engine, nodes):
    values = _scalars(engine, nodes)
    string, old, new = text(values[0]), text(values[1]), text(values[2])
    if len(values) > 3:
        instance = int(number(values[3]))
        position = -1
        for _ in range(instance):
            position = string.find(old, position + 1)
            if position < 0:
                return string
        return string[:position] + new + string[position + len(old):]
    return string.replace(old, new) if old else string


def _locate(engine, nodes, insensitive):
    values = _scalars(engine, nodes)
    needle, haystack = text(values[0]), text(values[1])
    start = int(number(values[2])) if len(values) > 2 else 1
    if insensitive:
        needle, haystack = needle.lower(), haystack.lower()
    position = haystack.find(needle, start - 1)
    if position < 0 or start < 1:
        raise VALUE
    return float(position + 1)


FUNCTIONS["FIND"] = _f(2, 3)(lambda e, n: _locate(e, n, False))
FUNCTIONS["SEARCH"] = _f(2, 3)(lambda e, n: _locate(e, n, True))


@register("REPT", low=2, high=2)
def _rept(engine, nodes):
    string, count = _scalars(engine, nodes)
    count = int(number(count))
    if count < 0 or count * len(text(string)) > 32767:
        raise VALUE
    return text(string) * count


# Dates.
def _date_of(value):
    if isinstance(value, (date, datetime)):
        return value if isinstance(value, date) and not isinstance(value, datetime) else value.date()
    serial = number(value)
    if serial < 0:
        raise NUM
    return from_serial(serial)


@register("DATE", low=3, high=3)
def _date(engine, nodes):
    year, month, day = (int(v) for v in _num_args(engine, nodes, 3))
    if year < 1900:
        year += 1900
    start = _add_months(date(year, 1, 1), month - 1)
    return to_serial(start + timedelta(days=day - 1))


register("YEAR", low=1, high=1)(lambda e, n: float(_date_of(e.scalar(e.evaluate(n[0]))).year))
register("MONTH", low=1, high=1)(lambda e, n: float(_date_of(e.scalar(e.evaluate(n[0]))).month))
register("DAY", low=1, high=1)(lambda e, n: float(_date_of(e.scalar(e.evaluate(n[0]))).day))
register("TODAY", low=0, high=0)(lambda e, n: to_serial(e.today))
register("NOW", low=0, high=0)(lambda e, n: to_serial(e.today))


@register("EDATE", low=2, high=2)
def _edate(engine, nodes):
    start, months = _scalars(engine, nodes)
    return to_serial(_add_months(_date_of(start), int(number(months))))


@register("EOMONTH", low=2, high=2)
def _eomonth(engine, nodes):
    start, months = _scalars(engine, nodes)
    shifted = _add_months(_date_of(start).replace(day=1), int(number(months)) + 1)
    return to_serial(shifted - timedelta(days=1))


@register("DAYS", low=2, high=2)
def _days(engine, nodes):
    end, start = _scalars(engine, nodes)
    return float((_date_of(end) - _date_of(start)).days)


@register("DATEDIF", low=3, high=3)
def _datedif(engine, nodes):
    start, end, unit = _scalars(engine, nodes)
    start, end, unit = _date_of(start), _date_of(end), text(unit).upper()
    if end < start:
        raise NUM
    if unit == "D":
        return float((end - start).days)
    months = (end.year - start.year) * 12 + end.month - start.month - (end.day < start.day)
    if unit == "M":
        return float(months)
    if unit == "Y":
        return float(months // 12)
    raise NUM


@register("WEEKDAY", low=1, high=2)
def _weekday(engine, nodes):
    values = _scalars(engine, nodes)
    day = _date_of(values[0])
    kind = int(number(values[1])) if len(values) > 1 else 1
    iso = day.isoweekday()  # Monday 1 .. Sunday 7
    if kind == 1:
        return float(iso % 7 + 1)
    if kind == 2:
        return float(iso)
    if kind == 3:
        return float(iso - 1)
    raise NUM


@register("NETWORKDAYS", low=2, high=3)
def _networkdays(engine, nodes):
    start = _date_of(engine.scalar(engine.evaluate(nodes[0])))
    end = _date_of(engine.scalar(engine.evaluate(nodes[1])))
    holidays = {_date_of(v) for v in _cells(engine.evaluate(nodes[2])) if v is not None} if len(nodes) > 2 else set()
    sign = 1
    if end < start:
        start, end, sign = end, start, -1
    if (end - start).days > 40_000:
        raise NUM
    count = sum(1 for offset in range((end - start).days + 1)
                if (start + timedelta(days=offset)).weekday() < 5 and start + timedelta(days=offset) not in holidays)
    return float(sign * count)


# Lookup.
def _area_rows(value):
    return value if isinstance(value, list) else [[value]]


def _match_position(target, values, mode):
    if mode == 0:
        test = _criterion(target) if isinstance(target, str) else (
            lambda v: v is not None and not isinstance(v, ExcelError) and compare(v, target, "="))
        for index, value in enumerate(values):
            if test(value):
                return index
        raise NA
    best = None
    for index, value in enumerate(values):
        if value is None or isinstance(value, ExcelError) or _rank(value) != _rank(target):
            continue
        if mode == 1 and compare(value, target, "<="):
            best = index
        elif mode == -1 and compare(value, target, ">="):
            best = index
    if best is None:
        raise NA
    return best


@register("MATCH", low=2, high=3)
def _match(engine, nodes):
    target = engine.scalar(engine.evaluate(nodes[0]))
    values = _cells(engine.evaluate(nodes[1]))
    mode = int(number(engine.scalar(engine.evaluate(nodes[2])))) if len(nodes) > 2 else 1
    return float(_match_position(target, values, mode) + 1)


@register("INDEX", low=2, high=3)
def _index(engine, nodes):
    rows = _area_rows(engine.evaluate(nodes[0]))
    row = int(number(engine.scalar(engine.evaluate(nodes[1]))))
    column = int(number(engine.scalar(engine.evaluate(nodes[2])))) if len(nodes) > 2 else 1
    if len(rows) == 1 and len(nodes) == 2:
        row, column = 1, row
    if not (1 <= row <= len(rows) and 1 <= column <= len(rows[0])):
        raise REF if row < 0 or column < 0 else ExcelError("#REF!")
    return rows[row - 1][column - 1]


def _lookup(engine, nodes, vertical):
    target = engine.scalar(engine.evaluate(nodes[0]))
    rows = _area_rows(engine.evaluate(nodes[1]))
    index = int(number(engine.scalar(engine.evaluate(nodes[2]))))
    approximate = truth(engine.scalar(engine.evaluate(nodes[3]))) if len(nodes) > 3 else True
    keys = [row[0] for row in rows] if vertical else rows[0]
    width = len(rows[0]) if vertical else len(rows)
    if not 1 <= index <= width:
        raise REF
    position = _match_position(target, keys, 1 if approximate else 0)
    return rows[position][index - 1] if vertical else rows[index - 1][position]


FUNCTIONS["VLOOKUP"] = _f(3, 4)(lambda e, n: _lookup(e, n, True))
FUNCTIONS["HLOOKUP"] = _f(3, 4)(lambda e, n: _lookup(e, n, False))


@register("XLOOKUP", low=3, high=5)
def _xlookup(engine, nodes):
    target = engine.scalar(engine.evaluate(nodes[0]))
    keys = _cells(engine.evaluate(nodes[1]))
    results = _cells(engine.evaluate(nodes[2]))
    if len(keys) != len(results):
        raise VALUE
    try:
        return results[_match_position(target, keys, 0)]
    except ExcelError as error:
        if error == NA and len(nodes) > 3:
            return engine.scalar(engine.evaluate(nodes[3]))
        raise


@register("CHOOSE", low=2)
def _choose(engine, nodes):
    index = int(number(engine.scalar(engine.evaluate(nodes[0]))))
    if not 1 <= index < len(nodes):
        raise VALUE
    return engine.evaluate(nodes[index])


def _reference_of(engine, nodes):
    if not nodes:
        _sheet, row, column = engine.current
        return row, column, row, column
    node = nodes[0]
    if node[0] != "ref":
        raise VALUE
    reference = node[1]
    return reference.top, reference.left, reference.bottom, reference.right


register("ROW", low=0, high=1)(lambda e, n: float(_reference_of(e, n)[0]))
register("COLUMN", low=0, high=1)(lambda e, n: float(_reference_of(e, n)[1]))
register("ROWS", low=1, high=1)(lambda e, n: float(len(_area_rows(e.evaluate(n[0])))))
register("COLUMNS", low=1, high=1)(lambda e, n: float(len(_area_rows(e.evaluate(n[0]))[0])))


# Finance.
def _optional(values, index, default=0.0):
    return values[index] if len(values) > index and values[index] is not None else default


@register("PMT", low=3, high=5)
def _pmt_function(engine, nodes):
    v = _num_args(engine, nodes, 5)
    return _pmt(v[0], v[1], v[2], _optional(v, 3), _optional(v, 4))


@register("FV", low=3, high=5)
def _fv_function(engine, nodes):
    v = _num_args(engine, nodes, 5)
    return _fv(v[0], v[1], v[2], _optional(v, 3), _optional(v, 4))


@register("PV", low=3, high=5)
def _pv_function(engine, nodes):
    v = _num_args(engine, nodes, 5)
    rate, periods, payment, future, when = v[0], v[1], v[2], _optional(v, 3), _optional(v, 4)
    if rate == 0:
        return -(payment * periods + future)
    factor = (1 + rate) ** periods
    return -(future + payment * (1 + rate * when) * (factor - 1) / rate) / factor


@register("IPMT", low=4, high=6)
def _ipmt_function(engine, nodes):
    v = _num_args(engine, nodes, 6)
    return _ipmt(v[0], v[1], v[2], v[3], _optional(v, 4), _optional(v, 5))


@register("PPMT", low=4, high=6)
def _ppmt_function(engine, nodes):
    v = _num_args(engine, nodes, 6)
    return _pmt(v[0], v[2], v[3], _optional(v, 4), _optional(v, 5)) - _ipmt(v[0], v[1], v[2], v[3], _optional(v, 4),
                                                                            _optional(v, 5))


@register("NPV", low=2)
def _npv(engine, nodes):
    rate = number(engine.scalar(engine.evaluate(nodes[0])))
    values = numbers_of([engine.evaluate(node) for node in nodes[1:]])
    if rate == -1:
        raise DIV0
    return math.fsum(value / (1 + rate) ** (index + 1) for index, value in enumerate(values))


@register("IRR", low=1, high=2)
def _irr_function(engine, nodes):
    values = numbers_of([engine.evaluate(nodes[0])])
    guess = number(engine.scalar(engine.evaluate(nodes[1]))) if len(nodes) > 1 else 0.1
    return _irr(values, guess)


@register("RATE", low=3, high=6)
def _rate_function(engine, nodes):
    v = _num_args(engine, nodes, 6)
    return _rate(v[0], v[1], v[2], _optional(v, 3), _optional(v, 4), _optional(v, 5, 0.1))


@register("NPER", low=3, high=5)
def _nper_function(engine, nodes):
    v = _num_args(engine, nodes, 5)
    return _nper(v[0], v[1], v[2], _optional(v, 3), _optional(v, 4))


@register("SLN", low=3, high=3)
def _sln(engine, nodes):
    cost, salvage, life = _num_args(engine, nodes, 3)
    if life == 0:
        raise DIV0
    return (cost - salvage) / life


# --- whole-workbook evaluation ------------------------------------------------------------


@dataclass
class Evaluation:
    values: dict          # (sheet name, row, column) -> computed value (float, str, bool, None or ExcelError)
    errors: list          # (sheet name, cell address, error code) for every formula that resulted in an error
    structural: list      # the subset that reflects a broken model: #REF! or #NAME?


def evaluate_workbook(workbook):
    """Evaluate every formula. Raises CircularReference or WorkTooLarge; otherwise returns an Evaluation."""
    from openpyxl.utils import get_column_letter
    values, errors, structural = {}, [], []
    for (key, row, column), cell in list(workbook.cells.items()):
        if isinstance(cell, tuple) and cell and cell[0] == "formula":
            try:
                result = workbook.value(key, row, column)
            except ExcelError as error:  # A parse failure.
                result = error
            values[(workbook.names[key], row, column)] = result
            if isinstance(result, ExcelError):
                where = (workbook.names[key], f"{get_column_letter(column)}{row}", result.code)
                errors.append(where)
                if result in STRUCTURAL:
                    structural.append(where)
    return Evaluation(values, errors, structural)
