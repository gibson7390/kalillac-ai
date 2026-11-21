"""Charts as SVG, drawn deterministically from validated data.

chart_svg(chart) returns one self-contained SVG for a spec.Chart, in the
document design system: its palette, its typeface, quiet gridlines, direct
value labels where they fit and a legend where they are needed. The same SVG
is embedded as vector art in PDFs and rasterised for formats that need an
image.

Layout is computed, not guessed: every label is measured with the real font
metrics (Pillow reading the same Inter files the PDF embeds), so axis labels
get exactly the room they need, a long category name wraps onto a second line
(and is shortened with an ellipsis only when two lines cannot hold it) instead
of colliding, and nothing is placed outside the canvas. check_bounds()
re-verifies that for every produced chart.

Text in the SVG is escaped data. There is no script, link, external
reference or embedded image.
"""
from dataclasses import dataclass
from functools import lru_cache
import math
from pathlib import Path
from xml.sax.saxutils import escape

FONTS = Path(__file__).resolve().parent / "fonts"
PALETTE = ("#0069ff", "#0b1737", "#12a15c", "#f59f00", "#7c5cff", "#e8590c")
INK, MUTED, GRID, AXIS = "#0e1526", "#5c677e", "#e6ecf5", "#c3cee0"
WIDTH = 720
LABEL, SMALL = 11.5, 10.5


@lru_cache(maxsize=8)
def _font(weight, size):
    from PIL import ImageFont
    name = {"regular": "Inter-Regular.ttf", "medium": "Inter-Medium.ttf", "semibold": "Inter-SemiBold.ttf"}[weight]
    return ImageFont.truetype(str(FONTS / name), int(round(size * 8)))  # Measured at 8x for sub-pixel accuracy.


def text_width(text, size=LABEL, weight="regular"):
    return _font(weight, size).getlength(text) / 8.0


def fit(text, width, size=LABEL, weight="regular"):
    """text, shortened with an ellipsis until it is no wider than width."""
    if text_width(text, size, weight) <= width:
        return text
    cut = text
    while len(cut) > 1 and text_width(cut.rstrip() + "…", size, weight) > width:
        cut = cut[:-1]
    return cut.rstrip() + "…"


LINE = 13.0  # Distance between the two lines of a wrapped label.


def wrap(text, width, size=LABEL, weight="regular", lines=2):
    """text as at most `lines` lines no wider than width.

    Breaks fall between words. Whatever the lines cannot hold is shortened with an ellipsis, so a
    label is cut only when wrapping is not enough: one word wider than the room, or more words than
    the lines take."""
    if text_width(text, size, weight) <= width or lines <= 1:
        return [fit(text, width, size, weight)]
    words = text.split()
    out = []
    while words and len(out) < lines - 1:
        count = 1
        while count < len(words) and text_width(" ".join(words[:count + 1]), size, weight) <= width:
            count += 1
        if text_width(words[0], size, weight) > width:
            break                     # A single word wider than the room: nothing to gain from a break.
        out.append(" ".join(words[:count]))
        words = words[count:]
    if words:
        out.append(fit(" ".join(words), width, size, weight))
    return out


def nice_scale(low, high, target=5, zero=True):
    """(minimum, maximum, step) with round tick values covering low..high.

    The scale includes zero, as a bar's or area's length must start there. A line only encodes
    position, so with zero=False a line whose values sit in a narrow band far from zero (the band
    less than half the distance from zero) gets an axis fitted to the band, where its changes show."""
    narrow = not zero and (0 < low and high - low < 0.5 * high or high < 0 and high - low < 0.5 * -low)
    if not narrow:
        low, high = min(low, 0.0), max(high, 0.0)
    if high == low:
        high = low + 1.0
    raw = (high - low) / max(1, target)
    power = 10 ** math.floor(math.log10(raw))
    step = next(m * power for m in (1, 2, 2.5, 5, 10) if m * power >= raw - 1e-12)
    return math.floor(low / step + 1e-9) * step, math.ceil(high / step - 1e-9) * step, step


