"""Presentations: the deck specification, and the designed PPTX read back from the file.

Every check reads what PowerPoint would read: the slides, shapes, tables and charts in the
generated package, measured again with the Calibri-compatible metrics. Nothing touches the
application or the network. Rendering through Microsoft PowerPoint is a development check
(docs/FILE_GENERATION.md), not part of this suite.
"""
from __future__ import annotations

import copy
import io
import itertools
import re
import time
import zipfile

import pytest

pptx = pytest.importorskip("pptx")
pytest.importorskip("typst")

from pptx.util import Emu  # noqa: E402

from kalillac_files import generate  # noqa: E402
from kalillac_files import pptx_design as design  # noqa: E402
from kalillac_files.deck import MAX_SLIDES, validate_deck  # noqa: E402
from kalillac_files.spec import SpecError  # noqa: E402
from quality_benchmark import EXECUTIVE_DECK  # noqa: E402

PROSE = ("Operating margins improved across every region as procurement savings offset higher freight costs, "
         "while the wholesale channel continued to expand faster than retail and subscriptions combined. ") * 20
FOOTER = ("Footer title", "Slide number", "Footer rule")


def words(limit):
    """Ordinary English, as long as possible within limit characters, ending on a whole word."""
    text = PROSE[:limit]
    if PROSE[limit] != " ":
        text = text[:text.rfind(" ")]
    return text.strip().rstrip(",.")


def deck_of(*slides, **options):
    return {"title": options.pop("title", "Quarterly review"), "slides": list(slides), **options}


def presentation(raw):
    return pptx.Presentation(io.BytesIO(generate(copy.deepcopy(raw), "pptx").data))


def inches(value):
    return Emu(value).inches


def refused(raw):
    with pytest.raises(SpecError) as error:
        generate(copy.deepcopy(raw), "pptx")
    return error.value.code


@pytest.fixture(scope="module")
def benchmark():
    data = generate(copy.deepcopy(EXECUTIVE_DECK), "pptx").data
    return data, pptx.Presentation(io.BytesIO(data))


# --- the specification ---------------------------------------------------------------------


def test_the_benchmark_deck_validates_with_every_layout():
    deck = validate_deck(copy.deepcopy(EXECUTIVE_DECK))
    assert len(deck.slides) == 14
    assert {slide.layout for slide in deck.slides} == {"title", "statement", "section", "metrics", "chart", "points",
                                                       "two_columns", "table", "timeline", "quote", "closing"}


@pytest.mark.parametrize("slide, code", [
    ({"layout": "hero", "title": "T"}, "invalid_slide"),
    ({"title": "T"}, "invalid_slide"),
    ({"layout": "points", "title": "T", "points": []}, "invalid_slide"),
    ({"layout": "points", "title": "T", "points": ["a"] * 7}, "invalid_slide"),
    ({"layout": "points", "title": "T", "points": ["a"], "colour": "red"}, "invalid_structure"),
    ({"layout": "points", "title": "T", "points": ["a" * 161]}, "invalid_slide"),
    ({"layout": "points", "title": "T", "points": ["two\nlines"]}, "invalid_slide"),
    ({"layout": "points", "title": "x" * 111, "points": ["a"]}, "invalid_slide"),
    ({"layout": "title", "title": "x" * 101}, "invalid_slide"),
    ({"layout": "metrics", "title": "T", "metrics": [{"label": "A", "value": "1"}]}, "invalid_slide"),
    ({"layout": "metrics", "title": "T", "metrics": [{"label": "A", "value": "1"}] * 5}, "invalid_slide"),
    ({"layout": "metrics", "title": "T", "metrics": [{"label": "A", "value": "1", "trend": "sideways"}] * 2},
     "invalid_slide"),
    ({"layout": "metrics", "title": "T", "metrics": [{"label": "A", "value": "123456789012345"}] * 2},
     "invalid_slide"),
    ({"layout": "table", "title": "T", "header": ["Only"], "rows": [["1"]]}, "invalid_slide"),
    ({"layout": "table", "title": "T", "header": ["A", "B"], "rows": [["1"]]}, "invalid_slide"),
    ({"layout": "table", "title": "T", "header": ["A", "B"], "rows": [["1", "2"]] * 9}, "invalid_slide"),
    ({"layout": "table", "title": "T", "header": ["A", "B"], "rows": [["1", "2"]], "total_row": "yes"},
     "invalid_slide"),
    ({"layout": "timeline", "title": "T", "steps": [{"title": "a"}] * 2}, "invalid_slide"),
    ({"layout": "timeline", "title": "T", "steps": [{"title": "a"}] * 7}, "invalid_slide"),
    ({"layout": "chart", "title": "T", "chart": "column"}, "invalid_chart"),
    ({"layout": "chart", "title": "T", "chart": {"kind": "radar", "categories": ["a"],
                                                 "series": [{"name": "s", "values": [1]}]}}, "invalid_chart"),
    ({"layout": "two_columns", "title": "T", "left": {"heading": "L", "points": ["a"]}}, "invalid_structure"),
    ({"layout": "statement", "text": "x" * 161}, "invalid_slide"),
    ({"layout": "quote", "quote": "Q", "notes": 3}, "invalid_slide"),
])
def test_malformed_slides_are_refused_with_a_stable_code(slide, code):
    assert refused(deck_of(slide)) == code


