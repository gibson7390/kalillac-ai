"""Which chat messages the file assistant receives."""
import pytest

from kalillac_files.intent import wants_file

FILE_REQUESTS = [
    "Make me a PDF report on our Q3 sales",
    "Can you create an Excel spreadsheet to track my monthly budget?",
    "Write a cover letter for a marketing role as a Word document",
    "Put together a 10-slide deck for Monday's board meeting",
    "I need a presentation about our new product launch",
    "Turn this into a PowerPoint",
    "Draft a project proposal and give me a docx",
    "Build a three-year financial model in a workbook",
    "export the table above as a CSV",
    "Could you do the itinerary as a PDF?",
    "Create slides summarising the research",
    "Generate an invoice template in Excel",
    "Rewrite my CV and send me a Word version",
    "save that as a downloadable file",
]

NOT_FILE_REQUESTS = [
    "Write Python code that creates a PDF",
    "How do I export a Word document to PDF?",
    "What is the difference between xlsx and csv?",
    "Explain how PowerPoint animations work",
    "Write a VBA macro for Excel that sorts rows",
    "Write a short poem about autumn",
    "Summarise the history of the printing press",
    "Is it possible to password-protect a PDF?",
    "The deck of my boat needs repainting, what colour should I choose?",
    "I attached a debugger to the process and it crashed",
    "what's the capital of Peru",
    "Use openpyxl to make a spreadsheet",
    "",
]


@pytest.mark.parametrize("message", FILE_REQUESTS)
def test_requests_for_a_file_go_to_the_file_assistant(message):
    assert wants_file(message)


@pytest.mark.parametrize("message", NOT_FILE_REQUESTS)
def test_questions_writing_and_programming_stay_with_the_chat(message):
    assert not wants_file(message)


def test_an_attachment_always_goes_to_the_file_assistant():
    assert wants_file("what do you think?", attached=["abc"])
    assert not wants_file(None) and not wants_file(7)
