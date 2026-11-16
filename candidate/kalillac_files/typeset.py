"""PDF typesetting: a validated Document through the Typst design template.

The document is handed to Typst as JSON data (sys.inputs), never as markup:
the template places every string with text(...), so nothing a document says
can be interpreted as layout code, read a file or reach the network. Only
the bundled, hash-verified fonts are visible to the compiler (system fonts
are ignored), which makes the output the same on every machine.

Typst shapes text with HarfBuzz-compatible shaping and applies the Unicode
bidirectional algorithm, embeds subsetted fonts and writes a tagged PDF.
What it cannot do is draw a character no bundled font contains, so coverage
is checked first and such a document is refused, never rendered with blank
boxes.
"""
from functools import lru_cache
import json
from pathlib import Path
import unicodedata

from . import charts
from .spec import (_NUMERIC, Callout, Chart, Code, Comparison, Heading, ListBlock, Metrics, PageBreak, Paragraph,
                   Quote, SpecError, Steps, Table, plain)

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "templates" / "document.typ"
FONTS = HERE / "fonts"
FONT_PATHS = (FONTS, FONTS / "cjk")
RTL = frozenset({"ar", "he", "fa", "ur", "yi", "ps", "sd", "dv", "ug"})
# Arabic script is set in IBM Plex Sans Arabic, which draws every letter in every joining form as its
# own glyph. Noto Sans Arabic builds letters from shared dotless bodies plus separate dot glyphs, so a
# PDF glyph-to-text map cannot tell them apart and extraction, search and copy return wrong or
# doubled letters (Codex document audit, finding 5). Noto stays as the fallback for anything Plex
# lacks.
SCRIPT_FONTS = ("IBM Plex Sans Arabic", "Noto Sans Arabic", "Noto Sans Hebrew", "Noto Sans Devanagari",
                "Noto Sans Thai")
CJK = {"ja": "Noto Sans JP", "ko": "Noto Sans KR", "zh-hant": "Noto Sans TC", "zh-tw": "Noto Sans TC",
       "zh-hk": "Noto Sans TC", "zh-mo": "Noto Sans TC", "zh": "Noto Sans SC"}
LABELS = {
    "en": ("Contents", "Page", "of", "Summary"), "es": ("Contenido", "Página", "de", "Resumen"),
    "fr": ("Sommaire", "Page", "sur", "Résumé"), "de": ("Inhalt", "Seite", "von", "Zusammenfassung"),
    "pt": ("Sumário", "Página", "de", "Resumo"), "it": ("Indice", "Pagina", "di", "Sintesi"),
    "nl": ("Inhoud", "Pagina", "van", "Samenvatting"),
    "ar": ("المحتويات", "صفحة", "من",
           "الملخص"),
    "he": ("תוכן עניינים", "עמוד",
           "מתוך", "תקציר"),
    "ja": ("目次", "ページ", "/", "概要"),
    "zh": ("目录", "第", "/", "摘要"),
    "ko": ("목차", "페이지", "/", "요약"),
    "hi": ("विषय सूची", "पृष्ठ", "/",
           "सारांश"),
}
# Characters that take no glyph of their own: joiners, direction marks, variation selectors, soft hyphen.
_INVISIBLE = frozenset("\n\t\r­​‌‍‎‏  ‪‫‬‭‮⁠"
                       "⁦⁧⁨⁩﻿") | frozenset(chr(c) for c in range(0xFE00, 0xFE10))


def _cjk_order(language):
    lowered = language.lower()
    first = next((font for prefix, font in CJK.items() if lowered == prefix or lowered.startswith(prefix + "-")),
                 "Noto Sans SC")
    return (first, *(font for font in ("Noto Sans SC", "Noto Sans JP", "Noto Sans TC", "Noto Sans KR")
                     if font != first))


def font_stacks(language, theme):
    fallbacks = (*SCRIPT_FONTS, *_cjk_order(language))
    return {"body": ["Source Serif 4" if theme == "editorial" else "Inter", "Inter", *fallbacks],
            "display": ["Inter Display", "Inter", *fallbacks],
            "mono": ["JetBrains Mono", "Inter", *fallbacks]}


@lru_cache(maxsize=1)
def coverage():
    """Every code point some bundled font can draw."""
    from fontTools.ttLib import TTFont
    covered = set()
    for folder in FONT_PATHS:
        for path in sorted(folder.glob("*.[ot]tf")) if folder.is_dir() else ():
            font = TTFont(str(path), lazy=True, fontNumber=0)
            try:
                covered.update(font.getBestCmap() or ())
            finally:
                font.close()
    if not covered:
        raise SpecError("fonts_unavailable")
    return frozenset(covered)