def test_deck_level_refusals():
    assert refused({"title": "T", "slides": []}) == "invalid_slides"
    assert refused({"title": "T", "slides": [{"layout": "statement", "text": "x"}] * (MAX_SLIDES + 1)}) \
        == "invalid_slides"
    assert refused({"title": "", "slides": [{"layout": "statement", "text": "x"}]}) == "invalid_title"
    assert refused(deck_of({"layout": "statement", "text": "x"}, language="english please")) == "invalid_options"
    assert refused({"title": "T", "blocks": []}) == "invalid_structure"


def test_generation_never_changes_the_callers_specification_and_is_deterministic():
    given = copy.deepcopy(EXECUTIVE_DECK)
    first = generate(given, "pptx")
    assert given == EXECUTIVE_DECK
    time.sleep(1.1)                                        # Across a second: nothing records the time of writing.
    assert generate(copy.deepcopy(EXECUTIVE_DECK), "pptx").data == first.data
    assert {item.date_time for item in zipfile.ZipFile(io.BytesIO(first.data)).infolist()} == {(1980, 1, 1, 0, 0, 0)}
    assert first.filename == "northwind-2027-from-one-roastery-to-two.pptx"
    assert first.media_type == "application/vnd.openxmlformats-officedocument.presentationml.presentation"


# --- the package ---------------------------------------------------------------------------


def test_the_deck_is_widescreen_with_one_slide_per_specified_slide(benchmark):
    _, deck = benchmark
    assert (round(inches(deck.slide_width), 3), round(inches(deck.slide_height), 3)) == (13.333, 7.5)
    assert len(deck.slides) == len(EXECUTIVE_DECK["slides"])


def test_the_package_carries_only_this_decks_metadata(benchmark):
    data, deck = benchmark
    package = zipfile.ZipFile(io.BytesIO(data))
    names = package.namelist()
    assert not [name for name in names if "thumbnail" in name or "printerSettings" in name or name.endswith(".bin")]
    assert not [name for name in names if re.search(r"vba|activeX|\.(exe|dll|js)$", name, re.I)]
    for name in names:
        if name.endswith(".rels"):
            assert 'TargetMode="External"' not in package.read(name).decode("utf-8"), name
    core = deck.core_properties
    assert (core.title, core.author, core.language) == (EXECUTIVE_DECK["title"], EXECUTIVE_DECK["author"], "en")
    assert core.comments == "" and core.last_modified_by == EXECUTIVE_DECK["author"]
    assert core.created is None and core.modified is None          # No template dates; the bytes stay deterministic.
    app = package.read("docProps/app.xml").decode("utf-8")
    assert "<Slides>14</Slides>" in app and "Widescreen" in app and "Macintosh" not in app
    everything = b"".join(package.read(name) for name in names if name.endswith(".xml"))
    assert b"python-pptx" not in everything and b"Steve Canny" not in everything


def test_speaker_notes_are_carried_through(benchmark):
    _, deck = benchmark
    assert deck.slides[0].notes_slide.notes_text_frame.text == EXECUTIVE_DECK["slides"][0]["notes"]
    assert not any(slide.has_notes_slide for slide in list(deck.slides)[1:])


