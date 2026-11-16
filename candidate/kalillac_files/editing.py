"""Editing an uploaded Word, PowerPoint or Excel file into a new file. The original is never changed.

Not reachable from the application; like intake, call it through the worker (worker.run_outline,
worker.run_edit), never in a request handler.

    outline(data, declared_name)                 what can be edited, each piece with an address
    edit(data, declared_name, operations, ...)   a GeneratedFile: the edited copy

The upload first passes intake.inspect, so every hostile-file defence (type by content, archive
bombs, traversal, macros, encryption, XML entities) applies before an Office library opens it. The
edit is made on the customer's own file: its styles, layout, images, charts, comments and
everything an operation does not name are kept. A result that intake would not accept back is
refused rather than delivered.

Addresses (from outline(); they always refer to the uploaded file, so the operations of one edit
may be listed in any order without one shifting another's address):

    Word        p4 (the fourth paragraph of the body), t1 (the first table), t1.r2 (its second row),
                t1.r2.c3 (a cell)
    PowerPoint  s2 (slide 2), s2.sh3 (its third text or table shape), s2.sh3.p1 (a paragraph),
                s2.sh3.r1.c2 (a table cell), s2.notes (the speaker notes)
    Excel       Sheet!B5, 'Sheet name'!B5 (a cell); Sheet, Sheet!A1:C9 (scopes for replace_text)

Operations (at most MAX_OPERATIONS, applied in order):

    {"op": "replace_text", "find": str, "replace": str, "match_case"?: bool, "in"?: address,
     "expect"?: int}                                              Word, PowerPoint, Excel
    {"op": "set_text", "at": address, "text": str}               Word paragraph or cell; PowerPoint
                                                                  paragraph, shape, cell or notes
    {"op": "insert_paragraph", "after" | "before": "p4", "text": str, "style"?: str}     Word
    {"op": "delete", "at": "p4" | "t1.r3"}                       Word
    {"op": "add_row", "after": "t1.r3", "cells": [str, ...]}     Word
    {"op": "set_cell", "at": "Sheet!B5", "value": number | str | bool | null}           Excel
    {"op": "set_cell", "at": "Sheet!B5", "formula": "=..."}      Excel; allowlisted formulas only

replace_text keeps the formatting of the text it replaces (the run where the match starts) and
refuses when nothing matches, or when "expect" is given and the count differs, so a mistaken edit
is reported instead of silently doing nothing or too much.

Word edits may be tracked changes (track_changes=True): every insertion and deletion is marked as a
revision by "Kalillac AI", and the reader accepts or rejects each in Word.

Excel is edited in its XML, so charts, pivot tables, shapes and formatting that spreadsheet
libraries drop on saving all survive. Afterwards every formula's cached value is recomputed with
the formula engine when the workbook uses only allowlisted formulas; otherwise the cached values are
cleared, never left stale, and Excel recalculates on opening (it is told to in either case).
"""
from copy import deepcopy
from dataclasses import dataclass
import io
import math
import re
import zipfile

from .generation import MAX_FILE_BYTES, GeneratedFile, safe_filename
from .intake import IntakeError, display_name, inspect
from .package import fixed_dates
from .spec import SpecError

MAX_OPERATIONS = 200
MAX_FIND = 500
MAX_TEXT = 5_000
MAX_OUTLINE_UNITS = 20_000
MAX_OUTLINE_CHARS = 400_000
AUTHOR = "Kalillac AI"
OBJECT = "\ufffc"  # Stands for anything in a paragraph that is not text (a picture, a field); never matched.

