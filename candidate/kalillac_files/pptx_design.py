"""PowerPoint decks in the Kalillac design system: native, editable, and measured to fit.

write_pptx(deck) builds a 16:9 deck with python-pptx. Every element is a native
PowerPoint object (text boxes, shapes, tables, charts), so the deck is fully
editable. The layouts carry the design: dark title, section and closing slides
with the brand artwork; light content slides with a running label, a fitted
title, an accent rule and a numbered footer.

Text never overflows. Each piece of text is wrapped and measured with the font
metrics of Carlito, which is metric-compatible with Calibri (the face the deck
uses, present wherever Office or Google Slides is), and set at the largest size
from its range that fits its box. Boxes are placed from those measurements, so
what follows a two-line title moves down rather than overlapping it. Text that
would not fit even at its smallest size is refused (slide_text_too_long); the
deck limits in deck.py are set so that real content does not get there.

Charts are native PowerPoint charts with their data embedded (editable in
PowerPoint), styled in the deck palette. Speaker notes are carried through.
"""
from dataclasses import dataclass
from functools import lru_cache
import io
import re
import unicodedata

from .package import application_record, drop_relationships, fixed_dates, own_record
from .spec import SpecError

WIDTH, HEIGHT = 13.333, 7.5
MARGIN = 0.75
CONTENT = WIDTH - 2 * MARGIN
BODY_TOP, BODY_BOTTOM = 2.2, 6.7
NAVY, BLUE, BLUE_DEEP, BLUE_SOFT, INK, BODY, MUTED = "0B1737", "0069FF", "0053CF", "EAF3FF", "0E1526", "1D2538", \
    "5C677E"
HAIR, RULE, PAPER, COVER_TEXT, LIGHT_BLUE = "E2E9F4", "CDD8EA", "F6F8FC", "C3D1EE", "6EA8FF"
SERIES = ("0069FF", "0B1737", "12A15C", "F59F00", "7C5CFF", "E8590C")
TRENDS = {"up": ("▲ ", "0B7A44"), "down": ("▼ ", "A1221A"), "flat": ("– ", MUTED)}
FONT = "Calibri"
LINE = 1.22  # Calibri's line height as a multiple of its size.
RTL = frozenset({"ar", "he", "fa", "ur", "yi", "ps", "sd", "ug"})
EAST_ASIAN = {"ja": "Yu Gothic", "ko": "Malgun Gothic", "zh-hant": "Microsoft JhengHei", "zh-tw": "Microsoft JhengHei",
              "zh-hk": "Microsoft JhengHei", "zh": "Microsoft YaHei"}


# --- measurement --------------------------------------------------------------------------


@lru_cache(maxsize=2)
def _advances(bold):
    """Each character's advance width in ems, from the font's own tables. PowerPoint does not kern
    text boxes by default, so neither does the measurement."""
    from fontTools.ttLib import TTFont
    from .typeset import FONTS
    font = TTFont(FONTS / "metrics" / ("Carlito-Bold.ttf" if bold else "Carlito-Regular.ttf"), lazy=True)
    em, metrics = font["head"].unitsPerEm, font["hmtx"].metrics
    return {code: metrics[glyph][0] / em for code, glyph in font.getBestCmap().items()}


# Ems, for a character Calibri lacks: PowerPoint sets it in another face (Arial for Arabic and Hebrew, whose
# letters average 0.50-0.55 em there), so this errs wide: such text gets a roomier box, never an overflow.
_FALLBACK = 0.6


def _wide(char):
    return unicodedata.east_asian_width(char) in ("W", "F")


def width(text, size, bold=False):
    """Width in inches of text set in Calibri at size points."""
    advances = _advances(bold)
    ems = sum(1.0 if _wide(char) else advances.get(ord(char), _FALLBACK) for char in text)  # Full width: one em.
    return ems * size / 72


_TOKEN = re.compile(r"\s+|[⺀-鿿가-힯豈-﫿＀-￯]|[^\s⺀-鿿가-힯"
                    r"豈-﫿＀-￯]+")


TEXT_SLACK = 0.01  # Inches a text box keeps in hand, so a line that only just fits by measurement still fits.
CELL_SLACK = 0.05  # A table cell needs up to 0.03 in more than its text and margins (measured in PowerPoint).


def wrap(text, box_width, size, bold=False):
    """Lines as PowerPoint would break them: at spaces, or anywhere in Chinese, Japanese and Korean."""
    box_width -= TEXT_SLACK
    lines, current = [], ""
    for token in _TOKEN.findall(text):
        candidate = current + token
        if width(candidate.rstrip(), size, bold) <= box_width:
            current = candidate
            continue
        if current.strip():
            lines.append(current.rstrip())
        current = token.lstrip()
        while current and width(current, size, bold) > box_width:  # One word longer than the line.
            cut = len(current)
            while cut > 1 and width(current[:cut], size, bold) > box_width:
                cut -= 1
            lines.append(current[:cut])
            current = current[cut:]
    if current.strip() or not lines:
        lines.append(current.rstrip())
    return lines


def balanced(text, box_width, size, bold=False):
    """The narrowest width that sets text in as few lines as box_width does, so a heading's lines are
    of even length instead of a full line followed by a stray word."""
    lines = len(wrap(text, box_width, size, bold))
    if lines < 2:
        return box_width
    low, high = box_width * 0.5, box_width
    while high - low > 0.02:
        middle = (low + high) / 2
        low, high = (low, middle) if len(wrap(text, middle, size, bold)) <= lines else (middle, high)
    return high


