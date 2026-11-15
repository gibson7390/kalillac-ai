"""Fetch and verify the fonts the document generator typesets with.

Every font is an openly licensed release from its publisher (SIL Open Font
License 1.1), named in candidate/kalillac_files/fonts/manifest.json with the
exact source and the SHA-256 of the file. A file that does not match its
recorded hash is refused and deleted; nothing unverified is ever left in the
font directory.

The Latin, Greek, Cyrillic, Arabic, Hebrew, Devanagari, Thai and monospace
fonts ("core") are committed to the repository, so this command only confirms
them. The Chinese, Japanese and Korean fonts ("cjk") are too large to commit
(about 5 MB each) and are downloaded into fonts/cjk/, which Git ignores.

Usage (from the candidate/ directory):
    python tools/fetch_document_fonts.py            verify core, fetch and verify cjk
    python tools/fetch_document_fonts.py --check    verify what is present, download nothing
    python tools/fetch_document_fonts.py --record   (maintainers) download every entry and print its hash

Exit codes: 0 every requested font present and verified; 1 a download or
verification failed; 2 bad arguments.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import sys
import urllib.request
import zipfile

FONTS = Path(__file__).resolve().parents[1] / "kalillac_files" / "fonts"
MANIFEST = FONTS / "manifest.json"
MAX_DOWNLOAD = 64 * 1024 * 1024
_ARCHIVES: dict[str, bytes] = {}


def _download(url: str) -> bytes:
    if not url.startswith("https://"):
        raise ValueError("font sources must be https")
    request = urllib.request.Request(url, headers={"User-Agent": "font-fetch"})
    with urllib.request.urlopen(request, timeout=120) as response:
        data = response.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD:
        raise ValueError("download larger than expected")
    return data


def _content(entry: dict) -> bytes:
    """The font file's bytes from its source: a direct URL or one member of a release archive."""
    if "member" not in entry:
        return _download(entry["url"])
    if entry["url"] not in _ARCHIVES:
        _ARCHIVES[entry["url"]] = _download(entry["url"])
    with zipfile.ZipFile(io.BytesIO(_ARCHIVES[entry["url"]])) as archive:
        return archive.read(entry["member"])


def _path(entry: dict) -> Path:
    name = entry["file"]
    if "/" in name.replace("cjk/", "", 1) or "\\" in name or ".." in name:
        raise ValueError("unsafe font file name in the manifest")
    return FONTS / name


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="fetch_document_fonts.py", allow_abbrev=False)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--record", action="store_true")
    try:
        args = parser.parse_args(argv)
    except SystemExit as stop:
        return 0 if stop.code == 0 else 2
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    failed = 0
    for entry in manifest["fonts"]:
        path = _path(entry)
        try:
            if args.record:
                data = _content(entry)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                print(f'{entry["file"]}  sha256={hashlib.sha256(data).hexdigest()}  bytes={len(data)}')
                continue
            if not path.is_file():
                if args.check:
                    print(f'MISSING   {entry["file"]}')
                    failed += entry["group"] == "core"
                    continue
                data = _content(entry)
                if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                    print(f'REFUSED   {entry["file"]} (downloaded file does not match the recorded hash)')
                    failed += 1
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                print(f'FETCHED   {entry["file"]}')
                continue
            if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
                path.unlink()
                print(f'REFUSED   {entry["file"]} (file on disk did not match the recorded hash; deleted)')
                failed += 1
                continue
            print(f'VERIFIED  {entry["file"]}')
        except Exception as error:  # A font source is public; the error names no secret.
            print(f'FAILED    {entry["file"]}: {type(error).__name__}')
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
