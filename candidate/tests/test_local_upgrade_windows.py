"""U2 on the owner's actual layout: a worktree Git for Windows created, checked from WSL (Codex's audit of ca92dc6).

Run in WSL, as root like the upgrade tools, where Git for Windows is installed. Not part of the Linux CI job,
which has no Windows: there, test_local_upgrade.py covers the Linux layouts and the fail-closed refusal when Git
for Windows is absent. Every repository here is new and disposable (under C:/Kalillac/kalillac-ai/tmp); the
installed checkout is never touched.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "deploy/local"))
import live_upgrade as live  # noqa: E402
import upgrade_support as u  # noqa: E402

GIT_EXE = next((g for g in live.WINDOWS_GIT if os.access(g, os.X_OK)), None)
SCRATCH = Path("/mnt/c/Kalillac/kalillac-ai/tmp")
CODEX = SCRATCH / "upgrade-audit-evidence-a8s3a0ii"

if GIT_EXE is None:
    pytest.fail("Git for Windows is required: run this file in WSL on the owner's machine", pytrace=False)


def win(path):
    return live._windows_path(path)


def gitexe(*args, cwd):
    return subprocess.run([GIT_EXE, "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=str(cwd), check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def windows_repo():
    base = SCRATCH / ("ku-windows-" + uuid.uuid4().hex[:8])
    main = base / "main"
    main.mkdir(parents=True)
    gitexe("init", "-q", "-b", "trunk", win(main), cwd=base)
    gitexe("-C", win(main), "config", "core.autocrlf", "true", cwd=base)      # As Git for Windows installs it.
    (main / "crlf.txt").write_bytes(b"line one\nline two\n")
    gitexe("-C", win(main), "add", ".", cwd=base)
    gitexe("-C", win(main), "commit", "-q", "-m", "synthetic", cwd=base)
    sha = gitexe("-C", win(main), "rev-parse", "HEAD", cwd=base)
    detached, branch = base / "detached", base / "branch"
    gitexe("-C", win(main), "worktree", "add", "-q", "--detach", win(detached), cwd=base)
    gitexe("-C", win(main), "worktree", "add", "-q", "-b", "release-test", win(branch), cwd=base)
    assert (detached / ".git").read_text().startswith("gitdir: C:/")             # The Windows-created layout.
    assert b"\r\n" in (detached / "crlf.txt").read_bytes()                       # Checked out with CRLF.
    yield type("Repo", (), {"base": base, "main": main, "detached": detached, "branch": branch, "sha": sha})
    shutil.rmtree(base)


def test_codex_original_probe_fixture_is_accepted():
    """Codex's synthetic Windows-created worktree (its fixture, unchanged): rejected on ca92dc6, accepted now."""
    fixture = json.loads((CODEX / "fixture.json").read_text())
    live.checkout_identity(CODEX / "synthetic-worktree", fixture["sha"])


def test_windows_created_detached_and_branch_worktrees_with_crlf_are_accepted(windows_repo):
    r = windows_repo
    live.checkout_identity(r.detached, r.sha)
    live.checkout_identity(r.branch, r.sha, branch="release-test")
    live.checkout_identity(r.main, r.sha, branch="trunk")


@pytest.mark.parametrize("change", ["tracked", "untracked", "sha", "branch", "subdirectory"])
def test_windows_created_worktree_dirty_or_wrong_is_refused(windows_repo, change):
    r = windows_repo
    target, expected, branch = r.detached, r.sha, None
    if change == "tracked":
        (target / "crlf.txt").write_bytes(b"edited\r\n")
    elif change == "untracked":
        (target / "new.txt").write_text("untracked\n")
    elif change == "sha":
        expected = "0" * 40
    elif change == "branch":
        target, branch = r.branch, "trunk"
    else:
        (target / "sub").mkdir()
        target = target / "sub"
    with pytest.raises(u.Refused):
        live.checkout_identity(target, expected, branch=branch)


def test_windows_git_environment_cannot_select_another_worktree(windows_repo, monkeypatch):
    r = windows_repo
    (r.detached / "dirty.txt").write_text("uncommitted\n")
    monkeypatch.setenv("GIT_DIR", win(r.main) + "/.git")
    monkeypatch.setenv("GIT_WORK_TREE", win(r.main))
    monkeypatch.setenv("WSLENV", "GIT_DIR/p:GIT_WORK_TREE/p")    # Would forward them to Windows processes.
    with pytest.raises(u.Refused, match="uncommitted"):
        live.checkout_identity(r.detached, r.sha)


def test_the_check_never_writes_the_index(windows_repo):
    r = windows_repo
    index = Path(gitexe("-C", win(r.detached), "rev-parse", "--git-path", "index", cwd=r.base).replace("C:/", "/mnt/c/"))
    before = (index.read_bytes(), index.stat().st_mtime_ns)
    os.utime(r.detached / "crlf.txt")                    # A stat change that a refreshing status would record.
    live.checkout_identity(r.detached, r.sha)
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before
