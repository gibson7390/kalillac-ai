"""The isolated worker for file generation and upload intake: real child
processes, real kills. Hostile children are small scripts substituted for the
worker command; nothing touches the application or the network.
"""
from __future__ import annotations

import copy
import io
import os
import subprocess
import sys
import time

import pytest

pytest.importorskip("pypdf")
docx = pytest.importorskip("docx")
openpyxl = pytest.importorskip("openpyxl")
pytest.importorskip("typst")
pytest.importorskip("pypdfium2")
pytest.importorskip("lxml")

from kalillac_files import generate, intake, worker  # noqa: E402
from kalillac_files.worker import WorkerError, run_generate, run_inspect  # noqa: E402
from test_file_generation import DOCUMENT, WORKBOOK  # noqa: E402
from test_file_intake import rezip  # noqa: E402

FRAME = ("import sys, json, struct\n"
         "def reply(header, data=b''):\n"
         "    h = json.dumps(header).encode()\n"
         "    sys.stdout.buffer.write(struct.pack('>I', len(h)) + h + data)\n"
         "    sys.stdout.buffer.flush()\n")


def refused(call, *args, **kwargs):
    with pytest.raises(WorkerError) as error:
        call(*args, **kwargs)
    assert str(error.value) == error.value.code
    return error.value.code


@pytest.fixture
def child(monkeypatch):
    """Replace the worker command with a script; records every process started."""
    started = []
    real_popen = subprocess.Popen

    def popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        started.append((process, kwargs.get("env")))
        return process

    monkeypatch.setattr(worker.subprocess, "Popen", popen)

    def use(script):
        monkeypatch.setattr(worker, "_command", lambda: [sys.executable, "-I", "-c", FRAME + script])
        return started
    use.started = started
    return use


# --- the real worker ------------------------------------------------------------------------


def test_generation_through_the_worker_matches_the_core():
    direct = generate(copy.deepcopy(DOCUMENT), "pdf")
    isolated = run_generate(copy.deepcopy(DOCUMENT), "pdf")
    assert isolated == direct                                  # Same name, media type and bytes.
    word = run_generate(copy.deepcopy(DOCUMENT), "docx")
    assert word.filename == "quarterly-review-cafe-operations.docx"
    assert "Summary" in [p.text for p in docx.Document(io.BytesIO(word.data)).paragraphs]
    sheet = run_generate(copy.deepcopy(WORKBOOK), "xlsx")
    assert openpyxl.load_workbook(io.BytesIO(sheet.data))["Inputs"]["B5"].value == "=SUM(B2:B4)"


def test_intake_through_the_worker_matches_the_core():
    for data, name in ((generate(copy.deepcopy(DOCUMENT), "docx").data, "report.docx"),
                       (generate(copy.deepcopy(DOCUMENT), "pdf").data, None),
                       (generate(copy.deepcopy(WORKBOOK), "xlsx").data, "model.xlsx"),
                       ("naïve — text ✓\n".encode("utf-8"), "notes.txt")):
        assert run_inspect(data, name) == intake.inspect(data, name)


def test_core_refusals_pass_through_with_their_own_codes():
    unsupported = {"title": "Plain", "blocks": [{"type": "paragraph", "text": "emoji \U0001f600"}]}
    assert refused(run_generate, unsupported, "pdf") == "pdf_unsupported_characters"
    assert refused(run_generate, {"title": "T", "blocks": []}, "docx") == "invalid_blocks"
    assert refused(run_generate, copy.deepcopy(WORKBOOK), "pdf") == "invalid_structure"
    word = generate(copy.deepcopy(DOCUMENT), "docx").data
    assert refused(run_inspect, rezip(word, add={"word/vbaProject.bin": b"\xd0\xcf"}), "x.docx") == "macros_not_allowed"
    assert refused(run_inspect, word, "x.pdf") == "type_mismatch"
    assert refused(run_inspect, b"%PDF-1.7\nnot a pdf", "x.pdf") == "unreadable_document"