MEDIA = {"docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
         "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
         "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
CHART = "{http://schemas.openxmlformats.org/drawingml/2006/chart}"
CHART_EX = "{http://schemas.microsoft.com/office/drawing/2014/chartex}"
# A chart reference this editor can recompute: one or more single-row or single-column areas on
# named sheets ("Summary!$B$2:$B$6", "('Q1 data'!$A$2:$A$4,'Q1 data'!$A$7)").
_CHART_AREA = re.compile(r"(?:'((?:[^']|'')+)'|([^'!:(),\s\[\]]+))!\$?([A-Za-z]{1,3})\$?([0-9]{1,7})"
                         r"(?::\$?([A-Za-z]{1,3})\$?([0-9]{1,7}))?")
MAX_CHART_POINTS = 100_000
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"


@dataclass(frozen=True)
class Unit:
    address: str
    kind: str          # paragraph, table, row, cell, slide, shape, notes, sheet
    text: str = ""
    detail: str = ""   # Word: the paragraph style; PowerPoint: the shape name; Excel: the formula


@dataclass(frozen=True)
class Outline:
    kind: str
    units: tuple
    truncated: bool


def _refuse(code):
    raise SpecError(code)


# --- checking the request -------------------------------------------------------------------


def _string(value, maximum, allow_empty=True):
    if type(value) is not str or len(value) > maximum or (not allow_empty and not value):
        _refuse("invalid_edit")
    if re.search("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ufffc]", value):
        _refuse("invalid_edit")
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _keys(operation, required, optional=()):
    if not set(required) <= set(operation) <= {"op", *required, *optional}:
        _refuse("invalid_edit")


def _check(operations, kind):
    if type(operations) is not list or not 1 <= len(operations) <= MAX_OPERATIONS:
        _refuse("invalid_edit")
    allowed = {"docx": {"replace_text", "set_text", "insert_paragraph", "delete", "add_row"},
               "pptx": {"replace_text", "set_text"},
               "xlsx": {"replace_text", "set_cell"}}[kind]
    checked = []
    for operation in operations:
        if type(operation) is not dict or operation.get("op") not in allowed:
            _refuse("invalid_edit")
        op = operation["op"]
        if op == "replace_text":
            _keys(operation, ("find", "replace"), ("match_case", "in", "expect"))
            find = _string(operation["find"], MAX_FIND, allow_empty=False)
            case = operation.get("match_case", True)
            expect = operation.get("expect")
            scope = operation.get("in")
            if type(case) is not bool or (expect is not None and (type(expect) is not int or not
                                                                  1 <= expect <= 10_000)):
                _refuse("invalid_edit")
            if scope is not None:
                _string(scope, 200, allow_empty=False)
            checked.append({"op": op, "find": find, "replace": _string(operation["replace"], MAX_TEXT),
                            "match_case": case, "in": scope, "expect": expect})
        elif op == "set_text":
            _keys(operation, ("at", "text"))
            checked.append({"op": op, "at": _string(operation["at"], 200, False),
                            "text": _string(operation["text"], MAX_TEXT)})
        elif op == "insert_paragraph":
            if ("after" in operation) == ("before" in operation):
                _refuse("invalid_edit")
            side = "after" if "after" in operation else "before"
            _keys(operation, (side, "text"), ("style",))
            style = operation.get("style")
            checked.append({"op": op, "side": side, "at": _string(operation[side], 200, False),
                            "text": _string(operation["text"], MAX_TEXT),
                            "style": None if style is None else _string(style, 100, False)})
        elif op == "delete":
            _keys(operation, ("at",))
            checked.append({"op": op, "at": _string(operation["at"], 200, False)})
        elif op == "add_row":
            _keys(operation, ("after", "cells"))
            cells = operation["cells"]
            if type(cells) is not list or not 1 <= len(cells) <= 64:
                _refuse("invalid_edit")
            checked.append({"op": op, "at": _string(operation["after"], 200, False),
                            "cells": [_string(cell, MAX_TEXT) for cell in cells]})
        else:  # set_cell
            if ("value" in operation) == ("formula" in operation):
                _refuse("invalid_edit")
            _keys(operation, ("at", "value" if "value" in operation else "formula"))
            entry = {"op": op, "at": _string(operation["at"], 200, False)}
            if "formula" in operation:
                from .formulas import is_safe_formula
                if not is_safe_formula(operation["formula"]):
                    _refuse("invalid_formula")
                entry["formula"] = operation["formula"]
            else:
                value = operation["value"]
                if type(value) is str:
                    value = _string(value, MAX_TEXT)
                elif type(value) in (int, float):
                    if not math.isfinite(value) or abs(value) > 1e300:
                        _refuse("invalid_edit")
                elif value is not None and type(value) is not bool:
                    _refuse("invalid_edit")
                entry["value"] = value
            checked.append(entry)
    return checked


def _pattern(find, match_case):
    return re.compile(re.escape(find), 0 if match_case else re.IGNORECASE)


# --- runs: text spread over formatted pieces -------------------------------------------------
#
# A paragraph's text is a sequence of runs, each with its own formatting. A match may start in one
# run and end in another. The runs are first split so that every match covers whole runs; the
# replacement then takes the formatting of the first run it replaces.


class _Runs:
    """The runs of one paragraph, for one format: their text, splitting, cloning and removal."""

    def __init__(self, runs, text_of, set_text, template_of):
        self.runs, self.text_of, self.set_text, self.template_of = runs, text_of, set_text, template_of

    def text(self):
        return "".join(self.text_of(run) for run in self.runs)

    def split_at(self, offset):
        """Make offset a boundary between runs. Only a text run is ever split."""
        position = 0
        for index, run in enumerate(self.runs):
            text = self.text_of(run)
            if position < offset < position + len(text):
                cut = offset - position
                second = deepcopy(run)
                self.set_text(run, text[:cut])
                self.set_text(second, text[cut:])
                run.addnext(second)
                self.runs.insert(index + 1, second)
                return
            position += len(text)

    def covering(self, start, end):
        self.split_at(end)
        self.split_at(start)
        covered, position = [], 0
        for run in self.runs:
            length = len(self.text_of(run))
            if start <= position and position + length <= end and length:
                covered.append(run)
            position += length
        return covered


def _replace_in(runs, pattern, replacement, tracked=None, limit=None):
    """Replace every match of pattern in the runs; returns the count. tracked: a Word revision maker."""
    matches = list(pattern.finditer(runs.text()))
    if limit is not None:
        matches = matches[:limit]
    for match in reversed(matches):  # From the end, so earlier offsets stay valid.
        covered = runs.covering(match.start(), match.end())
        if not covered:
            continue
        template = runs.template_of(covered)
        if tracked is not None:
            tracked.replace(covered, template, replacement)
        else:
            if replacement:
                new = deepcopy(template)
                runs.set_text(new, replacement)
                covered[0].addprevious(new)
            for run in covered:
                run.getparent().remove(run)
        runs.runs = [run for run in runs.runs if run.getparent() is not None]
    return len(matches)


# --- Word ------------------------------------------------------------------------------------


def _w_runs(paragraph):
    """A paragraph's runs in reading order, each holding exactly one piece of content.

    Runs inside a deletion, a moved-away range, a simple field or a text box (which has paragraphs of
    its own) are not this paragraph's editable text and are left out."""
    skipped = {W + "del", W + "moveFrom", W + "fldSimple", W + "txbxContent"}
    runs = []
    for run in paragraph.iter(W + "r"):
        node, inside = run.getparent(), False
        while node is not None and node is not paragraph:
            if node.tag in skipped or node.tag == W + "p":
                inside = True
                break
            node = node.getparent()
        if inside:
            continue
        runs.append(run)
        content = [child for child in run if child.tag != W + "rPr"]
        if len(content) > 1:  # One run per piece of content, each with the same formatting.
            properties, anchor = run.find(W + "rPr"), run
            for child in content[1:]:
                piece = run.makeelement(W + "r", {})
                if properties is not None:
                    piece.append(deepcopy(properties))
                piece.append(child)
                anchor.addnext(piece)
                anchor = piece
                runs.append(piece)
    return runs


def _w_text(run):
    content = [child for child in run if child.tag != W + "rPr"]
    if not content:
        return ""
    child = content[0]
    if child.tag in (W + "t", W + "delText"):
        return child.text or ""
    if child.tag == W + "tab":
        return "\t"
    if child.tag in (W + "br", W + "cr"):
        return "\n"
    return OBJECT


def _w_set(run, text):
    for child in [child for child in run if child.tag != W + "rPr"]:
        run.remove(child)
    pieces = text.split("\n")
    for index, piece in enumerate(pieces):
        if index:
            run.append(run.makeelement(W + "br", {}))
        if piece:
            node = run.makeelement(W + "t", {})
            node.text = piece
            node.set(XML_SPACE, "preserve")
            run.append(node)


def _w_template(covered):
    for run in covered:
        if _w_text(run) not in ("\t", "\n", OBJECT, ""):
            return run
    return covered[0]


def _word_runs(paragraph):
    return _Runs(_w_runs(paragraph), _w_text, _w_set, _w_template)


class _Revisions:
    """Word tracked changes: insertions and deletions marked as revisions by AUTHOR."""

    def __init__(self, root, date=None):
        values = (element.get(W + "id") for element in root.iter() if element.get(W + "id") is not None)
        self.next = max((int(value) for value in values if value.lstrip("-").isdigit()), default=0) + 1
        self.date = date

    def mark(self, tag, parent):
        attributes = {W + "id": str(self.next), W + "author": AUTHOR}
        if self.date:
            attributes[W + "date"] = self.date
        self.next += 1
        return parent.makeelement(W + tag, attributes)

    def delete_runs(self, runs):
        """Wrap runs, grouped by their parent, in deletion marks; their text becomes deleted text."""
        groups = []
        for run in runs:
            if groups and groups[-1][-1].getnext() is run:
                groups[-1].append(run)
            else:
                groups.append([run])
        marks = []
        for group in groups:
            mark = self.mark("del", group[0])
            group[0].addprevious(mark)
            for run in group:
                for node in run.iter(W + "t"):
                    node.tag = W + "delText"
                    node.set(XML_SPACE, "preserve")
                mark.append(run)
            marks.append(mark)
        return marks

    def insert_run(self, anchor, template, text, before=False):
        mark = self.mark("ins", anchor)
        run = deepcopy(template)
        for node in run.iter(W + "delText"):
            node.tag = W + "t"
        _w_set(run, text)
        mark.append(run)
        (anchor.addprevious if before else anchor.addnext)(mark)
        return mark

    def replace(self, covered, template, replacement):
        marks = self.delete_runs(covered)
        if replacement:
            self.insert_run(marks[0], template, replacement)

    def paragraph_mark(self, paragraph, tag):
        """Mark the paragraph's own end mark inserted or deleted (it is what joins it to the next)."""
        properties = paragraph.find(W + "pPr")
        if properties is None:
            properties = paragraph.makeelement(W + "pPr", {})
            paragraph.insert(0, properties)
        mark_properties = properties.find(W + "rPr")
        if mark_properties is None:
            mark_properties = properties.makeelement(W + "rPr", {})
            tail = [child for child in properties if child.tag in (W + "sectPr", W + "pPrChange")]
            (tail[0].addprevious if tail else properties.append)(mark_properties)
        mark_properties.insert(0, self.mark(tag, mark_properties))  # Revision marks come first here.

    def row_mark(self, row, tag):
        properties = row.find(W + "trPr")
        if properties is None:
            properties = row.makeelement(W + "trPr", {})
            exceptions = row.find(W + "tblPrEx")
            if exceptions is not None:
                exceptions.addnext(properties)
            else:
                row.insert(0, properties)
        change = properties.find(W + "trPrChange")
        mark = self.mark(tag, properties)
        (change.addprevious if change is not None else properties.append)(mark)


def _word_blocks(body):
    """Body paragraphs and tables in reading order, looking inside block-level content controls."""
    for child in body:
        if child.tag in (W + "p", W + "tbl"):
            yield child
        elif child.tag == W + "sdt":
            content = child.find(W + "sdtContent")
            if content is not None:
                yield from _word_blocks(content)


def _word_map(root):
    """Every address of a Word body: paragraphs p1.., tables t1.., rows t1.r1.., cells t1.r1.c1.."""
    body = root.find(W + "body")
    addresses, paragraphs, tables = {}, 0, 0
    for block in _word_blocks(body):
        if block.tag == W + "p":
            paragraphs += 1
            addresses[f"p{paragraphs}"] = ("paragraph", block)
        else:
            tables += 1
            addresses[f"t{tables}"] = ("table", block)
            for r, row in enumerate(block.findall(W + "tr"), start=1):
                addresses[f"t{tables}.r{r}"] = ("row", row)
                for c, cell in enumerate(row.findall(W + "tc"), start=1):
                    addresses[f"t{tables}.r{r}.c{c}"] = ("cell", cell)
    return addresses


def _paragraph_text(paragraph):
    return "".join(_w_text(run) for run in _w_runs(deepcopy(paragraph))).replace(OBJECT, "")


def _cell_text(cell):
    return "\n".join(_paragraph_text(paragraph) for paragraph in cell.findall(W + "p"))


def _style_names(styles_root):
    names = {}
    if styles_root is None:
        return names
    for style in styles_root.iter(W + "style"):
        name = style.find(W + "name")
        if style.get(W + "type") == "paragraph" and name is not None:  # By name or id, in any case.
            names[(name.get(W + "val") or "").lower()] = style.get(W + "styleId")
            names[(style.get(W + "styleId") or "").lower()] = style.get(W + "styleId")
    return names


def _set_paragraph_text(paragraph, text, revisions):
    """Replace a paragraph's text, keeping its paragraph formatting and its first run's formatting.
    Pictures and fields in it are kept."""
    runs = _word_runs(paragraph)
    textual = [run for run in runs.runs if _w_text(run) not in (OBJECT,)]
    template = _w_template(textual) if textual else paragraph.makeelement(W + "r", {})
    if revisions is not None:
        marks = revisions.delete_runs(textual) if textual else []
        if text:
            if marks:
                revisions.insert_run(marks[-1], template, text)
            else:
                mark = revisions.mark("ins", paragraph)
                run = deepcopy(template)
                _w_set(run, text)
                mark.append(run)
                paragraph.append(mark)
        return
    for run in textual[1:]:
        run.getparent().remove(run)
    if textual:
        _w_set(textual[0], text)
    elif text:
        run = deepcopy(template)
        _w_set(run, text)
        paragraph.append(run)


def _set_cell_text(cell, text, revisions):
    paragraphs = cell.findall(W + "p")
    _set_paragraph_text(paragraphs[0], text, revisions)
    for extra in paragraphs[1:]:
        if revisions is not None:
            _set_paragraph_text(extra, "", revisions)
            revisions.paragraph_mark(extra, "del")
        else:
            cell.remove(extra)


def _edit_docx(data, operations, track_changes, revision_date):
    import docx
    document = docx.Document(io.BytesIO(data))
    root = document.element
    addresses = _word_map(root)
    styles = _style_names(document.styles.element)
    revisions = _Revisions(root, revision_date) if track_changes else None
    removed = set()

    def resolve(address, kinds):
        found = addresses.get(address)
        if found is None:
            _refuse("edit_address_not_found")
        if found[0] not in kinds:
            _refuse("edit_address_wrong_kind")
        if id(found[1]) in removed:
            _refuse("edit_conflict")
        return found

    for operation in operations:
        op = operation["op"]
        if op == "replace_text":
            if operation["in"] is None:
                scope = [element for kind, element in addresses.values() if kind == "paragraph"] + \
                    [p for kind, element in addresses.values() if kind == "table" for p in element.iter(W + "p")]
            else:
                kind, element = resolve(operation["in"], ("paragraph", "table", "row", "cell"))
                scope = [element] if kind == "paragraph" else list(element.iter(W + "p"))
            pattern, count = _pattern(operation["find"], operation["match_case"]), 0
            for paragraph in scope:
                # Only a paragraph that matches is touched; its runs are split only then.
                if id(paragraph) in removed or not pattern.search(_paragraph_text(paragraph)):
                    continue
                count += _replace_in(_word_runs(paragraph), pattern, operation["replace"], revisions)
            _expect(count, operation["expect"])
        elif op == "set_text":
            kind, element = resolve(operation["at"], ("paragraph", "cell"))
            (_set_paragraph_text if kind == "paragraph" else _set_cell_text)(element, operation["text"], revisions)
        elif op == "insert_paragraph":
            _kind, anchor = resolve(operation["at"], ("paragraph",))
            paragraph = anchor.makeelement(W + "p", {})
            properties = anchor.find(W + "pPr")
            if operation["style"] is not None:
                style = styles.get(operation["style"].lower())
                if style is None:
                    _refuse("edit_unknown_style")
                properties = paragraph.makeelement(W + "pPr", {})
                properties.append(properties.makeelement(W + "pStyle", {W + "val": style}))
                paragraph.append(properties)
            elif properties is not None:
                properties = deepcopy(properties)
                for name in ("sectPr", "pPrChange", "rPr"):  # Not a section break, not a revision.
                    for found in properties.findall(W + name):
                        properties.remove(found)
                paragraph.append(properties)
            runs = _word_runs(anchor)
            textual = [run for run in runs.runs if _w_text(run) not in (OBJECT, "\t", "\n", "")]
            template = deepcopy(textual[0]) if textual else paragraph.makeelement(W + "r", {})
            for node in template.iter(W + "delText"):
                node.tag = W + "t"
            if operation["text"]:
                _w_set(template, operation["text"])
                if revisions is not None:
                    mark = revisions.mark("ins", paragraph)
                    mark.append(template)
                    paragraph.append(mark)
                else:
                    paragraph.append(template)
            (anchor.addnext if operation["side"] == "after" else anchor.addprevious)(paragraph)
            if revisions is not None:
                revisions.paragraph_mark(paragraph, "ins")
        elif op == "delete":
            kind, element = resolve(operation["at"], ("paragraph", "row"))
            if kind == "paragraph":
                properties = element.find(W + "pPr")
                if properties is not None and properties.find(W + "sectPr") is not None:
                    _refuse("edit_not_allowed")  # A section break: deleting it would merge sections.
                if revisions is not None:
                    _set_paragraph_text(element, "", revisions)
                    revisions.paragraph_mark(element, "del")
                else:
                    element.getparent().remove(element)
                    removed.add(id(element))
            else:
                table = element.getparent()
                if len(table.findall(W + "tr")) < 2:
                    _refuse("edit_not_allowed")  # A table keeps at least one row.
                if revisions is not None:
                    for cell in element.findall(W + "tc"):
                        for paragraph in cell.findall(W + "p"):
                            _set_paragraph_text(paragraph, "", revisions)
                    revisions.row_mark(element, "del")
                else:
                    table.remove(element)
                    removed.add(id(element))
        else:  # add_row
            _kind, anchor = resolve(operation["at"], ("row",))
            row = deepcopy(anchor)
            cells = row.findall(W + "tc")
            if len(operation["cells"]) != len(cells):
                _refuse("edit_row_width")
            for properties in row.findall(W + "trPr"):
                for mark in properties.findall(W + "ins") + properties.findall(W + "del"):
                    properties.remove(mark)
            for cell, text in zip(cells, operation["cells"]):
                for extra in cell.findall(W + "p")[1:]:
                    cell.remove(extra)
                paragraph = cell.find(W + "p")
                _set_paragraph_text(paragraph, "", None)
                if text:
                    _set_paragraph_text(paragraph, text, None)
                    if revisions is not None:
                        for run in [run for run in paragraph if run.tag == W + "r"]:
                            mark = revisions.mark("ins", paragraph)
                            run.addprevious(mark)
                            mark.append(run)
            anchor.addnext(row)
            if revisions is not None:
                revisions.row_mark(row, "ins")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _expect(count, expect):
    if count == 0:
        _refuse("edit_no_match")
    if expect is not None and count != expect:
        _refuse("edit_match_count")


# --- PowerPoint ------------------------------------------------------------------------------


def _a_runs(paragraph):
    return [child for child in paragraph if child.tag in (A + "r", A + "br", A + "fld")]


def _a_text(run):
    if run.tag == A + "br":
        return "\n"
    if run.tag == A + "fld":
        return OBJECT
    node = run.find(A + "t")
    return "" if node is None or node.text is None else node.text


def _a_set(run, text):
    node = run.find(A + "t")
    if node is None:
        node = run.makeelement(A + "t", {})
        run.append(node)
    node.text = text


def _a_template(covered):
    for run in covered:
        if run.tag == A + "r":
            return run
    template = covered[0].makeelement(A + "r", {})
    template.append(template.makeelement(A + "t", {}))
    return template


def _deck_runs(paragraph):
    return _Runs(_a_runs(paragraph), _a_text, _a_set, _a_template)


def _set_frame_text(body, text, paragraph=None):
    """Set a text body's text (or one paragraph's), keeping the first paragraph's and run's formatting."""
    paragraphs = body.findall(A + "p")
    targets = [paragraph] if paragraph is not None else paragraphs
    first = targets[0]
    lines = text.split("\n") if paragraph is None else [text.replace("\n", " ")]
    template_run = next((run for run in first.findall(A + "r")), None)
    for target in targets:
        for run in _a_runs(target):
            target.remove(run)
    if paragraph is None:
        for extra in paragraphs[1:]:
            body.remove(extra)
    anchor = first
    for index, line in enumerate(lines):
        target = first if index == 0 else deepcopy(first)
        if index:
            for run in _a_runs(target):
                target.remove(run)
            anchor.addnext(target)
            anchor = target
        if line:
            run = deepcopy(template_run) if template_run is not None else target.makeelement(A + "r", {})
            _a_set(run, line)
            end = target.find(A + "endParaRPr")
            (end.addprevious if end is not None else target.append)(run)


def _deck_map(presentation):
    """Every address of a deck: slides, their text and table shapes, paragraphs, cells and notes."""
    addresses = {}
    for s, slide in enumerate(presentation.slides, start=1):
        addresses[f"s{s}"] = ("slide", slide)
        for index, (kind, shape) in enumerate(_shapes_of(slide), start=1):
            base = f"s{s}.sh{index}"
            if kind == "table":
                addresses[base] = ("table", shape)
                for r, row in enumerate(shape.table.rows, start=1):
                    for c, cell in enumerate(row.cells, start=1):
                        addresses[f"{base}.r{r}.c{c}"] = ("cell", cell)
            else:
                addresses[base] = ("shape", shape)
                for p, paragraph in enumerate(shape.text_frame._txBody.findall(A + "p"), start=1):
                    addresses[f"{base}.p{p}"] = ("paragraph", paragraph)
        if slide.has_notes_slide:
            addresses[f"s{s}.notes"] = ("notes", slide.notes_slide)
    return addresses


def _deck_paragraphs(kind, element):
    if kind == "slide":
        for shape_kind, shape in _shapes_of(element):
            yield from _deck_paragraphs(shape_kind, shape)
    elif kind == "shape":
        yield from element.text_frame._txBody.findall(A + "p")
    elif kind == "table":
        for row in element.table.rows:
            for cell in row.cells:
                yield from cell._tc.iter(A + "p")
    elif kind == "cell":
        yield from element._tc.iter(A + "p")
    elif kind == "paragraph":
        yield element
    elif kind == "notes":
        frame = element.notes_text_frame
        if frame is not None:
            yield from frame._txBody.findall(A + "p")


def _shapes_of(slide):
    """Text and table shapes in drawing order, looking inside groups."""
    found = []

    def walk(collection):
        for shape in collection:
            if shape.__class__.__name__ == "GroupShape":
                walk(shape.shapes)
            elif getattr(shape, "has_table", False) and shape.has_table:
                found.append(("table", shape))
            elif shape.has_text_frame:
                found.append(("shape", shape))
    walk(slide.shapes)
    return found


EMU = 914400                                   # EMUs per inch.
DEFAULT_SIZE = 18.0                            # PowerPoint's body text size when nothing sets one.
FIT_TOLERANCE = 0.02                           # Inches: measurement noise, never a visible overflow.


def _inches(properties, name, default, signed=False):
    """An EMU attribute in inches, or default when the element or attribute is absent or malformed."""
    value = (properties.get(name) or "") if properties is not None else ""
    return int(value) / EMU if (value.lstrip("-") if signed else value).isdigit() else default


def _body_text(body):
    return "\n".join("".join(node.text or "" for node in paragraph.iter(A + "t"))
                     for paragraph in body.findall(A + "p"))


def _text_containers(presentation):
    """(key, kind, element, slide) for every shape text body and table cell, keyed stably."""
    found = []
    for s, slide in enumerate(presentation.slides, start=1):
        for index, (kind, shape) in enumerate(_shapes_of(slide), start=1):
            if kind == "table":
                for r, row in enumerate(shape.table.rows, start=1):
                    for c, cell in enumerate(row.cells, start=1):
                        found.append(((s, index, r, c), "cell", (shape, r - 1, c - 1, cell), slide))
            else:
                found.append(((s, index), "shape", shape, slide))
    return found


def _paragraph_height(paragraph, inner_width, scale, list_style_size):
    """Inches one paragraph needs at its own run sizes, wrapped as PowerPoint wraps it."""
    from .pptx_design import LINE, wrap
    sizes = [int(node.get("sz")) / 100 for node in (*paragraph.iter(A + "rPr"), *paragraph.iter(A + "endParaRPr"))
             if node.get("sz", "").isdigit()]
    size = (max(sizes) if sizes else list_style_size or DEFAULT_SIZE) * scale
    bold = any(node.get("b") in ("1", "true") for node in paragraph.iter(A + "rPr"))
    spacing, before, after = 1.0, 0.0, 0.0
    properties = paragraph.find(A + "pPr")
    if properties is not None:
        percent = properties.find(A + "lnSpc/" + A + "spcPct")
        spacing = int(percent.get("val")) / 100000 if percent is not None and percent.get("val", "").isdigit() else 1.0
        for tag in ("spcBef", "spcAft"):
            points = properties.find(A + tag + "/" + A + "spcPts")
            if points is not None and points.get("val", "").isdigit():
                if tag == "spcBef":
                    before = int(points.get("val")) / 100 / 72
                else:
                    after = int(points.get("val")) / 100 / 72
    text = "".join(node.text if node.tag == A + "t" else "\n" for node in paragraph.iter(A + "t", A + "br"))
    lines = sum(max(1, len(wrap(segment, inner_width, size, bold))) for segment in text.split("\n"))
    return before + lines * size * LINE * spacing / 72 + after


def _text_height(body, width_in):
    """(inches the text needs, inner width, vertical insets, autofit kind) for a shape's text body."""
    properties = body.find(A + "bodyPr")

    def get(name, default):
        return _inches(properties, name, default, signed=True)
    inner_width = width_in - get("lIns", 0.1) - get("rIns", 0.1)
    insets = get("tIns", 0.05) + get("bIns", 0.05)
    scale, autofit = 1.0, None
    if properties is not None:
        normal = properties.find(A + "normAutofit")
        if normal is not None:
            autofit = "shrink"
            if (normal.get("fontScale") or "").isdigit():
                scale = int(normal.get("fontScale")) / 100000
        elif properties.find(A + "spAutoFit") is not None:
            autofit = "grow"
    level = body.find(A + "lstStyle/" + A + "lvl1pPr/" + A + "defRPr")
    list_size = int(level.get("sz")) / 100 if level is not None and level.get("sz", "").isdigit() else None
    if not _body_text(body).strip():
        return 0.0, inner_width, insets, autofit       # No text (an accent bar, a frame): nothing to fit.
    if inner_width <= 0:
        return None
    needed = sum(_paragraph_height(p, inner_width, scale, list_size) for p in body.findall(A + "p"))
    return needed, inner_width, insets, autofit


def _cell_needed(cell, body, inner_width_in=None):
    """Inches a table cell's text needs, margins included, or None when its width is unknown."""
    properties = cell.find(A + "tcPr")

    def margin(name, default):
        return _inches(properties, name, default)
    inner = inner_width_in - margin("marL", 0.1) - margin("marR", 0.1)
    if inner <= 0:
        return None
    if not _body_text(body).strip():
        return 0.0
    level = body.find(A + "lstStyle/" + A + "lvl1pPr/" + A + "defRPr")
    list_size = int(level.get("sz")) / 100 if level is not None and level.get("sz", "").isdigit() else None
    return sum(_paragraph_height(p, inner, 1.0, list_size) for p in body.findall(A + "p")) \
        + margin("marT", 0.05) + margin("marB", 0.05)


def _check_deck_fit(presentation, before):
    """Every shape and table cell whose text an edit changed must still fit, measured the way generation
    measures (Codex document audit, finding 6), so an edit never returns a valid package whose text
    runs out of its shape or pushes a table off the slide. An edit may always need as much room as the
    original text did (a deliberate oversized glyph, a customer's own tight box): only growth past
    both the box and the original is refused."""
    slide_height = presentation.slide_height / EMU
    changed_tables = {}                               # (slide, shape) -> table shape with an edited cell
    for key, kind, element, _slide in _text_containers(presentation):
        original_text, original_body = before.get(key, (None, None))
        if kind == "shape":
            body = element.text_frame._txBody
            if original_text == _body_text(body):
                continue
            if element.width is None or element.height is None or element.top is None:
                _refuse("slide_text_too_long")          # No geometry to measure against: refuse, never guess.
            measured = _text_height(body, element.width / EMU)
            if measured is None:
                _refuse("slide_text_too_long")
            needed, _width, insets, autofit = measured
            if needed == 0:
                continue                                 # Emptied: nothing can overflow.
            room = element.height / EMU - insets
            if autofit == "grow":
                room = slide_height - element.top / EMU - insets   # The shape grows, but not off the slide.
            was = _text_height(original_body, element.width / EMU) if original_body is not None else None
            if needed > room + FIT_TOLERANCE and (was is None or needed > was[0] + FIT_TOLERANCE):
                _refuse("slide_text_too_long")
        else:
            graphic, _row_index, _column_index, cell = element
            body = cell._tc.find(A + "txBody")
            if body is None or original_text == _body_text(body):
                continue
            changed_tables[key[:2]] = graphic
    for key, graphic in changed_tables.items():
        _check_table_fit(graphic, key, before, slide_height)


def _check_table_fit(graphic, key, before, slide_height):
    """The whole final table must end on the slide (Codex reverification, finding B): every row grows to
    its tallest cell, a cell spanning several rows needs their sum, and the table's bottom is the sum of
    all rows, not of one edited row. Measured the same way for the original text, so an edit is refused
    only when it pushes the table further off the slide than it already was."""
    table = graphic.table
    rows, columns = len(table.rows), len(table.columns)
    new_heights = [row.height / EMU for row in table.rows]
    old_heights = list(new_heights)
    spanning = []
    for r, row in enumerate(table.rows):
        for c, cell in enumerate(row.cells):
            tc = cell._tc
            if tc.get("hMerge") in ("1", "true") or tc.get("vMerge") in ("1", "true"):
                continue                                  # Covered by the cell that spans it.
            body = tc.find(A + "txBody")
            if body is None:
                continue
            span = int(tc.get("gridSpan") or 1)
            row_span = min(int(tc.get("rowSpan") or 1), rows - r)
            width = sum(table.columns[i].width for i in range(c, min(c + span, columns))) / EMU
            needed = _cell_needed(tc, body, width)
            if needed is None:
                _refuse("slide_text_too_long")
            original = before.get((*key, r + 1, c + 1), (None, None))[1]
            was = _cell_needed(tc, original, width) if original is not None else needed
            if was is None:
                was = needed
            if row_span == 1:
                new_heights[r] = max(new_heights[r], needed)
                old_heights[r] = max(old_heights[r], was)
            else:
                spanning.append((r, row_span, needed, was))
    for r, row_span, needed, was in spanning:
        for heights, value in ((new_heights, needed), (old_heights, was)):
            short = value - sum(heights[r:r + row_span])
            if short > 0:
                heights[r + row_span - 1] += short
    if graphic.top is None:
        _refuse("slide_text_too_long")
    top = graphic.top / EMU
    bottom, previous = top + sum(new_heights), top + sum(old_heights)
    if bottom > slide_height + FIT_TOLERANCE and bottom > previous + FIT_TOLERANCE:
        _refuse("slide_text_too_long")


def _edit_pptx(data, operations):
    import pptx
    presentation = pptx.Presentation(io.BytesIO(data))
    addresses = _deck_map(presentation)
    before = {}                                   # key -> (text, a copy of the text body) before any edit
    for key, kind, element, _slide in _text_containers(presentation):
        body = element.text_frame._txBody if kind == "shape" else element[3]._tc.find(A + "txBody")
        if body is not None:
            before[key] = (_body_text(body), deepcopy(body))
    for operation in operations:
        if operation["op"] == "replace_text":
            if operation["in"] is None:
                scopes = [(kind, element) for kind, element in addresses.values() if kind in ("slide", "notes")]
            else:
                found = addresses.get(operation["in"])
                if found is None:
                    _refuse("edit_address_not_found")
                scopes = [found]
            pattern, count = _pattern(operation["find"], operation["match_case"]), 0
            for kind, element in scopes:
                for paragraph in _deck_paragraphs(kind, element):
                    count += _replace_in(_deck_runs(paragraph), pattern, operation["replace"])
            _expect(count, operation["expect"])
        else:  # set_text
            found = addresses.get(operation["at"])
            if found is None:
                _refuse("edit_address_not_found")
            kind, element = found
            if kind == "shape":
                _set_frame_text(element.text_frame._txBody, operation["text"])
            elif kind == "paragraph":
                _set_frame_text(element.getparent(), operation["text"], paragraph=element)
            elif kind == "cell":
                _set_frame_text(element._tc.find(A + "txBody"), operation["text"])
            elif kind == "notes":
                element.notes_text_frame.text = operation["text"]
            else:
                _refuse("edit_address_wrong_kind")
    _check_deck_fit(presentation, before)
    buffer = io.BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


# --- Excel -----------------------------------------------------------------------------------

_CELL = re.compile(r"\$?([A-Za-z]{1,3})\$?([0-9]{1,7})")
_FUTURE = re.compile(r"(?<![A-Za-z0-9_.])(IFS|IFNA|XOR|CONCAT|DAYS|XLOOKUP)\(", re.IGNORECASE)
_STRING_LITERAL = re.compile(r'"(?:[^"]|"")*"')


def _column_number(letters):
    number = 0
    for letter in letters.upper():
        number = number * 26 + ord(letter) - 64
    return number


def _column_letters(number):
    letters = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _split_address(address):
    """('Sheet', 'B5') from Sheet!B5 or 'Sheet name'!B5; ('Sheet', None) from a sheet name alone."""
    if address.startswith("'"):
        end = address.find("'!")
        if end < 0:
            return address.strip("'").replace("''", "'"), None
        return address[1:end].replace("''", "'"), address[end + 2:]
    if "!" in address:
        sheet, _, cell = address.partition("!")
        return sheet, cell
    return address, None


def _parse_xml(data):
    from lxml import etree
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)
    return etree.fromstring(data, parser)


