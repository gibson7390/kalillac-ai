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
         "series": [{"name": "Revenue", "values": [9568000, 5152000, 2392000]}]},
        {"type": "quote", "text": "Wholesale pays for everyone else's growth.", "attribution": "The CEO"},
        {"type": "heading", "level": 1, "text": "The plan"},
        {"type": "comparison", "columns": [{"title": "Build", "items": ["Adds capacity", "$4.2M"]},
                                           {"title": "Outsource", "items": ["No capital", "Lower margin"]}]},
        {"type": "steps", "items": [{"title": "Secure the site", "text": "Sign the lease."},
                                    {"title": "Order equipment"}, {"title": "Commission"}]},
        {"type": "chart", "kind": "line", "categories": ["2025", "2026", "2027"],
         "series": [{"name": "With", "values": [15, 18.4, 20.6]}, {"name": "Without", "values": [15, 18.4, None]}]},
        {"type": "code", "language": "python", "text": "def margin(r, c):\n    return (r - c) / r"},
    ],
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
    assert licenses == ["Carlito-OFL.txt", "Inter-OFL.txt", "JetBrainsMono-OFL.txt", "NotoSansArabic-OFL.txt", "NotoSansCJK-OFL.txt",
                        "NotoSansDevanagari-OFL.txt", "NotoSansHebrew-OFL.txt", "NotoSansThai-OFL.txt",
                        "SourceSerif4-OFL.md"]
    for path in (typeset.FONTS / "licenses").iterdir():
        assert "SIL Open Font License" in path.read_text(encoding="utf-8"), path.name
    core = [entry for entry in manifest["fonts"] if entry["group"] == "core"]
    assert len(core) == 24 and all(entry["license"] == "OFL-1.1" for entry in manifest["fonts"])
    for entry in core:
        data = (typeset.FONTS / entry["file"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == entry["sha256"], entry["file"]
        assert entry["url"].startswith("https://github.com/") or entry["url"].startswith(
            "https://raw.githubusercontent.com/notofonts/")
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
    assert families <= {"Inter", "InterDisplay", "JetBrainsMono", "NotoSansArabic", "NotoSansJP", "NotoSansKR",
                        "NotoSansSC", "SourceSerif4"}, families
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
                     "Secure the site", "Outsource", "def margin(r, c):"):
        assert expected in body, expected
    assert "Page 1 of" in pages[2] and f"Page {len(pages) - 2} of {len(pages) - 2}" in pages[-1]


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
    assert 'w:cs="Noto Sans Arabic"' in xml
    font_table = archive.read("word/fontTable.xml").decode()
    assert '<w:font w:name="Noto Sans Arabic">' in font_table and 'w:charset w:val="B2"' in font_table
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