@dataclass
class Fit:
    size: float
    lines: int

    def height(self, spacing=1.0):
        return self.lines * self.size * LINE * spacing / 72


def fit(text, box_width, box_height, largest, smallest, bold=False, spacing=1.0, max_lines=None):
    """The largest size in [smallest, largest], in half points, at which text fits the box."""
    size = largest
    while True:
        lines = len(wrap(text, box_width, size, bold))
        if lines * size * LINE * spacing / 72 <= box_height and (max_lines is None or lines <= max_lines):
            return Fit(size, lines)
        if size <= smallest:
            raise SpecError("slide_text_too_long")
        size = max(smallest, size - 0.5)


def fit_all(texts, box_width, box_height, largest, smallest, bold=False, spacing=1.0, gap=0.0):
    """One size for a group of paragraphs (a bulleted list) so they read as a set."""
    size = largest
    while True:
        lines = [len(wrap(text, box_width, size, bold)) for text in texts]
        total = sum(lines) * size * LINE * spacing / 72 + gap * size / 72 * (len(texts) - 1)
        if total <= box_height:
            return size, lines
        if size <= smallest:
            raise SpecError("slide_text_too_long")
        size = max(smallest, size - 0.5)


# --- chart data ---------------------------------------------------------------------------


def _chart_data():
    """python-pptx's chart data, embedding a workbook whose date is fixed (the ZIP epoch, which XlsxWriter
    already uses for its entries) instead of the time of writing, so the deck is byte-for-byte deterministic."""
    from contextlib import contextmanager
    from datetime import datetime
    from pptx.chart.data import CategoryChartData
    from pptx.chart.xlsx import CategoryWorkbookWriter
    from xlsxwriter import Workbook

    class Writer(CategoryWorkbookWriter):
        @contextmanager
        def _open_worksheet(self, xlsx_file):
            workbook = Workbook(xlsx_file, {"in_memory": True})
            workbook.set_properties({"created": datetime(1980, 1, 1), "author": "Kalillac AI"})
            yield workbook, workbook.add_worksheet()
            workbook.close()

    class Data(CategoryChartData):
        @property
        def _workbook_writer(self):
            return Writer(self)

    return Data()


# --- the writer ---------------------------------------------------------------------------


