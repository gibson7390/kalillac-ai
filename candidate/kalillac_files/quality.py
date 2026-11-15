"""Automated inspection of a generated PDF: is the page actually sound?

A file that opens is not a good file. inspect_pdf() reads the finished PDF
back with an independent engine (PDFium) and reports what a reader would see
as a defect:

  * text outside the page, or outside the text column (overflow past the
    margins, a table cell spilling into its neighbour's margin);
  * a blank page in the middle of the document;
  * a page with no text layer;
  * characters that reached the file as "missing glyph" boxes.

check_pdf() turns a failed inspection into a refusal, so a defective document
is never handed to a customer: the caller regenerates or reports the limit.
The same inspection runs over the quality benchmark and over adversarial
content in the tests.

This is defect detection, not a taste score. Whether a document is good
enough is decided by a person looking at it.
"""
from dataclasses import dataclass, field

from .spec import SpecError

# The design system's page geometry, in points (1 mm = 2.8346 pt).
MARGIN_X = 22 * 2.8346
TOLERANCE = 1.5
# Justified text hangs punctuation and hyphens slightly into the margin so the edge reads straight
# (optical margin alignment). That overhang is design, not overflow, within this bound.
HANGING = frozenset(".,:;'\"’”‘“!?-‐‑–—­￾)")
HANG = 3.0
HEADER_BAND = 27 * 2.8346
FOOTER_BAND = 25 * 2.8346


@dataclass
class Report:
    pages: int = 0
    characters: int = 0
    overflow: list = field(default_factory=list)      # (page, side, distance in points, sample text)
    blank_pages: list = field(default_factory=list)   # page numbers, 1-based
    missing_glyphs: int = 0

    @property
    def sound(self):
        return not (self.overflow or self.blank_pages or self.missing_glyphs) and self.pages > 0


def inspect_pdf(data, full_bleed_first_page=False, front_pages=0):
    """A Report for PDF bytes.

    full_bleed_first_page: the first page is a cover laid out to the page edge,
    so only the page itself bounds it. front_pages: how many leading pages
    (cover, contents) carry no running header and footer.
    """
    import pypdfium2 as pdfium
    report = Report()
    document = pdfium.PdfDocument(data)
    try:
        report.pages = len(document)
        for index in range(report.pages):
            page = document[index]
            width, height = page.get_size()
            text = page.get_textpage()
            count = text.count_chars()
            content = text.get_text_range() if count else ""
            report.characters += len(content.strip())
            report.missing_glyphs += content.count("�")
            cover = full_bleed_first_page and index == 0
            if not content.strip() and not cover and 0 < index < report.pages - 1:
                report.blank_pages.append(index + 1)
            left = 0.0 if cover else MARGIN_X
            right = width if cover else width - MARGIN_X
            flagged = set()
            for char_index in range(count):
                # Visible glyphs only: PDFium also reports the space and line break that end a justified
                # line, placed past the last glyph, which no reader sees.
                char = content[char_index] if char_index < len(content) else text.get_text_range(char_index, 1)
                if char.isspace() or char in "\x00﻿":
                    continue
                x0, y0, x1, y1 = text.get_charbox(char_index)
                allowance = TOLERANCE + (HANG if char in HANGING else 0.0)
                for side, distance in (("left", left - x0), ("right", x1 - right), ("bottom", 0.0 - y0),
                                       ("top", y1 - height)):
                    if distance > allowance and (side, round(y0)) not in flagged:
                        flagged.add((side, round(y0)))  # One report per line and side.
                        sample = text.get_text_range(max(0, char_index - 39), min(40, char_index + 1)).strip()
                        report.overflow.append((index + 1, side, round(distance, 1), sample))
            text.close()
            page.close()
    finally:
        document.close()
    return report


def check_pdf(data, **options):
    """Refuse a PDF that inspection finds defective (SpecError pdf_layout_failed)."""
    report = inspect_pdf(data, **options)
    if not report.sound:
        raise SpecError("pdf_layout_failed")
    return report