def _serialize(root):
    from lxml import etree
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _plain_text(container):
    """The text of a shared or inline string (plain or rich), without phonetic guides."""
    return "".join(node.text or "" for node in container.iter(S + "t") if node.getparent().tag != S + "rPh")


class _Book:
    """An Excel package opened for editing at the XML level."""

    def __init__(self, data):
        self.archive = zipfile.ZipFile(io.BytesIO(data))
        self.names = self.archive.namelist()
        self.parts = {}                        # part name -> parsed root
        self.dirty = set()                     # the parts an edit changed: only these are written anew
        self.workbook = self.root("xl/workbook.xml")
        rels = self.relations("xl/workbook.xml")
        self.sheets = {}                       # lower-case name -> (name as written, part name)
        for sheet in self.workbook.iter(S + "sheet"):
            target = rels.get(sheet.get(R + "id"))
            if target in self.names:
                self.sheets[sheet.get("name").lower()] = (sheet.get("name"), target)
        shared = "xl/sharedStrings.xml"
        self.shared = list(self.root(shared).iter(S + "si")) if shared in self.names else []
        self.cells = {}                        # part -> {(row, column): c element}
        self.removed = set()

    def root(self, part):
        if part not in self.parts:
            self.parts[part] = _parse_xml(self.archive.read(part))
        return self.parts[part]

    def relations(self, part):
        folder, name = part.rsplit("/", 1)
        rels_part = f"{folder}/_rels/{name}.rels"
        if rels_part not in self.names:
            return {}
        targets = {}
        for rel in self.root(rels_part).iter(REL + "Relationship"):
            if rel.get("TargetMode") == "External":
                continue
            target = rel.get("Target") or ""
            pieces = [] if target.startswith("/") else folder.split("/")
            for piece in target.lstrip("/").split("/"):
                if piece == "..":
                    pieces = pieces[:-1]
                elif piece not in ("", "."):
                    pieces.append(piece)
            targets[rel.get("Id")] = "/".join(pieces)
        return targets

    def sheet(self, name):
        found = self.sheets.get(name.lower())
        if found is None:
            _refuse("edit_address_not_found")
        return found

    def index(self, part):
        """Cell elements by position, with the implied positions of rows and cells that omit them."""
        if part in self.cells:
            return self.cells[part]
        root, cells, row_number = self.root(part), {}, 0
        data = root.find(S + "sheetData")
        for row in data.findall(S + "row") if data is not None else ():
            row_number = int(row.get("r")) if row.get("r") else row_number + 1
            row.set("r", str(row_number))
            column = 0
            for cell in row.findall(S + "c"):
                match = _CELL.fullmatch(cell.get("r") or "")
                column = _column_number(match.group(1)) if match else column + 1
                cell.set("r", f"{_column_letters(column)}{row_number}")
                cells[(row_number, column)] = cell
        self.expand_shared_formulas(part, cells)
        self.cells[part] = cells
        return cells

    def expand_shared_formulas(self, part, cells):
        """Write every shared formula out in full, so changing one cell cannot break the others."""
        from openpyxl.formula.translate import Translator
        masters = {}
        for (row, column), cell in cells.items():
            formula = cell.find(S + "f")
            if formula is not None and formula.get("t") == "shared" and formula.get("ref") and formula.text:
                masters[formula.get("si")] = ("=" + formula.text, f"{_column_letters(column)}{row}")
        for (row, column), cell in cells.items():
            formula = cell.find(S + "f")
            if formula is None or formula.get("t") != "shared":
                continue
            master = masters.get(formula.get("si"))
            if master is None:
                continue
            here = f"{_column_letters(column)}{row}"
            text = master[0] if here == master[1] else Translator(master[0], origin=master[1]).translate_formula(here)
            for attribute in ("t", "si", "ref"):
                formula.attrib.pop(attribute, None)
            formula.text = text[1:]

    def cell_text(self, cell):
        kind = cell.get("t")
        if kind == "s":
            value = cell.find(S + "v")
            try:
                return _plain_text(self.shared[int(value.text)])
            except (TypeError, ValueError, IndexError, AttributeError):
                return None
        if kind == "inlineStr":
            inline = cell.find(S + "is")
            return None if inline is None else _plain_text(inline)
        return None

    def guarded(self, part, row, column):
        """Refuse a cell that cannot be changed on its own: inside an array formula, or a merged cell
        other than its first."""
        root = self.root(part)
        for formula in root.iter(S + "f"):
            if formula.get("t") == "array" and formula.get("ref") and _inside(formula.get("ref"), row, column):
                _refuse("edit_not_allowed")
        for merge in root.iter(S + "mergeCell"):
            reference = merge.get("ref") or ""
            if _inside(reference, row, column):
                first = _CELL.match(reference)
                if (int(first.group(2)), _column_number(first.group(1))) != (row, column):
                    _refuse("edit_not_allowed")

    def cell(self, part, row, column, create=True):
        cells = self.index(part)
        if (row, column) in cells or not create:
            return cells.get((row, column))
        root = self.root(part)
        data = root.find(S + "sheetData")
        row_element = next((element for element in data.findall(S + "row") if int(element.get("r")) == row), None)
        if row_element is None:
            row_element = data.makeelement(S + "row", {"r": str(row)})
            later = next((element for element in data.findall(S + "row") if int(element.get("r")) > row), None)
            (later.addprevious if later is not None else data.append)(row_element)
        cell = row_element.makeelement(S + "c", {"r": f"{_column_letters(column)}{row}"})
        later = next((element for element in row_element.findall(S + "c")
                      if _column_number(_CELL.fullmatch(element.get("r")).group(1)) > column), None)
        (later.addprevious if later is not None else row_element.append)(cell)
        cells[(row, column)] = cell
        self.widen_dimension(part, row, column)
        return cell

    def widen_dimension(self, part, row, column):
        dimension = self.root(part).find(S + "dimension")
        if dimension is None:
            return
        reference = dimension.get("ref") or "A1"
        corners = [_CELL.fullmatch(piece) for piece in reference.split(":")]
        if not all(corners):
            return
        top = min(int(corners[0].group(2)), row)
        left = min(_column_number(corners[0].group(1)), column)
        bottom = max(int(corners[-1].group(2)), row)
        right = max(_column_number(corners[-1].group(1)), column)
        dimension.set("ref", f"{_column_letters(left)}{top}:{_column_letters(right)}{bottom}")

    def header_names(self, part, row, column, text):
        """A cell in the header row of an Excel table names a table column; keep the two identical,
        or Excel repairs the file."""
        rels = self.relations(part)
        sheet = self.root(part)
        for table_part_element in sheet.iter(S + "tablePart"):
            table_part = rels.get(table_part_element.get(R + "id"))
            if table_part not in self.names:
                continue
            table = self.root(table_part)
            reference = table.get("ref") or ""
            first = _CELL.match(reference)
            if not first or not _inside(reference, row, column) or int(first.group(2)) != row \
                    or table.get("headerRowCount") == "0":
                continue
            if text is None or not text.strip():
                _refuse("edit_not_allowed")  # A table column keeps a name, and a name is text.
            index = column - _column_number(first.group(1))
            columns = table.find(S + "tableColumns")
            names = [] if columns is None else columns.findall(S + "tableColumn")
            if 0 <= index < len(names):
                if any(other.get("name") == text for i, other in enumerate(names) if i != index):
                    _refuse("edit_not_allowed")  # Table column names are unique.
                if names[index].get("name") != text:
                    names[index].set("name", text)
                    self.dirty.add(table_part)

    def write_value(self, cell, value):
        for child in [child for child in cell if child.tag in (S + "f", S + "v", S + "is")]:
            cell.remove(child)
        cell.attrib.pop("t", None)
        if value is None:
            return
        if type(value) is bool:
            cell.set("t", "b")
            node = cell.makeelement(S + "v", {})
            node.text = "1" if value else "0"
            cell.append(node)
        elif type(value) in (int, float):
            node = cell.makeelement(S + "v", {})
            node.text = _number_text(value)
            cell.append(node)
        else:
            self.write_inline(cell, value)

    def write_inline(self, cell, text, rich=None):
        cell.set("t", "inlineStr")
        inline = rich if rich is not None else cell.makeelement(S + "is", {})
        if rich is None:
            node = inline.makeelement(S + "t", {})
            node.text = text
            if text != text.strip():
                node.set(XML_SPACE, "preserve")
            inline.append(node)
        cell.append(inline)

    def save(self, changed_formula_cells):
        self.recalculate(changed_formula_cells)
        self.refresh_charts()
        calculation = self.workbook.find(S + "calcPr")
        if calculation is None:
            calculation = self.workbook.makeelement(S + "calcPr", {})
            anchors = [child for child in self.workbook if child.tag in (
                S + "sheets", S + "functionGroups", S + "externalReferences", S + "definedNames")]
            anchors[-1].addnext(calculation)
        calculation.set("fullCalcOnLoad", "1")  # Excel recalculates everything on opening.
        self.dirty.add("xl/workbook.xml")
        # The calculation chain lists formula cells in Excel's order; a stale one makes Excel repair
        # the file. Excel rebuilds it.
        dropped = {"xl/calcChain.xml"}
        rels = "xl/_rels/workbook.xml.rels"
        if rels in self.names:
            for rel in list(self.root(rels).iter(REL + "Relationship")):
                if (rel.get("Type") or "").endswith("/calcChain"):
                    rel.getparent().remove(rel)
                    self.dirty.add(rels)
        types = self.root("[Content_Types].xml")
        for override in list(types):
            if override.get("PartName") == "/xl/calcChain.xml":
                types.remove(override)
                self.dirty.add("[Content_Types].xml")
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
            for name in self.names:
                if name in dropped:
                    continue
                target.writestr(name, _serialize(self.parts[name]) if name in self.dirty else self.archive.read(name))
        return output.getvalue()

    def recalculate(self, changed_formula_cells):
        """Fresh cached values for every formula, or none at all: never a stale number."""
        from . import formula_engine as fe
        from .formulas import is_safe_formula
        # The workbook's own date system: a 1904 workbook's serials count from 1 January 1904.
        properties = self.workbook.find(S + "workbookPr")
        date1904 = properties is not None and (properties.get("date1904") or "").lower() in ("1", "true")
        engine = fe.Workbook(date1904=date1904)
        self.engine, self.formula_keys = engine, set()
        formula_cells, computable = [], True
        for _key, (name, part) in self.sheets.items():
            engine.add_sheet(name)
            for (row, column), cell in self.index(part).items():
                formula = cell.find(S + "f")
                if formula is not None:
                    text = re.sub(r"_xl(fn|ws)\.", "", "=" + (formula.text or ""))
                    if formula.get("t") in ("array", "dataTable") or not is_safe_formula(text):
                        computable = False
                    engine.set_formula(name, row, column, text)
                    self.formula_keys.add((name.lower(), row, column))
                    formula_cells.append((name, part, row, column, cell))
                    continue
                engine.set(name, row, column, self.constant(cell, fe))
        values = {}
        if computable:
            try:
                for name, _part, row, column, _cell in formula_cells:
                    values[(name.lower(), row, column)] = engine.value(name.lower(), row, column)
            except (fe.CircularReference, fe.WorkTooLarge, fe.FormulaSyntaxError, RecursionError):
                computable = False
        for key in changed_formula_cells:
            if computable and values.get(key) in fe.STRUCTURAL:
                _refuse("formula_reference_error")
        for name, part, row, column, cell in formula_cells:
            previous = (cell.get("t"), [child.text for child in cell.findall(S + "v")])
            for child in cell.findall(S + "v"):
                cell.remove(child)
            cell.attrib.pop("t", None)
            if not computable:
                if previous != (None, []):
                    self.dirty.add(part)
                continue
            value = values[(name.lower(), row, column)]
            node = cell.makeelement(S + "v", {})
            if isinstance(value, fe.ExcelError):
                cell.set("t", "e")
                node.text = value.code
            elif type(value) is bool:
                cell.set("t", "b")
                node.text = "1" if value else "0"
            elif isinstance(value, (int, float)):
                node.text = _number_text(value)
            else:
                cell.set("t", "str")
                node.text = "" if value is None else str(value)
            cell.append(node)
            if previous != (cell.get("t"), [node.text]):
                self.dirty.add(part)
        self.computed = computable

    # --- chart caches -----------------------------------------------------------------------------
    # A chart part stores a copy of the values it plots (numCache, strCache) so that viewers which do
    # not recalculate can draw it. After an edit those copies must agree with the cells, or a preview
    # shows new tables beside old charts. Every reference is recomputed from the recalculated cells:
    # an unchanged one is left byte for byte; a changed one is rewritten when it is a plain list of
    # single-row or single-column areas, and its cache is removed otherwise (multi-level categories,
    # names, numbers shown as text, formulas this engine could not compute): Excel rebuilds a missing
    # cache on opening (fullCalcOnLoad), and a viewer shows no stale number.

    def refresh_charts(self):
        sheets_changed = any(part in self.dirty for _name, part in self.sheets.values()) \
            or "xl/sharedStrings.xml" in self.dirty
        for part in self.names:
            if not (part.startswith("xl/charts/") and part.endswith(".xml") and "/_rels/" not in part):
                continue
            root = self.root(part)
            changed = False
            for tag, kind in ((CHART + "numRef", "num"), (CHART + "strRef", "str")):
                for reference in list(root.iter(tag)):
                    changed |= self._refresh_reference(reference, kind)
            if sheets_changed:
                # Not recomputable here: drop the copies rather than keep possibly stale ones.
                for reference in list(root.iter(CHART + "multiLvlStrRef")):
                    for cache in reference.findall(CHART + "multiLvlStrCache"):
                        reference.remove(cache)
                        changed = True
                for dimension in [*root.iter(CHART_EX + "numDim"), *root.iter(CHART_EX + "strDim")]:
                    for level in dimension.findall(CHART_EX + "lvl"):
                        dimension.remove(level)
                        changed = True
            if changed:
                self.dirty.add(part)

    def _chart_values(self, formula):
        """The values a chart reference reads now, in order, or None when this editor cannot know them."""
        text = (formula or "").strip()
        if text.startswith("(") and text.endswith(")"):
            text = text[1:-1]
        pieces, current, quoted = [], "", False
        for character in text:
            if character == "'":
                quoted = not quoted
            if character == "," and not quoted:
                pieces.append(current)
                current = ""
            else:
                current += character
        pieces.append(current)
        values, count = [], 0
        for piece in pieces:
            match = _CHART_AREA.fullmatch(piece.strip())
            if match is None:
                return None
            sheet = match.group(1).replace("''", "'") if match.group(1) is not None else match.group(2)
            if sheet.lower() not in self.sheets:
                return None
            top, left = int(match.group(4)), _column_number(match.group(3).upper())
            bottom, right = ((int(match.group(6)), _column_number(match.group(5).upper())) if match.group(5)
                             else (top, left))
            if top > bottom or left > right or (top != bottom and left != right):
                return None
            count += (bottom - top + 1) * (right - left + 1)
            if count > MAX_CHART_POINTS:
                return None
            key = sheet.lower()
            for row in range(top, bottom + 1):
                for column in range(left, right + 1):
                    if (key, row, column) in self.formula_keys and not self.computed:
                        return None
                    values.append(self.engine.value(key, row, column))
        return values

    def _refresh_reference(self, reference, kind):
        """Bring one numRef or strRef cache in line with its cells; True when the part changed."""
        from . import formula_engine as fe
        cache = reference.find(CHART + ("numCache" if kind == "num" else "strCache"))
        if cache is None:
            return False                      # Nothing stored, nothing stale.
        values = self._chart_values(reference.findtext(CHART + "f"))
        if values is None:
            reference.remove(cache)
            return True
        old = {int(point.get("idx", "-1")): point.findtext(CHART + "v") or "" for point in cache.findall(CHART + "pt")}

        def same_number(text, value):
            try:
                return math.isclose(float(text), value, rel_tol=1e-12, abs_tol=1e-9)
            except ValueError:
                return False
        points = {}
        for index, value in enumerate(values):
            if kind == "num":
                if type(value) is str:
                    try:                      # Numeric text ("2027") is plotted, and cached, as its number.
                        value = float(value.strip())
                    except ValueError:
                        continue
                    if not math.isfinite(value):
                        continue
                if type(value) in (int, float):
                    points[index] = old[index] if index in old and same_number(old[index], value) else _number_text(value)
                continue
            if value is None or value == "":
                continue
            if type(value) is str:
                points[index] = value
            elif type(value) is bool:
                points[index] = "TRUE" if value else "FALSE"
            elif isinstance(value, fe.ExcelError):
                points[index] = value.code
            elif index in old and same_number(old[index], value):
                points[index] = old[index]    # A number shown as text, unchanged: keep its formatted text.
            else:
                reference.remove(cache)       # A changed number shown as text: its format is the cell's.
                return True
        count = cache.find(CHART + "ptCount")
        if points == old and count is not None and count.get("val") == str(len(values)):
            return False
        for child in list(cache):
            if child.tag in (CHART + "ptCount", CHART + "pt"):
                cache.remove(child)
        position = 1 if len(cache) and cache[0].tag == CHART + "formatCode" else 0
        count = cache.makeelement(CHART + "ptCount", {"val": str(len(values))})
        cache.insert(position, count)
        for offset, (index, text) in enumerate(sorted(points.items()), start=1):
            point = cache.makeelement(CHART + "pt", {"idx": str(index)})
            node = point.makeelement(CHART + "v", {})
            node.text = text
            point.append(node)
            cache.insert(position + offset, point)
        return True

    def constant(self, cell, fe):
        kind = cell.get("t")
        if kind in ("s", "inlineStr"):
            return self.cell_text(cell)
        value = cell.find(S + "v")
        if value is None or value.text is None:
            return None
        if kind == "b":
            return value.text == "1"
        if kind == "e":
            return fe.ExcelError(value.text)
        if kind == "str":
            return value.text
        try:
            return float(value.text)
        except ValueError:
            return value.text