def uncovered(texts):
    """The characters in `texts` that no bundled font contains."""
    covered = coverage()
    return {char for text in texts for char in text
            if ord(char) not in covered and char not in _INVISIBLE and not char.isspace()}


def direction(text):
    """"rtl" or "ltr" from the first strongly directional character, or None when there is none."""
    for char in text:
        kind = unicodedata.bidirectional(char)
        if kind in ("R", "AL"):
            return "rtl"
        if kind == "L":
            return "ltr"
    return None


def _own_direction(text, rtl_document, numeric=False):
    """The direction a piece of text needs when it differs from the document's.

    A quantity in a right-to-left document is always left-to-right: without
    that, the bidirectional algorithm moves its sign or currency to the far side.
    """
    if numeric and rtl_document:
        return "ltr"
    found = direction(text)
    if found is None or (found == "rtl") == rtl_document:
        return None
    return found


_BREAK_AFTER = frozenset("/\\._-?&=:;,+@#%|)]}")
_SOFT = "\u200b"
TEXT_WIDTH = {"a4": (210.0 - 44.0) * 2.8346, "letter": (215.9 - 44.0) * 2.8346}


def soften(text, limit=20, every=16):
    """text with invisible break opportunities inside tokens too long to fit a line or a cell.

    The typesetter only breaks lines at spaces. A URL, a file path, a long identifier or a
    very long number would otherwise run straight through the margin. Breaks go after
    separators first, then every sixteen characters. Scripts that already break anywhere
    (Chinese, Japanese, Korean) are left alone.
    """
    if not text or len(text) <= limit:
        return text
    out = []
    for token in text.split(" "):
        if len(token) <= limit or any(unicodedata.east_asian_width(char) in ("W", "F") for char in token):
            out.append(token)
            continue
        pieces, run = [], 0
        for index, char in enumerate(token):
            pieces.append(char)
            run += 1
            more = index < len(token) - 1
            if more and (char in _BREAK_AFTER or run >= every):
                if char in _BREAK_AFTER and run < min(4, every):
                    continue  # Not after every dot of "a.b.c".
                pieces.append(_SOFT)
                run = 0
        out.append("".join(pieces))
    return " ".join(out)


def _fit_size(texts, width, start, minimum, weight="regular"):
    """The largest size from start down to minimum at which every text fits width (points)."""
    size = start
    while size > minimum and any(charts.text_width(text, size, weight) > width for text in texts):
        size = round(size - 0.5, 2)
    return size


def _runs(runs):
    """Runs for the template, with newlines made explicit: a blank line starts a paragraph."""
    out = []
    for run in runs:
        paragraphs = run.text.split("\n\n")
        for p_index, paragraph in enumerate(paragraphs):
            if p_index:
                out.append({"par": True})
            for l_index, line in enumerate(paragraph.split("\n")):
                if l_index:
                    out.append({"br": True})
                if line:
                    out.append({"text": soften(line), "bold": run.bold, "italic": run.italic, "code": run.code})
    return out


def _table_layout(table, available):
    """(column widths, font size) for a table.

    Quantities take their natural width and text columns share the rest by how much they hold.
    When the quantities alone would not leave room, the type steps down; if they still do not
    fit at the smallest size, every column becomes proportional and long values wrap.
    """
    columns = len(table.header)
    size = 9.0 if columns <= 6 else 8.2 if columns <= 9 else 7.4
    numeric = [align == "right" and max([len(table.header[i])] + [len(row[i]) for row in table.rows]) <= 22
               for i, align in enumerate(table.align)]
    text_columns = columns - sum(numeric)

    def natural(at):
        return sum(max([charts.text_width(table.header[i].upper(), at * 0.85, "semibold")]
                       + [charts.text_width(row[i], at) for row in table.rows]) + 17 for i in range(columns)
                   if numeric[i])
    while size > 6.5 and natural(size) > available - 70 * text_columns:
        size = round(size - 0.4, 2)
    if natural(size) > available - 70 * text_columns:
        return [1] * columns, size  # Even the smallest size is too wide: share the space and wrap.
    widths = []
    for index in range(columns):
        longest = max([len(table.header[index])] + [len(row[index]) for row in table.rows])
        widths.append("auto" if numeric[index] else round(min(3.2, max(1.0, (longest / 14) ** 0.5)), 2))
    if all(width == "auto" for width in widths):
        widths[0] = 1
    return widths, size


