"""One renderer per format. Each takes a validated spec object and returns bytes.

The document libraries are imported inside the renderer that needs them, so
importing this package costs nothing until a file is actually generated.

PDF text coverage: PDF needs a font that contains every character. The only
fonts available without adding a font file to the repository are the Bitstream
Vera faces shipped inside reportlab, which cover Western European text. A
document with any other character is refused (pdf_unsupported_characters)
rather than rendered with missing glyphs. DOCX, XLSX and the text formats
carry any Unicode text. A PDF table cell whose text is taller than a page is
refused as well (pdf_layout_failed): a cell cannot continue on the next page.
"""
import csv
from datetime import date
import io
import json
from xml.sax.saxutils import escape

from .spec import Formula, Heading, ListBlock, PageBreak, Paragraph, SpecError, Table

# CSV: a text cell starting with one of these would be read as a formula by a
# spreadsheet program; it is prefixed with an apostrophe (OWASP CSV injection).
_CSV_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")
NUMBER_FORMATS = {"text": "@", "number": "#,##0.00", "integer": "#,##0", "currency": "#,##0.00",
                  "percent": "0.00%", "date": "yyyy-mm-dd"}


# --- PDF ------------------------------------------------------------------------------------

_PDF_FONTS = {}


def _pdf_fonts():
    """Register reportlab's bundled Vera faces once; returns the covered code points."""
    if not _PDF_FONTS:
        import os
        import reportlab
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        folder = os.path.join(os.path.dirname(reportlab.__file__), "fonts")
        regular = TTFont("KalillacSans", os.path.join(folder, "Vera.ttf"))
        bold = TTFont("KalillacSans-Bold", os.path.join(folder, "VeraBd.ttf"))
        pdfmetrics.registerFont(regular)
        pdfmetrics.registerFont(bold)
        _PDF_FONTS["covered"] = frozenset(regular.face.charToGlyph) & frozenset(bold.face.charToGlyph)
    return _PDF_FONTS["covered"]


def _pdf_text(text, covered):
    if any(ord(char) not in covered and char not in "\n\t" for char in text):
        raise SpecError("pdf_unsupported_characters")
    return escape(text.replace("\t", "    ")).replace("\n", "<br/>")


def render_pdf(document):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import ListFlowable, ListItem, PageBreak as Break, Paragraph as Para
    from reportlab.platypus import SimpleDocTemplate, Spacer, Table as Grid, TableStyle
    from reportlab.platypus.doctemplate import LayoutError
    covered = _pdf_fonts()
    body = ParagraphStyle("body", fontName="KalillacSans", fontSize=10.5, leading=15, spaceAfter=7)
    cell = ParagraphStyle("cell", parent=body, fontSize=9, leading=12, spaceAfter=0)
    head = ParagraphStyle("head", parent=cell, fontName="KalillacSans-Bold")
    headings = {0: ParagraphStyle("title", parent=body, fontName="KalillacSans-Bold", fontSize=20, leading=25,
                                  spaceAfter=14),
                1: ParagraphStyle("h1", parent=body, fontName="KalillacSans-Bold", fontSize=15, leading=20,
                                  spaceBefore=10, spaceAfter=8),
                2: ParagraphStyle("h2", parent=body, fontName="KalillacSans-Bold", fontSize=12.5, leading=17,
                                  spaceBefore=8, spaceAfter=6),
                3: ParagraphStyle("h3", parent=body, fontName="KalillacSans-Bold", fontSize=11, leading=15,
                                  spaceBefore=6, spaceAfter=5)}
    story = [Para(_pdf_text(document.title, covered), headings[0])]
    for block in document.blocks:
        if type(block) is Heading:
            story.append(Para(_pdf_text(block.text, covered), headings[block.level]))
        elif type(block) is Paragraph:
            story.append(Para(_pdf_text(block.text, covered), body))
        elif type(block) is ListBlock:
            items = [ListItem(Para(_pdf_text(item, covered), body), leftIndent=14) for item in block.items]
            story.append(ListFlowable(items, bulletType="1" if block.ordered else "bullet",
                                      bulletFontName="KalillacSans", bulletFontSize=10, leftIndent=16))
            story.append(Spacer(1, 4))
        elif type(block) is Table:
            data = [[Para(_pdf_text(value, covered), head) for value in block.header]]
            data += [[Para(_pdf_text(value, covered), cell) for value in row] for row in block.rows]
            grid = Grid(data, repeatRows=1, colWidths=[(A4[0] - 40 * mm) / len(block.header)] * len(block.header))
            grid.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#9aa0a6")),
                                      ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef0f2")),
                                      ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                      ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4)]))
            story.append(grid)
            story.append(Spacer(1, 8))
        elif type(block) is PageBreak:
            story.append(Break())
    buffer = io.BytesIO()
    template = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=20 * mm,
                                 bottomMargin=20 * mm, title=document.title, author="Kalillac AI",
                                 creator="Kalillac AI", invariant=1)
    try:
        template.build(story)
    except LayoutError:
        # A table cell cannot continue on the next page, so a cell taller
        # than a page has no layout. Refused rather than truncated.
        raise SpecError("pdf_layout_failed") from None
    return buffer.getvalue()


# --- DOCX -----------------------------------------------------------------------------------


