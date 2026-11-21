"""The document design system: specification, charts, fonts, typesetting and Word output.

Every file produced here is read back by an independent engine: PDFs by PDFium
(layout inspection) and pypdf (text and structure), Word files by python-docx
and by reading the package XML. Content is synthetic; nothing touches the
application or the network.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import re
import zipfile

import pytest

pytest.importorskip("typst")
pytest.importorskip("pypdfium2")
pypdf = pytest.importorskip("pypdf")
docx = pytest.importorskip("docx")
pytest.importorskip("fontTools")

from kalillac_files import charts, generate, quality, spec, typeset  # noqa: E402
from kalillac_files.docx_design import _obfuscate, normalize, script_segments  # noqa: E402
from kalillac_files.spec import SpecError  # noqa: E402

LONG_URL = ("https://example.test/a/very/long/path/that/never/breaks/because-it-has-no-spaces_and_keeps_going"
            "/index.html?query=1&other=2")
LONG_WORD = "Pneumonoultramicroscopicsilicovolcanoconiosis" * 3

REPORT = {
    "title": "Northwind Coffee Roasters: 2026 Annual Business Review",
    "subtitle": "Growth, margins and the plan for a second roastery",
    "organization": "Northwind Coffee Roasters", "author": "Finance and Strategy", "date": "November 2026",
    "cover": True, "contents": True,
    "summary": "Revenue grew 23% to $18.4M while gross margin widened by 2.1 points.",
    "blocks": [
        {"type": "heading", "level": 1, "text": "The year at a glance"},
        {"type": "paragraph", "text": [{"text": "2026 was the strongest year. "},
                                       {"text": "Revenue reached $18.4M", "bold": True}, {"text": "."}]},
        {"type": "metrics", "items": [{"label": "Revenue", "value": "$18.4M", "detail": "23% vs 2025", "trend": "up"},
                                      {"label": "Gross margin", "value": "41.6%", "detail": "2.1 pts", "trend": "up"},
                                      {"label": "Churn", "value": "2.4%", "detail": "0.6 pts", "trend": "down"}]},
        {"type": "chart", "kind": "column", "title": "Revenue by quarter", "caption": "Quarterly revenue.",
         "categories": ["Q1", "Q2", "Q3", "Q4"], "prefix": "$",
         "series": [{"name": "2025", "values": [3310000, 3620000, 3840000, 4230000]},
                    {"name": "2026", "values": [4050000, 4410000, 4690000, 5250000]}]},
        {"type": "heading", "level": 2, "text": "What drove growth"},
        {"type": "list", "ordered": False, "items": [[{"text": "Wholesale. ", "bold": True}, {"text": "42 accounts."}],
                                                     "Retention."]},
        {"type": "callout", "tone": "success", "title": "Key result", "text": "Acquisition cost fell 14%."},
        {"type": "heading", "level": 1, "text": "Financial performance"},
        {"type": "table", "caption": "Income statement, $ thousands", "total_row": True,
         "header": ["Line item", "2025", "2026", "Change"],
         "rows": [["Revenue", "15,000", "18,400", "+22.7%"], ["Cost of goods sold", "(9,075)", "(10,746)", "+18.4%"],
                  ["Gross profit", "5,925", "7,654", "+29.2%"]]},
        {"type": "chart", "kind": "donut", "title": "Revenue mix",
         "categories": ["Wholesale", "Subscriptions", "Cafes"], "prefix": "$",
         "series": [{"name": "Revenue", "values": [10304000, 5520000, 2576000]}]},
        {"type": "quote", "text": "Wholesale pays for everyone else's growth.", "attribution": "The CEO"},
        {"type": "heading", "level": 1, "text": "The plan"},
        {"type": "comparison", "columns": [{"title": "Build", "items": ["Adds capacity", "$4.2M"]},
                                           {"title": "Outsource", "items": ["No capital", "Lower margin"]}]},
        {"type": "steps", "items": [{"title": "Secure the site", "text": "Sign the lease."},
                                    {"title": "Order equipment"}, {"title": "Commission"}]},
        {"type": "chart", "kind": "line", "title": "Revenue outlook", "caption": "Revenue, $ millions.",
         "categories": ["2025", "2026", "2027"],
         "series": [{"name": "With", "values": [15, 18.4, 20.6]}, {"name": "Without", "values": [15, 18.4, None]}]},
    ],
}

# Code rendering is exercised by a technical note of its own, never by an executive report.
TECHNICAL_NOTE = {
    "title": "Margin calculation: technical note", "organization": "Northwind Coffee Roasters",
    "blocks": [{"type": "heading", "level": 1, "text": "Gross margin"},
               {"type": "paragraph", "text": "The finance model computes gross margin as follows."},
               {"type": "code", "language": "python", "text": "def margin(r, c):\n    return (r - c) / r"}],
}
ARABIC = {
    "title": "تقرير الأداء السنوي لعام 2026", "language": "ar", "organization": "شركة الشمال",
    "blocks": [{"type": "heading", "level": 1, "text": "لمحة عن العام"},
               {"type": "paragraph", "text": "بلغت الإيرادات 18.4 مليون دولار، مع كلمات مثل Gross margin داخل الجملة."},
               {"type": "table", "header": ["البند", "2025", "2026"], "rows": [["الإيرادات", "15,000", "18,400"]]},
               {"type": "callout", "title": "ملاحظة", "text": "الطاقة الإنتاجية هي القيد الرئيسي."}],
}


def pdf_of(raw):
    return typeset.typeset(spec.validate_document(copy.deepcopy(raw)))


def text_pages(data):
    return [page.extract_text() for page in pypdf.PdfReader(io.BytesIO(data)).pages]


# --- specification v2 ---------------------------------------------------------------------


def test_rich_text_is_data_never_markup():
    document = spec.validate_document({"title": "T", "blocks": [{"type": "paragraph", "text": [
        {"text": "plain "}, {"text": "*bold*", "bold": True}, {"text": "#set page(width: 1pt)", "code": True}]}]})
    runs = document.blocks[0].runs
    assert [r.text for r in runs] == ["plain ", "*bold*", "#set page(width: 1pt)"] and runs[1].bold and runs[2].code
    pages = text_pages(typeset.typeset(document))
    assert "#set page(width: 1pt)" in pages[0] and "*bold*" in pages[0]   # Shown, not obeyed.


@pytest.mark.parametrize("block, code", [
    ({"type": "paragraph", "text": []}, "invalid_paragraph"),
    ({"type": "paragraph", "text": [{"text": "x", "underline": True}]}, "invalid_paragraph"),
    ({"type": "paragraph", "text": [{"text": "x", "bold": "yes"}]}, "invalid_paragraph"),
    ({"type": "paragraph", "text": [{"text": "   "}]}, "invalid_paragraph"),
    ({"type": "callout", "text": "x", "tone": "purple"}, "invalid_callout"),
    ({"type": "metrics", "items": []}, "invalid_metrics"),
    ({"type": "metrics", "items": [{"label": "a", "value": "1"}] * 5}, "invalid_metrics"),
    ({"type": "metrics", "items": [{"label": "a", "value": "1", "trend": "sideways"}]}, "invalid_metrics"),
    ({"type": "metrics", "items": [{"label": "a", "value": "x" * 25}]}, "invalid_metrics"),
    ({"type": "chart", "kind": "pie3d", "categories": ["a"], "series": [{"name": "s", "values": [1]}]}, "invalid_chart"),
    ({"type": "chart", "kind": "column", "categories": ["a", "b"], "series": [{"name": "s", "values": [1]}]},
     "invalid_chart"),
    ({"type": "chart", "kind": "column", "categories": ["a"], "series": [{"name": "s", "values": [None]}]},
     "invalid_chart"),
    ({"type": "chart", "kind": "column", "categories": ["a"], "series": [{"name": "s", "values": [float("nan")]}]},
     "invalid_chart"),
    ({"type": "chart", "kind": "column", "categories": ["a"], "series": [{"name": "s", "values": [True]}]},
     "invalid_chart"),
    ({"type": "chart", "kind": "donut", "categories": ["a", "b"], "series": [{"name": "s", "values": [1, -1]}]},
     "invalid_chart"),
    ({"type": "chart", "kind": "donut", "categories": ["a"], "series": [{"name": "s", "values": [1]},
                                                                         {"name": "t", "values": [2]}]}, "invalid_chart"),
    ({"type": "chart", "kind": "line", "categories": ["a"], "series": [{"name": "s", "values": [1]}]}, "invalid_chart"),
    ({"type": "chart", "kind": "stacked", "categories": ["a"], "series": [{"name": "s", "values": [-1]}]},
     "invalid_chart"),
    ({"type": "chart", "kind": "column", "categories": ["a"], "series": [{"name": "s", "values": [1]}],
      "prefix": "<script>"}, "invalid_chart"),
    ({"type": "steps", "items": [{"title": "only one"}]}, "invalid_steps"),
    ({"type": "comparison", "columns": [{"title": "a", "items": ["x"]}]}, "invalid_comparison"),
    ({"type": "code", "text": "x", "language": "py thon"}, "invalid_code"),
    ({"type": "table", "header": ["a"], "rows": [], "total_row": True}, "invalid_table"),
    ({"type": "table", "header": ["a"], "rows": [["1"]], "align": ["justify"]}, "invalid_table"),
])
def test_invalid_design_blocks_are_refused(block, code):
    with pytest.raises(SpecError) as refused:
        spec.validate_document({"title": "T", "blocks": [block]})
    assert refused.value.code == code


@pytest.mark.parametrize("options", [{"theme": "neon"}, {"page": "a3"}, {"cover": "yes"}, {"contents": 1},
                                     {"language": "english"}, {"language": "en_US"}, {"subtitle": "a\nb"},
                                     {"logo": "https://example.test/logo.png"}])
def test_invalid_document_options_are_refused(options):
    with pytest.raises(SpecError):
        spec.validate_document({"title": "T", "blocks": [{"type": "paragraph", "text": "x"}], **options})


def test_numeric_columns_are_detected_for_alignment():
    table = spec.validate_document({"title": "T", "blocks": [{"type": "table", "header": ["Item", "Amount", "Share",
                                                                                          "Note"],
        "rows": [["A", "$1,234.50", "12%", "first"], ["B", "(9,075)", "-3.5 pp", "1 note"], ["C", "", "€4.2M", "x"]]}]}
    ).blocks[0]
    assert table.align == ("left", "right", "right", "left")


# --- charts -------------------------------------------------------------------------------


@pytest.mark.parametrize("low, high, expected", [
    (0, 5.25, (0, 6, 2)), (3.3e6, 5.25e6, (0, 6e6, 2e6)), (-120, 80, (-150, 100, 50)), (0, 0, (0, 1, 0.2)),
    (0.00012, 0.00045, (0, 0.0005, 0.0001)),
])
def test_axis_scales_are_round_and_include_zero(low, high, expected):
    assert tuple(round(v, 12) for v in charts.nice_scale(low, high)) == expected


@pytest.mark.parametrize("low, high, expected", [
    (68.7, 97.4, (60, 100, 10)),           # A narrow band far from zero: the axis fits the band.
    (-97.4, -68.7, (-100, -60, 10)),
    (20, 97, (0, 100, 20)),                # A wide range keeps zero.
    (-5, 40, (-10, 40, 10)),               # Data crossing zero keeps zero.
])
def test_a_line_may_drop_the_zero_baseline_only_for_a_narrow_band(low, high, expected):
    assert tuple(round(v, 12) for v in charts.nice_scale(low, high, zero=False)) == expected
    assert charts.nice_scale(low, high)[0] <= 0 <= charts.nice_scale(low, high)[1]   # Bars always include zero.


@pytest.mark.parametrize("value, prefix, suffix, expected", [
    (4050000, "$", "", "$4.0M"), (5250000, "$", "", "$5.2M"), (31200, "", "", "31.2K"), (1500, "", "", "1,500"),
    (58.2, "", "%", "58.2%"), (-2.5, "", "", "−2.50"), (3e9, "$", "", "$3B"), (0, "$", "", "$0"),
])
def test_values_read_like_a_person_wrote_them(value, prefix, suffix, expected):
    assert charts.format_value(value, prefix, suffix) == expected


CHART_CASES = [
    {"kind": "column", "categories": ["An extremely long category name number " + str(n) for n in range(14)],
     "series": [{"name": "A long series name for the legend " * 1, "values": [n * 1234567.891 for n in range(14)]},
                {"name": "B", "values": [-n * 999999.5 for n in range(14)]}]},
    {"kind": "bar", "categories": [LONG_WORD[:60], "b", "c"], "series": [{"name": "s", "values": [0.000123, 0.00045,
                                                                                                -0.0002]}]},
    {"kind": "donut", "categories": [LONG_WORD[:60], "b", "c", "d", "e", "f", "g", "h"],
     "series": [{"name": "s", "values": [1, 0, 3e9, 4, 5, 6, 7, 8]}]},
    {"kind": "stacked", "categories": [str(n) for n in range(40)],
     "series": [{"name": f"S{i}", "values": [float(n % 7 + i) for n in range(40)]} for i in range(6)]},
    {"kind": "area", "categories": ["Jan", "Feb", "Mar"], "series": [{"name": "s", "values": [1, None, 3]}]},
    {"kind": "line", "categories": [str(2000 + n) for n in range(40)],
     "series": [{"name": "x", "values": [n ** 1.5 for n in range(40)]}], "prefix": "$", "suffix": "M"},
]


@pytest.mark.parametrize("raw", CHART_CASES)
def test_every_chart_mark_and_label_stays_on_its_canvas(raw):
    chart = spec.validate_document({"title": "T", "blocks": [{"type": "chart", **raw}]}).blocks[0]
    assert charts.check_bounds(chart) == []
    svg = charts.chart_svg(chart)
    assert svg.startswith("<svg") and "<script" not in svg and "href" not in svg and "<image" not in svg
    assert len(charts.describe(chart)) <= 1500


def test_chart_text_is_escaped():
    chart = spec.validate_document({"title": "T", "blocks": [{"type": "chart", "kind": "column",
        "categories": ["<b>&amp;</b>"], "series": [{"name": "\"><script>", "values": [1]}]}]}).blocks[0]
    svg = charts.chart_svg(chart)
    assert "<b>" not in svg and "<script>" not in svg and "&lt;b&gt;&amp;amp;" in svg


# --- fonts --------------------------------------------------------------------------------


def test_every_bundled_font_matches_its_recorded_hash_and_has_its_license():
    manifest = json.loads((typeset.FONTS / "manifest.json").read_text(encoding="utf-8"))
    licenses = sorted(path.name for path in (typeset.FONTS / "licenses").iterdir())
    assert licenses == ["Carlito-OFL.txt", "IBMPlexSansArabic-OFL.txt", "Inter-OFL.txt", "JetBrainsMono-OFL.txt",
                        "NotoSansArabic-OFL.txt", "NotoSansCJK-OFL.txt",
                        "NotoSansDevanagari-OFL.txt", "NotoSansHebrew-OFL.txt", "NotoSansThai-OFL.txt",
                        "SourceSerif4-OFL.md"]
    for path in (typeset.FONTS / "licenses").iterdir():
        assert "SIL Open Font License" in path.read_text(encoding="utf-8"), path.name
    core = [entry for entry in manifest["fonts"] if entry["group"] == "core"]
    assert len(core) == 26 and all(entry["license"] == "OFL-1.1" for entry in manifest["fonts"])
    for entry in core:
        data = (typeset.FONTS / entry["file"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == entry["sha256"], entry["file"]
        assert entry["url"].startswith(("https://github.com/", "https://raw.githubusercontent.com/notofonts/",
                                        "https://raw.githubusercontent.com/google/fonts/"))
    on_disk = {path.name for path in typeset.FONTS.glob("*.[ot]tf")}
    assert on_disk == {entry["file"] for entry in core}       # Nothing unlisted is ever typeset with.
    metrics = [entry for entry in manifest["fonts"] if entry["group"] == "metrics"]
    assert sorted(entry["file"] for entry in metrics) == ["metrics/Carlito-Bold.ttf", "metrics/Carlito-Regular.ttf"]
    for entry in metrics:                                      # Measures slide text; never embedded or typeset.
        data = (typeset.FONTS / entry["file"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == entry["sha256"], entry["file"]


def test_only_bundled_fonts_reach_the_pdf_and_they_are_subsetted():
    data = pdf_of({**REPORT, "blocks": REPORT["blocks"] + [{"type": "paragraph", "text": "日本語と한국어とالعربية"}]})
    names = set()
    for page in pypdf.PdfReader(io.BytesIO(data)).pages:
        for font in (page.get("/Resources", {}).get("/Font", {}) or {}).values():
            names.add(str(font.get_object().get("/BaseFont")))
    families = {re.sub(r"^[A-Z]{6}\+", "", name.lstrip("/")).split("-")[0] for name in names}
    assert families <= {"IBMPlexSansArabic", "Inter", "InterDisplay", "JetBrainsMono", "NotoSansArabic", "NotoSansJP",
                        "NotoSansKR", "NotoSansSC", "SourceSerif4"}, families
    assert all(re.match(r"^/?[A-Z]{6}\+", name.lstrip("/")) for name in names)  # Subset prefixes.
    assert len(data) < 600_000


# --- typesetting --------------------------------------------------------------------------


def test_the_benchmark_report_is_sound_structured_and_accessible():
    data = generate(copy.deepcopy(REPORT), "pdf").data          # generate() itself inspects the layout.
    report = quality.inspect_pdf(data, full_bleed_first_page=True)
    assert report.sound and report.pages >= 4 and report.missing_glyphs == 0
    reader = pypdf.PdfReader(io.BytesIO(data))
    assert reader.metadata.title == REPORT["title"] and reader.metadata.author == "Finance and Strategy"
    assert "/StructTreeRoot" in reader.trailer["/Root"]             # A tagged PDF.
    assert "/Outlines" in reader.trailer["/Root"]                   # Bookmarks for every heading.
    pages = [" ".join(page.split()) for page in text_pages(data)]
    assert REPORT["title"] in pages[0]
    contents = pages[1]
    for heading in ("The year at a glance", "Financial performance", "The plan", "What drove growth"):
        assert heading in contents
    body = " ".join(pages[2:])
    for expected in ("Revenue reached $18.4M", "$18.4M", "41.6%", "Key result", "Acquisition cost fell 14%.",
                     "Gross profit", "7,654", "Figure 1", "Wholesale pays for everyone else's growth.",
                     "Secure the site", "Outsource"):
        assert expected in body, expected
    assert "def margin" not in body                                  # No code in an executive review (Q10).
    assert "Page 1 of" in pages[2] and f"Page {len(pages) - 2} of {len(pages) - 2}" in pages[-1]


@pytest.mark.parametrize("name", ["RESEARCH_DOCUMENT", "WORD_PROPOSAL", "MULTILINGUAL_DOCUMENT"])
def test_every_benchmark_document_is_sound_in_pdf_and_word(name):
    import quality_benchmark
    raw = getattr(quality_benchmark, name)
    data = generate(copy.deepcopy(raw), "pdf").data                 # generate() itself inspects the layout.
    report = quality.inspect_pdf(data, full_bleed_first_page=raw.get("cover", False))
    assert report.sound and report.missing_glyphs == 0, report
    word = docx.Document(io.BytesIO(generate(copy.deepcopy(raw), "docx").data))
    assert word.core_properties.title == raw["title"]


def test_justified_text_may_hang_punctuation_but_not_overflow():
    import quality_benchmark
    # The editorial theme justifies and hyphenates: hyphens and punctuation hang into the margin by design.
    research = quality.inspect_pdf(generate(copy.deepcopy(quality_benchmark.RESEARCH_DOCUMENT), "pdf").data,
                                   full_bleed_first_page=True)
    assert research.sound, research.overflow
    assert quality.HANG < 4 and set(".,-") <= quality.HANGING and not set("abcXYZ019") & quality.HANGING


def test_a_long_summary_fits_the_word_file_record_without_losing_the_text():
    summary = "A summary that runs long. " * 40
    data = generate({"title": "T", "summary": summary, "blocks": [{"type": "paragraph", "text": "Body."}]},
                    "docx").data
    document = docx.Document(io.BytesIO(data))
    assert len(document.core_properties.comments) <= 255 and document.core_properties.comments.endswith("…")
    assert summary.strip() in " ".join(p.text for t in document.tables for row in t.rows for c in row.cells
                                       for p in c.paragraphs) + " ".join(p.text for p in document.paragraphs)


def test_a_right_to_left_callout_in_a_left_to_right_word_file_reads_from_the_right():
    raw = {"title": "Guest information", "blocks": [
        {"type": "callout", "title": "خدمة الغرف", "text": "اطلب خدمة الغرف على الرقم 9 على مدار الساعة."},
        {"type": "callout", "title": "Room service", "text": "Dial 9 at any hour."}]}
    xml = zipfile.ZipFile(io.BytesIO(generate(raw, "docx").data)).read("word/document.xml").decode("utf-8")
    arabic, english = re.findall(r"<w:tcBorders>.*?</w:tcBorders>", xml, re.S)[:2]
    assert "<w:right " in arabic and "<w:left " not in arabic
    assert "<w:left " in english and "<w:right " not in english


def test_typesetting_is_reproducible():
    assert pdf_of(REPORT) == pdf_of(REPORT)


def test_charts_are_vector_art_with_alternative_text():
    data = pdf_of({"title": "T", "blocks": [REPORT["blocks"][3]]})
    reader = pypdf.PdfReader(io.BytesIO(data))
    assert not any("/Image" in str(x.get_object().get("/Subtype")) for page in reader.pages
                   for x in (page.get("/Resources", {}).get("/XObject", {}) or {}).values())
    assert b"Column chart of Revenue by quarter" in data or "Column chart" in str(reader.trailer["/Root"]["/StructTreeRoot"])


ADVERSARIAL = {
    "long-url-paragraph": [{"type": "paragraph", "text": "See " + LONG_URL + " for details. " + LONG_WORD}],
    "long-heading": [{"type": "heading", "level": 1, "text": LONG_WORD}, {"type": "heading", "level": 2, "text": LONG_URL}],
    "wide-table": [{"type": "table", "header": [f"Column {n}" for n in range(12)],
                    "rows": [[f"value{n}-{r}-" + "x" * 14 for n in range(12)] for r in range(6)]}],
    "url-table": [{"type": "table", "header": ["Name", "Link", "Amount"],
                   "rows": [["Alpha", LONG_URL, "1,200"], ["Beta " + LONG_WORD, "short", "(4,500.25)"]]}],
    "numbers-table": [{"type": "table", "header": list("ABCDEFGH"), "rows": [["1,234,567,890.12"] * 8] * 4}],
    "metrics": [{"type": "metrics", "items": [
        {"label": "A very long metric label that goes on", "value": "$1,234,567,890.12",
         "detail": "compared with the same period last year", "trend": "up"},
        {"label": "Second", "value": "12,345,678,901"}, {"label": "Third", "value": "99.99%"},
        {"label": "Fourth extraordinarily long label", "value": "1234567890123456789"}]}],
    "callout-quote-code": [{"type": "callout", "title": LONG_WORD, "text": LONG_URL},
                           {"type": "quote", "text": LONG_WORD + " " + LONG_URL, "attribution": LONG_URL},
                           {"type": "code", "text": "x = '" + "a" * 300 + "'\n" + LONG_URL}],
    "steps-comparison": [{"type": "steps", "items": [{"title": LONG_WORD[:110], "text": LONG_URL}, {"title": "b"}]},
                         {"type": "comparison", "columns": [{"title": LONG_WORD[:110], "items": [LONG_URL, "ok"]},
                                                            {"title": "B", "items": [LONG_WORD]},
                                                            {"title": "C", "items": ["fine"]}]}],
    "charts": [{"type": "chart", **case} for case in CHART_CASES],
}


@pytest.mark.parametrize("name", sorted(ADVERSARIAL))
@pytest.mark.parametrize("cover", [False, True])
def test_hostile_content_never_breaks_the_page(name, cover):
    raw = {"title": "Adversarial " + name, "subtitle": LONG_URL, "organization": LONG_WORD[:100], "author": "A" * 100,
           "date": "D" * 50, "summary": LONG_WORD + " " + LONG_URL, "blocks": ADVERSARIAL[name],
           "cover": cover, "contents": cover}
    report = quality.inspect_pdf(pdf_of(raw), full_bleed_first_page=cover)
    assert report.sound, report


def test_the_inspector_catches_real_defects():
    import typst
    overflowing = typst.compile(b'#set page(paper: "a4", margin: 22mm)\n#box(width: 300mm)[' + b"W" * 200 + b"]",
                                format="pdf", ignore_system_fonts=True, font_paths=[str(typeset.FONTS)])
    report = quality.inspect_pdf(overflowing)
    assert not report.sound and report.overflow
    with pytest.raises(SpecError) as refused:
        quality.check_pdf(overflowing)
    assert refused.value.code == "pdf_layout_failed"
    blank = typst.compile(b"Page one\n#pagebreak()\n#pagebreak()\nPage three", format="pdf", ignore_system_fonts=True,
                          font_paths=[str(typeset.FONTS)])
    assert quality.inspect_pdf(blank).blank_pages == [2]


def test_soft_breaks_never_change_the_words():
    for text in (LONG_URL, LONG_WORD, "plain words only", "日本語" * 20, "a.b.c.d.e.f.g.h.i.j.k.l.m.n.o.p.q"):
        assert typeset.soften(text).replace("​", "") == text
    assert "​" not in typeset.soften("short")
    assert "​" not in typeset.soften("日本語" * 20)               # CJK already breaks anywhere.


# --- multilingual -------------------------------------------------------------------------


def test_right_to_left_documents_mirror_and_keep_their_numbers():
    """Appearance is checked by eye in the benchmark. PDF text layers of Arabic, Devanagari and
    Thai do not survive extraction letter-for-letter (nor do Word's own PDF exports of them), so
    this checks what extraction can confirm; the Word file carries the exact text."""
    data = pdf_of(ARABIC)
    assert quality.inspect_pdf(data).sound
    reader = pypdf.PdfReader(io.BytesIO(data))
    text = reader.pages[0].extract_text()
    for word in ("18,400", "15,000", "2026"):
        assert word in text, word
    assert sum("\u0600" <= char <= "\u06ff" for char in text) > 80     # The Arabic is real text, not outlines.
    assert reader.trailer["/Root"].get("/Lang") == "ar"
    word = docx.Document(io.BytesIO(generate(copy.deepcopy(ARABIC), "docx").data))
    assert "لمحة عن العام" in [p.text for p in word.paragraphs]
    data = typeset.document_data(spec.validate_document(ARABIC))
    assert data["rtl"] is True and data["labels"]["page"] == "صفحة"
    table = next(b for b in data["blocks"] if b["t"] == "table")
    assert table["dirs"][0] == ["", "ltr", "ltr"]                       # Quantities stay left-to-right.
    paragraph = next(b for b in data["blocks"] if b["t"] == "paragraph")
    assert "dir" not in paragraph or paragraph["dir"] is None


@pytest.mark.parametrize("language, font", [("ja", "Noto Sans JP"), ("zh", "Noto Sans SC"), ("zh-TW", "Noto Sans TC"),
                                            ("ko", "Noto Sans KR"), ("en", "Noto Sans SC")])
def test_cjk_text_prefers_the_documents_own_regional_forms(language, font):
    stacks = typeset.font_stacks(language, "modern")
    cjk = [name for name in stacks["body"] if name.startswith("Noto Sans") and name[-2:] in ("JP", "SC", "TC", "KR")]
    assert cjk[0] == font and len(cjk) == 4


def test_uncovered_characters_are_found_exactly():
    assert typeset.uncovered(["Hello", "日本語", "Ελληνικά", "हिन्दी", "‍️ "]) == set()
    assert typeset.uncovered(["emoji \U0001f600"]) == {"\U0001f600"}


# --- Word -----------------------------------------------------------------------------------


def docx_parts(raw):
    data = generate(copy.deepcopy(raw), "docx").data
    archive = zipfile.ZipFile(io.BytesIO(data))
    return data, archive, archive.read("word/document.xml").decode("utf-8")


def test_word_report_has_real_navigation_fields_and_styles():
    data, archive, xml = docx_parts(REPORT)
    assert archive.testzip() is None
    document = docx.Document(io.BytesIO(data))
    styles = [(p.style.name, p.text) for p in document.paragraphs if p.text]
    assert ("Heading 1", "The year at a glance") in styles and ("Heading 2", "What drove growth") in styles
    assert ("Caption", "Figure 1  Quarterly revenue.") in styles
    assert 'TOC \\o "1-2" \\h \\z \\u' in xml and 'w:dirty="true"' in xml          # Refreshed when Word opens it.
    headers = "".join(archive.read(name).decode() for name in archive.namelist() if name.startswith("word/header"))
    footers = "".join(archive.read(name).decode() for name in archive.namelist() if name.startswith("word/footer"))
    assert 'STYLEREF "Heading 1"' in headers and " PAGE " in footers and " SECTIONPAGES " in footers
    assert '<w:pgNumType w:start="1"/>' in xml                                      # Numbered from the body.
    assert len(document.sections) == 3                                              # Cover, contents, body.
    assert document.core_properties.title == REPORT["title"]
    settings = archive.read("word/settings.xml").decode()
    assert 'w:name="compatibilityMode" w:uri="http://schemas.microsoft.com/office/word" w:val="15"' in settings
    assert "<w:embedTrueTypeFonts/>" in settings


def test_word_carries_only_this_documents_record_and_is_reproducible():
    data, archive, _xml = docx_parts(REPORT)
    names = archive.namelist()
    assert not [name for name in names if "thumbnail" in name or name.startswith("customXml/")]
    core = docx.Document(io.BytesIO(data)).core_properties
    assert core.author == core.last_modified_by == REPORT.get("author", "Kalillac AI")
    assert core.comments == REPORT.get("summary", "") and core.created is None and core.modified is None
    app = archive.read("docProps/app.xml").decode("utf-8")
    assert "<Application>Kalillac AI</Application>" in app and "Macintosh" not in app
    everything = b"".join(archive.read(name) for name in names if name.endswith(".xml"))
    assert b"python-docx" not in everything
    assert {item.date_time for item in archive.infolist()} == {(1980, 1, 1, 0, 0, 0)}
    assert generate(copy.deepcopy(REPORT), "docx").data == data


def test_word_tables_repeat_headers_keep_rows_together_and_align_numbers():
    data, _archive, _xml = docx_parts(REPORT)
    document = docx.Document(io.BytesIO(data))
    table = next(t for t in document.tables if t.rows[0].cells[0].text == "Line item")
    header = table.rows[0]._tr.trPr.xml
    assert "w:tblHeader" in header and "w:cantSplit" in header
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    assert table.rows[1].cells[2].paragraphs[0].paragraph_format.alignment == WD_ALIGN_PARAGRAPH.RIGHT
    assert table.rows[-1].cells[0].text == "Gross profit"


def test_word_charts_are_images_with_alternative_text():
    data, archive, xml = docx_parts(REPORT)
    images = [name for name in archive.namelist() if name.startswith("word/media/")]
    assert len(images) >= 4                                                     # Cover art and three charts.
    assert 'descr="Column chart of Revenue by quarter.' in xml


def test_word_embeds_inter_so_it_looks_the_same_everywhere():
    _data, archive, _xml = docx_parts(REPORT)
    font_table = archive.read("word/fontTable.xml").decode()
    relationships = archive.read("word/_rels/fontTable.xml.rels").decode()
    for slot in ("embedRegular", "embedBold", "embedItalic", "embedBoldItalic"):
        assert f"<w:{slot} " in font_table
    keys = dict(re.findall(r'r:id="(rIdFont\d+)" w:fontKey="(\{[0-9A-F-]+\})"', font_table))
    targets = dict(re.findall(r'Id="(rIdFont\d+)"[^>]*Target="(fonts/font\d+\.odttf)"', relationships))
    assert set(keys) == set(targets) and len(keys) == 5
    first = typeset.FONTS / "Inter-Regular.ttf"
    stored = archive.read("word/" + targets["rIdFont1"])
    assert _obfuscate(stored, keys["rIdFont1"]) == first.read_bytes()            # Obfuscation is reversible.
    assert stored[:32] != first.read_bytes()[:32]
    assert "application/vnd.openxmlformats-officedocument.obfuscatedFont" in archive.read(
        "[Content_Types].xml").decode()
    assert re.search(r'<w:font w:name="Inter">.*?<w:sig w:usb0="[0-9A-F]{8}"', font_table, re.S)


def test_word_right_to_left_document_is_mirrored_with_complex_script_fonts():
    _data, archive, xml = docx_parts(ARABIC)
    assert "<w:bidi/>" in xml and "<w:rtl/>" in xml and "<w:bidiVisual/>" in xml
    assert 'w:cs="IBM Plex Sans Arabic"' in xml
    font_table = archive.read("word/fontTable.xml").decode()
    assert '<w:font w:name="IBM Plex Sans Arabic">' in font_table and 'w:charset w:val="B2"' in font_table
    for run in re.findall(r"<w:rPr>(.*?)</w:rPr>", xml):                # No letter-spacing on Arabic runs.
        assert not ("<w:rtl/>" in run and "<w:spacing w:val=" in run), run


@pytest.mark.parametrize("text, base, expected", [
    ("plain", False, [("plain", False)]),
    ("بلغت 18.4 مليون", True, [("بلغت ", True), ("18.4", False), (" مليون", True)]),
    ("بنسبة 23% لتصل", True, [("بنسبة ", True), ("23%", False), (" لتصل", True)]),
    ("مثل Gross margin داخل", True, [("مثل ", True), ("Gross margin", False), (" داخل", True)]),
    ("Revenue of $5 rose", False, [("Revenue of $5 rose", False)]),
    ("Hello שלום world", False, [("Hello ", False), ("שלום", True), (" world", False)]),
])
def test_runs_split_by_script_with_neutral_characters_resolved(text, base, expected):
    pieces = script_segments(text, base)
    assert pieces == expected and "".join(piece for piece, _ in pieces) == text


def test_word_property_elements_are_in_schema_order():
    from docx.oxml import parse_xml
    element = parse_xml('<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:pPr>'
                        '<w:jc w:val="right"/><w:spacing w:after="0"/><w:bidi/><w:keepNext/><w:pStyle w:val="x"/>'
                        '</w:pPr><w:r><w:rPr><w:rtl/><w:sz w:val="20"/><w:b/><w:rFonts w:ascii="Inter"/>'
                        '<w:sz w:val="22"/></w:rPr></w:r></w:p>')
    normalize(element)
    order = [child.tag.split("}")[1] for child in element[0]]
    assert order == ["pStyle", "keepNext", "bidi", "spacing", "jc"]
    run = [child.tag.split("}")[1] for child in element[1][0]]
    assert run == ["rFonts", "b", "sz", "rtl"] and element[1][0][2].get(
        "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val") == "22"   # The later duplicate wins.
    _data, _archive, xml = docx_parts(REPORT)
    for p_pr in re.findall(r"<w:pPr>(.*?)</w:pPr>", xml):
        tags = re.findall(r"<w:([A-Za-z]+)[ />]", p_pr)
        positions = [list(("pStyle", "keepNext", "keepLines", "widowControl", "numPr", "pBdr", "shd", "tabs",
                           "bidi", "spacing", "ind", "jc", "rPr", "sectPr")).index(t) for t in tags
                     if t in ("pStyle", "keepNext", "keepLines", "widowControl", "numPr", "pBdr", "shd", "tabs",
                              "bidi", "spacing", "ind", "jc", "rPr", "sectPr")]
        assert positions == sorted(positions), p_pr[:200]



# --- Arabic text that survives extraction (Codex document audit, finding 5) -----------------------

LAM_ALEF = ("لا", "لأ", "لإ", "لآ")


def _extracted(data):
    import unicodedata
    import pypdfium2 as pdfium
    page = pdfium.PdfDocument(data)[0]
    textpage = page.get_textpage()
    return " ".join(unicodedata.normalize("NFKC", textpage.get_text_range()).split()), textpage


def test_arabic_pdf_text_keeps_every_letter_of_every_word():
    """Rendering alone is not proof: the PDF's text layer must give back the letters a reader sees.
    With Noto Sans Arabic, ب ت ث ن ي shared one dotless body glyph and their dots were separate glyphs,
    so PDFium returned "ققريير" for "تقرير" and found no heading. Now every source word's exact letters
    come back as one run, and headings are found by search.

    Known remaining limitation, tracked in the roadmap rather than hidden: PDFium (Chrome's viewer)
    and pypdf reverse the two letters of a required ligature glyph (lam-alef, and Plex's other rlig
    pairs) inside right-to-left runs, and PDFium may insert a space where a letter's tail leaves a
    gap. So letters are compared as a multiset over a window of the word's length, spaces ignored."""
    from collections import Counter
    data = pdf_of(ARABIC)
    text, textpage = _extracted(data)
    compact = text.replace(" ", "")
    labels = typeset.document_data(spec.validate_document(ARABIC))["labels"]
    source = [ARABIC["title"], ARABIC["organization"], labels["page"], labels["of"]] + [
        block.get("text", "") for block in ARABIC["blocks"]] + [block.get("title", "") for block in ARABIC["blocks"]]
    source += [cell for block in ARABIC["blocks"] if block["type"] == "table"
               for row in [block["header"], *block["rows"]] for cell in row]
    words = sorted({word.strip(".،:") for line in source for word in line.split()
                    if any("\u0600" <= char <= "\u06ff" for char in word)})
    assert len(words) >= 14

    def present(word):
        size, want = len(word), Counter(word)
        return any(Counter(compact[i:i + size]) == want for i in range(len(compact) - size + 1))
    assert [word for word in words if not present(word)] == []
    assert textpage.search("لمحة عن العام").get_next() is not None      # The heading, exactly.
    # Never a letter that is not in the source (the old text layer invented ق, ي and ت).
    assert set(char for char in compact if "\u0621" <= char <= "\u064a") <= set("".join(words))



# --- benchmark content (Codex document audit: Q1, Q7, Q8, Q10) -----------------------------------

def _thousands(text):
    return int(text.replace(",", "").strip("()$"))


def test_the_report_revenue_mix_reconciles_to_its_income_statement():
    """Q1: the channel breakdown summed to $17.112M against the $18.4M income statement."""
    statement = next(b for b in REPORT["blocks"] if b["type"] == "table" and b["header"][0] == "Line item")
    revenue = {row[0]: row for row in statement["rows"]}["Revenue"]
    mix = next(b for b in REPORT["blocks"] if b.get("kind") == "donut")
    assert sum(mix["series"][0]["values"]) == _thousands(revenue[2]) * 1000
    quarters = next(b for b in REPORT["blocks"] if b.get("title") == "Revenue by quarter")
    for series, column in ((quarters["series"][0], 1), (quarters["series"][1], 2)):
        assert sum(series["values"]) == _thousands(revenue[column]) * 1000, series["name"]
    gross = {row[0]: row for row in statement["rows"]}["Gross profit"]
    cost = {row[0]: row for row in statement["rows"]}["Cost of goods sold"]
    for column in (1, 2):
        assert _thousands(revenue[column]) - _thousands(cost[column]) == _thousands(gross[column])
    assert f"{_thousands(gross[2]) / _thousands(revenue[2]) * 100:.1f}%" == "41.6%"


def test_a_breakdowns_shown_parts_add_up_to_its_shown_total():
    """Q1, presentation: individually rounded labels ($9.6M + $5.2M + $2.4M) must never sit under a
    $17.1M total. The shown precision rises until the shown parts add up; shares total 100%."""
    from kalillac_files.charts import breakdown_labels
    for values in ([9568000, 5152000, 2392000], [10304000, 5520000, 2576000], [1, 1, 1], [333, 333, 334],
                   [1234567, 7654321, 1111111, 999999]):
        total, parts, shares = breakdown_labels(values, "$")
        number = lambda label: float(label.strip("$MK%").replace(",", ""))  # noqa: E731
        assert round(sum(number(part) for part in parts), 6) == number(total), (values, parts, total)
        assert round(sum(float(share[:-1]) for share in shares), 6) == 100, (values, shares)
    assert breakdown_labels([9568000, 5152000, 2392000], "$")[:2] == ("$17.11M", ["$9.57M", "$5.15M", "$2.39M"])


def test_the_research_report_follows_its_complete_follow_up():
    """Q7: enrolment to 30 November 2025 plus twelve months of follow-up ends 30 November 2026, so a
    report claiming complete follow-up cannot be dated October 2026."""
    from datetime import date
    import quality_benchmark
    research = quality_benchmark.RESEARCH_DOCUMENT
    method = next(b["text"] for b in research["blocks"] if b["type"] == "paragraph" and "joined between" in b["text"])
    assert "30 November 2025" in method and "followed for twelve months" in method
    follow_up_ends = date(2026, 11, 30)
    months = {"January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6, "July": 7, "August": 8,
              "September": 9, "October": 10, "November": 11, "December": 12}
    month, year = research["date"].split()
    assert date(int(year), months[month], 1) > follow_up_ends
    recommendation = next(b["text"] for b in research["blocks"] if b.get("title") == "Recommendation")
    start = recommendation.rsplit("from ", 1)[1].rstrip(".")
    assert date(int(start.split()[1]), months[start.split()[0]], 1) > date(int(year), months[month], 1)


def test_the_proposal_schedule_reconciles():
    """Q8: start, site access, fourteen weeks, the guaranteed opening and the offer's expiry agree."""
    from datetime import date, timedelta
    import quality_benchmark
    proposal = quality_benchmark.WORD_PROPOSAL
    text = " ".join(str(b.get("text", "")) for b in proposal["blocks"]) + " " + proposal["summary"]
    start, opening, access, expiry = date(2026, 11, 23), date(2027, 3, 1), date(2026, 12, 4), date(2026, 11, 20)
    assert "Monday 23 November 2026" in text and start.weekday() == 0
    assert "14 weeks" in proposal["summary"] and start + timedelta(weeks=14) == opening
    assert "valid until 20 November 2026" in text and expiry < start
    build = start + timedelta(weeks=5)                                 # Week 6 begins.
    assert access <= build
    steps = next(b for b in proposal["blocks"] if b["type"] == "steps")["items"]
    assert steps[-1]["title"].startswith("Weeks 13 to 14") and "1 March 2027" in text


def test_code_renders_in_a_technical_note():
    """Q10: code rendering keeps its coverage in a document where code belongs."""
    data = generate(copy.deepcopy(TECHNICAL_NOTE), "pdf").data
    assert "def margin(r, c):" in " ".join(" ".join(page.split()) for page in text_pages(data))
    xml = zipfile.ZipFile(io.BytesIO(generate(copy.deepcopy(TECHNICAL_NOTE), "docx").data)).read(
        "word/document.xml").decode("utf-8")
    assert "def margin(r, c):" in xml and "Consolas" in xml


def test_arabic_in_a_document_of_another_language_uses_the_arabic_face_in_word():
    """Owner decision 13: Arabic is IBM Plex Sans Arabic in every format, including inside an English
    document (whose complex-script face is otherwise Arial), and every face used is embedded."""
    import quality_benchmark
    _data, archive, xml = docx_parts(quality_benchmark.MULTILINGUAL_DOCUMENT)
    runs = re.findall(r"<w:r>(.*?)</w:r>", xml, re.S)
    arabic = [run for run in runs if re.search("[؀-ۿ]", run)]
    assert arabic and all('w:cs="IBM Plex Sans Arabic"' in run and "<w:rtl/>" in run for run in arabic)
    latin = [run for run in runs if re.search(r"<w:t[^>]*>[A-Za-z]", run)]
    assert latin and not any("IBM Plex Sans Arabic" in run for run in latin)
    font_table = archive.read("word/fontTable.xml").decode()
    assert font_table.count('<w:font w:name="IBM Plex Sans Arabic">') == 1 and "embedRegular" in font_table
    assert sum(name.startswith("word/fonts/") for name in archive.namelist()) == 7      # Inter x5 + Plex x2.


@pytest.mark.parametrize("text", ["أهلاً وسهلاً", "يُقدَّم الإفطار", "مرحباً 2026"])
def test_combining_marks_stay_in_their_letters_run(text):
    """A tanween or shadda is a combining mark: split into a run of its own it can render detached."""
    pieces = script_segments(text, base_rtl=False)
    for piece, rtl in pieces:
        if any("ً" <= char <= "ْ" for char in piece):
            assert rtl and any("ء" <= char <= "ي" for char in piece), pieces


@pytest.mark.parametrize("values", [[33333.4, 33333.4, 33333.2], [1, 1, 1, 1, 1, 1, 1], [100, 100, 100.001]])
def test_a_breakdown_that_cannot_add_up_as_shown_says_so(values):
    """Codex reverification, finding D: [33333.4, 33333.4, 33333.2] showed 33.333K three times under
    100.000K. Within two extra decimals no precision makes the shown parts add up, so the chart shows the
    total's own precision and discloses the rounding; the source values are never altered."""
    from kalillac_files.charts import ROUNDING_NOTE, breakdown
    source = list(values)
    shown = breakdown(values, "$")
    number = lambda label: float(label.strip("$MK%").replace(",", ""))  # noqa: E731
    adds_up = round(sum(number(part) for part in shown.parts), 6) == number(shown.total)
    assert adds_up or shown.note == ROUNDING_NOTE
    assert round(sum(float(share[:-1]) for share in shown.percents), 6) == 100 and values == source
    if shown.note:
        assert max(len(part.split(".")[-1]) if "." in part else 0 for part in shown.parts) <= 3


def test_the_rounding_disclosure_is_printed_on_the_chart_and_only_when_needed():
    from kalillac_files import charts as chart_module
    def drawn(values):
        chart = spec.validate_document({"title": "T", "blocks": [{"type": "chart", "kind": "donut", "title": "Mix",
            "categories": ["A", "B", "C"], "series": [{"name": "S", "values": values}]}]}).blocks[0]
        return chart_module.chart_svg(chart)
    assert chart_module.ROUNDING_NOTE in drawn([33333.4, 33333.4, 33333.2])
    assert chart_module.ROUNDING_NOTE not in drawn([10304000, 5520000, 2576000])


# --- page flow, table pagination and chart labels (document-quality benchmark, 2026-11-21) ----
# The benchmark report had pages left half empty because a table or chart was never divided or moved,
# a callout whose title was stranded at the foot of a page, Word tables cut after a row or two, and
# chart labels cut short where there was room to wrap. Each defect has its test here.

FILLER = ("The practice has sensible foundations and a dependable support provider, and the weaknesses are in how "
          "systems are accessed and recovered, not in the systems themselves. ") * 3
FLOW_CHART = {"type": "chart", "kind": "column", "title": "Chart title", "caption": "Caption.",
              "categories": ["A", "B", "C"], "series": [{"name": "S", "values": [1, 2, 3]}]}


def _filler(count, tag="LEAD"):
    return [{"type": "paragraph", "text": f"{tag}{index} {FILLER}"} for index in range(count)]


def _page_of(pages, marker):
    return next(index for index, text in enumerate(pages) if marker in text)


def _foot_gap(data, page_index):
    """Points of empty space between the lowest body text of a page and the foot of its text area."""
    import pypdfium2 as pdfium
    page = pdfium.PdfDocument(data)[page_index]
    text = page.get_textpage()
    content = text.get_text_range()
    lowest = min(text.get_charbox(index)[1] for index in range(text.count_chars())
                 if not content[index:index + 1].isspace() and text.get_charbox(index)[1] > quality.FOOTER_BAND)
    return lowest - quality.FOOTER_BAND


def test_table_flow_keeps_a_small_table_whole_and_binds_three_rows_at_each_end_of_any_other():
    def rows(count, size=10, heavy=()):
        return [["x" * (500 if index in heavy else size), "1"] for index in range(count)]
    assert typeset.table_flow(rows(5)) == (True, None)
    assert typeset.table_flow(rows(6)) == (False, [3, 3])
    assert typeset.table_flow(rows(9)) == (False, [3, 1, 1, 1, 3])
    # A row holding a great deal of text may itself break, and is never bound to its neighbours.
    assert typeset.table_flow(rows(9, heavy=(4,))) == (False, [3, 1, 0, 1, 3])
    assert typeset.table_flow(rows(8, heavy=(0,))) == (False, [0, 1, 1, 1, 1, 3])
    assert typeset.table_flow(rows(4, size=400)) == (False, [0, 0, 0, 0])


def test_a_table_begins_where_the_text_ends_and_never_leaves_a_fragment_on_either_page():
    table = {"type": "table", "caption": "Register", "header": ["Finding", "Score"],
             "rows": [[f"Row{index:02d} of the register", str(index)] for index in range(12)]}
    divided = 0
    for lead in range(2, 9):
        data = pdf_of({"title": "Flow", "blocks": _filler(lead) + [table] + _filler(1, "TAIL")})
        assert quality.inspect_pdf(data).sound
        pages = text_pages(data)
        counts = [sum(f"Row{index:02d}" in text for index in range(12)) for text in pages]
        holding = [index for index, count in enumerate(counts) if count]
        assert sum(counts) == 12 and all(counts[index] >= 3 for index in holding), (lead, counts)
        if len(holding) == 2:
            divided += 1
            first, second = holding
            assert "LEAD" in pages[first]                       # It started on the page the text was on,
            assert _foot_gap(data, first) < 40                  # filled that page,
            assert all("finding" in pages[index].lower() for index in holding)     # and repeats its header.
            assert "Register" in pages[first] and "Register" not in pages[second]  # The caption is not repeated.
    assert divided >= 2      # Before: never divided; the whole table moved on and left the page empty.


def test_a_callout_is_never_parted_from_its_title():
    def callout(length):
        return {"type": "callout", "tone": "success", "title": "Recommended fix",
                "text": "FIXBODY " + ("Add an encrypted backup and restore it as a test. " * 60)[:length] + " FIXEND"}
    for length, whole in ((300, True), (2400, False)):
        for words in range(0, 420, 30):               # Sweeps the callout across the foot of the page.
            data = pdf_of({"title": "Flow", "blocks": _filler(6) + [
                {"type": "paragraph", "text": "pad " * (words + 1)}, callout(length)]})
            assert quality.inspect_pdf(data).sound
            pages = text_pages(data)
            title = _page_of(pages, "Recommended fix")
            assert "FIXBODY" in pages[title], (length, words)   # The title always has its text under it.
            if whole:
                assert "FIXEND" in pages[title], (length, words)  # A short callout is one piece.


def test_text_after_a_chart_fills_the_space_the_chart_could_not_use():
    def build(lead, after):
        return {"title": "Flow", "blocks": _filler(lead) + [FLOW_CHART] + _filler(after, "AFTER") + [
            {"type": "heading", "level": 1, "text": "Next section"}] + _filler(1, "TAIL")}
    # A chart that fits is set exactly where the document has it.
    page = text_pages(pdf_of(build(2, 3)))[0]
    assert page.index("Chart title") < page.index("AFTER0")
    reordered = 0
    for lead in range(4, 9):
        data = pdf_of(build(lead, 3))
        assert quality.inspect_pdf(data).sound
        pages = text_pages(data)
        whole = "\n".join(pages)
        # Nothing is lost or repeated, the text keeps its own order, and the chart stays in its own section
        # (TAIL0 is the first text of the next one; the heading's own words also appear in page headers).
        assert [whole.count(f"AFTER{index}") for index in range(3)] == [1, 1, 1] and whole.count("Chart title") == 1
        assert whole.index("AFTER0") < whole.index("AFTER1") < whole.index("AFTER2") < whole.index("TAIL0")
        assert whole.index("Chart title") < whole.index("TAIL0")
        chart_page = _page_of(pages, "Chart title")
        if chart_page > _page_of(pages, f"LEAD{lead - 1}") or whole.index("AFTER0") < whole.index("Chart title"):
            # The chart did not fit under the text before it: the page it left is full, not half empty.
            assert _foot_gap(data, chart_page - 1) < 110, lead
            reordered += whole.index("AFTER0") < whole.index("Chart title")
    assert reordered >= 1


def test_a_chart_is_never_set_after_text_that_belongs_to_the_next_section():
    blocks = _filler(5) + [FLOW_CHART, {"type": "heading", "level": 1, "text": "Next section"}] + _filler(3, "AFTER")
    marked = typeset.document_data(spec.validate_document({"title": "Flow", "blocks": blocks}))["blocks"]
    assert all("ahead" not in block for block in marked)
    # Straight after a heading the chart stays bound to it; after running text it may follow up to three blocks.
    opening = [{"type": "heading", "level": 1, "text": "Section"}, FLOW_CHART] + _filler(5, "AFTER")
    assert "ahead" not in typeset.document_data(spec.validate_document({"title": "F", "blocks": opening}))["blocks"][1]
    running = _filler(1) + [FLOW_CHART] + _filler(5, "AFTER")
    assert typeset.document_data(spec.validate_document({"title": "F", "blocks": running}))["blocks"][1]["ahead"] == 3


def test_long_category_names_wrap_onto_a_second_line_before_they_are_ever_cut():
    room = charts.text_width("Access and", charts.SMALL) + 2
    assert charts.wrap("Access and passwords", room, charts.SMALL) == ["Access and", "passwords"]
    assert charts.wrap("Short", room, charts.SMALL) == ["Short"]
    one_word = charts.wrap(LONG_WORD, room, charts.SMALL)
    assert len(one_word) == 1 and one_word[0].endswith("…")       # Wrapping cannot help a single long word.
    many = charts.wrap("one two three four five six seven eight nine ten eleven twelve", room, charts.SMALL)
    assert len(many) == 2 and many[1].endswith("…") and all(charts.text_width(line, charts.SMALL) <= room
                                                             for line in many)
    names = ["Access and passwords", "Backup and recovery", "Devices and updates", "Email and web",
             "Staff awareness", "Network and vendors"]

    def column(categories):
        return spec.validate_document({"title": "T", "blocks": [{
            "type": "chart", "kind": "column", "categories": categories,
            "series": [{"name": "Today", "values": [2, 1, 3, 2, 1, 2]},
                       {"name": "After the plan", "values": [6, 4, 5, 4, 4, 4]}]}]}).blocks[0]
    svg = charts.chart_svg(column(names))
    assert "…" not in svg and ">Access and<" in svg and ">passwords<" in svg      # Before: "Access and passwo…".
    assert not charts.check_bounds(column(names))
    height = lambda text: float(re.search(r'viewBox="0 0 \d+ (\d+)"', text).group(1))  # noqa: E731
    # The second line lengthens the canvas; the plot itself is not squeezed.
    assert height(svg) == height(charts.chart_svg(column(list("ABCDEF")))) + charts.LINE
    bars = spec.validate_document({"title": "T", "blocks": [{
        "type": "chart", "kind": "bar", "categories": ["Two workstations run an operating system without updates",
                                                       "Short name"],
        "series": [{"name": "Score", "values": [12, 4]}]}]}).blocks[0]
    drawn = charts.chart_svg(bars)
    assert "…" not in drawn and not charts.check_bounds(bars)
    # Values are drawn from the data, untouched by the layout.
    assert ">12<" in drawn and ">4<" in drawn


def test_word_keeps_a_short_table_whole_and_three_rows_together_at_each_end_of_a_long_one():
    def bound(count):
        raw = {"title": "T", "blocks": [{"type": "table", "caption": "Schedule", "header": ["Milestone", "Share"],
                                         "rows": [[f"Step {index}", f"{index}%"] for index in range(count)]}]}
        document = docx.Document(io.BytesIO(generate(raw, "docx").data))
        table = next(t for t in document.tables if t.rows[0].cells[0].text == "Milestone")
        caption = next(p for p in document.paragraphs if p.text == "Schedule")
        assert caption.paragraph_format.keep_with_next          # The caption stays with its table.
        assert all("w:cantSplit" in row._tr.trPr.xml for row in table.rows)
        return [all(bool(p.paragraph_format.keep_with_next) for cell in row.cells for p in cell.paragraphs)
                for row in table.rows]
    # Header and every row but the last are kept with the row after them: Word cannot divide the table.
    assert bound(4) == [True, True, True, True, False]         # Before: divided after two rows.
    # A longer table: the header with its first three rows, and the last three rows together.
    assert bound(10) == [True, True, True, False, False, False, False, False, True, True, False]