def test_every_run_names_calibri_and_its_east_asian_and_complex_script_faces(benchmark):
    data, _ = benchmark
    package = zipfile.ZipFile(io.BytesIO(data))
    slides = [name for name in package.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)]
    for name in slides:
        xml = package.read(name).decode("utf-8")
        assert set(re.findall(r'<a:latin typeface="([^"]+)"', xml)) <= {"Calibri"}, name
        assert set(re.findall(r'<a:ea typeface="([^"]+)"', xml)) <= {"Microsoft YaHei"}, name
        assert set(re.findall(r'<a:cs typeface="([^"]+)"', xml)) <= {"Arial"}, name


# --- the charts ----------------------------------------------------------------------------


def charts(deck):
    return [shape.chart for slide in deck.slides for shape in slide.shapes if shape.has_chart]


def test_charts_are_native_editable_and_carry_their_data(benchmark):
    data, deck = benchmark
    found = charts(deck)
    assert [chart.chart_type for chart in found] == [
        pptx.enum.chart.XL_CHART_TYPE.COLUMN_CLUSTERED, pptx.enum.chart.XL_CHART_TYPE.DOUGHNUT,
        pptx.enum.chart.XL_CHART_TYPE.LINE_MARKERS]
    revenue = found[0]
    assert [series.name for series in revenue.plots[0].series] == ["2025", "2026"]
    assert list(revenue.plots[0].series[1].values) == [4.05, 4.41, 4.69, 5.25]
    assert list(revenue.plots[0].categories) == ["Q1", "Q2", "Q3", "Q4"]
    package = zipfile.ZipFile(io.BytesIO(data))
    embedded = [name for name in package.namelist() if name.startswith("ppt/embeddings/")]
    assert len(embedded) == 3 and all(name.endswith(".xlsx") for name in embedded)  # Edit Data opens these.


def test_no_chart_repeats_its_series_name_as_a_title(benchmark):
    _, deck = benchmark
    for chart in charts(deck):
        assert not chart.has_title
        assert chart._chartSpace.chart.find(
            "{http://schemas.openxmlformats.org/drawingml/2006/chart}autoTitleDeleted").get("val") == "1"


def test_axis_ticks_are_as_precise_as_their_step_and_labels_as_their_data(benchmark):
    _, deck = benchmark
    revenue, share, capacity = charts(deck)
    assert revenue.value_axis.tick_labels.number_format == '"$"#,##0"M"'           # $6M, not $6.0M.
    assert revenue.plots[0].series[0]._element.xpath(".//c:formatCode")[0].text == '"$"#,##0.0"M"'   # $4.1M.
    assert capacity.value_axis.tick_labels.number_format == '#,##0"%"'
    assert capacity.plots[0].data_labels.number_format == '#,##0"%"'
    assert share.plots[0].data_labels.show_percentage and share.plots[0].data_labels.number_format == "0%"


def test_a_quote_mark_in_an_affix_is_escaped_rather_than_breaking_the_format():
    deck = presentation(deck_of({"layout": "chart", "title": "Height", "chart": {
        "kind": "line", "categories": ["A", "B"], "suffix": '"', "series": [{"name": "Inches", "values": [10, 12]},
                                                                             {"name": "Other", "values": [9, 11]}]}}))
    assert charts(deck)[0].value_axis.tick_labels.number_format == '#,##0\\"'


def test_a_single_series_column_chart_labels_its_bars_instead_of_an_axis():
    deck = presentation(deck_of({"layout": "chart", "title": "Orders by region", "chart": {
        "kind": "column", "categories": ["North", "South", "West"],
        "series": [{"name": "Orders", "values": [40, 55, 31]}]}}))
    chart = charts(deck)[0]
    assert chart.plots[0].has_data_labels and not chart.value_axis.visible and not chart.has_legend


# --- the geometry: read back from the file and measured again -------------------------------


def text_shapes(slide):
    return [shape for shape in slide.shapes if shape.has_text_frame and shape.text_frame.text.strip()]


