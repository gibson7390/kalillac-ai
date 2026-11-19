"""Data-lifecycle cleanup, run by an operator (or a timer the owner approves) as the service identity.

    python3 -B tools/data_lifecycle.py files --delete-after-expiry-days N [--root PATH]
                                       [--apply --confirm-deployment ID]
    python3 -B tools/data_lifecycle.py usage --keep-days N [--apply --confirm-deployment ID]

* files: deletes from disk files that have been past their availability period (FileStore's expires_at)
  for N more days, plus a one-hour margin, abandoned upload temporaries and orphaned data
  (kalillac_files/retention.py has the safety rules). The root is --root or KALILLAC_FILES_ROOT.
* usage: deletes per-account usage records older than N days (at least 8; kalillac_accounts/
  usage_retention.py), through the application's own database settings and login.

Nothing is deleted unless --apply is given AND --confirm-deployment equals KALILLAC_DEPLOYMENT_ID: the
operator names the deployment they mean, so the command cannot delete on the wrong one by accident.
Without --apply it is a dry run that reports what would go. Every period is explicit, with no default:
the owner's retention decisions (docs/DATA-LIFECYCLE.md) set them. The output is one JSON line of counts:
never a file name, an account, an address or a path. Exit status: 0 done, 2 refused (arguments or
confirmation), 3 another file cleanup holds the lock.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

CANDIDATE = Path(__file__).resolve().parents[1]
if str(CANDIDATE) not in sys.path:
    sys.path.insert(0, str(CANDIDATE))


class Refused(Exception):
    pass


def _confirmed(args, environ):
    if not args.apply:
        return False
    deployment = environ.get("KALILLAC_DEPLOYMENT_ID", "")
    if not deployment or args.confirm_deployment != deployment:
        raise Refused("--apply needs --confirm-deployment equal to this environment's KALILLAC_DEPLOYMENT_ID")
    return True


def _days(value, name):
    try:
        days = int(value)
    except (TypeError, ValueError):
        raise Refused(f"{name} must be a whole number of days") from None
    if days < 0:
        raise Refused(f"{name} must not be negative")
    return days


def run_files(args, environ, out):
    from datetime import timedelta
    from kalillac_files.retention import run_exclusive
    apply = _confirmed(args, environ)
    days = _days(args.delete_after_expiry_days, "--delete-after-expiry-days")
    root = Path(args.root or environ.get("KALILLAC_FILES_ROOT", ""))
    if not str(root) or str(root) == ".":
        raise Refused("--root or KALILLAC_FILES_ROOT is required")
    if not root.is_dir() or root.is_symlink():
        raise Refused("the files root must be an existing directory")
    report = run_exclusive(root, delete_after=timedelta(days=days), apply=apply)
    if report is None:      # The application's scheduled run, or another operator run, holds the lock.
        out.write(json.dumps({"command": "files", "result": "busy"}) + "\n")
        return 3
    out.write(json.dumps({"command": "files", "delete_after_expiry_days": days, **report.as_dict()}) + "\n")
    return 0


def run_usage(args, environ, out, factory=None):
    from kalillac_accounts.usage_retention import clean_usage
    apply = _confirmed(args, environ)
    days = _days(args.keep_days, "--keep-days")
    if factory is None:
        from kalillac_db.engine import get_session_factory
        factory = get_session_factory()
    try:
        report = clean_usage(factory, keep_days=days, apply=apply)
    except ValueError as refused:
        raise Refused(str(refused)) from None
    out.write(json.dumps({"command": "usage", **report}) + "\n")
    return 0


def main(argv=None, environ=None, out=None, factory=None):
    environ = os.environ if environ is None else environ
    out = out or sys.stdout
    parser = argparse.ArgumentParser(prog="data_lifecycle.py", allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)
    files = sub.add_parser("files")
    files.add_argument("--delete-after-expiry-days", required=True)
    files.add_argument("--root")
    usage = sub.add_parser("usage")
    usage.add_argument("--keep-days", required=True)
    for parser_ in (files, usage):
        parser_.add_argument("--apply", action="store_true")
        parser_.add_argument("--confirm-deployment")
    args = parser.parse_args(argv)
    try:
        if args.command == "files":
            return run_files(args, environ, out)
        return run_usage(args, environ, out, factory)
    except Refused as refused:
        out.write(json.dumps({"command": args.command, "result": "refused", "reason": str(refused)}) + "\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
