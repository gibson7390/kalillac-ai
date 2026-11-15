"""Validation of an uploaded file and bounded extraction of its text.

Not reachable from the application. inspect(data, declared_name) takes the
raw bytes of one upload and returns what Kalillac may analyse: the detected
kind and plain text, both bounded. Anything else is refused with an
IntakeError whose code is stable and whose message never echoes content.

Rules:
  * The kind is decided from the content, never from the name or a declared
    media type. A declared extension that disagrees with the content is
    refused, so a file cannot be smuggled under another type's name.
  * Nothing is written to disk and nothing is executed: no macro, formula,
    script, embedded object or external reference is followed or evaluated.
    Macro-enabled Office files and encrypted files are refused.
  * An Office file is a ZIP archive. Before any part is read: entry count,
    entry names (no absolute path, drive, backslash or ".."), encryption
    flags, declared sizes and the overall expansion ratio are checked, and
    every part is then read through a hard byte cap, so a lying header cannot
    expand past it. Archives inside the archive are never opened.
  * XML parts with a DOCTYPE or entity declaration are refused before parsing
    (XXE, entity expansion), and the parser resolves no entity and makes no
    network access.
  * Extracted text is capped; the result says when it was truncated.

A hostile PDF can still make any parser spend unbounded time. Callers must
run inspect() in a separate worker process with a hard time and memory
limit, never in a request handler.
"""
from dataclasses import dataclass
import io
import re
import zipfile

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 2_000
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024       # All parts, uncompressed.
MAX_PART_BYTES = 40 * 1024 * 1024           # Any single part, uncompressed.
MAX_EXPANSION_RATIO = 200
MAX_TEXT_CHARS = 400_000
MAX_PDF_PAGES = 500
MAX_SHEET_CELLS = 200_000
TEXT_EXTENSIONS = ("txt", "md", "csv", "json")
EXTENSIONS = ("pdf", "docx", "xlsx", "pptx", *TEXT_EXTENSIONS)
MAX_SLIDES = 500

_WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_DRAWING = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_PRESENTATION = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
_RELATIONSHIPS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_R_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
_DECLARATION = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)
_ENCODING = re.compile(rb"""encoding\s*=\s*["']([^"']{1,40})["']""")
_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_NAME = re.compile(r"[^A-Za-z0-9._ -]")


