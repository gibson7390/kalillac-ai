"""Word documents in the Kalillac design system: real styles, real structure.

write_docx(document) produces an editable .docx that looks designed when it
is opened and stays consistent when it is edited:

  * every heading, list, caption and table uses a named Word style, so the
    navigation pane, the table of contents and later restyling all work;
  * a cover page, a contents page (a real TOC field), and a body section with
    a running header (the current section, via STYLEREF) and a footer with
    "Page X of Y" fields, numbered from the first body page;
  * tables with a repeated header row, rows that do not split across pages,
    quantities right-aligned, and an emphasised total row;
  * callouts, metric cards, comparisons and steps built from tables, which
    every Word-compatible program lays out the same way;
  * charts as crisp images with alternative text and numbered captions;
  * the Inter typeface embedded in the file (the SIL Open Font License allows
    it), so the document looks the same on a machine that has never seen the
    font and remains fully editable.

Nothing is executed and no field reaches outside the document.
"""
import io
import re
import uuid
import zipfile

from . import charts
from .package import application_record, drop_relationships, fixed_dates, own_record
from .spec import (_NUMERIC, Callout, Chart, Code, Comparison, Heading, ListBlock, Metrics, PageBreak, Paragraph,
                   Quote, Steps, Table, plain)
from .typeset import FONTS, LABELS, RTL, cover_art_png, direction, svg_png

NAVY, BLUE, BLUE_DEEP, BLUE_SOFT = "0B1737", "0069FF", "0053CF", "EAF3FF"
INK, BODY, MUTED, HAIR, RULE, PAPER = "0E1526", "1D2538", "5C677E", "E2E9F4", "CDD8EA", "F6F8FC"
COVER_TEXT = "C3D1EE"
TONES = {"info": (BLUE, BLUE_DEEP, BLUE_SOFT), "success": ("12A15C", "0B7A44", "E7F7EE"),
         "warning": ("E08A00", "8A5600", "FFF5DF"), "danger": ("D92D20", "A1221A", "FDEEEC"),
         "note": ("8793AB", "3B4660", PAPER)}
FONT, SEMIBOLD, MONO = "Inter", "Inter SemiBold", "Consolas"
EAST_ASIAN = {"ja": "Yu Gothic", "ko": "Malgun Gothic", "zh-hant": "Microsoft JhengHei",
              "zh-tw": "Microsoft JhengHei", "zh-hk": "Microsoft JhengHei", "zh": "Microsoft YaHei"}
PAGES = {"a4": (210.0, 297.0), "letter": (215.9, 279.4)}
MARGIN_X, MARGIN_TOP, MARGIN_BOTTOM = 22.0, 27.0, 25.0
# (Word family name, bundled file, fontTable slot): the faces embedded in every document.
EMBEDDED = (("Inter", "Inter-Regular.ttf", "embedRegular"), ("Inter", "Inter-Bold.ttf", "embedBold"),
            ("Inter", "Inter-Italic.ttf", "embedItalic"), ("Inter", "Inter-BoldItalic.ttf", "embedBoldItalic"),
            ("Inter SemiBold", "Inter-SemiBold.ttf", "embedRegular"))
# Right-to-left text is drawn with Word's "complex script" font, embedded as well when the language needs it.
# Arabic script is IBM Plex Sans Arabic in every format (owner decision 13): it covers Arabic, Persian,
# Urdu, Pashto, Sindhi and Uyghur, permits installable embedding, and draws each letter form as its own
# glyph. Noto Sans Arabic stays available as the Arabic face for anything that asks for it.
COMPLEX = {"IBM Plex Sans Arabic": (("IBM Plex Sans Arabic", "IBMPlexSansArabic-Regular.ttf", "embedRegular"),
                                    ("IBM Plex Sans Arabic", "IBMPlexSansArabic-Bold.ttf", "embedBold")),
           "Noto Sans Arabic": (("Noto Sans Arabic", "NotoSansArabic-Regular.ttf", "embedRegular"),
                                ("Noto Sans Arabic", "NotoSansArabic-Bold.ttf", "embedBold")),
           "Noto Sans Hebrew": (("Noto Sans Hebrew", "NotoSansHebrew-Regular.ttf", "embedRegular"),
                                ("Noto Sans Hebrew", "NotoSansHebrew-Bold.ttf", "embedBold"))}
COMPLEX_BY_LANGUAGE = {"ar": "IBM Plex Sans Arabic", "fa": "IBM Plex Sans Arabic", "ur": "IBM Plex Sans Arabic",
                       "ps": "IBM Plex Sans Arabic", "sd": "IBM Plex Sans Arabic", "ug": "IBM Plex Sans Arabic",
                       "he": "Noto Sans Hebrew", "yi": "Noto Sans Hebrew"}


# WordprocessingML requires the children of a property element in a fixed order, and Word ignores
# (or repairs) what arrives out of order. Properties are added here in whatever order is natural
# and sorted once, at the end, by these sequences.
_ORDER = {
    "pPr": ("pStyle", "keepNext", "keepLines", "pageBreakBefore", "framePr", "widowControl", "numPr",
            "suppressLineNumbers", "pBdr", "shd", "tabs", "suppressAutoHyphens", "kinsoku", "wordWrap",
            "overflowPunct", "topLinePunct", "autoSpaceDE", "autoSpaceDN", "bidi", "adjustRightInd", "snapToGrid",
            "spacing", "ind", "contextualSpacing", "mirrorIndents", "suppressOverlap", "jc", "textDirection",
            "textAlignment", "textboxTightWrap", "outlineLvl", "divId", "cnfStyle", "rPr", "sectPr", "pPrChange"),
    "rPr": ("rStyle", "rFonts", "b", "bCs", "i", "iCs", "caps", "smallCaps", "strike", "dstrike", "outline",
            "shadow", "emboss", "imprint", "noProof", "snapToGrid", "vanish", "webHidden", "color", "spacing", "w",
            "kern", "position", "sz", "szCs", "highlight", "u", "effect", "bdr", "shd", "fitText", "vertAlign",
            "rtl", "cs", "em", "lang", "eastAsianLayout", "specVanish", "oMath"),
    "tblPr": ("tblStyle", "tblpPr", "tblOverlap", "bidiVisual", "tblStyleRowBandSize", "tblStyleColBandSize",
              "tblW", "jc", "tblCellSpacing", "tblInd", "tblBorders", "shd", "tblLayout", "tblCellMar", "tblLook"),
    "tcPr": ("cnfStyle", "tcW", "gridSpan", "hMerge", "vMerge", "tcBorders", "shd", "noWrap", "tcMar",
             "textDirection", "tcFitText", "vAlign", "hideMark"),
    "sectPr": ("headerReference", "footerReference", "footnotePr", "endnotePr", "type", "pgSz", "pgMar",
               "paperSrc", "pgBorders", "lnNumType", "pgNumType", "cols", "formProt", "vAlign", "noEndnote",
               "titlePg", "textDirection", "bidi", "rtlGutter", "docGrid"),
    "pBdr": ("top", "left", "bottom", "right", "between", "bar"),
    "tcBorders": ("top", "left", "start", "bottom", "right", "end", "insideH", "insideV"),
    "tblBorders": ("top", "left", "start", "bottom", "right", "end", "insideH", "insideV"),
    "tcMar": ("top", "left", "start", "bottom", "right", "end"),
}
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_REPEATABLE = {"headerReference", "footerReference"}


