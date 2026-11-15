"""Run file generation and upload intake in a separate, limited process.

Not reachable from the application. run_generate() and run_inspect() do the
work of generate() and intake.inspect() in a child Python process so that a
slow or hostile document can never hold a chat worker, exhaust the server's
memory or crash the API process:

  * hard wall-clock limit: the child is killed when it expires;
  * memory and CPU limits set by the child on itself before it touches any
    input, after which it also forbids itself any new process or thread
    (POSIX only: on Windows there is no equivalent here, so the time limit
    is the only bound; production runs on Linux);
  * a concurrency cap: beyond MAX_CONCURRENT the call is refused, not queued;
  * a minimal environment: the child receives no variable of the parent's
    except what Python needs to start, so no provider key, database
    credential or other secret is ever present in the process that parses
    customer files;
  * bounded output: the parent stops reading and kills the child past the cap;
  * nothing but a stable code crosses back on failure; the child's stderr is
    discarded.

Every failure is a WorkerError with a stable code. A refusal by the core
itself (a SpecError or IntakeError code) is passed through unchanged.
"""
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import threading

from .generation import FORMATS, MAX_FILE_BYTES, GeneratedFile
from .intake import MAX_TEXT_CHARS, MAX_UPLOAD_BYTES, Inspected

MAX_CONCURRENT = 2
DEFAULT_TIMEOUT_SECONDS = 20
MAX_MEMORY_BYTES = 768 * 1024 * 1024
MAX_SPEC_BYTES = 4 * 1024 * 1024
# Header plus the largest payload either task can return (UTF-8 text is at
# most four bytes a character).
MAX_OUTPUT_BYTES = 64 * 1024 + max(MAX_FILE_BYTES, 4 * MAX_TEXT_CHARS)
_PACKAGE_PARENT = str(Path(__file__).resolve().parents[1])
_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT)
# The few variables Python needs to start on each platform. Nothing else is inherited.
_INHERITED = ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL")


class WorkerError(RuntimeError):
    """The isolated run failed or was refused. `code` is stable."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class _Reply:
    header: dict
    data: bytes = field(repr=False)


def _command():
    """The child: an isolated interpreter (-I: no environment variables, user
    site or current directory on its path) that can import only this
    package's parent directory and the installed libraries."""
    bootstrap = ("import sys; sys.path.insert(0, sys.argv[1]); "
                 "from kalillac_files import worker; worker.main()")
    return [sys.executable, "-I", "-c", bootstrap, _PACKAGE_PARENT]


def _environment():
    environment = {name: os.environ[name] for name in _INHERITED if name in os.environ}
    environment["RAYON_NUM_THREADS"] = "2"  # The typesetter's thread pool: small and fixed.
    # glibc reserves 64 MB of address space per malloc arena; under an address-space limit,
    # a few arenas on a many-core machine would exhaust it before any real work.
    environment["MALLOC_ARENA_MAX"] = "2"
    return environment


def _frame(header, payload=b""):
    encoded = json.dumps(header, separators=(",", ":")).encode("utf-8")
    return struct.pack(">I", len(encoded)) + encoded + payload


def _unframe(blob):
    if len(blob) < 4:
        raise WorkerError("worker_failed")
    (length,) = struct.unpack(">I", blob[:4])
    if not 2 <= length <= 64 * 1024 or len(blob) < 4 + length:
        raise WorkerError("worker_failed")
    try:
        header = json.loads(blob[4:4 + length].decode("utf-8"))
    except ValueError:
        raise WorkerError("worker_failed") from None
    if type(header) is not dict:
        raise WorkerError("worker_failed")
    return _Reply(header, blob[4 + length:])


def _run(header, payload, timeout_seconds, max_memory_bytes):
    if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 120:
        raise WorkerError("invalid_limits")
    if type(max_memory_bytes) is not int or not 64 * 1024 * 1024 <= max_memory_bytes <= 4 * 1024**3:
        raise WorkerError("invalid_limits")
    if not _SLOTS.acquire(blocking=False):
        raise WorkerError("busy")
    try:
        request = _frame({**header, "memory": max_memory_bytes, "cpu": int(timeout_seconds) + 2}, payload)
        try:
            process = subprocess.Popen(_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, env=_environment(), cwd=_PACKAGE_PARENT,
                                       close_fds=True)
        except OSError:
            raise WorkerError("worker_failed") from None
        chunks, state = [], {"size": 0, "overflow": False}

        def feed():
            try:
                process.stdin.write(request)
                process.stdin.close()
            except OSError:
                pass  # The child ended early; its exit is reported below.

        def collect():
            while True:
                chunk = process.stdout.read(65536)
                if not chunk:
                    return
                state["size"] += len(chunk)
                if state["size"] > MAX_OUTPUT_BYTES:
                    state["overflow"] = True
                    process.kill()
                    return
                chunks.append(chunk)

        threads = [threading.Thread(target=feed, daemon=True), threading.Thread(target=collect, daemon=True)]
        for thread in threads:
            thread.start()
        try:
            process.wait(timeout=timeout_seconds)
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            for thread in threads:
                thread.join(timeout=5)
            process.stdout.close()
        if timed_out:
            raise WorkerError("timeout")
        if state["overflow"]:
            raise WorkerError("output_too_large")
        if process.returncode != 0:
            raise WorkerError("worker_failed")
        reply = _unframe(b"".join(chunks))
        if reply.header.get("ok") is not True:
            code = reply.header.get("code")
            raise WorkerError(code if type(code) is str and code.isascii() and 0 < len(code) <= 64 else "worker_failed")
        return reply
    finally:
        _SLOTS.release()