class _Writer:
    def __init__(self, deck):
        from pptx import Presentation
        from pptx.util import Inches
        self.deck = deck
        self.prs = Presentation()
        self.prs.slide_width, self.prs.slide_height = Inches(WIDTH), Inches(HEIGHT)
        self.blank = self.prs.slide_layouts[6]
        primary = deck.language.split("-")[0].lower()
        self.rtl = primary in RTL
        lowered = deck.language.lower()
        self.east = next((font for prefix, font in EAST_ASIAN.items()
                          if lowered == prefix or lowered.startswith(prefix + "-")), "Microsoft YaHei")
        self.section = None
        self.sections = 0
        self.number = 0
        self.boxes = []  # (slide number, name, left, top, right, bottom) of every placed element, for checks.

    # --- primitives -----------------------------------------------------------------------

    def _color(self, value):
        from pptx.dml.color import RGBColor
        return RGBColor.from_string(value)

    def x(self, left, box_width):
        """A box's left edge, mirrored for right-to-left decks, so the layouts read from the right."""
        return WIDTH - left - box_width if self.rtl else left

    def note_box(self, name, left, top, box_width, box_height):
        self.boxes.append((self.number, name, left, top, left + box_width, top + box_height))

    def text(self, slide, text, left, top, box_width, box_height, size, color=BODY, bold=False, align="left",
             anchor="top", name="text", spacing=1.0, italic=False, balance=False):
        from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
        from pptx.util import Inches
        if balance:  # Narrow the box to even out the lines, keeping the edge the text is aligned to.
            narrower = balanced(text, box_width, size, bold)
            if self.rtl and align == "left":
                left += box_width - narrower
            box_width = narrower
        box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(box_width), Inches(box_height))
        box.name = name
        frame = box.text_frame
        frame.word_wrap = True
        frame.auto_size = MSO_AUTO_SIZE.NONE
        frame.margin_left = frame.margin_right = frame.margin_top = frame.margin_bottom = 0
        frame.vertical_anchor = {"top": MSO_ANCHOR.TOP, "middle": MSO_ANCHOR.MIDDLE,
                                 "bottom": MSO_ANCHOR.BOTTOM}[anchor]
        paragraph = frame.paragraphs[0]
        if self.rtl and align == "left":
            align = "right"
        paragraph.alignment = {"left": PP_ALIGN.LEFT, "right": PP_ALIGN.RIGHT, "center": PP_ALIGN.CENTER}[align]
        paragraph.line_spacing = spacing
        if self.rtl:
            paragraph._p.get_or_add_pPr().set("rtl", "1")
        run = paragraph.add_run()
        run.text = text
        self.font(run.font, size, color, bold, italic)
        self.note_box(name, left, top, box_width, box_height)
        return box

    def font(self, font, size, color, bold=False, italic=False):
        from pptx.oxml.ns import qn
        from pptx.util import Pt
        font.name = FONT
        font.size = Pt(size)
        font.bold = bold
        font.italic = italic
        font.color.rgb = self._color(color)
        r_pr = font._element
        for tag, face in (("a:ea", self.east), ("a:cs", "Arial")):
            existing = r_pr.find(qn(tag))
            if existing is None:
                from lxml import etree
                existing = etree.SubElement(r_pr, qn(tag))
            existing.set("typeface", face)

    def shape(self, slide, kind, left, top, box_width, box_height, fill, line=None, name="shape"):
        from pptx.enum.shapes import MSO_SHAPE
        from pptx.util import Inches, Pt
        shape = slide.shapes.add_shape({"rect": MSO_SHAPE.RECTANGLE, "round": MSO_SHAPE.ROUNDED_RECTANGLE,
                                        "oval": MSO_SHAPE.OVAL}[kind],
                                       Inches(left), Inches(top), Inches(box_width), Inches(box_height))
        shape.name = name
        shape.shadow.inherit = False
        if fill is None:
            shape.fill.background()
        else:
            shape.fill.solid()
            shape.fill.fore_color.rgb = self._color(fill)
        if line is None:
            shape.line.fill.background()
        else:
            shape.line.color.rgb = self._color(line[1])
            shape.line.width = Pt(line[0])
        if kind == "round":
            shape.adjustments[0] = min(0.5, 0.09 / max(0.1, min(box_width, box_height)))
        shape.text_frame.text = ""
        self.note_box(name, left, top, box_width, box_height)
        return shape

    def new_slide(self, notes=None):
        self.number += 1
        slide = self.prs.slides.add_slide(self.blank)
        if notes:
            slide.notes_slide.notes_text_frame.text = notes
        return slide

    def background(self, slide, color):
        slide.background.fill.solid()
        slide.background.fill.fore_color.rgb = self._color(color)

    def artwork(self, slide):
        from pptx.util import Inches
        from .typeset import cover_art_png
        picture = slide.shapes.add_picture(io.BytesIO(cover_art_png("wide", self.rtl, ppi=150)), 0, 0,
                                           Inches(WIDTH), Inches(HEIGHT))
        picture.name = "Background artwork"

    # --- frame of a content slide ---------------------------------------------------------

    def frame(self, slide, title):
        """Running label, fitted title, accent rule and footer. Returns the top of the body area."""
        label = (self.section or self.deck.title).upper()
        label = label if width(label, 11) <= CONTENT else self.clip(label, CONTENT, 11)
        self.text(slide, label, MARGIN, 0.42, CONTENT, 0.25, 11, MUTED, name="Running label")
        title_fit = fit(title, CONTENT, 1.2, 32, 22, bold=True, spacing=0.92, max_lines=2)
        title_height = title_fit.height(0.92)
        self.text(slide, title, MARGIN, 0.78, CONTENT, title_height + 0.05, title_fit.size, NAVY, bold=True,
                  name="Title", balance=True, spacing=0.92)
        rule_top = 0.78 + title_height + 0.16
        self.shape(slide, "rect", MARGIN if not self.rtl else WIDTH - MARGIN - 0.6, rule_top, 0.6, 0.06, BLUE,
                   name="Accent rule")
        self.footer(slide)
        return max(BODY_TOP, rule_top + 0.4)

    def footer(self, slide, dark=False):
        color = COVER_TEXT if dark else MUTED
        if not dark:
            self.shape(slide, "rect", MARGIN, 6.92, CONTENT, 0.01, HAIR, name="Footer rule")
        title = self.clip(self.deck.title, CONTENT - 1.5, 10)
        self.text(slide, title, self.x(MARGIN, CONTENT - 1.0), 7.0, CONTENT - 1.0, 0.22, 10, color,
                  name="Footer title")
        self.text(slide, str(self.number), self.x(WIDTH - MARGIN - 0.8, 0.8), 7.0, 0.8, 0.22, 10, color,
                  align="left" if self.rtl else "right", name="Slide number")

    @staticmethod
    def clip(text, box_width, size, bold=False):
        """Text shortened with an ellipsis to fit one line."""
        box_width -= TEXT_SLACK
        if width(text, size, bold) <= box_width:
            return text
        while text and width(text + "…", size, bold) > box_width:
            text = text[:-1]
        return text.rstrip() + "…"

    # --- layouts --------------------------------------------------------------------------

    def title_slide(self, slide_spec, closing=False):
        slide = self.new_slide(slide_spec.notes)
        self.background(slide, NAVY)
        self.artwork(slide)
        column = 6.3  # The reading side of the slide; the artwork owns the other.
        left = MARGIN if not self.rtl else WIDTH - MARGIN - column
        top = 2.35 if not closing else 2.7
        if self.deck.organization and not closing:
            self.text(slide, self.clip(self.deck.organization.upper(), column, 12, bold=True), left, 0.85, column,
                      0.3, 12, "D5DEF2", bold=True, name="Organization")
        self.shape(slide, "rect", left if not self.rtl else left + column - 0.6, top - 0.32, 0.6, 0.07, LIGHT_BLUE,
                   name="Accent rule")
        title_fit = fit(slide_spec.title, column, 2.3, 46 if not closing else 40, 26, bold=True, spacing=0.9,
                        max_lines=4)
        self.text(slide, slide_spec.title, left, top, column, title_fit.height(0.9) + 0.05, title_fit.size,
                  "FFFFFF", bold=True, name="Title", balance=True, spacing=0.9)
        cursor = top + title_fit.height(0.9) + 0.3
        if slide_spec.subtitle:
            sub_fit = fit(slide_spec.subtitle, column, 1.4, 22, 15, max_lines=4)
            self.text(slide, slide_spec.subtitle, left, cursor, column, sub_fit.height() + 0.05, sub_fit.size,
                      COVER_TEXT, name="Subtitle", balance=True)
        meta = " · ".join(v for v in (self.deck.author, self.deck.date) if v)
        if meta and not closing:
            self.shape(slide, "rect", left, 6.45, column, 0.01, "39466B", name="Meta rule")
            self.text(slide, self.clip(meta, column, 13), left, 6.6, column, 0.3, 13, COVER_TEXT, name="Byline")

    def section_slide(self, slide_spec):
        self.sections += 1
        self.section = slide_spec.title
        slide = self.new_slide(slide_spec.notes)
        self.background(slide, NAVY)
        self.text(slide, f"{self.sections:02d}", self.x(MARGIN, 2.0), 2.0, 2.0, 0.6, 30, LIGHT_BLUE, bold=True,
                  name="Number")
        title_fit = fit(slide_spec.title, 10.0, 2.0, 44, 28, bold=True, spacing=0.92, max_lines=3)
        self.text(slide, slide_spec.title, self.x(MARGIN, 10.0), 2.85, 10.0, title_fit.height(0.92) + 0.05,
                  title_fit.size, "FFFFFF", bold=True, name="Title", balance=True, spacing=0.92)
        if slide_spec.subtitle:
            sub_fit = fit(slide_spec.subtitle, 10.0, 1.2, 20, 15, max_lines=3)
            self.text(slide, slide_spec.subtitle, self.x(MARGIN, 10.0), 2.85 + title_fit.height(0.92) + 0.3, 10.0,
                      sub_fit.height() + 0.05, sub_fit.size, COVER_TEXT, name="Subtitle", balance=True)
        self.footer(slide, dark=True)

    def bullets(self, slide, points, left, top, box_width, box_height, largest, smallest, name="Point"):
        indent = 0.36
        size, lines = fit_all(points, box_width - indent, box_height, largest, smallest, gap=0.9)
        cursor = top
        for point, count in zip(points, lines):
            line_height = size * LINE / 72
            dot = size * 0.32 / 72
            dot_left = left if not self.rtl else left + box_width - dot
            self.shape(slide, "oval", dot_left, cursor + line_height / 2 - dot / 2, dot, dot, BLUE,
                       name=name + " marker")
            text_left = left + indent if not self.rtl else left
            self.text(slide, point, text_left, cursor, box_width - indent, count * line_height + 0.04, size, BODY,
                      name=name)
            cursor += count * line_height + 0.9 * size / 72
        return cursor

    def points_slide(self, slide_spec):
        slide = self.new_slide(slide_spec.notes)
        top = self.frame(slide, slide_spec.title)
        column = CONTENT if any(width(p, 22) > 5.0 for p in slide_spec.points) else 8.6
        if slide_spec.lead:
            # The lead gives way first: the largest lead size that still leaves the list room to fit.
            largest = 22
            while True:
                lead_fit = fit(slide_spec.lead, CONTENT, 1.1, largest, 16, max_lines=3)
                body = top + lead_fit.height() + 0.35
                try:
                    fit_all(slide_spec.points, column - 0.36, BODY_BOTTOM - body, 24, 13, gap=0.9)
                    break
                except SpecError:
                    if lead_fit.size <= 16:
                        raise
                    largest = lead_fit.size - 0.5
            self.text(slide, slide_spec.lead, MARGIN, top, CONTENT, lead_fit.height() + 0.05, lead_fit.size, MUTED,
                      name="Lead", balance=True)
            top = body
        self.bullets(slide, slide_spec.points, self.x(MARGIN, column), top, column, BODY_BOTTOM - top, 24, 13)

    def columns_slide(self, slide_spec):
        slide = self.new_slide(slide_spec.notes)
        top = self.frame(slide, slide_spec.title)
        gap = 0.4
        card = (CONTENT - gap) / 2
        height = BODY_BOTTOM - top
        inner = card - 0.7
        sizes = []
        for column in (slide_spec.left, slide_spec.right):
            size, _ = fit_all(column.points, inner - 0.36, height - 1.35, 20, 13, gap=0.9)
            sizes.append(size)
        size = min(sizes)
        for index, column in enumerate((slide_spec.left, slide_spec.right)):
            left = self.x(MARGIN + index * (card + gap), card)
            self.shape(slide, "round", left, top, card, height, PAPER if index else BLUE_SOFT, name="Card")
            self.shape(slide, "rect", left + 0.35 if not self.rtl else left + card - 0.8, top + 0.35, 0.45, 0.05,
                       BLUE if index == 0 else NAVY, name="Card rule")
            heading_fit = fit(column.heading, inner, 0.5, 22, 15, bold=True, max_lines=1)
            self.text(slide, column.heading, left + 0.35, top + 0.5, inner, 0.45, heading_fit.size, NAVY, bold=True,
                      name="Card heading")
            self.bullets(slide, column.points, left + 0.35, top + 1.15, inner, height - 1.35, size, size)

    def metrics_slide(self, slide_spec):
        slide = self.new_slide(slide_spec.notes)
        top = self.frame(slide, slide_spec.title)
        count = len(slide_spec.metrics)
        gap = 0.3
        card = (CONTENT - gap * (count - 1)) / count
        inner = card - 0.6
        value_size = min(fit(m.value, inner, 1.0, 54 if count <= 3 else 46, 26, bold=True, max_lines=1).size
                         for m in slide_spec.metrics)
        label_size = min(fit(m.label.upper(), inner, 0.48, 14, 10, max_lines=2).size for m in slide_spec.metrics)
        detail_size = min([fit(m.detail, inner - 0.25, 0.6, 15, 11, max_lines=2).size
                           for m in slide_spec.metrics if m.detail] or [15])
        height = 2.5
        card_top = top + 0.3 if slide_spec.note else top + (BODY_BOTTOM - top - height) / 2 - 0.2
        for index, metric in enumerate(slide_spec.metrics):
            left = self.x(MARGIN + index * (card + gap), card)
            self.shape(slide, "round", left, card_top, card, height, "FFFFFF", line=(1.0, HAIR), name="Metric card")
            self.shape(slide, "rect", left, card_top + height - 0.07, card, 0.07, BLUE, name="Metric rule")
            self.text(slide, metric.label.upper(), left + 0.3, card_top + 0.3, inner, 0.48, label_size, MUTED,
                      name="Metric label")
            self.text(slide, metric.value, left + 0.3, card_top + 0.8, inner, value_size * LINE / 72 + 0.05,
                      value_size, NAVY, bold=True, name="Metric value")
            if metric.detail:
                mark, color = TRENDS.get(metric.trend, ("", MUTED))
                self.text(slide, mark + metric.detail, left + 0.3, card_top + 0.85 + value_size * LINE / 72, inner,
                          0.6, detail_size, color, name="Metric detail")
        if slide_spec.note:
            note_fit = fit(slide_spec.note, CONTENT, BODY_BOTTOM - card_top - height - 0.4, 18, 13, max_lines=3)
            self.text(slide, slide_spec.note, MARGIN, card_top + height + 0.4, CONTENT, note_fit.height() + 0.05,
                      note_fit.size, MUTED, name="Note")

    def chart_slide(self, slide_spec):
        slide = self.new_slide(slide_spec.notes)
        top = self.frame(slide, slide_spec.title)
        chart_width = CONTENT if not slide_spec.takeaway else 7.85
        self.native_chart(slide, slide_spec.chart, self.x(MARGIN, chart_width), top - 0.1, chart_width,
                          BODY_BOTTOM - top + 0.1)
        if slide_spec.takeaway:
            left = MARGIN + chart_width + 0.45
            panel = WIDTH - MARGIN - left
            self.shape(slide, "rect", self.x(left, 0.06), top + 0.2, 0.06, BODY_BOTTOM - top - 0.4, BLUE,
                       name="Takeaway rule")
            self.text(slide, "KEY TAKEAWAY", self.x(left + 0.35, panel - 0.35), top + 0.25, panel - 0.35, 0.3, 12,
                      BLUE_DEEP, bold=True, name="Takeaway label")
            take_fit = fit(slide_spec.takeaway, panel - 0.35, BODY_BOTTOM - top - 1.1, 26, 16, spacing=1.0)
            self.text(slide, slide_spec.takeaway, self.x(left + 0.35, panel - 0.35), top + 0.7, panel - 0.35,
                      take_fit.height() + 0.05, take_fit.size, NAVY, name="Takeaway", balance=True)

    def native_chart(self, slide, chart_spec, left, top, box_width, box_height):
        from pptx.enum.chart import XL_CHART_TYPE, XL_LABEL_POSITION, XL_LEGEND_POSITION, XL_MARKER_STYLE
        from pptx.util import Inches, Pt
        from . import charts as chart_math
        kinds = {"column": XL_CHART_TYPE.COLUMN_CLUSTERED, "bar": XL_CHART_TYPE.BAR_CLUSTERED,
                 "stacked": XL_CHART_TYPE.COLUMN_STACKED, "line": XL_CHART_TYPE.LINE_MARKERS,
                 "area": XL_CHART_TYPE.AREA, "donut": XL_CHART_TYPE.DOUGHNUT}
        data = _chart_data()
        data.categories = list(chart_spec.categories)
        known = [v for s in chart_spec.series for v in s.values if v is not None]
        step = chart_math.nice_scale(min(known), max(known))[2]
        decimals = 0 if all(float(v).is_integer() for v in known) and step >= 1 else (1 if step >= 0.1 else 2)
        def literal(affix):  # Quoted text in a number format; a quote mark itself must be escaped instead.
            if not affix:
                return ""
            return "".join("\\" + char for char in affix) if '"' in affix else f'"{affix}"'

        def styled(places):
            body = "#,##0" + ("." + "0" * places if places else "")
            return literal(chart_spec.prefix) + body + literal(chart_spec.suffix)

        number_format = styled(decimals)                       # Values, as precise as the data.
        axis_format = styled(0 if step >= 1 else (1 if step >= 0.1 else 2))  # Ticks, as precise as the step.
        for series in chart_spec.series:
            data.add_series(series.name, list(series.values), number_format)
        frame = slide.shapes.add_chart(kinds[chart_spec.kind], Inches(left), Inches(top), Inches(box_width),
                                       Inches(box_height), data)
        frame.name = "Chart"
        self.note_box("Chart", left, top, box_width, box_height)
        chart = frame.chart
        chart.has_title = False  # The slide title names the chart; PowerPoint would repeat the series name.
        chart.font.name = FONT
        chart.font.size = Pt(13)
        chart.font.color.rgb = self._color(MUTED)
        multi = len(chart_spec.series) > 1 or chart_spec.kind == "donut"
        chart.has_legend = multi
        if multi:
            chart.legend.position = XL_LEGEND_POSITION.RIGHT if chart_spec.kind == "donut" else \
                XL_LEGEND_POSITION.BOTTOM
            chart.legend.include_in_layout = False
            chart.legend.font.size = Pt(13)
        plot = chart.plots[0]
        if chart_spec.kind == "donut":
            series = plot.series[0]
            for index, point in enumerate(series.points):
                point.format.fill.solid()
                point.format.fill.fore_color.rgb = self._color(SERIES[index % len(SERIES)])
                point.format.line.color.rgb = self._color("FFFFFF")
            plot.has_data_labels = True
            labels = plot.data_labels
            labels.show_percentage, labels.show_value, labels.number_format_is_linked = True, False, False
            labels.number_format = "0%"
            labels.font.size, labels.font.color.rgb, labels.font.bold = Pt(13), self._color("FFFFFF"), True
            hole = plot._element.find("{http://schemas.openxmlformats.org/drawingml/2006/chart}holeSize")
            if hole is not None:
                hole.set("val", "58")
            return
        if chart_spec.kind in ("column", "bar", "stacked"):
            plot.gap_width = 70 if len(chart_spec.series) > 1 else 55
            if chart_spec.kind != "stacked":
                plot.overlap = -8 if len(chart_spec.series) > 1 else 0
        for index, series in enumerate(plot.series):
            color = self._color(SERIES[index % len(SERIES)])
            if chart_spec.kind in ("line",):
                series.format.line.color.rgb = color
                series.format.line.width = Pt(2.75)
                series.smooth = False
                series.marker.style = XL_MARKER_STYLE.CIRCLE
                series.marker.size = 7
                series.marker.format.fill.solid()
                series.marker.format.fill.fore_color.rgb = self._color("FFFFFF")
                series.marker.format.line.color.rgb = color
                series.marker.format.line.width = Pt(2)
            else:
                series.format.fill.solid()
                series.format.fill.fore_color.rgb = color
                series.format.line.fill.background()
                if chart_spec.kind == "area" and len(chart_spec.series) > 1:
                    from pptx.oxml.ns import qn
                    solid = series.format.fill._xPr.find(qn("a:solidFill"))
                    if solid is not None and len(solid):
                        alpha = solid[0].makeelement(qn("a:alpha"), {"val": "75000"})
                        solid[0].append(alpha)
        if len(chart_spec.series) == 1 and len(chart_spec.categories) <= 12 and chart_spec.kind != "area":
            plot.has_data_labels = True
            labels = plot.data_labels
            labels.number_format, labels.number_format_is_linked = number_format, False
            labels.font.size, labels.font.color.rgb, labels.font.bold = Pt(13), self._color(INK), True
            labels.position = XL_LABEL_POSITION.OUTSIDE_END if chart_spec.kind in ("column", "bar") \
                else XL_LABEL_POSITION.ABOVE
        value_axis, category_axis = chart.value_axis, chart.category_axis
        value_axis.has_major_gridlines = True
        value_axis.major_gridlines.format.line.color.rgb = self._color(HAIR)
        value_axis.major_gridlines.format.line.width = Pt(0.75)
        value_axis.format.line.fill.background()
        value_axis.tick_labels.number_format, value_axis.tick_labels.number_format_is_linked = axis_format, False
        value_axis.tick_labels.font.size = Pt(12)
        if len(chart_spec.series) == 1 and len(chart_spec.categories) <= 12 and chart_spec.kind in ("column", "bar"):
            value_axis.visible = False  # The bars carry their own labels; the axis would repeat them.
            value_axis.has_major_gridlines = False
        category_axis.format.line.color.rgb = self._color(RULE)
        category_axis.tick_labels.font.size = Pt(12)
        category_axis.has_major_gridlines = False
        from pptx.enum.chart import XL_TICK_LABEL_POSITION, XL_TICK_MARK
        category_axis.major_tick_mark = XL_TICK_MARK.NONE
        category_axis.tick_label_position = XL_TICK_LABEL_POSITION.LOW
        if chart_spec.kind == "bar":
            category_axis.reverse_order = True
        value_axis.major_tick_mark = XL_TICK_MARK.NONE

    def table_slide(self, slide_spec):
        from pptx.util import Inches
        slide = self.new_slide(slide_spec.notes)
        top = self.frame(slide, slide_spec.title)
        columns = len(slide_spec.header)
        rows = [slide_spec.header] + list(slide_spec.rows)
        size = 18.0
        while True:
            padding = size / 75  # 0.24 in at 18 pt: the cell's air shrinks with its type.
            numeric = [align == "right" for align in slide_spec.align]
            strong = [r == 0 or (slide_spec.total_row and r == len(rows) - 1) for r in range(len(rows))]
            natural = []
            for i in range(columns):
                if numeric[i]:  # Numbers on one line; the header only needs its longest word.
                    widest = max([width(row[i], size, strong[r]) for r, row in enumerate(rows) if r] +
                                 [width(word, size, True) for word in rows[0][i].split()])
                else:
                    widest = max(width(row[i], size, strong[r]) for r, row in enumerate(rows))
                natural.append(widest + 2 * padding + CELL_SLACK + TEXT_SLACK)
            fixed = sum(w for w, n in zip(natural, numeric) if n)
            text_columns = [i for i in range(columns) if not numeric[i]]
            room = CONTENT - fixed
            if text_columns:
                share = sum(natural[i] for i in text_columns)
                widths = [natural[i] if numeric[i] else max(1.2, room * natural[i] / share) for i in range(columns)]
            else:
                widths = [CONTENT * w / sum(natural) for w in natural]
            scale = CONTENT / sum(widths)
            widths = [w * scale for w in widths]
            inner = [w - 2 * padding - CELL_SLACK for w in widths]
            heights = [max(len(wrap(row[i], inner[i], size, strong[r])) for i in range(columns))
                       * size * LINE / 72 + 0.24 for r, row in enumerate(rows)]
            fits = sum(heights) <= BODY_BOTTOM - top and all(
                width(row[i], size, strong[r]) <= inner[i] - TEXT_SLACK or not numeric[i] or r == 0
                for r, row in enumerate(rows) for i in range(columns))
            if fits:
                break
            if size <= 11:
                raise SpecError("slide_text_too_long")
            size -= 0.5
        shape = slide.shapes.add_table(len(rows), columns, Inches(MARGIN), Inches(top), Inches(CONTENT),
                                       Inches(sum(heights)))
        shape.name = "Table"
        self.note_box("Table", MARGIN, top, CONTENT, sum(heights))
        table = shape.table
        table.first_row = True
        table.horz_banding = False
        if self.rtl:
            table._tbl.tblPr.set("rtl", "1")  # The first column on the right.
        for i, column_width in enumerate(widths):
            table.columns[i].width = Inches(column_width)
        last = len(rows) - 1
        from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
        for r, row in enumerate(rows):
            table.rows[r].height = Inches(heights[r])
            for i, value in enumerate(row):
                cell = table.cell(r, i)
                header, total = r == 0, slide_spec.total_row and r == last
                cell.fill.solid()
                cell.fill.fore_color.rgb = self._color(NAVY if header else (PAPER if total else "FFFFFF"))
                cell.margin_left = cell.margin_right = Inches(padding)
                cell.margin_top = cell.margin_bottom = Inches(0.08)
                cell.vertical_anchor = MSO_ANCHOR.MIDDLE
                paragraph = cell.text_frame.paragraphs[0]
                # Numbers align to the cell's far edge, text to its reading edge; both mirror for right to left.
                paragraph.alignment = PP_ALIGN.RIGHT if numeric[i] != self.rtl else PP_ALIGN.LEFT
                if self.rtl:
                    paragraph._p.get_or_add_pPr().set("rtl", "1")
                run = paragraph.add_run()
                run.text = value
                self.font(run.font, size * (0.9 if header else 1.0), "FFFFFF" if header else (INK if total or i == 0
                                                                                               else BODY),
                          bold=header or total)
                self.cell_border(cell, bottom=(1.5, NAVY) if (header or total or (slide_spec.total_row and
                                                                                   r == last - 1))
                                 else (0.75, HAIR))

    def cell_border(self, cell, bottom):
        from pptx.oxml.ns import qn
        tc_pr = cell._tc.get_or_add_tcPr()
        for side in ("a:lnL", "a:lnR", "a:lnT", "a:lnB"):
            existing = tc_pr.find(qn(side))
            if existing is not None:
                tc_pr.remove(existing)
        for side in ("a:lnL", "a:lnR", "a:lnT"):
            line = tc_pr.makeelement(qn(side), {"w": "0"})
            line.append(line.makeelement(qn("a:noFill"), {}))
            tc_pr.insert(0, line) if side == "a:lnL" else tc_pr.append(line)
        line = tc_pr.makeelement(qn("a:lnB"), {"w": str(int(bottom[0] * 12700))})
        solid = line.makeelement(qn("a:solidFill"), {})
        solid.append(solid.makeelement(qn("a:srgbClr"), {"val": bottom[1]}))
        line.append(solid)
        tc_pr.append(line)
        # Schema order: lnL, lnR, lnT, lnB, then fill.
        children = list(tc_pr)
        order = {qn("a:lnL"): 0, qn("a:lnR"): 1, qn("a:lnT"): 2, qn("a:lnB"): 3}
        for child in children:
            tc_pr.remove(child)
        for child in sorted(children, key=lambda c: order.get(c.tag, 10)):
            tc_pr.append(child)

    def quote_slide(self, slide_spec):
        slide = self.new_slide(slide_spec.notes)
        self.background(slide, PAPER)
        top = 2.35
        self.text(slide, "“", self.x(MARGIN - 0.05, 1.5), 0.9, 1.5, top - 0.9, 140, BLUE, bold=True,
                  name="Quote mark")
        quote_fit = fit(slide_spec.quote, 10.6, 3.2, 38, 24, spacing=1.0)
        column = self.x(MARGIN + 0.15, 10.6)
        self.text(slide, slide_spec.quote, column, top, 10.6, quote_fit.height() + 0.05, quote_fit.size, NAVY,
                  name="Quote", balance=True)
        if slide_spec.attribution:
            attribution = "— " + slide_spec.attribution
            by_fit = fit(attribution, 10.6, 0.65, 18, 14, max_lines=2)
            self.text(slide, attribution, column, top + quote_fit.height() + 0.4, 10.6, by_fit.height() + 0.05,
                      by_fit.size, MUTED, name="Attribution")
        self.footer(slide)

    def timeline_slide(self, slide_spec):
        slide = self.new_slide(slide_spec.notes)
        top = self.frame(slide, slide_spec.title)
        count = len(slide_spec.steps)
        slot = CONTENT / count
        circle = 0.62
        line_y = top + 0.75
        self.shape(slide, "rect", MARGIN + slot / 2, line_y - 0.015, slot * (count - 1), 0.03, RULE, name="Line")
        inner = slot - 0.3
        title_size = min(fit(s.title, inner, 0.85, 20, 13, bold=True, max_lines=2).size for s in slide_spec.steps)
        text_size = min([fit(s.text, inner, BODY_BOTTOM - line_y - 1.6, 16, 11).size
                         for s in slide_spec.steps if s.text] or [16])
        for index, step in enumerate(slide_spec.steps):
            center = MARGIN + slot * (index + 0.5)
            center = WIDTH - center if self.rtl else center
            self.shape(slide, "oval", center - circle / 2, line_y - circle / 2, circle, circle, BLUE, name="Step")
            self.text(slide, str(index + 1), center - circle / 2, line_y - circle / 2, circle, circle, 18, "FFFFFF",
                      bold=True, align="center", anchor="middle", name="Step number")
            title_lines = len(wrap(step.title, inner, title_size, True))
            self.text(slide, step.title, center - inner / 2, line_y + 0.6, inner,
                      title_lines * title_size * LINE / 72 + 0.05, title_size, NAVY, bold=True, align="center",
                      name="Step title")
            if step.text:
                lines = len(wrap(step.text, inner, text_size))
                self.text(slide, step.text, center - inner / 2, line_y + 0.75 + title_lines * title_size * LINE / 72,
                          inner, lines * text_size * LINE / 72 + 0.05, text_size, BODY, align="center",
                          name="Step text")

    def statement_slide(self, slide_spec):
        slide = self.new_slide(slide_spec.notes)
        column = 10.4
        text_fit = fit(slide_spec.text, column, 3.6, 48, 30, bold=True, spacing=0.95)
        height = text_fit.height(0.95)
        top = (HEIGHT - height) / 2 - 0.2
        left = self.x(MARGIN, column)
        if slide_spec.eyebrow:
            self.text(slide, self.clip(slide_spec.eyebrow.upper(), column, 15, bold=True), left, top - 0.65, column,
                      0.35, 15, BLUE_DEEP, bold=True, name="Eyebrow")
        self.shape(slide, "rect", self.x(MARGIN, 0.6), top - 0.22, 0.6, 0.07, BLUE, name="Accent rule")
        self.text(slide, slide_spec.text, left, top, column, height + 0.05, text_fit.size, NAVY, bold=True,
                  name="Statement", balance=True, spacing=0.95)
        self.footer(slide)

    def clean_package(self):
        """This deck's own record in place of python-pptx's starter template (see package.py)."""
        package = self.prs.part.package
        own_record(self.prs.core_properties, self.deck.author or "Kalillac AI")
        drop_relationships(package._rels, "thumbnail")
        drop_relationships(self.prs.part.rels, "printerSettings")
        application_record(package._rels, PresentationFormat="Widescreen", Slides=len(self.prs.slides),
                           Notes=sum(1 for slide in self.prs.slides if slide.has_notes_slide), HiddenSlides=0,
                           Company=self.deck.organization)

    def build(self):
        properties = self.prs.core_properties
        properties.title = self.deck.title
        properties.author = self.deck.author or "Kalillac AI"
        properties.language = self.deck.language
        handlers = {"points": self.points_slide, "two_columns": self.columns_slide, "metrics": self.metrics_slide,
                    "chart": self.chart_slide, "table": self.table_slide, "quote": self.quote_slide,
                    "timeline": self.timeline_slide, "statement": self.statement_slide,
                    "section": self.section_slide}
        for slide_spec in self.deck.slides:
            if slide_spec.layout == "title":
                self.title_slide(slide_spec)
            elif slide_spec.layout == "closing":
                self.title_slide(slide_spec, closing=True)
            else:
                handlers[slide_spec.layout](slide_spec)
        self.clean_package()
        buffer = io.BytesIO()
        self.prs.save(buffer)
        return fixed_dates(buffer.getvalue())


def write_pptx(deck):
    return _Writer(deck).build()


def layout_boxes(deck):
    """Every placed element's box, in inches: (slide number, name, left, top, right, bottom)."""
    writer = _Writer(deck)
    writer.build()
    return writer.boxes