def _texts(document):
    yield from (v for v in (document.title, document.subtitle, document.summary, document.author,
                            document.organization, document.date) if v)
    for block in document.blocks:
        kind = type(block)
        if kind is Heading:
            yield block.text
        elif kind in (Paragraph, Callout):
            yield plain(block.runs)
            if kind is Callout and block.title:
                yield block.title
        elif kind is ListBlock:
            yield from block.texts
        elif kind is Table:
            yield from block.header
            yield from (cell for row in block.rows for cell in row)
            if block.caption:
                yield block.caption
        elif kind is Metrics:
            for item in block.items:
                yield from (v for v in (item.label, item.value, item.detail) if v)
        elif kind is Chart:
            yield from block.categories
            yield from (series.name for series in block.series)
            yield from (v for v in (block.title, block.caption, block.prefix, block.suffix) if v)
        elif kind in (Quote, Code):
            yield block.text
            if kind is Quote and block.attribution:
                yield block.attribution
        elif kind is Steps:
            for item in block.items:
                yield item.title
                if item.text:
                    yield item.text
        elif kind is Comparison:
            for column in block.columns:
                yield column.title
                yield from column.items


def _block(block, rtl, available):
    kind = type(block)
    if kind is Heading:
        short = block.text if len(block.text) <= 60 else block.text[:58].rstrip() + "\u2026"
        return {"t": "heading", "level": block.level, "text": soften(block.text), "short": short,
                "dir": _own_direction(block.text, rtl)}
    if kind is Paragraph:
        return {"t": "paragraph", "runs": _runs(block.runs), "dir": _own_direction(plain(block.runs), rtl)}
    if kind is ListBlock:
        return {"t": "list", "ordered": block.ordered,
                "items": [{"runs": _runs(item), "dir": _own_direction(plain(item), rtl)} for item in block.items]}
    if kind is Table:
        dirs = [[_own_direction(cell, rtl, numeric=bool(cell.strip()) and _NUMERIC.fullmatch(cell) is not None)
                 for cell in row] for row in block.rows]
        widths, size = _table_layout(block, available)
        # How many characters fit a line of each column: the break interval for unbreakable tokens.
        inset = 9.0 if len(widths) > 8 else 16.0
        share = sum(w for w in widths if w != "auto")
        auto_room = sum(max([charts.text_width(row[i], size) for row in block.rows] or [0]) + inset
                        for i, w in enumerate(widths) if w == "auto")
        per_char = size * 0.62
        fits = [99 if w == "auto" else max(3, int(((available - auto_room) * w / share - inset) / per_char))
                for w in widths]
        return {"t": "table", "header": [soften(cell, min(12, fits[i]), fits[i]) for i, cell in enumerate(block.header)],
                "rows": [[soften(cell, min(14, fits[i]), fits[i]) for i, cell in enumerate(row)] for row in block.rows],
                "align": list(block.align), "widths": widths, "size": size, "caption": soften(block.caption or "") or None,
                "total": block.total_row,
                # Kept on one page when it is short enough that it can never be taller than one.
                "keep": len(block.rows) <= 15 and sum(len(cell) for row in block.rows for cell in row) <= 1_200,
                "dirs": [[cell or "" for cell in row] for row in dirs] if any(any(row) for row in dirs) else None}
    if kind is PageBreak:
        return {"t": "pagebreak"}
    if kind is Callout:
        return {"t": "callout", "tone": block.tone, "title": soften(block.title or "") or None,
                "runs": _runs(block.runs), "dir": _own_direction(plain(block.runs), rtl)}
    if kind is Metrics:
        inner = (available - 9 * (len(block.items) - 1)) / len(block.items) - 27
        return {"t": "metrics", "size": _fit_size([m.value for m in block.items], inner, 21.0, 10.0, "semibold"),
                "items": [{"label": soften(m.label, 14), "value": soften(m.value, 12),
                           "detail": soften(m.detail or "", 14) or None, "trend": m.trend} for m in block.items]}
    if kind is Chart:
        if charts.check_bounds(block):
            raise SpecError("chart_layout_failed")
        return {"t": "chart", "svg": charts.chart_svg(block), "alt": charts.describe(block),
                "title": soften(block.title or "") or None, "caption": soften(block.caption or "") or None}
    if kind is Quote:
        return {"t": "quote", "text": soften(block.text), "attribution": soften(block.attribution or "") or None,
                "dir": _own_direction(block.text, rtl)}
    if kind is Code:
        # Code keeps its characters exactly (no hidden breaks): the type steps down so the
        # longest line fits, and only a line longer than any size can hold is wrapped.
        longest = max(len(line) for line in block.text.split("\n"))
        size = 8.4
        while size > 6.2 and longest * size * 0.6 > available - 24:
            size = round(size - 0.3, 2)
        room = max(20, int((available - 24) / (size * 0.6)) - 1)
        lines = []
        for line in block.text.splitlines():
            if len(line) > room and max(len(token) for token in line.split(" ")) > room:
                line = " ".join(token if len(token) <= room else _SOFT.join(
                    token[i:i + room] for i in range(0, len(token), room)) for token in line.split(" "))
            lines.append(line)
        return {"t": "code", "text": chr(10).join(lines), "language": block.language, "size": size}
    if kind is Steps:
        return {"t": "steps", "items": [{"title": soften(s.title), "text": soften(s.text or "") or None}
                                        for s in block.items]}
    if kind is Comparison:
        return {"t": "comparison", "columns": [{"title": soften(c.title, 14), "items": [soften(i, 14) for i in c.items]}
                                               for c in block.columns]}
    raise SpecError("unknown_block")


