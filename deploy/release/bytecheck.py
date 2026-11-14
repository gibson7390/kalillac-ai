"""Kalillac release tooling: prove every Python bytecode file in a release is its source, compiled.

CPython imports __pycache__/<module>.<tag>.pyc instead of compiling <module>.py whenever the pyc
header matches the source's mtime and size (or, for hash-based pycs, without checking the source
at all when unchecked). -B and PYTHONDONTWRITEBYTECODE only stop WRITING bytecode, never loading
it. A pyc with a valid header but different code therefore runs instead of the reviewed source.

Run with the SAME interpreter the service uses (the venv python), so the magic number, cache tag
and compiler are the ones that import the application:

    <venv>/bin/python3 -I -B bytecheck.py <release dir>

Every *.pyc / *.pyo below the release (outside .git) must be:
  * inside a __pycache__ directory, named <module>.<this interpreter's cache tag>[.opt-N].pyc;
  * next to an existing <module>.py source file;
  * stamped with this interpreter's magic number; and
  * a code object EQUAL to compile(<source>, optimize=N): CPython compares code objects by their
    bytecode, constants, names, line tables and nested code objects.
Anything else fails. Output: one line, BYTECODE_OK <count> or BYTECODE_FAILED <relative path>: <reason>.
Files are only read; nothing is compiled to disk or deleted.
"""
import importlib.util
import marshal
import os
import sys

TAG = sys.implementation.cache_tag
MAGIC = importlib.util.MAGIC_NUMBER


def check(release):
    count = 0
    for root, dirs, files in os.walk(release):
        if root == release and ".git" in dirs:
            dirs.remove(".git")
        for name in sorted(files):
            if not name.endswith((".pyc", ".pyo")):
                continue
            path = os.path.join(root, name)
            rel = os.path.relpath(path, release)
            if os.path.basename(root) != "__pycache__" or not name.endswith(".pyc"):
                return "BYTECODE_FAILED %s: bytecode outside __pycache__ (sourceless or legacy pyc)" % rel
            parts = name[:-4].split(".")
            if len(parts) not in (2, 3) or parts[1] != TAG:
                return "BYTECODE_FAILED %s: not bytecode of this interpreter (%s)" % (rel, TAG)
            optimize = 0
            if len(parts) == 3:
                if parts[2] not in ("opt-1", "opt-2"):
                    return "BYTECODE_FAILED %s: unknown optimization tag" % rel
                optimize = int(parts[2][-1])
            source = os.path.join(os.path.dirname(root), parts[0] + ".py")
            if os.path.islink(path) or not os.path.isfile(source) or os.path.islink(source):
                return "BYTECODE_FAILED %s: no regular source file %s" % (rel, os.path.relpath(source, release))
            with open(path, "rb") as handle:
                data = handle.read()
            if len(data) < 16 or data[:4] != MAGIC:
                return "BYTECODE_FAILED %s: wrong magic number for this interpreter" % rel
            try:
                loaded = marshal.loads(data[16:])
            except Exception:
                return "BYTECODE_FAILED %s: unreadable code object" % rel
            with open(source, "rb") as handle:
                expected = compile(handle.read(), source, "exec", dont_inherit=True, optimize=optimize)
            if type(loaded) is not type(expected) or loaded != expected:
                return "BYTECODE_FAILED %s: code differs from its source %s" % (rel, os.path.relpath(source, release))
            count += 1
    return "BYTECODE_OK %d" % count


def main(argv):
    if len(argv) != 2 or not os.path.isdir(argv[1]):
        print("BYTECODE_FAILED usage")
        return 2
    try:
        line = check(os.path.realpath(argv[1]))
    except Exception as error:
        line = "BYTECODE_FAILED unexpected " + type(error).__name__
    print(line)
    return 0 if line.startswith("BYTECODE_OK") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