def normalize(root):
    """Sort the children of every property element into schema order; a later duplicate replaces an earlier one."""
    for name, order in _ORDER.items():
        rank = {tag: index for index, tag in enumerate(order)}
        for element in root.iter(_W + name):
            children = list(element)
            if len(children) < 2:
                continue
            kept, seen = [], {}
            for child in children:
                local = child.tag[len(_W):] if child.tag.startswith(_W) else child.tag
                if local in seen and local not in _REPEATABLE:
                    kept[seen[local]] = child
                    continue
                seen[local] = len(kept)
                kept.append(child)
            ordered = sorted(enumerate(kept), key=lambda pair: (
                rank.get(pair[1].tag[len(_W):] if pair[1].tag.startswith(_W) else "", len(order)), pair[0]))
            for child in children:
                element.remove(child)
            for _index, child in ordered:
                element.append(child)


def script_segments(text, base_rtl=False):
    """text split into (segment, is_right_to_left) pieces by script.

    Word picks the font, size and weight of right-to-left text from a run's complex-script
    properties and of everything else from its Latin ones. A run that mixes the two leaves the
    choice to Word, which then substitutes fonts mid-sentence, so each stretch gets its own run.

    Spaces and punctuation follow the Unicode bidirectional rules in simplified form: a number
    takes the terminators that touch it ("23%", "$5") and separators between its digits ("18.4"),
    and any other neutral character belongs to the stretch it sits inside, or to the document's
    own direction when it sits between two different stretches. That keeps the space on the
    correct side of an embedded number or foreign word.
    """
    import unicodedata
    kinds = []
    for char in text:
        kind = unicodedata.bidirectional(char)
        kinds.append("R" if kind in ("R", "AL") else "L" if kind == "L" else "N" if kind in ("EN", "AN")
                     else "T" if kind == "ET" else "S" if kind in ("ES", "CS") else "M" if kind == "NSM" else "n")
    for index, kind in enumerate(kinds):  # A combining mark (tanween, shadda) takes its base's direction (W1).
        if kind == "M":
            kinds[index] = kinds[index - 1] if index else "n"
    count = len(kinds)
    for index, kind in enumerate(kinds):  # Separators inside a number.
        if kind == "S":
            kinds[index] = "N" if 0 < index < count - 1 and kinds[index - 1] == "N" and kinds[index + 1] == "N" else "n"
    for sweep in (range(count), range(count - 1, -1, -1)):  # Terminators touching a number, either side.
        previous = None
        for index in sweep:
            if kinds[index] == "T" and previous == "N":
                kinds[index] = "N"
            previous = kinds[index]
    kinds = ["n" if kind == "T" else kind for kind in kinds]
    resolved = [None] * count
    for index, kind in enumerate(kinds):
        if kind != "n":
            resolved[index] = kind == "R"
    index = 0
    while index < count:  # Neutral stretches.
        if resolved[index] is not None:
            index += 1
            continue
        stop = index
        while stop < count and resolved[stop] is None:
            stop += 1
        before = kinds[index - 1] if index else None
        after = kinds[stop] if stop < count else None
        if before == "L" and after == "L":
            inner = False
        elif before == "R" and after == "R":
            inner = True
        else:
            inner = base_rtl
        for position in range(index, stop):
            resolved[position] = inner
        index = stop
    pieces, begin = [], 0
    for index in range(1, count + 1):
        if index == count or resolved[index] != resolved[begin]:
            pieces.append((text[begin:index], resolved[begin]))
            begin = index
    return pieces


def _mm(value):
    from docx.shared import Mm
    return Mm(value)


def _pt(value):
    from docx.shared import Pt
    return Pt(value)


def _color(hex_value):
    from docx.shared import RGBColor
    return RGBColor.from_string(hex_value)


def _element(tag, **attributes):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    element = OxmlElement(tag)
    for name, value in attributes.items():
        element.set(qn("w:" + name), str(value))
    return element


def _child(parent, tag):
    """The first existing child with this tag, or a new one appended in schema-safe position."""
    from docx.oxml.ns import qn
    found = parent.find(qn(tag))
    if found is None:
        found = _element(tag)
        parent.append(found)
    return found


