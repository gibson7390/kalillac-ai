"""Generation of real, downloadable files from a validated specification.

Not imported by the application yet: nothing here is reachable from a request.
It is the generation core only. Storage, download authorization, per-user
quotas, the worker that runs it off the chat path and the model tool that
produces a specification all depend on customer accounts and come later.

    spec      the bounded, validated description of a document or workbook
    formulas  the spreadsheet-formula allowlist
    render    one renderer per format
    generation  generate(spec, format) -> GeneratedFile

Design rules:
  * A specification is plain data. Nothing in it is executed, evaluated,
    imported, fetched or used as a path. There is no template language and no
    HTML: text is always text.
  * Every size is bounded before any rendering starts, and the produced file
    is bounded again afterwards.
  * The file name is built from the title by an allowlist; nothing a caller
    supplies is ever used as a path.
  * A format is either fully supported or refused. A document a format cannot
    represent faithfully (for example text outside the PDF font's coverage)
    is refused with a stable code, never rendered wrongly.
"""
from .generation import FORMATS, GeneratedFile, generate
from .spec import SpecError

__all__ = ["FORMATS", "GeneratedFile", "SpecError", "generate"]
