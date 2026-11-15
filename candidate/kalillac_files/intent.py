"""Does a chat message ask Kalillac to make (or change) a file?

Used by /api/chat, when files are enabled and the account has access, to hand a message to the file
assistant. It decides only where the message goes: the file assistant still answers in plain text
when no file is wanted, so a false positive costs a different model path, not a wrong answer. A
false negative keeps the ordinary chat. The rule is therefore deliberately plain:

  * a message with attached files always goes to the file assistant;
  * otherwise it needs a request to produce something (make, create, write, draft, build, turn into,
    export, ...) and a deliverable that is a file: a named format (PDF, Word, Excel, PowerPoint, CSV,
    .docx ...), or a spreadsheet, workbook, slide deck, slides, presentation or "file";
  * a message about programming or a how-to question about file software stays with the ordinary
    chat ("write Python that creates a PDF", "how do I export a Word file to PDF").
"""
import re

_PRODUCE = re.compile(
    r"\b(make|create|generate|build|produce|write|draft|prepare|design|put together|turn|convert|export|save|"
    r"format|lay out|give me|send me|need|want|could you do|can you do|redo|rewrite|update|edit|revise)\b",
    re.IGNORECASE)
_DELIVERABLE = re.compile(
    r"(\bpdfs?\b|\bword (?:doc|docs|document|documents|file|files|version)\b|\bdocx\b|\bexcel\b|\bspreadsheets?\b|"
    r"\bworkbooks?\b|\bxlsx\b|\bcsv\b|\bpower\s?point\b|\bpptx\b|\b(?:slide )?decks?\b|\bslides\b|"
    r"\bpresentations?\b|\b(?:as|into|in) an? (?:file|document)\b|\bdownloadable\b|\.(?:docx|xlsx|pptx|pdf)\b)",
    re.IGNORECASE)
_PROGRAMMING = re.compile(
    r"\b(code|script|python|javascript|typescript|java|c#|node|library|package|api|function|program|regex|"
    r"bash|powershell|command line|terminal|macro|vba|sql|html|css|react|latex|pandoc|openpyxl|xlsxwriter|"
    r"python-docx|reportlab|pdfkit|puppeteer)\b",
    re.IGNORECASE)
_HOW_TO = re.compile(r"^\s*(how (do|can|to|would|should)|what('s| is| are)|why|explain|is it possible)\b",
                     re.IGNORECASE)


def wants_file(message, attached=()):
    if attached:
        return True
    if type(message) is not str or not message.strip():
        return False
    text = message.strip()
    if _PROGRAMMING.search(text) or _HOW_TO.match(text):
        return False
    return bool(_PRODUCE.search(text) and _DELIVERABLE.search(text))
