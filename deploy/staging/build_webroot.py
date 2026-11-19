"""Assemble a staging web root from a built site and the chat frontend. It writes one new directory.

    python deploy/staging/build_webroot.py --site site/dist/public --frontend frontend \
        --out /var/www/<staging> --marks accounts,files,reasoning

The site (built with `npm ci && npm run build` in site/) is served at / and the chat at /app/, as in
production. The chat page offers accounts, files and reasoning modes only where the deployment marks
its <html> element; a release does that by hand, and this does it for staging: each name in --marks
adds data-kalillac-<name>="on". The site reads one mark, accounts (its header, mobile menu and hero then
offer Sign in and Create account), so with accounts in --marks each of the site's own pages (its top-level
.html files) is marked the same way. Nothing else in either tree is changed, and nothing is fetched.
It refuses an output directory that already exists, a mark it does not know, and a page whose <html>
element is not the one it expects (so a changed page is never marked wrongly).
"""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import sys

MARKS = ("accounts", "files", "images", "reasoning", "spaces")
SITE_MARKS = ("accounts", "files")   # the marks the site reads (App.tsx: accounts; the account page: files)
HTML = '<html lang="en">'


def marked(page, marks):
    if page.count(HTML) != 1:
        raise ValueError("a page's <html> element is not the expected one")
    return page.replace(HTML, '<html lang="en"' + "".join(f' data-kalillac-{m}="on"' for m in marks) + ">")


def build(site, frontend, out, marks):
    site, frontend, out = Path(site), Path(frontend), Path(out)
    unknown = [m for m in marks if m not in MARKS]
    if unknown or len(set(marks)) != len(marks):
        raise ValueError("--marks takes each of " + ", ".join(MARKS) + " at most once")
    if not (site / "index.html").is_file() or not (frontend / "index.html").is_file():
        raise ValueError("--site must be the built site and --frontend the chat frontend")
    if out.exists():
        raise ValueError("--out already exists")
    page = marked((frontend / "index.html").read_text(encoding="utf-8"), [m for m in MARKS if m in marks])
    # The site's marks belong to its account pages, which exist only with accounts.
    site_marks = [m for m in SITE_MARKS if m in marks] if "accounts" in marks else []
    pages = {path.name: marked(path.read_text(encoding="utf-8"), site_marks)
             for path in sorted(site.glob("*.html"))} if site_marks else {}
    shutil.copytree(site, out)
    for name, text in pages.items():
        (out / name).write_text(text, encoding="utf-8", newline="\n")
    if (out / "app").exists():
        raise ValueError("the built site already has an /app/ directory")
    shutil.copytree(frontend, out / "app")
    (out / "app" / "index.html").write_text(page, encoding="utf-8", newline="\n")
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(prog="build_webroot.py", allow_abbrev=False)
    for option in ("--site", "--frontend", "--out"):
        parser.add_argument(option, required=True)
    parser.add_argument("--marks", default="")
    args = parser.parse_args(argv)
    try:
        out = build(args.site, args.frontend, args.out, [m for m in args.marks.split(",") if m])
    except ValueError as refused:
        print(f"refused: {refused}")
        return 2
    print(f"built {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