def _number_style(value, suffix="", step=None):
    """(scale, unit letter, decimals) format_value would use for value."""
    magnitude = abs(value)
    scale, unit = 1, ""
    if not suffix or suffix == "%":
        for limit, letter in ((1e9, "B"), (1e6, "M"), (1e4, "K")):
            if magnitude >= limit and suffix != "%":
                scale, unit = limit if letter != "K" else 1e3, letter
                break
    scaled = magnitude / scale
    reference = (step / scale) if step else scaled
    if unit:
        decimals = 0 if scaled >= 100 or float(scaled).is_integer() else 1
    elif reference and reference < 1 and not float(scaled).is_integer():
        decimals = min(3, max(1, -math.floor(math.log10(reference))))
    else:
        decimals = 0 if float(round(scaled, 6)).is_integer() else (1 if scaled >= 10 else 2)
    return scale, unit, decimals


def _styled(value, prefix, suffix, scale, unit, decimals):
    sign = "−" if value < 0 else ""
    return f"{sign}{prefix}{abs(value) / scale:,.{decimals}f}{unit}{suffix}"


def format_value(value, prefix="", suffix="", step=None):
    """A compact, readable number: thousands separators, K/M/B above 10,000, no float noise."""
    return _styled(value, prefix, suffix, *_number_style(value, suffix, step))


ROUNDING_NOTE = "Rounded; parts may not add to the total."
MAX_EXTRA_DECIMALS = 2


@dataclass(frozen=True)
class Breakdown:
    total: str
    parts: list
    percents: list
    note: str | None          # Set when the shown parts cannot add up exactly to the shown total.


def breakdown(values, prefix="", suffix=""):
    """How a breakdown is shown, under one deterministic policy (Codex document audit Q1 and
    reverification finding D). Source values are never changed.

    1. Parts and total use the total's own precision, raised one decimal at a time, at most
       MAX_EXTRA_DECIMALS, until the shown parts add up exactly to the shown total.
    2. If no such precision exists within that bound (thirds, for instance), everything is shown at the
       total's own precision and the breakdown carries ROUNDING_NOTE, which the chart prints.
    3. Shares are whole percentages when they sum to 100, else tenths, else tenths apportioned by
       largest remainder, so they always total 100%."""
    total = sum(values)
    scale, unit, decimals = _number_style(total, suffix)
    note, places = ROUNDING_NOTE, decimals
    for extra in range(MAX_EXTRA_DECIMALS + 1):
        candidate = decimals + extra
        shown = [round(abs(value) / scale, candidate) for value in values]
        if round(sum(shown), candidate) == round(abs(total) / scale, candidate):
            note, places = None, candidate
            break
    total_label = _styled(total, prefix, suffix, scale, unit, places)
    parts = [_styled(value, prefix, suffix, scale, unit, places) for value in values]
    shares = [value / total * 100 if total else 0.0 for value in values]
    for share_places in (0, 1):
        rounded = [round(share, share_places) for share in shares]
        if round(sum(rounded), share_places) == 100:
            break
    else:
        tenths = [math.floor(share * 10) for share in shares]
        order = sorted(range(len(shares)), key=lambda i: shares[i] * 10 - tenths[i], reverse=True)
        for i in order[:max(0, 1000 - sum(tenths))]:
            tenths[i] += 1
        rounded, share_places = [t / 10 for t in tenths], 1
    percents = [f"{share:.{share_places}f}%" for share in rounded]
    return Breakdown(total_label, parts, percents, note)


def breakdown_labels(values, prefix="", suffix=""):
    """(total label, part labels, percentage labels): see breakdown()."""
    shown = breakdown(values, prefix, suffix)
    return shown.total, shown.parts, shown.percents


