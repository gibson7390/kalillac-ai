"""The bounded, validated description of a presentation.

A deck is a sequence of slides, each built from one purpose-made layout. There
is no free positioning: the layouts carry the design, so every deck has a
consistent visual system. The text limits below are set so that any slide with
its fields at their limits, in ordinary prose, fits its layout (tests/
test_deck_design.py proves this layout by layout). Only a table dense in every
cell can still be refused, with slide_text_too_long, so the author shortens it.

Deck:   {"title": str, "subtitle"?: str, "author"?: str, "organization"?: str, "date"?: str,
         "language"?: BCP 47, "slides": [slide, ...]}
slide:  every slide may carry "notes": speaker notes (plain text).
        {"layout": "title", "title": str (100), "subtitle"?: str (160)}
        {"layout": "section", "title": str (100), "subtitle"?: str (160)}
        {"layout": "points", "title": str (110), "points": [str (160), ...] (1-6), "lead"?: str (180)}
        {"layout": "two_columns", "title": str,
         "left": {"heading": str (48), "points": [str (110), ...] (1-5)}, "right": {... the same}}
        {"layout": "metrics", "title": str, "metrics": [{"label", "value", "detail"?, "trend"?}] (2-4), "note"?: str}
        {"layout": "chart", "title": str, "chart": {"kind", "categories", "series", "prefix"?, "suffix"?},
         "takeaway"?: str}
        {"layout": "table", "title": str, "header": [str (32), ...] (2-6), "rows": [[str (60), ...]] (1-8),
         "total_row"?: bool}
        {"layout": "quote", "quote": str, "attribution"?: str}
        {"layout": "timeline", "title": str, "steps": [{"title": str (30), "text"?: str (120)}] (3-6)}
        {"layout": "statement", "text": str, "eyebrow"?: str}
        {"layout": "closing", "title": str (100), "subtitle"?: str (160)}

Charts use the document chart grammar (spec.Chart), so the same data
validation applies; they become native, editable PowerPoint charts.
"""
from dataclasses import dataclass

from .spec import (_LANGUAGE, CHART_KINDS, TRENDS, Chart, SpecError, _auto_align, _chart, _keys, _list, _optional,
                   _text)

MAX_SLIDES = 60
LAYOUTS = ("title", "section", "points", "two_columns", "metrics", "chart", "table", "quote", "timeline",
           "statement", "closing")


@dataclass(frozen=True)
class Column:
    heading: str
    points: tuple


@dataclass(frozen=True)
class Metric:
    label: str
    value: str
    detail: str | None = None
    trend: str | None = None


@dataclass(frozen=True)
class Step:
    title: str
    text: str | None = None


@dataclass(frozen=True)
class Slide:
    layout: str
    title: str | None = None
    subtitle: str | None = None
    points: tuple = ()
    lead: str | None = None
    left: Column | None = None
    right: Column | None = None
    metrics: tuple = ()
    note: str | None = None
    chart: Chart | None = None
    takeaway: str | None = None
    header: tuple = ()
    rows: tuple = ()
    align: tuple = ()
    total_row: bool = False
    quote: str | None = None
    attribution: str | None = None
    steps: tuple = ()
    text: str | None = None
    eyebrow: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class Deck:
    title: str
    slides: tuple
    subtitle: str | None = None
    author: str | None = None
    organization: str | None = None
    date: str | None = None
    language: str = "en"


def _points(value, maximum_items, maximum_length, code):
    return tuple(_text(item, maximum_length, code, single_line=True)
                 for item in _list(value, maximum_items, code, minimum=1))