def document_data(document):
    """The JSON-ready data the template renders."""
    primary = document.language.split("-")[0].lower()
    labels = LABELS.get(primary, LABELS["en"])
    title = document.title
    available = TEXT_WIDTH[document.page]

    def clip(value, length):
        return value if value is None or len(value) <= length else value[:length - 1].rstrip() + "\u2026"
    return {
        "title": soften(title), "plain-title": title, "short-title": clip(title, 60),
        "meta-author": document.author or "Kalillac AI",
        "subtitle": soften(document.subtitle or "") or None, "summary": soften(document.summary or "") or None,
        "author": soften(document.author or "") or None,
        "organization": soften(document.organization or "") or None, "date": document.date,
        # The running footer has one line: each part is clipped to its share of it.
        "footer": [clip(v, 42) for v in (document.organization, document.author, document.date) if v],
        "lang": primary, "rtl": primary in RTL, "theme": document.theme, "page": document.page,
        "cover": document.cover, "contents": document.contents,
        "fonts": font_stacks(document.language, document.theme),
        "labels": dict(zip(("contents", "page", "of", "summary"), labels)),
        "blocks": [_block(block, primary in RTL, available) for block in document.blocks],
    }


def _strip_none(value):
    if type(value) is dict:
        return {key: _strip_none(item) for key, item in value.items() if item is not None}
    if type(value) is list:
        return [_strip_none(item) for item in value]
    return value


def typeset(document, format="pdf", ppi=None):
    """The document typeset by Typst: PDF bytes, or a list of PNG pages when format is "png"."""
    import typst
    if uncovered(_texts(document)):
        raise SpecError("pdf_unsupported_characters")
    payload = json.dumps(_strip_none(document_data(document)), ensure_ascii=False, separators=(",", ":"))
    options = {"root": str(TEMPLATE.parent), "font_paths": [str(path) for path in FONT_PATHS if path.is_dir()],
               "ignore_system_fonts": True, "sys_inputs": {"doc": payload}, "format": format}
    if ppi is not None:
        options["ppi"] = ppi
    try:
        return typst.compile(str(TEMPLATE), **options)
    except typst.TypstError:
        raise SpecError("pdf_layout_failed") from None


def svg_png(svg, width, height, ppi=220):
    """One of our own chart SVGs as PNG bytes, drawn with the bundled fonts."""
    import typst
    try:
        png = typst.compile(str(TEMPLATE.parent / "figure.typ"), root=str(TEMPLATE.parent),
                            font_paths=[str(path) for path in FONT_PATHS if path.is_dir()], ignore_system_fonts=True,
                            sys_inputs={"svg": svg, "width": str(width), "height": str(height)}, format="png",
                            ppi=ppi)
    except typst.TypstError:
        raise SpecError("chart_layout_failed") from None
    return png[0] if isinstance(png, list) else png


@lru_cache(maxsize=4)
def cover_art_png(page, rtl, ppi=132):
    """The cover artwork (no text) as a full-page PNG, for formats that put editable text over it."""
    import typst
    png = typst.compile(str(TEMPLATE.parent / "cover.typ"), root=str(TEMPLATE.parent), font_paths=[],
                        ignore_system_fonts=True, sys_inputs={"page": page, "rtl": "true" if rtl else "false"},
                        format="png", ppi=ppi)
    return png[0] if isinstance(png, list) else png


def warm_up():
    """Start the typesetter (and its thread pool) on a constant, so a sandboxed caller can forbid
    new threads before any real document is read."""
    import typst
    typst.compile(b"#set page(width: 10pt, height: 10pt)\n", format="pdf", ignore_system_fonts=True)