@dataclass
class _Canvas:
    width: float
    height: float

    def __post_init__(self):
        self.parts, self.boxes = [], []

    def add(self, markup, box=None):
        self.parts.append(markup)
        if box is not None:
            self.boxes.append(box)

    def text(self, x, y, value, size=LABEL, anchor="start", fill=MUTED, weight="regular", tabular=False):
        width = text_width(value, size, weight)
        left = x - (width if anchor == "end" else width / 2 if anchor == "middle" else 0)
        weights = {"regular": 400, "medium": 500, "semibold": 600}
        features = ' style="font-feature-settings: \'tnum\' 1"' if tabular else ""
        self.add(f'<text x="{x:.2f}" y="{y:.2f}" font-size="{size}" text-anchor="{anchor}" fill="{fill}" '
                 f'font-weight="{weights[weight]}"{features}>{escape(value)}</text>',
                 (left, y - size, left + width, y + size * 0.3))

    def svg(self):
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.width:.0f} {self.height:.0f}" '
                f'width="{self.width:.0f}" height="{self.height:.0f}" font-family="Inter, sans-serif">'
                + "".join(self.parts) + "</svg>")


def _legend(canvas, names, y, colors=PALETTE, left=0.0):
    """A wrapping row of swatches and names; returns the y below it."""
    x = left
    for index, name in enumerate(names):
        label = fit(name, 220)
        width = 16 + text_width(label, SMALL) + 18
        if x + width > canvas.width and x > left:
            x, y = left, y + 18
        canvas.add(f'<rect x="{x:.2f}" y="{y - 8.5:.2f}" width="10" height="10" rx="2.5" '
                   f'fill="{colors[index % len(colors)]}"/>', (x, y - 8.5, x + 10, y + 1.5))
        canvas.text(x + 16, y, label, SMALL, fill=INK)
        x += width
    return y + 16


def _plot_axes(canvas, chart, low, high, step, top, bottom, left, right):
    """Horizontal gridlines with tick labels on the left; returns a value -> y mapping."""
    def y_of(value):
        return bottom - (value - low) / (high - low) * (bottom - top)
    ticks = int(round((high - low) / step))
    for index in range(ticks + 1):
        value = low + index * step
        y = y_of(value)
        zero = abs(value) < step * 1e-9
        canvas.add(f'<line x1="{left:.2f}" y1="{y:.2f}" x2="{right:.2f}" y2="{y:.2f}" '
                   f'stroke="{AXIS if zero else GRID}" stroke-width="{1.2 if zero else 1}"/>')
        canvas.text(left - 8, y + SMALL * 0.35, format_value(value, chart.prefix, chart.suffix, step), SMALL,
                    anchor="end", tabular=True)
    return y_of


def _value_range(chart, stacked=False):
    if stacked:
        totals = [sum(s.values[i] or 0.0 for s in chart.series) for i in range(len(chart.categories))]
        return nice_scale(0.0, max(totals))
    known = [v for s in chart.series for v in s.values if v is not None]
    return nice_scale(min(known), max(known), zero=chart.kind != "line")


def _axis_width(chart, low, high, step):
    ticks = int(round((high - low) / step))
    return max(text_width(format_value(low + i * step, chart.prefix, chart.suffix, step), SMALL)
               for i in range(ticks + 1)) + 14


def _category_layout(chart, slot):
    """(every, lines per category) for the names under a plot.

    Every name is shown when each has room, otherwise every nth. A name wider than its room wraps
    onto a second line before it is ever shortened."""
    every = 1
    while every < len(chart.categories) and min(slot * every - 6, 150) < min(
            48, max(text_width(c, SMALL) for c in chart.categories)):
        every += 1
    room = max(24.0, slot * every - 6)
    return every, [wrap(name, room, SMALL) if index % every == 0 else []
                   for index, name in enumerate(chart.categories)]


def _category_extra(chart, slot):
    """Height the wrapped names need beyond a single line."""
    return (max(len(lines) for lines in _category_layout(chart, slot)[1]) - 1) * LINE


def _category_labels(canvas, chart, centers, slot, y):
    """Category names under the plot, each centred on its mark and kept inside the canvas."""
    for center, lines in zip(centers, _category_layout(chart, slot)[1]):
        for row, label in enumerate(lines):
            half = text_width(label, SMALL) / 2
            x = min(max(center, half), canvas.width - half)
            canvas.text(x, y + row * LINE, label, SMALL, anchor="middle")