def _slide(value):
    if type(value) is not dict or type(value.get("layout")) is not str or value["layout"] not in LAYOUTS:
        raise SpecError("invalid_slide")
    layout = value["layout"]
    code = "invalid_slide"
    notes = _optional(value, "notes", 3_000, code, single_line=False)

    def title(limit=110):
        return _text(value["title"], limit, code, single_line=True)

    if layout in ("title", "section", "closing"):
        _keys(value, ("layout", "title"), ("subtitle", "notes"))
        return Slide(layout, title(100), _optional(value, "subtitle", 160, code), notes=notes)
    if layout == "points":
        _keys(value, ("layout", "title", "points"), ("lead", "notes"))
        return Slide(layout, title(), points=_points(value["points"], 6, 160, code),
                     lead=_optional(value, "lead", 180, code), notes=notes)
    if layout == "two_columns":
        _keys(value, ("layout", "title", "left", "right"), ("notes",))
        columns = []
        for side in ("left", "right"):
            _keys(value[side], ("heading", "points"))
            columns.append(Column(_text(value[side]["heading"], 48, code, single_line=True),
                                  _points(value[side]["points"], 5, 110, code)))
        return Slide(layout, title(), left=columns[0], right=columns[1], notes=notes)
    if layout == "metrics":
        _keys(value, ("layout", "title", "metrics"), ("note", "notes"))
        metrics = []
        for item in _list(value["metrics"], 4, code, minimum=2):
            _keys(item, ("label", "value"), ("detail", "trend"))
            trend = item.get("trend")
            if trend is not None and trend not in TRENDS:
                raise SpecError(code)
            metrics.append(Metric(_text(item["label"], 40, code, single_line=True),
                                  _text(item["value"], 14, code, single_line=True),
                                  _optional(item, "detail", 60, code), trend))
        return Slide(layout, title(), metrics=tuple(metrics), note=_optional(value, "note", 240, code), notes=notes)
    if layout == "chart":
        _keys(value, ("layout", "title", "chart"), ("takeaway", "notes"))
        raw = value["chart"]
        if type(raw) is not dict:
            raise SpecError("invalid_chart")
        chart = _chart({**raw, "type": "chart"})
        if chart.kind not in CHART_KINDS or len(chart.categories) > 24:
            raise SpecError("invalid_chart")
        return Slide(layout, title(), chart=chart, takeaway=_optional(value, "takeaway", 220, code), notes=notes)
    if layout == "table":
        _keys(value, ("layout", "title", "header", "rows"), ("total_row", "notes"))
        header = _points(value["header"], 6, 32, code)
        if len(header) < 2:
            raise SpecError(code)
        rows = _list(value["rows"], 8, code, minimum=1)
        if any(type(row) is not list or len(row) != len(header) for row in rows):
            raise SpecError(code)
        cells = tuple(tuple(_text(cell, 60, code, allow_empty=True, single_line=True) for cell in row)
                      for row in rows)
        total = value.get("total_row", False)
        if type(total) is not bool:
            raise SpecError(code)
        return Slide(layout, title(), header=header, rows=cells, align=_auto_align(header, cells), total_row=total,
                     notes=notes)
    if layout == "quote":
        _keys(value, ("layout", "quote"), ("attribution", "notes"))
        return Slide(layout, quote=_text(value["quote"], 280, code, single_line=True),
                     attribution=_optional(value, "attribution", 100, code), notes=notes)
    if layout == "timeline":
        _keys(value, ("layout", "title", "steps"), ("notes",))
        steps = []
        for item in _list(value["steps"], 6, code, minimum=3):
            _keys(item, ("title",), ("text",))
            steps.append(Step(_text(item["title"], 30, code, single_line=True), _optional(item, "text", 120, code)))
        return Slide(layout, title(), steps=tuple(steps), notes=notes)
    _keys(value, ("layout", "text"), ("eyebrow", "notes"))
    return Slide(layout, text=_text(value["text"], 160, code, single_line=True),
                 eyebrow=_optional(value, "eyebrow", 60, code), notes=notes)


def validate_deck(value):
    _keys(value, ("title", "slides"), ("subtitle", "author", "organization", "date", "language"))
    title = _text(value["title"], 130, "invalid_title", single_line=True)
    slides = tuple(_slide(item) for item in _list(value["slides"], MAX_SLIDES, "invalid_slides", minimum=1))
    language = value.get("language", "en")
    if type(language) is not str or _LANGUAGE.fullmatch(language) is None:
        raise SpecError("invalid_options")
    return Deck(title, slides, _optional(value, "subtitle", 200, "invalid_options"),
                _optional(value, "author", 100, "invalid_options"),
                _optional(value, "organization", 100, "invalid_options"),
                _optional(value, "date", 60, "invalid_options"), language)