def box(shape):
    left, top = inches(shape.left), inches(shape.top)
    return left, top, left + inches(shape.width), top + inches(shape.height)


def check_geometry(deck):
    """Everything on the slide; text measured to fit its box; no two text boxes overlap; content above the footer."""
    for number, slide in enumerate(deck.slides, start=1):
        for shape in slide.shapes:
            left, top, right, bottom = box(shape)
            assert left >= -0.001 and top >= -0.001 and right <= design.WIDTH + 0.001 and \
                bottom <= design.HEIGHT + 0.001, (number, shape.name)
        texts = text_shapes(slide)
        for shape in texts:
            if shape.name == "Quote mark":   # A decorative glyph, larger than its box by design.
                continue
            frame = shape.text_frame
            paragraph = frame.paragraphs[0]
            run = paragraph.runs[0]
            size = run.font.size.pt
            spacing = paragraph.line_spacing or 1.0
            lines = len(design.wrap(frame.text, inches(shape.width) + 0.001, size, bool(run.font.bold)))
            needed = lines * size * design.LINE * spacing / 72
            assert needed <= inches(shape.height) + 0.001, (number, shape.name, frame.text, size, lines)
        for first, second in itertools.combinations(texts, 2):
            a, b = box(first), box(second)
            overlap = min(a[2], b[2]) - max(a[0], b[0]) > 0.001 and min(a[3], b[3]) - max(a[1], b[1]) > 0.001
            assert not overlap, (number, first.name, second.name)
        if any(shape.name == "Footer rule" for shape in slide.shapes):
            for shape in slide.shapes:
                if shape.name not in FOOTER:
                    assert box(shape)[3] <= 6.92 + 0.001, (number, shape.name)
        for shape in slide.shapes:
            if shape.has_table:
                check_table(number, shape)


def check_table(number, shape):
    table = shape.table
    widths = [inches(column.width) for column in table.columns]
    assert abs(sum(widths) - design.CONTENT) < 0.01
    far = pptx.enum.text.PP_ALIGN.LEFT if table._tbl.tblPr.get("rtl") == "1" else pptx.enum.text.PP_ALIGN.RIGHT
    for r, row in enumerate(table.rows):
        for i, cell in enumerate(row.cells):
            paragraph = cell.text_frame.paragraphs[0]
            if r and paragraph.alignment == far and cell.text:
                run = paragraph.runs[0]
                # PowerPoint needs up to 0.03 in more than text and margins in a cell (CELL_SLACK keeps 0.05).
                room = widths[i] - inches(cell.margin_left) - inches(cell.margin_right) - 0.03
                assert design.width(cell.text, run.font.size.pt, bool(run.font.bold)) <= room, \
                    (number, r, i, cell.text)  # Numbers never wrap.


def test_the_benchmark_deck_fits_everywhere(benchmark):
    check_geometry(benchmark[1])


def test_layout_boxes_report_every_placed_element_inside_the_slide():
    boxes = design.layout_boxes(validate_deck(copy.deepcopy(EXECUTIVE_DECK)))
    assert {number for number, *_ in boxes} == set(range(1, 15))
    for number, name, left, top, right, bottom in boxes:
        assert 0 <= left < right <= design.WIDTH and 0 <= top < bottom <= design.HEIGHT, (number, name)


def at_the_limits():
    yield {"layout": "title", "title": words(100), "subtitle": words(160)}
    yield {"layout": "section", "title": words(100), "subtitle": words(160)}
    yield {"layout": "points", "title": words(110), "lead": words(180), "points": [words(160)] * 6}
    yield {"layout": "points", "title": words(110), "points": [words(160)] * 6}
    yield {"layout": "points", "title": "Short", "points": ["One", "Two"]}
    yield {"layout": "two_columns", "title": words(110), "left": {"heading": words(48), "points": [words(110)] * 5},
           "right": {"heading": words(48), "points": [words(110)] * 5}}
    for count in (2, 3, 4):
        yield {"layout": "metrics", "title": words(110), "note": words(240),
               "metrics": [{"label": words(40), "value": "$1,234,567.89", "detail": words(60), "trend": "up"}] * count}
    yield {"layout": "chart", "title": words(110), "takeaway": words(220),
           "chart": {"kind": "column", "categories": [f"Category {i}" for i in range(24)],
                     "series": [{"name": words(40), "values": list(range(24))}]}}
    yield {"layout": "table", "title": words(110), "header": [words(32)] * 6,
           "rows": [[words(60)] + ["$1,234,567.89"] * 5] * 7 + [["Total"] + ["$9,876,543.21"] * 5], "total_row": True}
    yield {"layout": "table", "title": words(110), "header": [words(32)] * 3, "rows": [[words(60)] * 3] * 8}
    yield {"layout": "quote", "quote": words(280), "attribution": words(100)}
    for count in (3, 6):
        yield {"layout": "timeline", "title": words(110), "steps": [{"title": words(30), "text": words(120)}] * count}
    yield {"layout": "statement", "text": words(160), "eyebrow": words(60)}
    yield {"layout": "closing", "title": words(100), "subtitle": words(160)}