def _columns(chart, stacked):
    multi = len(chart.series) > 1
    low, high, step = _value_range(chart, stacked)
    count = len(chart.categories)
    left, right = _axis_width(chart, low, high, step), WIDTH - 6
    slot = (right - left) / count
    extra = _category_extra(chart, slot)       # A second line of category names lengthens the canvas;
    height = (330 if multi else 300) + extra   # the plot itself keeps its height.
    canvas = _Canvas(WIDTH, height)
    top = _legend(canvas, [s.name for s in chart.series], 12) + 14 if multi else 22
    bottom = height - 30 - extra
    y_of = _plot_axes(canvas, chart, low, high, step, top, bottom, left, right)
    groups = 1 if stacked else len(chart.series)
    bar = min(46.0, slot * 0.72 / groups)
    centers = [left + slot * (i + 0.5) for i in range(count)]
    labelled = count * groups <= 14
    for i, center in enumerate(centers):
        if stacked:
            base = 0.0
            for index, series in enumerate(chart.series):
                value = series.values[i] or 0.0
                if value > 0:
                    y0, y1 = y_of(base + value), y_of(base)
                    canvas.add(f'<rect x="{center - bar / 2:.2f}" y="{y0:.2f}" width="{bar:.2f}" '
                               f'height="{max(0.0, y1 - y0 - 1):.2f}" fill="{PALETTE[index % len(PALETTE)]}"/>',
                               (center - bar / 2, y0, center + bar / 2, y1))
                base += value
            if labelled and base > 0:
                canvas.text(center, y_of(base) - 6, format_value(base, chart.prefix, chart.suffix), SMALL,
                            anchor="middle", fill=INK, weight="medium", tabular=True)
            continue
        start = center - bar * groups / 2
        for index, series in enumerate(chart.series):
            value = series.values[i]
            if value is None:
                continue
            x = start + index * bar
            y0, y1 = sorted((y_of(value), y_of(0.0)))
            canvas.add(f'<rect x="{x + 1:.2f}" y="{y0:.2f}" width="{bar - 2:.2f}" height="{max(1.0, y1 - y0):.2f}" '
                       f'rx="2" fill="{PALETTE[index % len(PALETTE)]}"/>', (x + 1, y0, x + bar - 1, y1))
            if labelled:
                label_y = y0 - 6 if value >= 0 else y1 + SMALL + 3
                canvas.text(x + bar / 2, label_y, format_value(value, chart.prefix, chart.suffix), SMALL,
                            anchor="middle", fill=INK, weight="medium", tabular=True)
    _category_labels(canvas, chart, centers, slot, bottom + 18)
    return canvas


def _bars(chart):
    """Horizontal bars: the right form for long category names. One series."""
    series = chart.series[0]
    count = len(chart.categories)
    row = 30 if count <= 10 else 24
    height = count * row + 34
    canvas = _Canvas(WIDTH, height)
    known = [v for v in series.values if v is not None]
    low, high, step = nice_scale(min(known), max(known))
    labels = [wrap(c, 230) for c in chart.categories]
    if any(len(lines) > 1 for lines in labels):
        row = max(row, 32)             # Two lines of a name need a taller row.
        height = count * row + 34
        canvas = _Canvas(WIDTH, height)
    left = max(text_width(line) for lines in labels for line in lines) + 14
    value_room = max(text_width(format_value(v, chart.prefix, chart.suffix), SMALL, "medium") for v in known) + 10
    right = WIDTH - value_room

    def x_of(value):
        return left + (value - low) / (high - low) * (right - left)
    ticks = int(round((high - low) / step))
    for index in range(ticks + 1):
        value = low + index * step
        x = x_of(value)
        zero = abs(value) < step * 1e-9
        canvas.add(f'<line x1="{x:.2f}" y1="6" x2="{x:.2f}" y2="{height - 26:.2f}" '
                   f'stroke="{AXIS if zero else GRID}" stroke-width="{1.2 if zero else 1}"/>')
        tick = format_value(value, chart.prefix, chart.suffix, step)
        half = text_width(tick, SMALL) / 2
        canvas.text(min(max(x, half), WIDTH - half), height - 8, tick, SMALL, anchor="middle", tabular=True)
    for index, (lines, value) in enumerate(zip(labels, series.values)):
        y = 10 + index * row
        first = y + row * 0.5 + 1 - (len(lines) - 1) * LINE / 2
        for line_index, label in enumerate(lines):
            canvas.text(left - 10, first + line_index * LINE, label, anchor="end", fill=INK)
        if value is None:
            continue
        x0, x1 = sorted((x_of(0.0), x_of(value)))
        thickness = row * 0.56
        canvas.add(f'<rect x="{x0:.2f}" y="{y + (row - thickness) / 2 - 3:.2f}" width="{max(1.0, x1 - x0):.2f}" '
                   f'height="{thickness:.2f}" rx="2.5" fill="{PALETTE[0]}"/>',
                   (x0, y, x1, y + row))
        canvas.text(x1 + 7 if value >= 0 else x0 - 7, y + row * 0.5 + 1,
                    format_value(value, chart.prefix, chart.suffix), SMALL,
                    anchor="start" if value >= 0 else "end", fill=INK, weight="medium", tabular=True)
    return canvas