class _Writer:
    def __init__(self, document):
        from docx import Document as Word
        self.spec = document
        self.word = Word()
        primary = document.language.split("-")[0].lower()
        self.rtl = primary in RTL
        self.labels = dict(zip(("contents", "page", "of", "summary"), LABELS.get(primary, LABELS["en"])))
        lowered = document.language.lower()
        self.east = next((font for prefix, font in EAST_ASIAN.items()
                          if lowered == prefix or lowered.startswith(prefix + "-")), "Microsoft YaHei")
        self.complex = COMPLEX_BY_LANGUAGE.get(primary, "Arial")
        self.complex_used = set()          # Right-to-left faces given to runs in a document of another script.
        self.page_width, self.page_height = PAGES[document.page]
        self.text_width = self.page_width - 2 * MARGIN_X
        self.figures = 0
        self.number_ids = {}

    # --- low-level formatting ---------------------------------------------------------------

    def fonts(self, r_pr, name=FONT):
        from docx.oxml.ns import qn
        r_fonts = r_pr.find(qn("w:rFonts"))
        if r_fonts is None:
            r_fonts = _element("w:rFonts")
            r_pr.insert(0, r_fonts)
        for attribute in list(r_fonts.attrib):
            del r_fonts.attrib[attribute]  # Theme fonts would override the explicit face.
        for attribute, value in (("ascii", name), ("hAnsi", name), ("eastAsia", self.east), ("cs", self.complex)):
            r_fonts.set(qn("w:" + attribute), value)

    def run(self, paragraph, text, size=None, color=None, bold=False, italic=False, font=None, tracking=None,
            ltr=False, caps=False):
        """A run of text. Word draws right-to-left scripts from the complex-script properties (szCs, bCs,
        iCs), so each is set beside its Latin twin; a semibold face stands in as bold there. Text that
        mixes scripts is placed as one run per script."""
        if not ltr:
            pieces = script_segments(text, self.rtl)
            if len(pieces) > 1 or (pieces and pieces[0][1] != self.rtl):
                last = None
                for piece, rtl_piece in pieces:
                    last = self._run(paragraph, piece, size, color, bold, italic, font, tracking, rtl_piece, caps)
                return last
        return self._run(paragraph, text, size, color, bold, italic, font, tracking, self.rtl and not ltr, caps)

    def _run(self, paragraph, text, size, color, bold, italic, font, tracking, rtl, caps=False):
        run = paragraph.add_run(text)
        r_pr = run._r.get_or_add_rPr()
        if caps:
            r_pr.append(_element("w:caps"))  # Shown in capitals; the stored text stays as written.
        if font is not None:
            self.fonts(r_pr, font)
        if size is not None:
            run.font.size = _pt(size)
            r_pr.append(_element("w:szCs", val=int(round(size * 2))))
        if color is not None:
            run.font.color.rgb = _color(color)
        run.font.bold = bold or None
        run.font.italic = italic or None
        if bold or font == SEMIBOLD:
            r_pr.append(_element("w:bCs"))
        if italic:
            r_pr.append(_element("w:iCs"))
        if rtl:
            # A right-to-left run in a document of another script still gets its own script's face (the
            # document's complex-script face is Arial there), so Arabic looks the same in every document.
            face = _complex_face(text)
            if face is not None and face != self.complex:
                from docx.oxml.ns import qn
                r_fonts = r_pr.find(qn("w:rFonts"))
                if r_fonts is None:
                    r_fonts = _element("w:rFonts")
                    r_pr.insert(0, r_fonts)
                r_fonts.set(qn("w:cs"), face)
                self.complex_used.add(face)
        if tracking is not None and not rtl:  # Letter-spacing breaks the joins of cursive scripts.
            r_pr.append(_element("w:spacing", val=int(round(tracking * 20))))
        if rtl:
            r_pr.append(_element("w:rtl"))
        return run

    def spacing(self, paragraph, before=0.0, after=0.0, line=None, keep_next=False, align=None):
        fmt = paragraph.paragraph_format
        fmt.space_before, fmt.space_after = _pt(before), _pt(after)
        if line is not None:
            fmt.line_spacing = self.leading(line)
        if keep_next:
            fmt.keep_with_next = True
        if align is not None:
            from docx.enum.text import WD_ALIGN_PARAGRAPH
            fmt.alignment = {"left": WD_ALIGN_PARAGRAPH.LEFT, "right": WD_ALIGN_PARAGRAPH.RIGHT,
                             "center": WD_ALIGN_PARAGRAPH.CENTER}[align]
        if self.rtl:
            p_pr = paragraph._p.get_or_add_pPr()
            from docx.oxml.ns import qn
            if p_pr.find(qn("w:bidi")) is None:
                p_pr.insert(0, _element("w:bidi"))
        return paragraph

    def border(self, paragraph, side, size, color, space=1):
        p_pr = paragraph._p.get_or_add_pPr()
        borders = _child(p_pr, "w:pBdr")
        borders.append(_element("w:" + side, val="single", sz=int(size * 8), space=space, color=color))

    def shade(self, cell, fill):
        cell._tc.get_or_add_tcPr().append(_element("w:shd", val="clear", color="auto", fill=fill))

    def cell_borders(self, cell, **sides):
        """sides: top/bottom/left/right = (points, colour) or None for no line."""
        tc_pr = cell._tc.get_or_add_tcPr()
        borders = _child(tc_pr, "w:tcBorders")
        for side in ("top", "left", "bottom", "right"):
            if side in sides:
                spec = sides[side]
                borders.append(_element("w:" + side, val="nil") if spec is None else
                               _element("w:" + side, val="single", sz=max(2, int(spec[0] * 8)), space=0,
                                        color=spec[1]))

    def cell_margins(self, cell, top=0.0, bottom=0.0, left=0.0, right=0.0):
        margins = _child(cell._tc.get_or_add_tcPr(), "w:tcMar")
        for side, value in (("top", top), ("left", left), ("bottom", bottom), ("right", right)):
            margins.append(_element("w:" + side, w=int(value * 20), type="dxa"))

    def table(self, rows, columns, widths_mm):
        """A borderless fixed-layout table spanning the given column widths."""
        from docx.oxml.ns import qn
        table = self.word.add_table(rows=rows, cols=columns)
        tbl_pr = table._tbl.tblPr
        for stale in list(tbl_pr):
            tbl_pr.remove(stale)  # No inherited table style: every line and margin here is deliberate.
        twips = [int(round(width / 25.4 * 1440)) for width in widths_mm]
        tbl_pr.append(_element("w:tblW", w=sum(twips), type="dxa"))
        tbl_pr.append(_element("w:tblInd", w=0, type="dxa"))  # Flush with the text column, not offset by padding.
        borders = _element("w:tblBorders")
        for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
            borders.append(_element("w:" + side, val="nil"))
        tbl_pr.append(borders)
        tbl_pr.append(_element("w:tblLayout", type="fixed"))
        margins = _element("w:tblCellMar")
        for side in ("top", "left", "bottom", "right"):
            margins.append(_element("w:" + side, w=0, type="dxa"))
        tbl_pr.append(margins)
        if self.rtl:
            tbl_pr.append(_element("w:bidiVisual"))
        grid = table._tbl.find(qn("w:tblGrid"))
        for column, width in zip(grid.findall(qn("w:gridCol")), twips):
            column.set(qn("w:w"), str(width))
        for row in table.rows:
            for index, cell in enumerate(row.cells):
                cell.width = _mm(widths_mm[index])
        return table

    def row_rules(self, row, header=False, cant_split=True, height_mm=None, exact=False):
        tr_pr = row._tr.get_or_add_trPr()
        if cant_split:
            tr_pr.append(_element("w:cantSplit"))
        if header:
            tr_pr.append(_element("w:tblHeader"))
        if height_mm is not None:
            tr_pr.append(_element("w:trHeight", val=int(height_mm / 25.4 * 1440), hRule="exact" if exact else "atLeast"))

    def field(self, paragraph, instruction, cached="", size=None, color=None, dirty=False):
        """A Word field (PAGE, SECTIONPAGES, STYLEREF, TOC) with the text shown until Word updates it."""
        def piece(kind, **extra):
            run = self.run(paragraph, "", size=size, color=color)
            run._r.append(_element("w:fldChar", fldCharType=kind, **extra))
        piece("begin", **({"dirty": "true"} if dirty else {}))
        run = self.run(paragraph, "", size=size, color=color)
        text = _element("w:instrText")
        text.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        text.text = " " + instruction + " "
        run._r.append(text)
        piece("separate")
        if cached:
            self.run(paragraph, cached, size=size, color=color)
        piece("end")

    def spacer(self, points):
        """A paragraph that only holds vertical space (after a table, where Word requires one)."""
        paragraph = self.word.add_paragraph()
        self.spacing(paragraph, 0, 0, line=None)
        paragraph.paragraph_format.line_spacing = _pt(max(1.0, points))
        from docx.enum.text import WD_LINE_SPACING
        paragraph.paragraph_format.line_spacing_rule = WD_LINE_SPACING.EXACTLY
        return paragraph

    # --- styles -----------------------------------------------------------------------------

    def styles(self):
        from docx.enum.text import WD_LINE_SPACING
        styles = self.word.styles
        normal = styles["Normal"]
        self.fonts(normal.element.get_or_add_rPr())
        normal.font.size, normal.font.color.rgb = _pt(10), _color(BODY)
        normal.element.get_or_add_rPr().append(_element("w:szCs", val=20))
        normal.paragraph_format.space_after = _pt(7.5)
        normal.paragraph_format.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
        normal.paragraph_format.line_spacing = self.leading(1.32)
        normal.paragraph_format.widow_control = True
        for name, size, face, color, before, after, tracking in (
                ("Title", 26, FONT, NAVY, 0, 6, -0.5), ("Heading 1", 19, FONT, NAVY, 22, 9, -0.25),
                ("Heading 2", 13.5, SEMIBOLD, NAVY, 16, 6, -0.1), ("Heading 3", 10.5, SEMIBOLD, BLUE_DEEP, 13, 4, 0)):
            style = styles[name]
            r_pr = style.element.get_or_add_rPr()
            self.fonts(r_pr, face)
            style.font.size, style.font.color.rgb = _pt(size), _color(color)
            style.font.bold = face == FONT
            style.font.italic = False
            r_pr.append(_element("w:szCs", val=int(round(size * 2))))
            r_pr.append(_element("w:bCs"))
            if tracking and not self.rtl:
                r_pr.append(_element("w:spacing", val=int(tracking * 20)))
            fmt = style.paragraph_format
            fmt.space_before, fmt.space_after = _pt(before), _pt(after)
            fmt.keep_with_next, fmt.line_spacing = True, 1.08
            p_pr = style.element.get_or_add_pPr()
            from docx.oxml.ns import qn
            for stale in p_pr.findall(qn("w:pBdr")):
                p_pr.remove(stale)  # The built-in Title style draws a rule under itself.
        for name in ("List Bullet", "List Number"):
            styles[name].paragraph_format.space_after = _pt(3.5)
        caption = styles["Caption"]
        self.fonts(caption.element.get_or_add_rPr())
        caption.font.size, caption.font.color.rgb, caption.font.bold = _pt(8.5), _color(MUTED), False
        caption.font.italic = False
        caption.element.get_or_add_rPr().append(_element("w:szCs", val=17))
        caption.paragraph_format.space_before, caption.paragraph_format.space_after = _pt(5), _pt(12)

    def leading(self, latin):
        """Line spacing as a multiple: the Latin value, tightened for the taller right-to-left faces."""
        return latin if self.complex == "Arial" else max(1.0, round(latin * 0.8, 2))

    def page(self, section, bleed=False):
        section.page_width, section.page_height = _mm(self.page_width), _mm(self.page_height)
        if bleed:
            section.left_margin = section.right_margin = section.top_margin = section.bottom_margin = _mm(0)
            section.header_distance = section.footer_distance = _mm(0)
        else:
            section.left_margin = section.right_margin = _mm(MARGIN_X)
            section.top_margin, section.bottom_margin = _mm(MARGIN_TOP), _mm(MARGIN_BOTTOM)
            section.header_distance, section.footer_distance = _mm(13), _mm(11)

    def next_section(self):
        from docx.enum.section import WD_SECTION
        section = self.word.add_section(WD_SECTION.NEW_PAGE)
        self.page(section)
        return section

    # --- front matter -----------------------------------------------------------------------

    def backdrop(self, paragraph, png):
        """A full-page picture anchored to the page, behind the text: how a Word cover is built."""
        from docx.oxml import parse_xml
        from docx.oxml.ns import qn
        run = paragraph.add_run()
        shape = run.add_picture(io.BytesIO(png), width=_mm(self.page_width), height=_mm(self.page_height))
        inline = shape._inline
        graphic = inline.find(qn("a:graphic"))
        anchor = parse_xml(
            '<wp:anchor xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
            'distT="0" distB="0" distL="0" distR="0" simplePos="0" relativeHeight="0" behindDoc="1" locked="0" '
            'layoutInCell="0" allowOverlap="1"><wp:simplePos x="0" y="0"/>'
            '<wp:positionH relativeFrom="page"><wp:posOffset>0</wp:posOffset></wp:positionH>'
            '<wp:positionV relativeFrom="page"><wp:posOffset>0</wp:posOffset></wp:positionV>'
            f'<wp:extent cx="{int(inline.extent.cx)}" cy="{int(inline.extent.cy)}"/>'
            '<wp:effectExtent l="0" t="0" r="0" b="0"/><wp:wrapNone/>'
            '<wp:docPr id="900" name="Cover background" descr=""/><wp:cNvGraphicFramePr/></wp:anchor>')
        anchor.append(graphic)
        inline.getparent().replace(inline, anchor)

    def cover(self):
        spec = self.spec
        section = self.word.sections[0]
        self.page(section)
        section.top_margin = section.bottom_margin = _mm(20)
        usable = self.page_height - 40.0 - 8.0  # Slack, so no renderer pushes the last row to a second page.
        holder = self.word.add_paragraph()  # The backdrop hangs from a paragraph outside the table.
        self.spacing(holder, 0, 0)
        from docx.enum.text import WD_LINE_SPACING
        holder.paragraph_format.line_spacing_rule = WD_LINE_SPACING.EXACTLY
        holder.paragraph_format.line_spacing = _pt(1)
        self.backdrop(holder, cover_art_png(spec.page, self.rtl))
        table = self.table(3, 1, [self.text_width])
        heights = (62.0, usable - 62.0 - 34.0, 34.0)
        for row, height in zip(table.rows, heights):
            self.row_rules(row, height_mm=height, exact=True)
        from docx.enum.table import WD_ALIGN_VERTICAL
        top, middle, bottom = (row.cells[0] for row in table.rows)
        paragraph = top.paragraphs[0]
        self.spacing(paragraph, before=14, after=0)
        if spec.organization:
            self.run(paragraph, spec.organization.upper(), size=8.5, color="D5DEF2", font=SEMIBOLD, tracking=1.2)
        middle.vertical_alignment = WD_ALIGN_VERTICAL.BOTTOM
        accent = middle.paragraphs[0]
        self.spacing(accent, 0, 12, line=1.0)
        self.run(accent, "━━", size=13, color="4D94FF", bold=True)
        title = middle.add_paragraph()
        self.spacing(title, 0, 0, line=1.02)
        self.run(title, spec.title, size=36, color="FFFFFF", bold=True, tracking=-0.8)
        if spec.subtitle:
            subtitle = middle.add_paragraph()
            self.spacing(subtitle, 16, 0, line=1.2)
            self.run(subtitle, spec.subtitle, size=14.5, color=COVER_TEXT)
        bottom.vertical_alignment = WD_ALIGN_VERTICAL.BOTTOM
        meta = bottom.paragraphs[0]
        self.spacing(meta, 0, 0)
        self.border(meta, "top", 0.5, "39466B", space=10)
        left = " · ".join(value for value in (spec.author, spec.organization) if value)
        self.tab_stop(meta, self.text_width)
        self.run(meta, left, size=9, color=COVER_TEXT)
        if spec.date:
            self.run(meta, "\t" + spec.date, size=9, color=COVER_TEXT)
        tail = self.word.add_paragraph()  # Word needs a paragraph after a table; keep it invisible.
        self.spacing(tail, 0, 0)
        self.run(tail, "", size=1)
        tail.paragraph_format.line_spacing = _pt(1)
        self.next_section()

    def clear_tabs(self, paragraph):
        """Remove the centre and right tab stops Word's Header and Footer styles carry."""
        tabs = _child(paragraph._p.get_or_add_pPr(), "w:tabs")
        for position in (4320, 4513, 4680, 8640, 9026, 9360):
            tabs.append(_element("w:tab", val="clear", pos=position))

    def tab_stop(self, paragraph, position_mm):
        from docx.enum.text import WD_TAB_ALIGNMENT
        paragraph.paragraph_format.tab_stops.add_tab_stop(_mm(position_mm), WD_TAB_ALIGNMENT.RIGHT)

    def contents(self):
        heading = self.word.add_paragraph()
        self.spacing(heading, 8, 14, line=1.05)
        self.run(heading, self.labels["contents"], size=24, color=NAVY, bold=True, tracking=-0.4)
        entries = [block for block in self.spec.blocks if type(block) is Heading and block.level <= 2]
        paragraph = self.word.add_paragraph()
        self.spacing(paragraph, 0, 0)
        # Shown until Word refreshes the field with page numbers on opening.
        self.field(paragraph, 'TOC \\o "1-2" \\h \\z \\u', dirty=True,
                   cached="\n".join(("    " if entry.level == 2 else "") + entry.text for entry in entries))
        self.next_section()

    def running_parts(self, section):
        section.header.is_linked_to_previous = False
        section.footer.is_linked_to_previous = False
        from docx.oxml.ns import qn
        section._sectPr.append(_element("w:pgNumType", start=1))
        header = section.header.paragraphs[0]
        self.spacing(header, 0, 0)
        self.clear_tabs(header)
        self.tab_stop(header, self.text_width)
        self.border(header, "bottom", 0.5, HAIR, space=5)
        title = self.spec.title if len(self.spec.title) <= 72 else self.spec.title[:70].rstrip() + "…"
        self.run(header, title, size=7.6, color=MUTED, font="Inter")
        self.run(header, "\t", size=7.6)
        self.field(header, 'STYLEREF "Heading 1"', size=7.6, color=MUTED)
        footer = section.footer.paragraphs[0]
        self.spacing(footer, 0, 0)
        self.clear_tabs(footer)
        self.tab_stop(footer, self.text_width)
        self.border(footer, "top", 0.5, HAIR, space=5)
        meta = " · ".join(v for v in (self.spec.organization, self.spec.author, self.spec.date) if v)
        self.run(footer, meta + "\t" + self.labels["page"] + " ", size=7.6, color=MUTED)
        self.field(footer, "PAGE", cached="1", size=7.6, color=MUTED)
        self.run(footer, " " + self.labels["of"] + " ", size=7.6, color=MUTED)
        self.field(footer, "SECTIONPAGES", cached="1", size=7.6, color=MUTED)
        assert section._sectPr.find(qn("w:pgNumType")) is not None

    def opening(self):
        spec = self.spec
        if not spec.cover:
            title = self.word.add_paragraph(style="Title")
            self.spacing(title, 0, 4, line=1.05)
            self.run(title, spec.title)
            if spec.subtitle:
                subtitle = self.word.add_paragraph()
                self.spacing(subtitle, 0, 10, line=1.2)
                self.run(subtitle, spec.subtitle, size=12.5, color=MUTED)
            rule = self.word.add_paragraph()
            self.spacing(rule, 0, 10)
            self.border(rule, "top", 0.75, RULE, space=1)
            rule.paragraph_format.line_spacing = _pt(4)
        if spec.summary:
            table = self.table(1, 1, [self.text_width])
            cell = table.rows[0].cells[0]
            self.cell_borders(cell, left=(2.25, BLUE))  # In a mirrored table "left" is the leading edge.
            self.cell_margins(cell, top=1, bottom=1, left=13, right=13)
            label = cell.paragraphs[0]
            self.spacing(label, 0, 4)
            self.run(label, self.labels["summary"].upper(), size=7.6, color=BLUE_DEEP, font=SEMIBOLD, tracking=0.8)
            body = cell.add_paragraph()
            self.spacing(body, 0, 0, line=1.3)
            self.run(body, spec.summary, size=11.4, color=INK)
            self.spacer(12)

    # --- blocks -----------------------------------------------------------------------------

    def rich(self, paragraph, runs, size=None, color=None):
        """Emphasis runs into a paragraph; a newline is a line break. Returns the text placed."""
        for run in runs:
            lines = run.text.split("\n")
            for index, line in enumerate(lines):
                if line:
                    placed = self.run(paragraph, line, size=(size or 10) * 0.9 if run.code else size,
                                      color=INK if run.bold and color is None else color, bold=run.bold,
                                      italic=run.italic, font=MONO if run.code else None)
                    if run.code:
                        placed._r.get_or_add_rPr().append(_element("w:shd", val="clear", color="auto", fill=PAPER))
                if index < len(lines) - 1:
                    paragraph.add_run().add_break()

    def paragraph(self, block):
        text = plain(block.runs)
        chunks, current = [], []
        for run in block.runs:  # A blank line starts a new paragraph.
            parts = run.text.split("\n\n")
            for index, part in enumerate(parts):
                if index:
                    chunks.append(current)
                    current = []
                if part:
                    current.append(type(run)(part, run.bold, run.italic, run.code))
        chunks.append(current)
        for chunk in chunks:
            if chunk:
                paragraph = self.word.add_paragraph()
                self.spacing(paragraph, 0, 7.5)
                self.direct(paragraph, text)
                self.rich(paragraph, chunk)

    def direct(self, paragraph, text):
        """Give a paragraph its own direction when its script runs against the document's."""
        found = direction(text)
        if found is not None and (found == "rtl") != self.rtl:
            p_pr = paragraph._p.get_or_add_pPr()
            from docx.oxml.ns import qn
            for stale in p_pr.findall(qn("w:bidi")):
                p_pr.remove(stale)
            if found == "rtl":
                p_pr.insert(0, _element("w:bidi"))
            else:
                p_pr.insert(0, _element("w:bidi", val="0"))

    def numbering(self, ordered):
        """A numbering instance: each ordered list restarts at 1 instead of continuing the previous list."""
        from docx.oxml.ns import qn
        style = self.word.styles["List Number" if ordered else "List Bullet"]
        base = style.element.pPr.numPr.numId.val
        if not ordered:
            return base
        numbering = self.word.part.numbering_part.element
        abstract = next(num for num in numbering.findall(qn("w:num"))
                        if num.get(qn("w:numId")) == str(base)).find(qn("w:abstractNumId")).get(qn("w:val"))
        new_id = max(int(num.get(qn("w:numId"))) for num in numbering.findall(qn("w:num"))) + 1
        num = _element("w:num", numId=new_id)
        num.append(_element("w:abstractNumId", val=abstract))
        override = _element("w:lvlOverride", ilvl=0)
        override.append(_element("w:startOverride", val=1))
        num.append(override)
        numbering.append(num)
        return new_id

    def list(self, block):
        number_id = self.numbering(block.ordered)
        style = "List Number" if block.ordered else "List Bullet"
        for index, item in enumerate(block.items):
            paragraph = self.word.add_paragraph(style=style)
            self.spacing(paragraph, 0, 7.5 if index == len(block.items) - 1 else 3.5)
            if block.ordered:
                num_pr = _element("w:numPr")
                num_pr.append(_element("w:ilvl", val=0))
                num_pr.append(_element("w:numId", val=number_id))
                paragraph._p.get_or_add_pPr().append(num_pr)
            self.rich(paragraph, item)

    def data_table(self, block):
        if block.caption:
            caption = self.word.add_paragraph()
            self.spacing(caption, 6, 5, keep_next=True)
            self.run(caption, block.caption, size=8.5, color=MUTED)
        columns = len(block.header)
        widths = self.column_widths(block)
        table = self.table(1 + len(block.rows), columns, widths)
        last = len(block.rows)
        for r_index, row in enumerate(table.rows):
            self.row_rules(row, header=r_index == 0)
            total = block.total_row and r_index == last
            values = block.header if r_index == 0 else block.rows[r_index - 1]
            for c_index, cell in enumerate(row.cells):
                numeric = block.align[c_index] == "right"
                self.cell_margins(cell, top=4.6, bottom=4.6, left=6.5, right=6.5)
                if r_index == 0:
                    self.cell_borders(cell, bottom=(1.0, NAVY))
                elif total:
                    self.cell_borders(cell, bottom=(0.75, NAVY))
                    self.shade(cell, PAPER)
                elif block.total_row and r_index == last - 1:
                    self.cell_borders(cell, bottom=(0.75, NAVY))  # The rule above the total belongs to this row.
                else:
                    self.cell_borders(cell, bottom=(0.75 if r_index == last else 0.5, RULE if r_index == last else HAIR))
                paragraph = cell.paragraphs[0]
                self.spacing(paragraph, 0, 0, line=1.18,
                             align="right" if numeric else ("center" if block.align[c_index] == "center" else None))
                value = values[c_index]
                if r_index == 0:
                    self.run(paragraph, value, size=7.6, color=NAVY, font=SEMIBOLD, tracking=0.35, caps=True)
                else:
                    if numeric and self.rtl:
                        self.direct(paragraph, "x")  # Quantities stay left-to-right.
                    else:
                        self.direct(paragraph, value)
                    lines = value.split("\n")
                    for l_index, line in enumerate(lines):
                        self.run(paragraph, line, size=9, color=INK if total or c_index == 0 else None,
                                 font=SEMIBOLD if total else None, ltr=numeric)
                        if l_index < len(lines) - 1:
                            paragraph.add_run().add_break()
        self.spacer(10)

    def column_widths(self, table):
        """Quantities take what they need; text columns share the rest by how much they hold."""
        weights, fixed = [], []
        for index, align in enumerate(table.align):
            longest = max([len(table.header[index])] + [len(row[index]) for row in table.rows])
            if align == "right" and longest <= 18:
                fixed.append(min(38.0, max(17.0, longest * 2.05 + 9.0)))
                weights.append(None)
            else:
                fixed.append(None)
                weights.append(min(3.2, max(1.0, (longest / 14) ** 0.5)))
        if all(weight is None for weight in weights):
            return [self.text_width / len(fixed)] * len(fixed)
        room = self.text_width - sum(width for width in fixed if width is not None)
        share = room / sum(weight for weight in weights if weight is not None)
        return [fixed[i] if weights[i] is None else weights[i] * share for i in range(len(fixed))]

    def callout(self, block):
        accent, deep, soft = TONES[block.tone]
        table = self.table(1, 1, [self.text_width])
        cell = table.rows[0].cells[0]
        self.shade(cell, soft)
        text = plain(block.runs)
        # A right-to-left callout in a left-to-right document is marked, and titled, from the right.
        against = not self.rtl and direction(text) == "rtl"
        self.cell_borders(cell, **{"right" if against else "left": (3.0, accent)})
        self.cell_margins(cell, top=8.5, bottom=9.5, left=12, right=12)
        self.row_rules(table.rows[0])
        paragraph = cell.paragraphs[0]
        if block.title:
            self.spacing(paragraph, 0, 3.5)
            self.direct(paragraph, text)
            self.run(paragraph, block.title, color=deep, font=SEMIBOLD)
            paragraph = cell.add_paragraph()
        self.spacing(paragraph, 0, 0)
        self.direct(paragraph, plain(block.runs))
        self.rich(paragraph, block.runs)
        self.spacer(10)

    def metrics(self, block):
        count = len(block.items)
        gap = 2.6
        widths = []
        card = (self.text_width - gap * (count - 1)) / count
        for index in range(count):
            widths.append(card)
            if index < count - 1:
                widths.append(gap)
        table = self.table(1, len(widths), widths)
        self.row_rules(table.rows[0])
        for index, item in enumerate(block.items):
            cell = table.rows[0].cells[index * 2]
            self.cell_borders(cell, top=(0.75, HAIR), bottom=(0.75, HAIR), left=(0.75, HAIR), right=(0.75, HAIR))
            self.cell_margins(cell, top=9, bottom=10, left=10.5, right=8)
            label = cell.paragraphs[0]
            self.spacing(label, 0, 3, line=1.1)
            self.run(label, item.label, size=7.3, color=MUTED, font="Inter", tracking=0.4, caps=True)
            value = cell.add_paragraph()
            self.spacing(value, 0, 2 if item.detail else 0, line=1.0)
            self.run(value, item.value, size=20, color=NAVY, bold=True, tracking=-0.4)
            if item.detail:
                detail = cell.add_paragraph()
                self.spacing(detail, 0, 0, line=1.1)
                color = {"up": "0B7A44", "down": "A1221A"}.get(item.trend, MUTED)
                mark = {"up": "▲ ", "down": "▼ ", "flat": "– "}.get(item.trend, "")
                self.run(detail, mark + item.detail, size=8, color=color)
        self.spacer(10)

    def chart(self, block):
        if block.title:
            title = self.word.add_paragraph()
            self.spacing(title, 8, 6, keep_next=True)
            self.run(title, block.title, size=10.5, color=NAVY, font=SEMIBOLD)
        svg = charts.chart_svg(block)
        width, height = (float(v) for v in re.search(r'viewBox="0 0 (\d+) (\d+)"', svg).groups())
        picture = self.word.add_paragraph()
        self.spacing(picture, 0, 0, keep_next=bool(block.caption))
        run = picture.add_run()
        shape = run.add_picture(io.BytesIO(svg_png(svg, width, height)), width=_mm(self.text_width))
        description = charts.describe(block)
        shape._inline.docPr.set("descr", description)
        shape._inline.docPr.set("title", block.title or "Chart")
        if block.caption:
            self.figures += 1
            caption = self.word.add_paragraph(style="Caption")
            self.run(caption, f"Figure {self.figures}  ", color=NAVY, font=SEMIBOLD)
            self.run(caption, block.caption)
        else:
            self.spacer(8)

    def quote(self, block):
        paragraph = self.word.add_paragraph()
        self.spacing(paragraph, 8, 4 if block.attribution else 12, line=1.25)
        paragraph.paragraph_format.left_indent = _pt(0 if self.rtl else 16)
        paragraph.paragraph_format.right_indent = _pt(16 if self.rtl else 20)
        self.border(paragraph, "right" if self.rtl else "left", 2.25, BLUE, space=12)
        self.direct(paragraph, block.text)
        self.run(paragraph, block.text, size=13, color=NAVY, font="Inter")
        if block.attribution:
            source = self.word.add_paragraph()
            self.spacing(source, 0, 12)
            source.paragraph_format.left_indent = _pt(0 if self.rtl else 16)
            self.border(source, "right" if self.rtl else "left", 2.25, BLUE, space=12)
            self.run(source, "— " + block.attribution, size=8.5, color=MUTED)

    def code(self, block):
        table = self.table(1, 1, [self.text_width])
        cell = table.rows[0].cells[0]
        self.shade(cell, PAPER)
        self.cell_borders(cell, top=(0.5, HAIR), bottom=(0.5, HAIR), left=(0.5, HAIR), right=(0.5, HAIR))
        self.cell_margins(cell, top=8, bottom=8, left=10, right=10)
        paragraph = cell.paragraphs[0]
        self.spacing(paragraph, 0, 0, line=1.2, align="left")
        self.direct(paragraph, "x")
        lines = block.text.split("\n")
        for index, line in enumerate(lines):
            run = self.run(paragraph, line, size=8.4, color=INK, font=MONO)
            run._r.find("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t").set(
                "{http://www.w3.org/XML/1998/namespace}space", "preserve")
            if index < len(lines) - 1:
                paragraph.add_run().add_break()
        self.spacer(10)

    def steps(self, block):
        table = self.table(len(block.items), 2, [9.0, self.text_width - 9.0])
        for index, (row, step) in enumerate(zip(table.rows, block.items)):
            self.row_rules(row)
            number, body = row.cells
            self.cell_margins(number, top=1, bottom=6, left=0, right=0)
            self.cell_margins(body, top=1, bottom=8, left=6, right=0)
            badge = number.paragraphs[0]
            self.spacing(badge, 0, 0, line=1.0, align="center")
            self.run(badge, str(index + 1), size=11, color=BLUE, bold=True)
            title = body.paragraphs[0]
            self.spacing(title, 0, 2 if step.text else 0, line=1.15)
            self.run(title, step.title, color=NAVY, font=SEMIBOLD)
            if step.text:
                text = body.add_paragraph()
                self.spacing(text, 0, 0, line=1.25)
                self.run(text, step.text, size=9.4)
        self.spacer(8)

    def comparison(self, block):
        count = len(block.columns)
        gap = 3.0
        widths = []
        card = (self.text_width - gap * (count - 1)) / count
        for index in range(count):
            widths.append(card)
            if index < count - 1:
                widths.append(gap)
        table = self.table(2, len(widths), widths)
        for row in table.rows:
            self.row_rules(row)
        for cell in table.rows[0].cells:
            self.spacing(cell.paragraphs[0], 0, 0, keep_next=True)
        for index, column in enumerate(block.columns):
            head, body = table.rows[0].cells[index * 2], table.rows[1].cells[index * 2]
            self.shade(head, BLUE_SOFT if index == 0 else PAPER)
            self.cell_borders(head, top=(0.75, HAIR), left=(0.75, HAIR), right=(0.75, HAIR))
            self.cell_borders(body, bottom=(0.75, HAIR), left=(0.75, HAIR), right=(0.75, HAIR))
            self.cell_margins(head, top=6.5, bottom=6.5, left=10.5, right=10.5)
            self.cell_margins(body, top=8, bottom=8, left=10.5, right=10.5)
            title = head.paragraphs[0]
            self.spacing(title, 0, 0, line=1.15, keep_next=True)  # The card heading stays with its body.
            self.run(title, column.title, color=NAVY, font=SEMIBOLD)
            for i_index, item in enumerate(column.items):
                paragraph = body.paragraphs[0] if i_index == 0 else body.add_paragraph()
                self.spacing(paragraph, 0, 3 if i_index < len(column.items) - 1 else 0, line=1.22)
                paragraph.paragraph_format.left_indent = _pt(9)
                paragraph.paragraph_format.first_line_indent = _pt(-9)
                self.run(paragraph, "•  ", size=9.2, color=BLUE)
                self.run(paragraph, item, size=9.2)
        self.spacer(10)

    def body(self):
        handlers = {Paragraph: self.paragraph, ListBlock: self.list, Table: self.data_table, Callout: self.callout,
                    Metrics: self.metrics, Chart: self.chart, Quote: self.quote, Code: self.code, Steps: self.steps,
                    Comparison: self.comparison}
        for block in self.spec.blocks:
            kind = type(block)
            if kind is Heading:
                heading = self.word.add_heading(level=block.level)
                self.direct(heading, block.text)
                if self.rtl:
                    self.spacing(heading, *{1: (22, 9), 2: (16, 6), 3: (13, 4)}[block.level], keep_next=True)
                self.run(heading, block.text)
            elif kind is PageBreak:
                self.word.add_page_break()
            else:
                handlers[kind](block)

    def build(self):
        spec = self.spec
        self.styles()
        properties = self.word.core_properties
        properties.title, properties.author = spec.title, spec.author or "Kalillac AI"
        properties.language = spec.language
        if spec.cover:
            self.cover()
        else:
            self.page(self.word.sections[0])
        if spec.contents:
            self.contents()
        self.running_parts(self.word.sections[-1])
        self.opening()
        self.body()
        normalize(self.word.element)
        normalize(self.word.styles.element)
        normalize(self.word.part.numbering_part.element)
        for section in self.word.sections:
            normalize(section.header._element)
            normalize(section.footer._element)
        # This document's own record in place of python-docx's starter template (see package.py).
        package = self.word.part.package
        own_record(properties, spec.author or "Kalillac AI", spec.summary or "")
        drop_relationships(package.rels, "thumbnail")
        drop_relationships(self.word.part.rels, "customXml")
        application_record(package.rels, Company=spec.organization)
        buffer = io.BytesIO()
        self.word.save(buffer)
        return buffer.getvalue()