def _number_text(value):
    if type(value) is int or (type(value) is float and value.is_integer() and abs(value) < 1e15):
        return str(int(value))
    return repr(float(value))


def _inside(reference, row, column):
    corners = [_CELL.fullmatch(piece) for piece in reference.split(":")]
    if not all(corners):
        return False
    top, left = int(corners[0].group(2)), _column_number(corners[0].group(1))
    bottom, right = int(corners[-1].group(2)), _column_number(corners[-1].group(1))
    return top <= row <= bottom and left <= column <= right


def _position(cell):
    match = _CELL.fullmatch(cell or "")
    if match is None:
        _refuse("edit_address_not_found")
    row, column = int(match.group(2)), _column_number(match.group(1))
    if not (1 <= row <= 1_048_576 and 1 <= column <= 16_384):
        _refuse("edit_address_not_found")
    return row, column


def _x_text(run):
    if run.tag == S + "t":
        return run.text or ""
    node = run.find(S + "t")
    return "" if node is None else (node.text or "")


def _x_set(run, text):
    node = run if run.tag == S + "t" else run.find(S + "t")
    node.text = text
    node.set(XML_SPACE, "preserve")


def _edit_xlsx(data, operations):
    book = _Book(data)
    changed_formulas = []
    for operation in operations:
        if operation["op"] == "set_cell":
            sheet, cell_reference = _split_address(operation["at"])
            name, part = book.sheet(sheet)
            row, column = _position(cell_reference)
            book.index(part)
            book.guarded(part, row, column)
            cell = book.cell(part, row, column)
            if "formula" in operation:
                for child in [child for child in cell if child.tag in (S + "f", S + "v", S + "is")]:
                    cell.remove(child)
                cell.attrib.pop("t", None)
                _check_header_formula(book, part, row, column)
                node = cell.makeelement(S + "f", {})
                node.text = _stored_formula(operation["formula"])
                cell.append(node)
                changed_formulas.append((name.lower(), row, column))
                book.dirty.add(part)
            else:
                value = operation["value"]
                book.header_names(part, row, column, value if type(value) is str else None)
                book.write_value(cell, value)
                book.dirty.add(part)
        else:  # replace_text, in text cells only: never inside a formula
            pattern, count = _pattern(operation["find"], operation["match_case"]), 0
            scope_sheets, area = list(book.sheets.values()), None
            if operation["in"] is not None:
                sheet, area = _split_address(operation["in"])
                scope_sheets = [book.sheet(sheet)]
            for _name, part in scope_sheets:
                for (row, column), cell in list(book.index(part).items()):
                    if area is not None and not _inside(area if ":" in area else f"{area}:{area}", row, column):
                        continue
                    text = book.cell_text(cell)
                    if text is None or not pattern.search(text):
                        continue
                    if cell.get("t") == "s":  # Shared: this cell gets its own copy, others are untouched.
                        rich = book.shared[int(cell.find(S + "v").text)]
                        inline = cell.makeelement(S + "is", {})
                        for child in rich:
                            if child.tag in (S + "t", S + "r"):
                                inline.append(deepcopy(child))
                    else:
                        inline = cell.find(S + "is")
                    if inline.find(S + "r") is not None:  # Rich text: each run keeps its formatting.
                        runs = _Runs(inline.findall(S + "r"), _x_text, _x_set, lambda covered: covered[0])
                        count += _replace_in(runs, pattern, operation["replace"])
                    else:
                        plain = inline.find(S + "t")
                        count += len(pattern.findall(plain.text or ""))
                        _x_set(plain, pattern.sub(lambda _match: operation["replace"], plain.text or ""))
                    new_text = _plain_text(inline)
                    book.guarded(part, row, column)
                    book.header_names(part, row, column, new_text)
                    for child in [child for child in cell if child.tag in (S + "v", S + "is")]:
                        cell.remove(child)
                    book.write_inline(cell, new_text, rich=inline)
                    book.dirty.add(part)
            _expect(count, operation["expect"])
    return book.save(changed_formulas), book