def _lines(chart, area):
    multi = len(chart.series) > 1
    low, high, step = _value_range(chart)
    count = len(chart.categories)
    left, right = _axis_width(chart, low, high, step), WIDTH - 14
    inset = 16.0  # The first and last points stand clear of the axis and the edge.
    slot = (right - left - 2 * inset) / (count - 1)
    extra = _category_extra(chart, slot)
    height = (330 if multi else 300) + extra
    canvas = _Canvas(WIDTH, height)
    top = _legend(canvas, [s.name for s in chart.series], 12) + 14 if multi else 22
    bottom = height - 30 - extra
    y_of = _plot_axes(canvas, chart, low, high, step, top, bottom, left, right)
    xs = [left + inset + slot * i for i in range(count)]
    for index, series in enumerate(chart.series):
        color = PALETTE[index % len(PALETTE)]
        runs, current = [], []
        for x, value in zip(xs, series.values):
            if value is None:
                if current:
                    runs.append(current)
                current = []
            else:
                current.append((x, y_of(value)))
        if current:
            runs.append(current)
        for run in runs:
            points = " ".join(f"{x:.2f},{y:.2f}" for x, y in run)
            if area and len(run) > 1:
                canvas.add(f'<polygon points="{run[0][0]:.2f},{y_of(0.0):.2f} {points} {run[-1][0]:.2f},'
                           f'{y_of(0.0):.2f}" fill="{color}" fill-opacity="{0.16 if multi else 0.14}"/>')
            if len(run) > 1:
                canvas.add(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2.4" '
                           'stroke-linejoin="round" stroke-linecap="round"/>')
            if count <= 24:
                for x, y in run:
                    canvas.add(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="3.2" fill="#ffffff" stroke="{color}" '
                               'stroke-width="2"/>', (x - 4, y - 4, x + 4, y + 4))
        if not multi and count <= 12:
            for x, value in zip(xs, series.values):
                if value is not None:
                    label = format_value(value, chart.prefix, chart.suffix)
                    half = text_width(label, SMALL, "medium") / 2
                    canvas.text(min(max(x, left + half), WIDTH - half), y_of(value) - 9, label, SMALL,
                                anchor="middle", fill=INK, weight="medium", tabular=True)
    _category_labels(canvas, chart, xs, slot, bottom + 18)
    return canvas