def render_docx(document):
    from docx import Document as Word
    from docx.enum.text import WD_BREAK
    word = Word()
    word.core_properties.title = document.title
    word.core_properties.author = "Kalillac AI"
    word.add_heading(document.title, level=0)
    for block in document.blocks:
        if type(block) is Heading:
            word.add_heading(block.text, level=block.level)
        elif type(block) is Paragraph:
            for part in block.text.split("\n\n"):
                paragraph = word.add_paragraph()
                lines = part.split("\n")
                for index, line in enumerate(lines):
                    run = paragraph.add_run(line)
                    if index < len(lines) - 1:
                        run.add_break()
        elif type(block) is ListBlock:
            style = "List Number" if block.ordered else "List Bullet"
            for item in block.items:
                word.add_paragraph(item.replace("\n", " "), style=style)
        elif type(block) is Table:
            table = word.add_table(rows=1, cols=len(block.header))
            table.style = "Table Grid"
            for index, value in enumerate(block.header):
                table.rows[0].cells[index].paragraphs[0].add_run(value).bold = True
            for row in block.rows:
                cells = table.add_row().cells
                for index, value in enumerate(row):
                    cells[index].text = value
            word.add_paragraph()
        elif type(block) is PageBreak:
            word.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
    buffer = io.BytesIO()
    word.save(buffer)
    return buffer.getvalue()


# --- XLSX -----------------------------------------------------------------------------------


def render_xlsx(workbook):
    from openpyxl import Workbook as Book
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter
    book = Book()
    book.remove(book.active)
    book.properties.title = workbook.title
    book.properties.creator = "Kalillac AI"
    for sheet in workbook.sheets:
        page = book.create_sheet(title=sheet.name)
        for index, column in enumerate(sheet.columns, start=1):
            header = page.cell(row=1, column=index, value=column.header)
            header.data_type = "s"  # A header is text even when it starts with "=".
            header.font = Font(bold=True)
            page.column_dimensions[get_column_letter(index)].width = column.width or max(10, min(40, len(column.header) + 2))
        page.freeze_panes = "A2"
        for row_index, row in enumerate(sheet.rows, start=2):
            for index, value in enumerate(row, start=1):
                if value is None:
                    continue
                column = sheet.columns[index - 1]
                cell = page.cell(row=row_index, column=index)
                if type(value) is Formula:
                    cell.value = value.text  # openpyxl stores a leading "=" as a formula.
                elif type(value) is str:
                    cell.value = value
                    cell.data_type = "s"  # Never a formula, whatever the text starts with.
                else:
                    cell.value = value
                cell.number_format = NUMBER_FORMATS[column.format]
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


# --- text formats ---------------------------------------------------------------------------


def _csv_value(value):
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
    sheet = workbook.sheets[0]
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow([_csv_value(column.header) for column in sheet.columns])
    for row in sheet.rows:
        writer.writerow([_csv_value(value) for value in row])
    return b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8")


def _markdown_cell(text):
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")


def render_markdown(document):
    lines = ["# " + document.title, ""]
    for block in document.blocks:
        if type(block) is Heading:
            lines += ["#" * (block.level + 1) + " " + block.text, ""]
        elif type(block) is Paragraph:
            lines += [block.text, ""]
        elif type(block) is ListBlock:
            lines += [(f"{index}. " if block.ordered else "- ") + item.replace("\n", " ")
                      for index, item in enumerate(block.items, start=1)] + [""]
        elif type(block) is Table:
            lines.append("| " + " | ".join(_markdown_cell(value) for value in block.header) + " |")
            lines.append("|" + " --- |" * len(block.header))
            lines += ["| " + " | ".join(_markdown_cell(value) for value in row) + " |" for row in block.rows]
            lines.append("")
        elif type(block) is PageBreak:
            lines += ["---", ""]
    return "\n".join(lines).rstrip("\n").encode("utf-8") + b"\n"


def render_text(document):
    lines = [document.title, "=" * min(len(document.title), 78), ""]
    for block in document.blocks:
        if type(block) is Heading:
            lines += [block.text, ("-" if block.level > 1 else "=") * min(len(block.text), 78), ""]
        elif type(block) is Paragraph:
            lines += [block.text, ""]
        elif type(block) is ListBlock:
            lines += [(f"{index}. " if block.ordered else "* ") + item.replace("\n", " ")
                      for index, item in enumerate(block.items, start=1)] + [""]
        elif type(block) is Table:
            lines.append("\t".join(block.header))
            lines += ["\t".join(value.replace("\n", " ").replace("\t", " ") for value in row) for row in block.rows]
            lines.append("")
        elif type(block) is PageBreak:
            lines += ["", ""]
    return "\n".join(lines).rstrip("\n").encode("utf-8") + b"\n"


def render_json(workbook):
    """Each sheet as a list of records keyed by column header."""
    def plain(value):
        if type(value) is Formula:
            return {"formula": value.text}
        return value.isoformat() if type(value) is date else value
    sheets = []
    for sheet in workbook.sheets:
        headers = [column.header for column in sheet.columns]
        if len(set(headers)) != len(headers):
            raise SpecError("json_requires_unique_headers")
        sheets.append({"name": sheet.name, "rows": [dict(zip(headers, (plain(v) for v in row))) for row in sheet.rows]})
    return json.dumps({"title": workbook.title, "sheets": sheets}, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