# --- embedded fonts -------------------------------------------------------------------------

_CHARSETS = {"IBM Plex Sans Arabic": "B2", "Noto Sans Arabic": "B2", "Noto Sans Hebrew": "B1"}


def _font_facts(file_name):
    """The PANOSE classification and Unicode/code-page signature of a bundled font, as the font
    table declares them. Word decides from the signature whether a font can draw a script; without
    it an embedded Arabic or Hebrew face is ignored and another font substituted."""
    from fontTools.ttLib import TTFont
    font = TTFont(str(FONTS / file_name), lazy=True)
    try:
        os2 = font["OS/2"]
        panose = "".join(f"{getattr(os2.panose, name):02X}" for name in (
            "bFamilyType", "bSerifStyle", "bWeight", "bProportion", "bContrast", "bStrokeVariation", "bArmStyle",
            "bLetterForm", "bMidline", "bXHeight"))
        signature = " ".join(f'w:{name}="{value & 0xFFFFFFFF:08X}"' for name, value in (
            ("usb0", os2.ulUnicodeRange1), ("usb1", os2.ulUnicodeRange2), ("usb2", os2.ulUnicodeRange3),
            ("usb3", os2.ulUnicodeRange4), ("csb0", getattr(os2, "ulCodePageRange1", 0)),
            ("csb1", getattr(os2, "ulCodePageRange2", 0))))
    finally:
        font.close()
    return panose, signature