def _stored_formula(formula):
    """A formula as the file stores it: no "=", and the "_xlfn." prefix Excel requires on functions newer
    than Excel 2007 (without it Excel shows #NAME?), added outside string literals only."""
    body, pieces, last = formula[1:], [], 0

    def prefix(text):
        return _FUTURE.sub(lambda match: "_xlfn." + match.group(1).upper() + "(", text)
    for literal in _STRING_LITERAL.finditer(body):
        pieces += [prefix(body[last:literal.start()]), literal.group(0)]
        last = literal.end()
    return "".join(pieces) + prefix(body[last:])


def _check_header_formula(book, part, row, column):
    """A formula cannot name a table column."""
    rels = book.relations(part)
    for table_part_element in book.root(part).iter(S + "tablePart"):
        table_part = rels.get(table_part_element.get(R + "id"))
        if table_part in book.names:
            table = book.root(table_part)
            first = _CELL.match(table.get("ref") or "")
            if first and _inside(table.get("ref"), row, column) and int(first.group(2)) == row \
                    and table.get("headerRowCount") != "0":
                _refuse("edit_not_allowed")


# --- outline ---------------------------------------------------------------------------------


class _Gather:
    def __init__(self):
        self.units, self.size, self.truncated = [], 0, False

    def add(self, unit):
        if self.truncated:
            return False
        if len(self.units) >= MAX_OUTLINE_UNITS or self.size + len(unit.text) > MAX_OUTLINE_CHARS:
            self.truncated = True
            return False
        self.units.append(unit)
        self.size += len(unit.text)
        return True