class IntakeError(ValueError):
    """A refused upload. `code` is stable; the message is the code."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Inspected:
    kind: str            # "pdf", "docx", "xlsx", "pptx" or one of TEXT_EXTENSIONS
    text: str
    truncated: bool
    units: int           # pages (pdf), paragraphs (docx), cells (xlsx), slides (pptx), characters (text)


def declared_extension(name):
    """The lowercase extension of a declared file name, or None. The name is
    never used as a path."""
    if type(name) is not str or len(name) > 255 or "." not in name or _CONTROL.search(name):
        return None
    extension = name.rsplit(".", 1)[1].lower()
    return extension if extension.isascii() and extension.isalnum() and len(extension) <= 8 else None


def display_name(name):
    """A short, inert label for a declared name: its last path component with
    everything outside a small allowlist replaced."""
    if type(name) is not str:
        return "upload"
    last = re.split(r"[\\/]", name)[-1][:120]
    cleaned = _NAME.sub("_", last).strip(" .")
    return cleaned or "upload"


class _Collector:
    """Pieces of text joined by a separator, never longer than MAX_TEXT_CHARS
    in total; `truncated` records that something was left out."""

    def __init__(self, separator="\n"):
        self.separator, self.parts, self.size, self.truncated = separator, [], 0, False

    def add(self, text):
        if self.truncated or not text:
            return not self.truncated
        joint = len(self.separator) if self.parts else 0
        room = MAX_TEXT_CHARS - self.size - joint
        if len(text) > room:
            if room > 0:
                self.parts.append(text[:room])
            self.size, self.truncated = MAX_TEXT_CHARS, True
            return False
        self.parts.append(text)
        self.size += joint + len(text)
        return True

    def text(self):
        return self.separator.join(self.parts)


# --- archives -------------------------------------------------------------------------------


def _open_archive(data):
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        entries = archive.infolist()
    except (zipfile.BadZipFile, ValueError, OSError, EOFError, NotImplementedError):
        raise IntakeError("unreadable_archive") from None
    if len(entries) > MAX_ARCHIVE_ENTRIES:
        raise IntakeError("archive_too_many_entries")
    total = 0
    names = set()
    for entry in entries:
        # orig_filename is the name as stored: on Windows, zipfile rewrites a
        # backslash to "/" in filename, which would hide it from this check.
        name, stored = entry.filename, entry.orig_filename
        if (not name or name.startswith("/") or "\\" in stored or ":" in stored or "\x00" in stored
                or ".." in name.split("/") or name in names):
            raise IntakeError("archive_unsafe_entry")
        names.add(name)
        if entry.flag_bits & 0x1:
            raise IntakeError("encrypted_file")
        if entry.file_size > MAX_PART_BYTES:
            raise IntakeError("archive_too_large")
        total += entry.file_size
    if total > MAX_ARCHIVE_BYTES or total > MAX_EXPANSION_RATIO * max(1, len(data)):
        raise IntakeError("archive_too_large")
    lowered = {name.lower() for name in names}
    if any(name.endswith(("vbaproject.bin", "vbadata.xml")) or "/macrosheets/" in name for name in lowered):
        raise IntakeError("macros_not_allowed")
    return archive, names


def _read_part(archive, name):
    """One part, never more than MAX_PART_BYTES whatever its header declares."""
    limit = MAX_PART_BYTES
    try:
        with archive.open(name) as part:
            data = part.read(limit + 1)
    except (zipfile.BadZipFile, KeyError, ValueError, OSError, EOFError, RuntimeError, NotImplementedError):
        raise IntakeError("unreadable_archive") from None
    if len(data) > limit:
        raise IntakeError("archive_too_large")
    return data


def _safe_xml_parts(archive, names):
    """Refuse the file if any XML part declares a DOCTYPE or an entity."""
    budget = MAX_ARCHIVE_BYTES
    for name in sorted(names):
        if not name.lower().endswith((".xml", ".rels", ".vml")):
            continue
        data = _read_part(archive, name)
        budget -= len(data)
        if budget < 0:
            raise IntakeError("archive_too_large")
        # UTF-8 only: another encoding (UTF-16, UTF-7, EBCDIC) could hide a
        # declaration from the byte check below.
        head = data[:200]
        encoding = _ENCODING.search(head)
        if head[:2] in (b"\xff\xfe", b"\xfe\xff") or b"\x00" in head or (
                encoding is not None and encoding.group(1).lower() not in (b"utf-8", b"utf8")):
            raise IntakeError("xml_encoding_not_allowed")
        if _DECLARATION.search(data):
            raise IntakeError("xml_declaration_not_allowed")
        if b"macroEnabled" in data and name == "[Content_Types].xml":
            raise IntakeError("macros_not_allowed")


def _parse_xml(data):
    from lxml import etree
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, dtd_validation=False,
                             huge_tree=False, recover=False, remove_comments=True, remove_pis=True)
    try:
        return etree.fromstring(data, parser)
    except etree.XMLSyntaxError:
        raise IntakeError("unreadable_document") from None


# --- extractors -----------------------------------------------------------------------------


def _docx(archive):
    root = _parse_xml(_read_part(archive, "word/document.xml"))
    collector, units = _Collector(), 0
    for paragraph in root.iter(_WORD + "p"):
        pieces = []
        for node in paragraph.iter(_WORD + "t", _WORD + "tab", _WORD + "br"):
            pieces.append(node.text or "" if node.tag == _WORD + "t" else ("\t" if node.tag == _WORD + "tab" else "\n"))
        text = "".join(pieces).strip()
        if text:
            units += 1
            if not collector.add(text):
                break
    return collector.text(), collector.truncated, units


def _relationships(archive, names, part):
    """Relationship id -> target part name, for one part, resolved inside the archive only."""
    folder, file_name = part.rsplit("/", 1)
    rels = f"{folder}/_rels/{file_name}.rels"
    if rels not in names:
        return {}
    targets = {}
    for rel in _parse_xml(_read_part(archive, rels)).iter(_RELATIONSHIPS + "Relationship"):
        if rel.get("TargetMode") == "External":
            continue                                    # Never followed.
        parts = folder.split("/")
        for piece in (rel.get("Target") or "").split("/"):
            if piece == "..":
                parts = parts[:-1]
            elif piece not in ("", "."):
                parts.append(piece)
        targets[rel.get("Id")] = "/".join(parts)
    return targets


def slide_parts(archive, names):
    """The slide part names in presentation order (the order of the slide list, not of the files)."""
    presentation = _parse_xml(_read_part(archive, "ppt/presentation.xml"))
    targets = _relationships(archive, names, "ppt/presentation.xml")
    order = [targets.get(item.get(_R_ID)) for item in presentation.iter(_PRESENTATION + "sldId")]
    return [name for name in order if name in names]


def _paragraphs(root):
    for paragraph in root.iter(_DRAWING + "p"):
        text = "".join(node.text or "" for node in paragraph.iter(_DRAWING + "t")).strip()
        if text:
            yield text


def _pptx(archive, names):
    collector, slides = _Collector(), slide_parts(archive, names)
    for number, part in enumerate(slides[:MAX_SLIDES], start=1):
        if not collector.add(f"# Slide {number}"):
            break
        for text in _paragraphs(_parse_xml(_read_part(archive, part))):
            if not collector.add(text):
                break
        notes = [target for target in _relationships(archive, names, part).values()
                 if target.startswith("ppt/notesSlides/") and target in names]
        for note_part in notes[:1]:
            # The notes page repeats the slide image placeholder; only its body text is the speaker's.
            spoken = list(_paragraphs(_parse_xml(_read_part(archive, note_part))))
            if spoken and not collector.add("Notes: " + " ".join(spoken)):
                break
        if collector.truncated:
            break
    return collector.text(), collector.truncated or len(slides) > MAX_SLIDES, len(slides)


def _xlsx(data):
    from openpyxl import load_workbook
    try:
        book = load_workbook(io.BytesIO(data), read_only=True, data_only=False, keep_links=False)
    except Exception:
        raise IntakeError("unreadable_document") from None
    collector, cells = _Collector(), 0
    try:
        if not book.worksheets:
            raise IntakeError("unreadable_document")
        for sheet in book.worksheets:
            if not collector.add("# " + str(sheet.title)):
                break
            for row in sheet.iter_rows(values_only=True):
                cells += len(row)
                if cells > MAX_SHEET_CELLS:
                    collector.truncated = True
                    break
                values = ["" if value is None else str(value) for value in row]
                while values and not values[-1]:
                    values.pop()
                if values and not collector.add("\t".join(values)):
                    break
            if collector.truncated:
                break
    except IntakeError:
        raise
    except Exception:
        raise IntakeError("unreadable_document") from None
    finally:
        book.close()
    return collector.text(), collector.truncated, min(cells, MAX_SHEET_CELLS)


def _pdf(data):
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise IntakeError("encrypted_file")
        count = len(reader.pages)
    except IntakeError:
        raise
    except Exception:
        raise IntakeError("unreadable_document") from None
    if count == 0:
        raise IntakeError("unreadable_document")
    collector = _Collector("\n\n")
    truncated = count > MAX_PDF_PAGES
    for index in range(min(count, MAX_PDF_PAGES)):
        try:
            text = reader.pages[index].extract_text() or ""
        except Exception:
            raise IntakeError("unreadable_document") from None
        if not collector.add(_CONTROL.sub(" ", text).strip()):
            break
    return collector.text(), truncated or collector.truncated, count


def _text(data):
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise IntakeError("unsupported_type") from None
    if _CONTROL.search(text):
        raise IntakeError("unsupported_type")  # Binary content under a text name.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text[:MAX_TEXT_CHARS], len(text) > MAX_TEXT_CHARS, len(text)


def inspect(data, declared_name=None):
    """Inspected for one upload, or IntakeError."""
    if type(data) is not bytes:
        raise IntakeError("invalid_upload")
    if not data:
        raise IntakeError("empty_file")
    if len(data) > MAX_UPLOAD_BYTES:
        raise IntakeError("file_too_large")
    extension = declared_extension(declared_name)
    if declared_name is not None and extension not in EXTENSIONS:
        raise IntakeError("unsupported_type")

    if data.startswith(b"%PDF-"):
        kind, result = "pdf", None
    elif data.startswith(b"PK"):
        archive, names = _open_archive(data)
        try:
            _safe_xml_parts(archive, names)
            if "[Content_Types].xml" in names and "word/document.xml" in names:
                kind, result = "docx", _docx(archive)
            elif "[Content_Types].xml" in names and "xl/workbook.xml" in names:
                kind, result = "xlsx", None
            elif "[Content_Types].xml" in names and "ppt/presentation.xml" in names:
                kind, result = "pptx", _pptx(archive, names)
            else:
                raise IntakeError("unsupported_type")
        finally:
            archive.close()
    else:
        if extension not in TEXT_EXTENSIONS:
            # Unnamed, or named as a binary type the content is not.
            raise IntakeError("unsupported_type" if extension is None else "type_mismatch")
        kind, result = extension, _text(data)

    if extension is not None and extension != kind:
        raise IntakeError("type_mismatch")
    if result is None:
        result = _pdf(data) if kind == "pdf" else _xlsx(data)
    text, truncated, units = result
    return Inspected(kind, text, truncated, units)