def test_requests_are_refused_in_the_parent_before_any_process_starts(child):
    started = child("raise SystemExit('a process was started')")
    assert refused(run_generate, DOCUMENT, "pptx") == "unsupported_format"
    assert refused(run_generate, {"title": "T", "blocks": [float("nan")]}, "pdf") == "invalid_structure"
    assert refused(run_generate, {"title": {1, 2}}, "pdf") == "invalid_structure"
    huge = {"title": "T", "blocks": [{"type": "paragraph", "text": "x" * (worker.MAX_SPEC_BYTES + 1)}]}
    assert refused(run_generate, huge, "txt") == "document_too_large"
    assert refused(run_inspect, "text", "x.txt") == "invalid_upload"
    assert refused(run_inspect, b"", "x.txt") == "empty_file"
    assert refused(run_inspect, b"x" * (intake.MAX_UPLOAD_BYTES + 1), "x.txt") == "file_too_large"
    assert refused(run_inspect, b"x", 7) == "unsupported_type"
    for limits in ({"timeout_seconds": 0}, {"timeout_seconds": 121}, {"timeout_seconds": "5"},
                   {"max_memory_bytes": 1024}, {"max_memory_bytes": 8 * 1024**3}, {"max_memory_bytes": 1e9}):
        assert refused(run_generate, DOCUMENT, "txt", **limits) == "invalid_limits"
    assert started == []


# --- limits ---------------------------------------------------------------------------------


def test_a_child_that_never_finishes_is_killed_at_the_time_limit(child):
    started = child("import time\ntime.sleep(120)\n")
    begun = time.monotonic()
    assert refused(run_generate, DOCUMENT, "txt", timeout_seconds=1.5) == "timeout"
    assert time.monotonic() - begun < 15
    [(process, _env)] = started
    assert process.poll() is not None                           # Killed and reaped, not left running.
    # The slot is free again: the next request runs.
    child("reply({'ok': False, 'code': 'next_ran'})")
    assert refused(run_generate, DOCUMENT, "txt") == "next_ran"


def test_a_child_that_ignores_its_input_and_floods_output_is_cut_off(child, monkeypatch):
    monkeypatch.setattr(worker, "MAX_OUTPUT_BYTES", 50_000)
    started = child("while True:\n    sys.stdout.buffer.write(b'x' * 65536)\n")
    begun = time.monotonic()
    assert refused(run_inspect, b"x" * 1000, "x.txt") == "output_too_large"
    assert time.monotonic() - begun < 15 and started[0][0].poll() is not None


@pytest.mark.parametrize("script", [
    "sys.exit(3)",
    "raise RuntimeError('crash with a traceback')",
    "sys.stdout.buffer.write(b'not a frame')",
    "sys.stdout.buffer.write(struct.pack('>I', 10**6) + b'{}')",
    "sys.stdout.buffer.write(struct.pack('>I', 4) + b'[1]x')",
    "sys.stdout.buffer.write(struct.pack('>I', 5) + b'\\xff\\xfe\\x00\\x01\\x02')",
    "reply({'ok': False})",
    "reply({'ok': False, 'code': 'x' * 200})",
    "reply({'ok': False, 'code': 7})",
    "reply({'ok': 'yes', 'filename': 'a.txt', 'media_type': 'text/plain; charset=utf-8'}, b'data')",
    "reply({'ok': True, 'filename': 'a.exe', 'media_type': 'text/plain; charset=utf-8'}, b'data')",
    "reply({'ok': True, 'filename': 'a.txt', 'media_type': 'text/html'}, b'<script>')",
    "reply({'ok': True, 'filename': 'a.txt', 'media_type': 'text/plain; charset=utf-8'}, b'')",
    "reply({'ok': True, 'filename': ['a.txt'], 'media_type': 'text/plain; charset=utf-8'}, b'data')",
    "",
])
def test_a_broken_or_lying_child_is_only_ever_worker_failed(child, script):
    child(script)
    assert refused(run_generate, DOCUMENT, "txt") == "worker_failed"