def _obfuscate(font, key):
    """ECMA-376 font obfuscation: the first 32 bytes XORed with the GUID key, in reverse byte order."""
    mask = bytes.fromhex(key.strip("{}").replace("-", ""))[::-1]
    data = bytearray(font)
    for index in range(32):
        data[index] ^= mask[index % 16]
    return bytes(data)


def _complex_face(text):
    """The embedded right-to-left face for text's script, or None."""
    if any("\u0600" <= char <= "\u06ff" or "\u0750" <= char <= "\u077f" or "\u08a0" <= char <= "\u08ff"
           or "\ufb50" <= char <= "\ufdff" or "\ufe70" <= char <= "\ufeff" for char in text):
        return "IBM Plex Sans Arabic"
    if any("\u0590" <= char <= "\u05ff" or "\ufb1d" <= char <= "\ufb4f" for char in text):
        return "Noto Sans Hebrew"
    return None


def embed_fonts(docx, complex_font=None, extra=()):
    """The same .docx with the Inter faces (and every right-to-left face it uses) embedded and declared
    in its font table."""
    source = zipfile.ZipFile(io.BytesIO(docx))
    out = io.BytesIO()
    families, relationships, parts = {}, [], {}
    faces = [face for face in dict.fromkeys((complex_font, *sorted(extra))) if face in COMPLEX]
    chosen = [entry for face in faces for entry in COMPLEX[face]]
    for index, (family, file_name, slot) in enumerate((*EMBEDDED, *chosen), start=1):
        key = "{" + str(uuid.uuid5(uuid.NAMESPACE_URL, "kalillac-font/" + file_name)).upper() + "}"
        parts[f"word/fonts/font{index}.odttf"] = _obfuscate((FONTS / file_name).read_bytes(), key)
        relationships.append(f'<Relationship Id="rIdFont{index}" Type="http://schemas.openxmlformats.org/'
                             f'officeDocument/2006/relationships/font" Target="fonts/font{index}.odttf"/>')
        families.setdefault(family, []).append(f'<w:{slot} r:id="rIdFont{index}" w:fontKey="{key}"/>')
    facts = {}
    for family, file_name, slot in (*EMBEDDED, *chosen):
        if slot == "embedRegular":
            facts[family] = _font_facts(file_name)
    fonts = "".join(f'<w:font w:name="{family}"><w:altName w:val="Segoe UI"/><w:panose1 w:val="{facts[family][0]}"/>'
                    f'<w:charset w:val="{_CHARSETS.get(family, "00")}"/><w:family w:val="swiss"/>'
                    f'<w:pitch w:val="variable"/><w:sig {facts[family][1]}/>{"".join(slots)}</w:font>'
                    for family, slots in families.items())
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for entry in source.infolist():
            data = source.read(entry.filename)
            if entry.filename == "word/fontTable.xml":
                text = data.decode("utf-8")
                text = re.sub(r'<w:font w:name="(Inter( SemiBold)?|Noto Sans (Arabic|Hebrew)|IBM Plex Sans Arabic)"'
                              r'.*?</w:font>', "", text,
                              flags=re.S)
                if "xmlns:r=" not in text.split(">", 2)[1]:
                    text = text.replace("<w:fonts ", "<w:fonts xmlns:r=\"http://schemas.openxmlformats.org/"
                                        "officeDocument/2006/relationships\" ", 1)
                data = text.replace("</w:fonts>", fonts + "</w:fonts>").encode("utf-8")
            elif entry.filename == "word/settings.xml":
                text = data.decode("utf-8")
                if "<w:embedTrueTypeFonts" not in text:
                    anchor = re.search(r"<w:zoom[^>]*/>", text)
                    position = anchor.end() if anchor else text.index(">", text.index("<w:settings")) + 1
                    text = text[:position] + "<w:embedTrueTypeFonts/>" + text[position:]
                # Current layout rules (Word 2013 and later): tables sit on the margin they are given,
                # and the file does not open in "Compatibility Mode".
                mode = ('<w:compatSetting w:name="compatibilityMode" w:uri="http://schemas.microsoft.com/office/word" '
                        'w:val="15"/>')
                existing = re.compile(r'<w:compatSetting w:name="compatibilityMode"[^>]*/>')
                if existing.search(text):
                    text = existing.sub(mode, text, count=1)  # In place: compat settings have a fixed position.
                elif "</w:compat>" in text:
                    text = text.replace("</w:compat>", mode + "</w:compat>", 1)
                else:
                    text = text.replace("</w:settings>", "<w:compat>" + mode + "</w:compat></w:settings>", 1)
                data = text.encode("utf-8")
            elif entry.filename == "[Content_Types].xml":
                text = data.decode("utf-8")
                if 'Extension="odttf"' not in text:
                    text = text.replace("<Default ", '<Default Extension="odttf" ContentType="application/'
                                        'vnd.openxmlformats-officedocument.obfuscatedFont"/><Default ', 1)
                data = text.encode("utf-8")
            target.writestr(entry, data)
        target.writestr("word/_rels/fontTable.xml.rels",
                        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://'
                        'schemas.openxmlformats.org/package/2006/relationships">' + "".join(relationships)
                        + "</Relationships>")
        for name, data in parts.items():
            target.writestr(name, data)
    return out.getvalue()


def write_docx(document):
    writer = _Writer(document)
    return fixed_dates(embed_fonts(writer.build(), writer.complex, writer.complex_used))