def _donut(chart):
    values = chart.series[0].values
    total = sum(values)
    height = max(250, 46 + 26 * len(values))
    canvas = _Canvas(WIDTH, height)
    cx, cy, outer, inner = 150.0, height / 2, 104.0, 64.0
    angle = -math.pi / 2
    for index, value in enumerate(values):
        if value <= 0:
            continue
        sweep = value / total * 2 * math.pi
        color = PALETTE[index % len(PALETTE)]
        if sweep >= 2 * math.pi - 1e-9:
            canvas.add(f'<circle cx="{cx}" cy="{cy}" r="{(outer + inner) / 2}" fill="none" stroke="{color}" '
                       f'stroke-width="{outer - inner}"/>')
        else:
            gap = 0.012 if sweep > 0.08 else 0.0  # A hairline of white between segments.
            a0, a1 = angle + gap, angle + sweep - gap
            large = 1 if (a1 - a0) > math.pi else 0
            p = [(cx + r * math.cos(a), cy + r * math.sin(a)) for r, a in
                 ((outer, a0), (outer, a1), (inner, a1), (inner, a0))]
            canvas.add(f'<path d="M{p[0][0]:.2f},{p[0][1]:.2f} A{outer},{outer} 0 {large} 1 {p[1][0]:.2f},{p[1][1]:.2f} '
                       f'L{p[2][0]:.2f},{p[2][1]:.2f} A{inner},{inner} 0 {large} 0 {p[3][0]:.2f},{p[3][1]:.2f} Z" '
                       f'fill="{color}"/>')
        angle += sweep
    canvas.boxes.append((cx - outer, cy - outer, cx + outer, cy + outer))
    shown = breakdown(values, chart.prefix, chart.suffix)
    total_label, part_labels, percent_labels = shown.total, shown.parts, shown.percents
    canvas.text(cx, cy + 2, total_label, 21, anchor="middle", fill=INK, weight="semibold", tabular=True)
    canvas.text(cx, cy + 20, "Total", SMALL, anchor="middle")
    x, y = 300.0, height / 2 - 13 * len(values) + 17
    number_x, percent_x = WIDTH - 62, WIDTH - 4
    for index, (name, value) in enumerate(zip(chart.categories, values)):
        canvas.add(f'<rect x="{x:.2f}" y="{y - 9:.2f}" width="10" height="10" rx="2.5" '
                   f'fill="{PALETTE[index % len(PALETTE)]}"/>', (x, y - 9, x + 10, y + 1))
        amount = part_labels[index]
        canvas.text(x + 18, y, fit(name, number_x - text_width(amount, LABEL, "medium") - x - 34), fill=INK)
        canvas.text(number_x, y, amount, anchor="end", fill=INK, weight="medium", tabular=True)
        canvas.text(percent_x, y, percent_labels[index], anchor="end", tabular=True)
        if index < len(values) - 1:
            canvas.add(f'<line x1="{x:.2f}" y1="{y + 8.5:.2f}" x2="{WIDTH - 4:.2f}" y2="{y + 8.5:.2f}" '
                       f'stroke="{GRID}" stroke-width="1"/>')
        y += 26
    if shown.note:                   # The parts as shown cannot add up exactly: say so.
        canvas.text(x, y - 6, shown.note, SMALL, fill=MUTED)
    return canvas


def _draw(chart):
    if chart.kind == "donut":
        return _donut(chart)
    if chart.kind == "bar":
        return _bars(chart) if len(chart.series) == 1 else _columns(chart, stacked=False)
    if chart.kind in ("line", "area"):
        return _lines(chart, area=chart.kind == "area")
    return _columns(chart, stacked=chart.kind == "stacked")


def chart_svg(chart):
    """The SVG for one validated spec.Chart."""
    return _draw(chart).svg()


def check_bounds(chart, tolerance=0.75):
    """Every mark and label lies inside the canvas: the list of boxes that do not (empty when sound)."""
    canvas = _draw(chart)
    return [box for box in canvas.boxes
            if box[0] < -tolerance or box[1] < -tolerance or box[2] > canvas.width + tolerance
            or box[3] > canvas.height + tolerance]


def describe(chart):
    """Alternative text: what the chart shows, for readers who cannot see it."""
    kind = {"column": "Column chart", "bar": "Bar chart", "stacked": "Stacked column chart", "line": "Line chart",
            "area": "Area chart", "donut": "Donut chart"}[chart.kind]
    parts = []
    for series in chart.series:
        points = ", ".join(f"{name} {format_value(value, chart.prefix, chart.suffix)}"
                           for name, value in zip(chart.categories, series.values) if value is not None)
        parts.append(f"{series.name}: {points}" if len(chart.series) > 1 else points)
    head = f"{kind}{' of ' + chart.title if chart.title else ''}"
    return (head + ". " + "; ".join(parts))[:1500]