@pytest.mark.parametrize("slide", list(at_the_limits()), ids=lambda slide: slide["layout"])
def test_every_layout_fits_with_every_field_at_its_limit(slide):
    raw = deck_of(slide, title=words(130), organization=words(100), author=words(100), date=words(60))
    check_geometry(presentation(raw))


def test_only_a_table_dense_in_every_cell_is_refused_and_it_says_why():
    dense = {"layout": "table", "title": "Dense", "header": [words(32)] * 6, "rows": [[words(60)] * 6] * 8}
    assert refused(deck_of(dense)) == "slide_text_too_long"


def test_a_two_line_title_moves_the_body_down_rather_than_overlapping_it():
    short = presentation(deck_of({"layout": "points", "title": "Short", "points": ["One"]}))
    long = presentation(deck_of({"layout": "points", "title": words(110), "points": ["One"]}))

    def point_top(deck):
        return next(box(shape)[1] for shape in deck.slides[0].shapes if shape.name == "Point")
    assert point_top(long) > point_top(short)
    check_geometry(long)


# --- languages -----------------------------------------------------------------------------


ARABIC_DECK = deck_of(
    {"layout": "title", "title": "خطة النمو للعام المقبل", "subtitle": "مضاعفة الطاقة الإنتاجية"},
    {"layout": "points", "title": "ثلاثة أسباب", "points": ["نمو المبيعات", "هوامش أفضل"]},
    {"layout": "metrics", "title": "أقوى عام", "metrics": [{"label": "الإيرادات", "value": "$18.4M"},
                                                         {"label": "الهامش", "value": "41.6%"}]},
    {"layout": "two_columns", "title": "خياران", "left": {"heading": "البناء", "points": ["طاقة أكبر"]},
     "right": {"heading": "الاستعانة", "points": ["دون رأس مال"]}},
    {"layout": "table", "title": "المقارنة", "header": ["المقياس", "البناء"], "rows": [["رأس المال", "$4.2M"]]},
    {"layout": "timeline", "title": "الخطوات", "steps": [{"title": "الموقع"}, {"title": "المعدات"},
                                                       {"title": "التشغيل"}]},
    {"layout": "chart", "title": "الإيرادات", "takeaway": "نمو في كل ربع",
     "chart": {"kind": "column", "categories": ["Q1", "Q2"], "series": [{"name": "2026", "values": [4, 5]}]}},
    {"layout": "statement", "text": "نطلب الموافقة على الخطة", "eyebrow": "القرار"},
    title="خطة النمو", language="ar")


def test_a_right_to_left_deck_reads_from_the_right():
    deck = presentation(ARABIC_DECK)
    slides = list(deck.slides)

    def named(slide, name):
        return [box(shape) for shape in slide.shapes if shape.name == name]
    assert named(slides[0], "Title")[0][2] == pytest.approx(design.WIDTH - design.MARGIN)  # Title column: right.
    first, second = named(slides[2], "Metric card")
    assert first[0] > second[0]                                        # The first metric is the rightmost.
    first, second = named(slides[3], "Card")
    assert first[0] > second[0]
    steps = named(slides[5], "Step")
    assert steps[0][0] > steps[1][0] > steps[2][0]                    # The timeline runs right to left.
    assert named(slides[6], "Chart")[0][0] > named(slides[6], "Takeaway")[0][0]
    number, title = named(slides[1], "Slide number")[0], named(slides[1], "Footer title")[0]
    assert number[0] < title[0]                                        # The page number at the far side.
    table = next(shape for shape in slides[4].shapes if shape.has_table).table
    assert table._tbl.tblPr.get("rtl") == "1"
    assert table.cell(1, 1).text_frame.paragraphs[0].alignment == pptx.enum.text.PP_ALIGN.LEFT  # Numbers: far edge.
    for slide in slides:
        for shape in text_shapes(slide):
            assert shape.text_frame.paragraphs[0]._p.pPr.get("rtl") == "1", shape.name
    check_geometry(deck)