def run_generate(spec, format, timeout_seconds=DEFAULT_TIMEOUT_SECONDS, max_memory_bytes=MAX_MEMORY_BYTES):
    """generate(spec, format) in an isolated process: a GeneratedFile, or WorkerError."""
    if type(format) is not str or format not in FORMATS:
        raise WorkerError("unsupported_format")
    try:
        payload = json.dumps(spec, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise WorkerError("invalid_structure") from None
    if len(payload) > MAX_SPEC_BYTES:
        raise WorkerError("document_too_large")
    reply = _run({"task": "generate", "format": format}, payload, timeout_seconds, max_memory_bytes)
    filename, media_type = reply.header.get("filename"), reply.header.get("media_type")
    if type(filename) is not str or media_type != FORMATS[format][3] or not reply.data \
            or len(reply.data) > MAX_FILE_BYTES or not filename.endswith("." + FORMATS[format][2]):
        raise WorkerError("worker_failed")
    return GeneratedFile(filename, media_type, reply.data)


def run_inspect(data, declared_name=None, timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
                max_memory_bytes=MAX_MEMORY_BYTES):
    """intake.inspect(data, declared_name) in an isolated process: Inspected, or WorkerError."""
    if type(data) is not bytes:
        raise WorkerError("invalid_upload")
    if not data:
        raise WorkerError("empty_file")
    if len(data) > MAX_UPLOAD_BYTES:
        raise WorkerError("file_too_large")
    if declared_name is not None and (type(declared_name) is not str or len(declared_name) > 255):
        raise WorkerError("unsupported_type")
    reply = _run({"task": "inspect", "name": declared_name}, data, timeout_seconds, max_memory_bytes)
    kind, truncated, units = reply.header.get("kind"), reply.header.get("truncated"), reply.header.get("units")
    try:
        text = reply.data.decode("utf-8")
    except UnicodeDecodeError:
        raise WorkerError("worker_failed") from None
    if type(kind) is not str or type(truncated) is not bool or type(units) is not int or len(text) > MAX_TEXT_CHARS:
        raise WorkerError("worker_failed")
    return Inspected(kind, text, truncated, units)


# --- the child ------------------------------------------------------------------------------


def _limit_self(memory, cpu_seconds):
    try:
        import resource
    except ImportError:
        return  # Windows: the parent's wall-clock limit is the bound.
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _forbid_new_tasks():
    """From here on the child can start no process and no thread."""
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
    except (ImportError, ValueError, OSError):
        pass


def main():
    """Child entry point: one request on stdin, one reply on stdout."""
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    try:
        (length,) = struct.unpack(">I", stdin.read(4))
        header = json.loads(stdin.read(length).decode("utf-8"))
        _limit_self(int(header["memory"]), int(header["cpu"]))
        # openpyxl imports numpy when it is installed, and numpy starts
        # threads on import, which is forbidden below (it hangs or aborts).
        # Nothing here needs numpy: make the import fail cleanly.
        sys.modules["numpy"] = None
        if header.get("task") == "generate" and header.get("format") in ("pdf", "docx"):
            # The typesetter runs on a small thread pool. It is started here, on
            # nothing but a constant, before any customer data is read; after
            # that no further thread or process can be created.
            from .typeset import warm_up
            warm_up()
        _forbid_new_tasks()
        payload = stdin.read(max(MAX_SPEC_BYTES, MAX_UPLOAD_BYTES) + 1)
        from .intake import IntakeError, inspect
        from .generation import generate
        from .spec import SpecError
        try:
            if header["task"] == "generate":
                result = generate(json.loads(payload.decode("utf-8")), header["format"])
                reply = _frame({"ok": True, "filename": result.filename, "media_type": result.media_type}, result.data)
            elif header["task"] == "inspect":
                found = inspect(payload, header.get("name"))
                reply = _frame({"ok": True, "kind": found.kind, "truncated": found.truncated, "units": found.units},
                               found.text.encode("utf-8", "replace"))
            else:
                reply = _frame({"ok": False, "code": "worker_failed"})
        except (SpecError, IntakeError) as refused:
            reply = _frame({"ok": False, "code": refused.code})
        except MemoryError:
            reply = _frame({"ok": False, "code": "out_of_memory"})
        except RecursionError:
            reply = _frame({"ok": False, "code": "unreadable_document"})
    except BaseException:
        # Never a traceback: it could carry document content.
        os._exit(3)
    stdout.write(reply)
    stdout.flush()