def _accepted(data, declared_name):
    try:
        found = inspect(data, declared_name)
    except IntakeError as refused:
        raise SpecError(refused.code) from None
    if found.kind not in MEDIA:
        _refuse("edit_unsupported_type")
    return found.kind


def outline(data, declared_name=None):
    """What an edit can address in an uploaded Word, PowerPoint or Excel file."""
    kind = _accepted(data, declared_name)
    gather = _Gather()
    if kind == "docx":
        import docx
        document = docx.Document(io.BytesIO(data))
        styles = {style.get(W + "styleId"): style.find(W + "name").get(W + "val")
                  for style in document.styles.element.iter(W + "style") if style.find(W + "name") is not None}
        for address, (unit_kind, element) in _word_map(document.element).items():
            if unit_kind == "paragraph":
                text = _paragraph_text(element)
                properties = element.find(W + "pPr")
                style_id = None if properties is None or properties.find(W + "pStyle") is None else \
                    properties.find(W + "pStyle").get(W + "val")
                if text.strip() and not gather.add(Unit(address, "paragraph", text, styles.get(style_id, "Normal"))):
                    break
            elif unit_kind == "table":
                rows = element.findall(W + "tr")
                columns = max((len(row.findall(W + "tc")) for row in rows), default=0)
                if not gather.add(Unit(address, "table", "", f"{len(rows)} rows, {columns} columns")):
                    break
            elif unit_kind == "cell":
                if not gather.add(Unit(address, "cell", _cell_text(element))):
                    break
    elif kind == "pptx":
        import pptx
        presentation = pptx.Presentation(io.BytesIO(data))
        for address, (unit_kind, element) in _deck_map(presentation).items():
            if unit_kind == "slide":
                unit = Unit(address, "slide")
            elif unit_kind == "shape":
                unit = Unit(address, "shape", element.text_frame.text, element.name)
                if not unit.text.strip():
                    continue  # A shape without text (a rule, a card): its address stays reserved.
            elif unit_kind == "table":
                unit = Unit(address, "table", "", element.name)
            elif unit_kind == "paragraph":
                unit = Unit(address, "paragraph", "".join(node.text or "" for node in element.iter(A + "t")))
                if not unit.text.strip():
                    continue
            elif unit_kind == "cell":
                unit = Unit(address, "cell", element.text)
            else:
                unit = Unit(address, "notes", element.notes_text_frame.text if element.notes_text_frame else "")
            if not gather.add(unit):
                break
    else:
        book = _Book(data)
        for _key, (name, part) in book.sheets.items():
            quoted = name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) else "'" + name.replace("'", "''") + "'"
            cells = book.index(part)
            rows = max((row for row, _column in cells), default=0)
            columns = max((column for _row, column in cells), default=0)
            if not gather.add(Unit(quoted, "sheet", "", f"{rows} rows, {columns} columns")):
                break
            for (row, column), cell in sorted(cells.items()):
                formula = cell.find(S + "f")
                text = book.cell_text(cell)
                if text is None:
                    value = cell.find(S + "v")
                    text = "" if value is None or value.text is None else value.text
                if formula is None and not text:
                    continue
                detail = "" if formula is None else "=" + re.sub(r"_xl(fn|ws)\.", "", formula.text or "")
                if not gather.add(Unit(f"{quoted}!{_column_letters(column)}{row}", "cell", text, detail)):
                    break
            if gather.truncated:
                break
    return Outline(kind, tuple(gather.units), gather.truncated)