@pytest.mark.parametrize("script", [
    "reply({'ok': True, 'kind': 'txt', 'truncated': False, 'units': 1}, b'\\xff\\xfe')",
    "reply({'ok': True, 'kind': 7, 'truncated': False, 'units': 1}, b'text')",
    "reply({'ok': True, 'kind': 'txt', 'truncated': 'no', 'units': 1}, b'text')",
    "reply({'ok': True, 'kind': 'txt', 'truncated': False, 'units': '1'}, b'text')",
    "reply({'ok': True, 'kind': 'txt', 'truncated': False, 'units': 1}, b'x' * 400001)",
])
def test_a_lying_intake_child_is_worker_failed(child, script):
    child(script)
    assert refused(run_inspect, b"hello", "x.txt") == "worker_failed"


def test_concurrency_is_capped_and_refused_not_queued(child):
    child("reply({'ok': False, 'code': 'ran'})")
    held = [worker._SLOTS.acquire(blocking=False) for _ in range(worker.MAX_CONCURRENT)]
    try:
        assert all(held)
        begun = time.monotonic()
        assert refused(run_generate, DOCUMENT, "txt") == "busy"
        assert time.monotonic() - begun < 1 and child.started == []
    finally:
        for _ in held:
            worker._SLOTS.release()
    assert refused(run_generate, DOCUMENT, "txt") == "ran"
    # Every outcome returned its slot.
    assert all(worker._SLOTS.acquire(blocking=False) for _ in range(worker.MAX_CONCURRENT))
    for _ in range(worker.MAX_CONCURRENT):
        worker._SLOTS.release()


def test_the_child_inherits_no_secret_and_no_unlisted_variable(child, monkeypatch):
    for name in ("OPENAI_API_KEY", "BRAVE_API_KEY", "TAVILY_API_KEY", "KALILLAC_DATABASE_URL",
                 "KALILLAC_STRIPE_SECRET_KEY", "PYTHONPATH", "PYTHONSTARTUP", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "synthetic-secret-value")
    started = child(
        "import os\n"
        "leaked = sorted(k for k in os.environ if k.upper().startswith(('OPENAI', 'BRAVE', 'TAVILY', 'KALILLAC', "
        "'STRIPE', 'AWS', 'PYTHONPATH', 'PYTHONSTARTUP')) or 'synthetic-secret-value' in os.environ[k])\n"
        "reply({'ok': False, 'code': 'leaked' if leaked else 'clean'})\n")
    assert refused(run_generate, DOCUMENT, "txt") == "clean"
    [(_process, env)] = started
    assert set(env) <= set(worker._INHERITED) | {"RAYON_NUM_THREADS", "MALLOC_ARENA_MAX"}
    assert "synthetic-secret-value" not in env.values()


def test_the_real_command_is_an_isolated_interpreter():
    command = worker._command()
    assert command[0] == sys.executable and command[1] == "-I" and command[2] == "-c"
    assert "worker.main()" in command[3] and os.path.isdir(os.path.join(command[4], "kalillac_files"))
    assert set(worker._environment()) <= set(worker._INHERITED) | {"RAYON_NUM_THREADS", "MALLOC_ARENA_MAX"}
    assert worker._environment()["RAYON_NUM_THREADS"] == "2"


@pytest.mark.skipif(sys.platform == "win32", reason="memory limits are set with POSIX resource limits")
def test_memory_limit_stops_the_child():
    spec = {"title": "T", "sheets": [{"name": "S", "columns": [{"header": "A", "format": "number"}] * 5,
                                     "rows": [[float(n)] * 5 for n in range(10_000)]}]}
    assert run_generate(copy.deepcopy(spec), "xlsx").data          # Fits in the default limit.
    # Starved of memory the child fails fast, or, if the allocator stalls instead (seen once on a
    # GitHub runner, never reproduced locally), it is killed at the time limit. Either way it is stopped.
    assert refused(run_generate, copy.deepcopy(spec), "xlsx", max_memory_bytes=64 * 1024 * 1024) in (
        "out_of_memory", "worker_failed", "timeout")


def test_the_application_does_not_reach_the_worker_yet():
    from pathlib import Path
    candidate = Path(__file__).resolve().parents[1]
    assert "kalillac_files" not in (candidate / "app_fastapi_candidate.py").read_text(encoding="utf-8")