def test_east_asian_text_uses_the_languages_face_and_wraps_between_characters():
    point = "売上高は前年比で二十三パーセント増加し、すべての地域で過去最高を記録しました。" * 2
    raw = deck_of({"layout": "points", "title": "来年の成長計画", "points": [point]}, title="成長計画", language="ja")
    data = generate(copy.deepcopy(raw), "pptx").data
    xml = zipfile.ZipFile(io.BytesIO(data)).read("ppt/slides/slide1.xml").decode("utf-8")
    assert set(re.findall(r'<a:ea typeface="([^"]+)"', xml)) == {"Yu Gothic"}
    assert len(design.wrap("売上高は前年比で" * 10, 3.0, 20)) > 1                 # No spaces needed to break.
    check_geometry(pptx.Presentation(io.BytesIO(data)))


# --- measurement ---------------------------------------------------------------------------


def test_measurement_follows_the_metric_compatible_fonts_advance_widths():
    from fontTools.ttLib import TTFont
    from kalillac_files.typeset import FONTS
    for bold, name in ((False, "Carlito-Regular.ttf"), (True, "Carlito-Bold.ttf")):
        font = TTFont(FONTS / "metrics" / name)
        em, cmap, advances = font["head"].unitsPerEm, font.getBestCmap(), font["hmtx"].metrics
        for text in ("M", "i i", "Revenue $4.2M", "Wholesale now funds the rest"):
            expected = sum(advances[cmap[ord(char)]][0] for char in text) / em   # At 72 pt, one em is one inch.
            assert design.width(text, 72, bold) == pytest.approx(expected, abs=0.004), (name, text)
    assert design.width("Total", 20, bold=True) > design.width("Total", 20)
    assert design.width("漢字", 36) == pytest.approx(1.0)                          # Full-width: one em each.


def test_wrapping_breaks_at_spaces_and_splits_only_a_word_longer_than_the_line():
    assert design.wrap("alpha beta gamma", 100, 12) == ["alpha beta gamma"]
    lines = design.wrap("alpha beta gamma", design.width("alpha beta", 12) + 0.01, 12)
    assert lines == ["alpha beta", "gamma"]
    long = design.wrap("x" * 200, 1.0, 12)
    assert len(long) > 1 and all(design.width(line, 12) <= 1.0 for line in long)
    assert "".join(long) == "x" * 200


def test_balancing_evens_out_a_headings_lines_without_adding_one():
    text = "The case for doubling capacity, and how we will do it"
    full = design.wrap(text, 6.3, 22)
    narrower = design.balanced(text, 6.3, 22)
    even = design.wrap(text, narrower, 22)
    assert len(full) == len(even) == 2 and narrower < 6.3
    assert len(full[-1]) < 6 and len(even[-1]) > 15          # "it" alone before; an even second line after.
    assert design.balanced("One line", 6.3, 22) == 6.3


def test_fitting_takes_the_largest_size_that_fits_and_refuses_what_never_will():
    assert design.fit("Short", 5.0, 1.0, 32, 20).size == 32
    smaller = design.fit(words(200), 6.0, 1.2, 32, 12)
    assert 12 <= smaller.size < 32 and smaller.height() <= 1.2
    with pytest.raises(SpecError) as error:
        design.fit(words(1000), 2.0, 0.5, 20, 12)
    assert error.value.code == "slide_text_too_long"
    assert design._Writer.clip(words(200), 3.0, 12).endswith("…")
    assert design.width(design._Writer.clip(words(200), 3.0, 12), 12) <= 3.0