# --- edit ------------------------------------------------------------------------------------


def _accepted_text(paragraph):
    """A Word paragraph's text with every revision accepted: inserted text kept, deleted text dropped."""
    skipped = (W + "del", W + "moveFrom")
    return "".join(node.text or "" for node in paragraph.iter(W + "t")
                   if not any(ancestor.tag in skipped for ancestor in node.iterancestors()))


def _word_tables_accepted(data):
    """(header, rows) of every body table of a Word document as it reads with every tracked revision
    accepted (Codex reverification, finding A): a row marked deleted is gone, an inserted row counts,
    inserted text counts and deleted text does not. A cell spanning columns repeats its text in each
    column it covers, and a vertically merged cell repeats the text of the cell it continues, so columns
    stay aligned with the header."""
    root = _parse_xml(zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml"))
    found = []
    for table in [block for block in _word_blocks(root.find(W + "body")) if block.tag == W + "tbl"]:
        rows, above = [], {}
        for row in table.findall(W + "tr"):
            properties = row.find(W + "trPr")
            if properties is not None and properties.find(W + "del") is not None:
                continue
            cells, column = [], 0
            for cell in row.findall(W + "tc"):
                cell_properties = cell.find(W + "tcPr")
                span, merge = 1, None
                if cell_properties is not None:
                    grid = cell_properties.find(W + "gridSpan")
                    span = int(grid.get(W + "val", "1")) if grid is not None and grid.get(W + "val", "1").isdigit() else 1
                    merge = cell_properties.find(W + "vMerge")
                if merge is not None and merge.get(W + "val") in (None, "continue"):
                    text = above.get(column, "")
                else:
                    text = "\n".join(_accepted_text(paragraph) for paragraph in cell.findall(W + "p"))
                for offset in range(max(1, span)):
                    cells.append(text)
                    above[column + offset] = text
                column += max(1, span)
            rows.append(cells)
        if rows:
            found.append((rows[0], rows[1:]))
    return found


def _tables(data, kind):
    """(header, rows) of every table in a Word document (with its revisions accepted) or deck, as cell
    text, in document order."""
    found = []
    if kind == "docx":
        found = _word_tables_accepted(data)
    elif kind == "pptx":
        import pptx
        for slide in pptx.Presentation(io.BytesIO(data)).slides:
            for shape_kind, shape in _shapes_of(slide):
                if shape_kind == "table":
                    rows = [[cell.text for cell in row.cells] for row in shape.table.rows]
                    if rows:
                        found.append((rows[0], rows[1:]))
    return found


def _check_totals_kept(before, after, kind):
    """Codex document audit, Q4: an edit must not leave a total row that added up no longer adding up.
    A table that was already inconsistent in the customer's file never blocks an edit, and a customer's
    explicitly exact edit skips this (edit(..., exact=True))."""
    from .consistency import TotalMismatch, check_table
    old = _tables(before, kind)
    for number, (header, rows) in enumerate(_tables(after, kind), start=1):
        try:
            check_table(header, rows, number)
        except TotalMismatch:
            if number <= len(old):
                try:
                    check_table(*old[number - 1], number)
                except TotalMismatch:
                    continue                          # Already inconsistent before this edit.
            raise


def _document_text(data, kind):
    """All of a document's text as it reads once revisions are accepted: Word paragraphs (in tables too),
    or every text body and table cell of a deck."""
    if kind == "docx":
        root = _parse_xml(zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml"))
        return "\n".join(_accepted_text(paragraph) for paragraph in root.iter(W + "p"))
    import pptx
    presentation = pptx.Presentation(io.BytesIO(data))
    texts = []
    for _key, kind_, element, _slide in _text_containers(presentation):
        body = element.text_frame._txBody if kind_ == "shape" else element[3]._tc.find(A + "txBody")
        if body is not None:
            texts.append(_body_text(body))
    return "\n".join(texts)


# A complete amount in running text, in the forms a table total may take (consistency._QUANTITY): an
# optional opening parenthesis, a sign (-, \u2212, \u2013), a currency sign or a three-letter ISO code
# followed by a space, digits with thousands separators and an optional decimal part, an optional K/M/B or
# bn scale, and an optional closing parenthesis. Never part of a longer number or word: "$300" is not
# found inside "$3000", "$3,000", "$300.5" or "A$300", and "$100-$300" holds "$100" and "$300" (a range,
# not a negative). Spaces are ordinary or no-break spaces only, so a figure never spans two lines.
_GAP = "[ \u00a0]"
_FIGURE = re.compile(
    rf"(?<![\w.,$€£¥₹])"
    rf"(?P<open>\({_GAP}?)?"
    rf"(?P<sign>[-\u2212\u2013](?P<spaced>{_GAP})?)?"
    rf"(?P<currency>[$€£¥₹]|[A-Z]{{3}}(?={_GAP}))?{_GAP}?"
    rf"(?P<number>\d{{1,3}}(?:,\d{{3}})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    rf"(?:{_GAP}?(?P<scale>[KMB]|bn)(?![A-Za-z]))?"
    rf"(?![\w$€£¥₹])(?!\.\d)(?!,\d)"
    rf"(?P<close>{_GAP}?\))?")


def _figure_keys(match):
    """The (value, currency, scale) keys one amount in running text may stand for. Where prose is
    ambiguous the amount stands for both signs, so a stale total is never missed: parentheses may be
    accounting notation or punctuation ("The total fee ($300) is fixed."), and a spaced hyphen or an en
    dash may be a minus sign or a dash ("Fees \u2013 $300"). An adjacent hyphen or a minus sign (\u2212) is
    negative, as in a table."""
    value = round(float(match.group("number").replace(",", "")), 9)
    unit = (match.group("currency") or "", match.group("scale") or "")
    sign = match.group("sign")
    if sign and (sign[0] == "\u2212" or (sign[0] == "-" and not match.group("spaced"))):
        return {(-value, *unit)}
    if sign or match.group("open"):
        return {(value, *unit), (-value, *unit)}
    return {(value, *unit)}


def _figure_key(cell):
    """(value, currency, scale) of a table cell read exactly as the total check reads it, or None.
    "$300" and "$300.00" are the same amount; "300" (no currency), "$300", "USD 300", "-$300" and
    "$300K" are different ones, so an unrelated count, currency or scale never blocks an edit."""
    from .consistency import _quantity
    found = _quantity(cell)
    if found is None:
        return None
    value, (currency, scale), _decimals = found
    return round(value, 9), currency, scale


def _figures(text):
    """How many amounts in the text may stand for each key."""
    from collections import Counter
    return Counter(key for match in _FIGURE.finditer(text) for key in _figure_keys(match))


def _check_restated_totals(before, after, kind):
    """When an edit changes a table's total, the old total may not survive anywhere else in the document
    (a summary sentence, a metric, another table): the document would then state two totals. The amount
    must be restated everywhere it appears, or the customer must confirm the exact edit. Amounts are
    compared as complete tokens by value, sign, currency and scale (Codex reverifications: substring
    counting found "$300" inside "$3000"; the token matcher that replaced it missed "-$300", "($300)"
    and "USD 300"); nothing is ever changed automatically."""
    from .consistency import _TOTAL_LABEL
    old_tables, new_tables = _tables(before, kind), _tables(after, kind)
    if len(old_tables) != len(new_tables):
        return
    document = _figures(_document_text(after, kind))
    for (_old_header, old_rows), (_new_header, new_rows) in zip(old_tables, new_tables):
        if not old_rows or not new_rows or _TOTAL_LABEL.match(old_rows[-1][0] or "") is None:
            continue
        own = _figures("\n".join(cell for row in new_rows for cell in row))
        for old, new in zip(old_rows[-1][1:], new_rows[-1][1:]):
            key = _figure_key(old)
            # Not an amount the total check reads (text, a percentage), or the same amount rewritten.
            if key is None or key == _figure_key(new):
                continue
            # Occurrences outside this table's own cells (which may legitimately still show it).
            if document[key] - own[key] > 0:
                raise SpecError("dependent_figure_unchanged")


def edit(data, declared_name, operations, track_changes=False, revision_date=None, exact=False):
    """A GeneratedFile: the uploaded file with the operations applied. The upload is not modified.

    revision_date: an ISO 8601 timestamp recorded on Word revisions (optional).
    exact: the customer explicitly asked for exactly these changes, accepting that a total row they
    affect may no longer add up. Otherwise such an edit is refused with table_total_mismatch, so a
    restatement carries its dependent figures (a workbook's formulas recompute by themselves)."""
    if type(exact) is not bool:
        _refuse("invalid_edit")
    kind = _accepted(data, declared_name)
    checked = _check(operations, kind)
    if type(track_changes) is not bool or (track_changes and kind != "docx"):
        _refuse("invalid_edit")
    if revision_date is not None and (type(revision_date) is not str or
                                      not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", revision_date)):
        _refuse("invalid_edit")
    if kind == "docx":
        result = _edit_docx(data, checked, track_changes, revision_date)
    elif kind == "pptx":
        result = _edit_pptx(data, checked)
    else:
        result = _edit_xlsx(data, checked)[0]
    if kind in ("docx", "pptx") and not exact:
        _check_totals_kept(data, result, kind)
        _check_restated_totals(data, result, kind)
    result = fixed_dates(result)
    if len(result) > MAX_FILE_BYTES:
        _refuse("file_too_large")
    try:
        inspect(result, "edited." + kind)  # What intake would not accept back is not delivered.
    except IntakeError:
        _refuse("edit_failed")
    stem = display_name(declared_name).rsplit(".", 1)[0] if declared_name else "document"
    return GeneratedFile(safe_filename(stem + "-edited", kind), MEDIA[kind], result)
