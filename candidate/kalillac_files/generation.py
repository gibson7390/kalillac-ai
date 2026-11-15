"""generate(spec, format): validate, render, bound the result and name the file.

CPU and memory are bounded by the specification limits (spec.py) and the
output is bounded by MAX_FILE_BYTES. Rendering is synchronous and can take
seconds for a large document: a caller must run it off the chat-serving path,
in a worker with its own time limit, never inside a request handler.
"""
from dataclasses import dataclass, field
import re
import unicodedata

from . import render
from .deck import validate_deck
from .spec import SpecError, validate_document, validate_workbook

MAX_FILE_BYTES = 10 * 1024 * 1024

# format -> (specification kind, renderer, extension, media type)
FORMATS = {
    "pdf": ("document", render.render_pdf, "pdf", "application/pdf"),
    "docx": ("document", render.render_docx, "docx",
             "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    "md": ("document", render.render_markdown, "md", "text/markdown; charset=utf-8"),
    "txt": ("document", render.render_text, "txt", "text/plain; charset=utf-8"),
    "xlsx": ("workbook", render.render_xlsx, "xlsx",
             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "csv": ("workbook", render.render_csv, "csv", "text/csv; charset=utf-8"),
    "json": ("workbook", render.render_json, "json", "application/json"),
    "pptx": ("deck", render.render_pptx, "pptx",
             "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
}
_VALIDATORS = {"document": validate_document, "workbook": validate_workbook, "deck": validate_deck}
_UNSAFE = re.compile(r"[^A-Za-z0-9]+")


@dataclass(frozen=True)
class GeneratedFile:
    filename: str
    media_type: str
    data: bytes = field(repr=False)


def safe_filename(title, extension):
    """An ASCII file name built from the title by an allowlist: letters and
    digits joined by hyphens, at most 60 characters, never empty, never a
    path, never a reserved device name."""
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    stem = _UNSAFE.sub("-", ascii_title).strip("-")[:60].strip("-").lower()
    if not stem or stem.upper() in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(com|lpt)[0-9]", stem):
        stem = "kalillac-document"
    return stem + "." + extension


def generate(spec, format):
    """A GeneratedFile for untrusted plain-data `spec` in `format`.

    Raises SpecError with a stable code for an unknown format, a refused
    specification, a document the format cannot represent, or a result above
    MAX_FILE_BYTES.
    """
    if type(format) is not str or format not in FORMATS:
        raise SpecError("unsupported_format")
    kind, renderer, extension, media_type = FORMATS[format]
    validated = _VALIDATORS[kind](spec)
    data = renderer(validated)
    if len(data) > MAX_FILE_BYTES:
        raise SpecError("file_too_large")
    if format == "pdf":
        # Read the finished file back: text past a margin or a blank page in the middle is
        # refused here rather than handed to a customer.
        from .quality import check_pdf
        check_pdf(data, full_bleed_first_page=validated.cover)
    return GeneratedFile(safe_filename(validated.title, extension), media_type, data)
