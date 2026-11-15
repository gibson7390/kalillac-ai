"""Upload intake core: identification by content, hostile-file defences and
bounded text extraction. Every fixture is built in the test; nothing is read
from or written to disk, and nothing touches the application or the network.
"""
from __future__ import annotations

import copy
import io
import re
from pathlib import Path
import struct
import zipfile

import pytest

pypdf = pytest.importorskip("pypdf")
pytest.importorskip("docx")
pytest.importorskip("openpyxl")
pytest.importorskip("typst")
pytest.importorskip("pypdfium2")
pytest.importorskip("lxml")

from kalillac_files import generate, intake  # noqa: E402
from kalillac_files.intake import IntakeError, Inspected, inspect  # noqa: E402
from test_file_generation import DOCUMENT, WORKBOOK  # noqa: E402

CANDIDATE = Path(__file__).resolve().parents[1]


def made(spec, format):
    return generate(copy.deepcopy(spec), format).data


def refused(data, name=None):
    with pytest.raises(IntakeError) as error:
        inspect(data, name)
    assert str(error.value) == error.value.code
    return error.value.code


def rezip(data, change=None, add=None, drop=()):
    """A copy of an Office file with parts replaced, added or dropped."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for entry in source.infolist():
            if entry.filename in drop:
                continue
            content = source.read(entry.filename)
            if change and entry.filename in change:
                content = change[entry.filename](content)
            target.writestr(entry.filename, content)
        for name, content in (add or {}).items():
            target.writestr(name, content)
    return out.getvalue()


# --- supported files round-trip -------------------------------------------------------------


def test_generated_documents_are_identified_by_content_and_their_text_extracted():
    for format, name in (("docx", "report.docx"), ("docx", None), ("docx", "REPORT.DOCX")):
        result = inspect(made(DOCUMENT, format), name)
        assert type(result) is Inspected and result.kind == "docx" and not result.truncated
        for expected in ("Quarterly Review — Café Operations", "Summary", "Revenue grew 12% while costs held steady.",
                         "Open the second site", "Hire a manager", "Coffee", "1,450", "<b>710</b> & more",
                         "Closing remarks on the following page."):
            assert expected in result.text, expected
        assert result.units >= 15

    result = inspect(made(DOCUMENT, "pdf"), "report.pdf")
    assert (result.kind, result.units, result.truncated) == ("pdf", 2, False)
    assert "Revenue grew 12% while costs held steady." in " ".join(result.text.split())
    assert "Closing remarks on the following page." in result.text

    result = inspect(made(WORKBOOK, "xlsx"), "model.xlsx")
    assert result.kind == "xlsx" and not result.truncated
    lines = result.text.split("\n")
    assert lines[0] == "# Inputs" and lines[1] == "Name\tValue\tShare\tStarts\tUnits\tCost"
    assert "Principal\t250000\t0.8" in result.text and "# Notes & Sources" in result.text
    assert "日本語のメモ" in result.text
    # A formula is reported as text. It is never evaluated.
    assert "Total\t=SUM(B2:B4)\t=AVERAGE(C2:C4)" in result.text and result.units == 6 * 5 + 5


def test_a_deck_is_read_slide_by_slide_in_presentation_order_with_its_notes():
    pytest.importorskip("pptx")
    from quality_benchmark import EXECUTIVE_DECK
    deck = made(EXECUTIVE_DECK, "pptx")
    for name in ("board.pptx", None, "BOARD.PPTX"):
        result = inspect(deck, name)
        assert (result.kind, result.units, result.truncated) == ("pptx", 14, False)
    lines = result.text.split("\n")
    assert lines[:4] == ["# Slide 1", "NORTHWIND COFFEE ROASTERS", "Northwind 2027: From One Roastery to Two",
                         "The case for doubling capacity, and how we will do it"]
    assert "Notes: " + EXECUTIVE_DECK["slides"][0]["notes"] in lines
    assert lines.index("# Slide 2") < lines.index("# Slide 14") and "$4.2M" in result.text
    assert "Building wins on every measure that lasts" in result.text and "Contractual" in result.text
    # Slide order is the presentation's list, not the order of the files in the archive.
    def swap_first_two(xml):
        first, second = re.findall(rb"<p:sldId [^>]*/>", xml)[:2]
        return xml.replace(first + second, second + first)
    reordered = inspect(rezip(deck, change={"ppt/presentation.xml": swap_first_two}), "x.pptx").text.split("\n")
    assert reordered[:3] == ["# Slide 1", "THE DECISION", EXECUTIVE_DECK["slides"][1]["text"]]
    assert refused(deck, "deck.docx") == "type_mismatch"
    assert refused(made(DOCUMENT, "docx"), "x.pptx") == "type_mismatch"
    macro = rezip(deck, change={"[Content_Types].xml": lambda xml: xml.replace(
        b"presentationml.presentation.main+xml", b"presentation.macroEnabled.main+xml")})
    assert refused(macro, "x.pptx") == "macros_not_allowed"
    hostile = rezip(deck, change={"ppt/slides/slide2.xml": lambda xml: b'<!DOCTYPE x [<!ENTITY a "b">]>' + xml})
    assert refused(hostile, "x.pptx") == "xml_declaration_not_allowed"
    outside = rezip(deck, change={"ppt/_rels/presentation.xml.rels": lambda xml: xml.replace(
        b'Target="slides/slide2.xml"', b'Target="../../../etc/slide2.xml"')})
    assert inspect(outside, "x.pptx").units == 13                    # A target outside the package is not read.


def test_text_files_need_a_text_name_and_valid_utf8():
    assert inspect(b"plain \xe2\x9c\x93 text\r\nline two\rline three", "notes.txt") == Inspected(
        "txt", "plain \u2713 text\nline two\nline three", False, 32)
    assert inspect(b"\xef\xbb\xbfa,b\n1,2\n", "data.csv").text == "a,b\n1,2\n"
    assert inspect(b"# Title\n", "readme.MD").kind == "md"
    assert inspect(b'{"a": 1}', "x.json").kind == "json"
    assert refused(b"plain text") == "unsupported_type"               # No name: nothing says it is text.
    assert refused(b"plain text", "notes") == "unsupported_type"
    assert refused(b"\xff\xfe\x00binary", "notes.txt") == "unsupported_type"
    assert refused(b"text with a null \x00 byte", "notes.txt") == "unsupported_type"
    assert refused(b"MZ\x90\x00\x03", "notes.txt") == "unsupported_type"
    long = inspect(b"x" * (intake.MAX_TEXT_CHARS + 10), "long.txt")
    assert (len(long.text), long.truncated, long.units) == (intake.MAX_TEXT_CHARS, True, intake.MAX_TEXT_CHARS + 10)


# --- type confusion -------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["tool.exe", "page.html", "macro.docm", "deck.pptm", "legacy.ppt", "legacy.doc",
                                  "x.zip",
                                  "archive.tar.gz", "script.py", "image.png", "noextension", "trailingdot.",
                                  "x.pdf\x00.txt", "a.verylongextension"])
def test_only_supported_extensions_are_accepted(name):
    assert refused(made(DOCUMENT, "docx"), name) == "unsupported_type"
    assert refused(b"hello", name) == "unsupported_type"


def test_a_name_that_disagrees_with_the_content_is_refused():
    pdf, word, sheet = made(DOCUMENT, "pdf"), made(DOCUMENT, "docx"), made(WORKBOOK, "xlsx")
    for data, name in ((pdf, "x.docx"), (pdf, "x.txt"), (pdf, "x.xlsx"), (word, "x.pdf"), (word, "x.xlsx"),
                       (word, "x.csv"), (sheet, "x.docx"), (sheet, "x.json"), (b"just text", "x.pdf"),
                       (b"just text", "x.docx"), (b"PK just text", "x.txt")):
        assert refused(data, name) in ("type_mismatch", "unreadable_archive"), name
    assert refused(b"just text", "x.pdf") == "type_mismatch"
    assert refused(pdf, "x.txt") == "type_mismatch"


@pytest.mark.parametrize("data, code", [
    (b"", "empty_file"), (None, "invalid_upload"), ("text", "invalid_upload"), (bytearray(b"%PDF-"), "invalid_upload"),
    (b"%PDF-1.7\nnot really a pdf", "unreadable_document"),
    (b"%PDF-", "unreadable_document"),
    (b"PK\x03\x04 truncated", "unreadable_archive"),
    (b"PK\x05\x06" + b"\x00" * 18, "unsupported_type"),               # A valid, empty archive.
])
def test_malformed_uploads_are_refused(data, code):
    assert refused(data) == code


def test_size_limit_is_checked_before_anything_is_parsed(monkeypatch):
    monkeypatch.setattr(intake, "_open_archive", lambda data: pytest.fail("parsed an oversized upload"))
    assert refused(b"PK" + b"\x00" * intake.MAX_UPLOAD_BYTES, "x.docx") == "file_too_large"
    assert refused(b"%PDF-" + b"\x00" * intake.MAX_UPLOAD_BYTES, "x.pdf") == "file_too_large"
    assert refused(b"x" * (intake.MAX_UPLOAD_BYTES + 1), "x.txt") == "file_too_large"


# --- hostile archives -----------------------------------------------------------------------


def _plain_docx():
    """A small Word file (no embedded fonts), so the archive ratios below are about the padding."""
    import docx
    document = docx.Document()
    document.add_paragraph("Revenue grew 12% while costs held steady.")
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


def test_decompression_bomb_is_refused_from_its_headers():
    word = _plain_docx()
    bomb = rezip(word, add={"word/media/padding.bin": b"\x00" * (60 * 1024 * 1024)})
    assert len(bomb) < 200_000                                        # 60 MB compresses to almost nothing.
    assert refused(bomb, "bomb.docx") == "archive_too_large"
    many = rezip(word, add={f"word/media/p{n}.bin": b"\x00" * (30 * 1024 * 1024) for n in range(4)})
    assert refused(many, "bomb.docx") == "archive_too_large"         # Each part fits; the total does not.
    ratio = rezip(word, add={"customXml/pad.bin": b"\x00" * (20 * 1024 * 1024)})
    assert len(ratio) * intake.MAX_EXPANSION_RATIO < 20 * 1024 * 1024 + len(word) * 20
    assert refused(ratio, "bomb.docx") == "archive_too_large"


def test_a_part_that_lies_about_its_size_cannot_expand_past_the_cap(monkeypatch):
    """The declared sizes pass the header checks; the read itself is capped."""
    word = made(DOCUMENT, "docx")
    monkeypatch.setattr(intake, "MAX_PART_BYTES", 2_000)
    with zipfile.ZipFile(io.BytesIO(word)) as archive:
        assert len(archive.read("word/document.xml")) > 2_000
        with pytest.raises(IntakeError) as error:
            intake._read_part(archive, "word/document.xml")
        assert error.value.code == "archive_too_large"
        with pytest.raises(IntakeError) as error:
            intake._read_part(archive, "word/missing.xml")
        assert error.value.code == "unreadable_archive"


@pytest.mark.parametrize("name", ["../outside.xml", "word/../../outside.xml", "/absolute.xml", "C:/windows.xml",
                                  "..", "a/../b.xml"])
def test_entry_names_that_could_escape_are_refused(name):
    assert refused(rezip(made(DOCUMENT, "docx"), add={name: b"<x/>"}), "x.docx") == "archive_unsafe_entry"


def test_a_stored_backslash_name_is_refused_on_every_platform():
    # zipfile rewrites "\\" itself when writing on Windows, so the stored name is patched in the bytes.
    plain = rezip(made(DOCUMENT, "docx"), add={"word/escapeXname.xml": b"<x/>"})
    assert inspect(plain, "x.docx").kind == "docx"
    assert plain.count(b"word/escapeXname.xml") == 2          # Local header and central directory.
    assert refused(plain.replace(b"word/escapeXname.xml", b"word\\escapeXname.xml"), "x.docx") == \
        "archive_unsafe_entry"


def test_duplicate_entries_and_too_many_entries_are_refused():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<a/>")
        with pytest.warns(UserWarning):
            archive.writestr("word/document.xml", "<b/>")
    assert refused(out.getvalue(), "x.docx") == "archive_unsafe_entry"
    crowded = rezip(made(DOCUMENT, "docx"),
                    add={f"customXml/extra{n}.xml": b"<x/>" for n in range(intake.MAX_ARCHIVE_ENTRIES)})
    assert refused(crowded, "x.docx") == "archive_too_many_entries"


def test_encrypted_archive_entries_are_refused():
    word = bytearray(made(DOCUMENT, "docx"))
    # Set the "encrypted" general-purpose flag of every central-directory entry.
    position, patched = 0, 0
    while (position := word.find(b"PK\x01\x02", position)) != -1:
        flags = struct.unpack_from("<H", word, position + 8)[0]
        struct.pack_into("<H", word, position + 8, flags | 0x1)
        position, patched = position + 4, patched + 1
    assert patched > 5
    assert refused(bytes(word), "locked.docx") == "encrypted_file"


def test_macro_enabled_office_files_are_refused():
    word, sheet = made(DOCUMENT, "docx"), made(WORKBOOK, "xlsx")
    assert refused(rezip(word, add={"word/vbaProject.bin": b"\xd0\xcf\x11\xe0"}), "x.docx") == "macros_not_allowed"
    assert refused(rezip(sheet, add={"xl/vbaProject.bin": b"\xd0\xcf\x11\xe0"}), "x.xlsx") == "macros_not_allowed"
    assert refused(rezip(sheet, add={"xl/macrosheets/sheet1.xml": b"<x/>"}), "x.xlsx") == "macros_not_allowed"
    retyped = rezip(word, change={"[Content_Types].xml": lambda xml: xml.replace(
        b"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
        b"application/vnd.ms-word.document.macroEnabled.main+xml")})
    assert refused(retyped, "x.docx") == "macros_not_allowed"


XXE = (b'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
       b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r>'
       b'<w:t>&x;</w:t></w:r></w:p></w:body></w:document>')
LAUGHS = (b'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "ha"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
          b'<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">]>'
          b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r>'
          b'<w:t>&c;</w:t></w:r></w:p></w:body></w:document>')


@pytest.mark.parametrize("part, payload, code", [
    ("word/document.xml", XXE, "xml_declaration_not_allowed"),
    ("word/document.xml", LAUGHS, "xml_declaration_not_allowed"),
    ("word/styles.xml", b'<!doctype x SYSTEM "http://example.test/x.dtd"><x/>', "xml_declaration_not_allowed"),
    ("docProps/core.xml", b'<?xml version="1.0"?><! ENTITY % p SYSTEM "http://example.test/p"><x/>',
     "xml_declaration_not_allowed"),
    ("word/document.xml", XXE.decode("ascii").encode("utf-16"), "xml_encoding_not_allowed"),
    ("word/document.xml", b'<?xml version="1.0" encoding="UTF-7"?><x/>', "xml_encoding_not_allowed"),
    ("word/document.xml", b'<?xml version="1.0" encoding="cp037"?><x/>', "xml_encoding_not_allowed"),
    ("word/document.xml", b"<w:document><unclosed>", "unreadable_document"),
    ("word/document.xml", b"not xml at all", "unreadable_document"),
])
def test_hostile_or_broken_xml_parts_are_refused(part, payload, code):
    assert refused(rezip(made(DOCUMENT, "docx"), change={part: lambda _xml: payload}), "x.docx") == code


def test_hostile_xml_in_a_workbook_is_refused_before_the_spreadsheet_library_sees_it(monkeypatch):
    sheet = made(WORKBOOK, "xlsx")
    monkeypatch.setattr(intake, "_xlsx", lambda data: pytest.fail("the workbook was parsed"))
    for part in ("xl/workbook.xml", "xl/worksheets/sheet1.xml", "xl/sharedStrings.xml", "xl/_rels/workbook.xml.rels"):
        hostile = rezip(sheet, change={part: lambda _xml: XXE}, add=None)
        if part not in zipfile.ZipFile(io.BytesIO(sheet)).namelist():
            hostile = rezip(sheet, add={part: XXE})
        assert refused(hostile, "x.xlsx") == "xml_declaration_not_allowed", part


def test_a_broken_workbook_or_pdf_is_refused_not_raised():
    sheet = made(WORKBOOK, "xlsx")
    assert refused(rezip(sheet, change={"xl/workbook.xml": lambda _xml: b"<workbook/>"}), "x.xlsx") == \
        "unreadable_document"
    # A workbook missing one sheet part is refused or read without that sheet; no other error escapes.
    try:
        partial = inspect(rezip(sheet, drop=("xl/worksheets/sheet1.xml",)), "x.xlsx")
    except IntakeError as error:
        assert error.code == "unreadable_document"
    else:
        assert "Principal" not in partial.text
    pdf = made(DOCUMENT, "pdf")
    assert refused(pdf[:400], "cut.pdf") == "unreadable_document"
    assert refused(b"%PDF-1.4\n" + b"\x00\xff" * 500, "noise.pdf") == "unreadable_document"


def test_encrypted_pdf_is_refused():
    writer = pypdf.PdfWriter()
    writer.append(pypdf.PdfReader(io.BytesIO(made(DOCUMENT, "pdf"))))
    writer.encrypt("synthetic-password")
    out = io.BytesIO()
    writer.write(out)
    assert refused(out.getvalue(), "locked.pdf") == "encrypted_file"


# --- bounded extraction ---------------------------------------------------------------------


def test_extraction_is_capped_and_says_so(monkeypatch):
    monkeypatch.setattr(intake, "MAX_TEXT_CHARS", 300)
    for data, name in ((made(DOCUMENT, "docx"), "x.docx"), (made(DOCUMENT, "pdf"), "x.pdf"),
                       (made(WORKBOOK, "xlsx"), "x.xlsx")):
        result = inspect(data, name)
        assert result.truncated and len(result.text) <= 300, name
    monkeypatch.setattr(intake, "MAX_TEXT_CHARS", 400_000)
    monkeypatch.setattr(intake, "MAX_SHEET_CELLS", 12)
    result = inspect(made(WORKBOOK, "xlsx"), "x.xlsx")
    # Two rows of six cells fit; the third row would pass the limit.
    assert result.truncated and result.units == 12 and "Principal" in result.text and "Rate" not in result.text
    monkeypatch.setattr(intake, "MAX_PDF_PAGES", 1)
    result = inspect(made(DOCUMENT, "pdf"), "x.pdf")
    assert result.truncated and result.units == 2 and "Closing remarks" not in result.text


def test_embedded_and_external_content_is_never_followed():
    word = made(DOCUMENT, "docx")
    external = (b'<?xml version="1.0" encoding="UTF-8"?><Relationships '
                b'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId99" '
                b'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/attachedTemplate" '
                b'Target="http://example.test/template.dotm" TargetMode="External"/></Relationships>')
    carrying = rezip(word, add={"word/_rels/settings.xml.rels": external, "word/embeddings/oleObject1.bin": b"\xd0\xcf",
                                "word/media/inner.zip": rezip(word)})
    result = inspect(carrying, "x.docx")      # conftest refuses any non-loopback connection for the whole run.
    assert "Revenue grew 12%" in result.text and "example.test" not in result.text


# --- names ----------------------------------------------------------------------------------


@pytest.mark.parametrize("name, expected", [
    ("report.pdf", "report.pdf"), ("../../etc/passwd", "passwd"), ("C:\\Users\\x\\secret plan.docx", "secret plan.docx"),
    ("<script>alert(1)</script>.txt", "script_.txt"), ("  .hidden  ", "hidden"), ("日本語.docx", "___.docx"),
    ("", "upload"), (None, "upload"), (42, "upload"), ("a" * 300 + ".pdf", "a" * 120), ("...", "upload"),
])
def test_display_names_are_inert_labels(name, expected):
    label = intake.display_name(name)
    assert label == expected
    assert "/" not in label and "\\" not in label and "<" not in label and len(label) <= 120


def _app_outside_the_files_flag(source):
    """The application source outside its KALILLAC_FILES_ENABLED block, where kalillac_files is used."""
    start = source.index('if os.getenv("KALILLAC_FILES_ENABLED"')
    end = source.index('\n@api.get("/api/health")', start)
    return (source[:start] + source[end:]).replace("nothing in kalillac_files is imported", "")


def test_the_application_reaches_the_intake_core_only_behind_the_files_flag():
    source = (CANDIDATE / "app_fastapi_candidate.py").read_text(encoding="utf-8")
    assert "kalillac_files" not in _app_outside_the_files_flag(source)
    for package in ("kalillac_routing", "kalillac_accounts", "kalillac_billing", "kalillac_spaces", "kalillac_db"):
        for path in (CANDIDATE / package).rglob("*.py"):
            assert "kalillac_files" not in path.read_text(encoding="utf-8"), path.name
