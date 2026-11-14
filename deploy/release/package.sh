#!/usr/bin/env bash
# Build the upload package for one release, entirely from Git (run on a workstation, not production):
#
#   bash deploy/release/package.sh <release short sha> <new output directory>
#
# Every script and configuration file is the exact blob at the tooling commit (HEAD, which must be
# clean under deploy/release); release.env is deploy/release/releases/<short>.env. The source bundle
# holds the release commit and its history only. SHA256SUMS covers every file. A reviewer can rebuild
# the package from the same tooling commit and compare every line of SHA256SUMS except the bundle's
# (a bundle's pack layout may vary between Git versions; its content is bound by the commit ID, which
# stage.sh checks after an fsck-verified clone).
#
# Development only: KALILLAC_PACKAGE_FROM_WORKTREE=1 copies deploy/release from the working tree and
# records tooling_commit=UNCOMMITTED. Such a package is for disposable tests, never for production.
set -euo pipefail
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
# The exact approved commits are packaged: no inherited GIT_* variable, no replacement objects or
# grafts (they would substitute other objects for the named commits), no hooks.
while IFS= read -r _var; do unset "$_var"; done < <(compgen -e | grep '^GIT_' || true)
export GIT_NO_REPLACE_OBJECTS=1 GIT_GRAFT_FILE=/dev/null
git() { command git --no-replace-objects -c core.hooksPath=/dev/null -c advice.graftFileDeprecated=false "$@"; }
[[ $# -eq 2 ]] || die "usage: package.sh <release short sha> <new output directory>"
short=$1; out=$2
[[ $short =~ ^[0-9a-f]{8}$ ]] || die "release short sha must be 8 hex characters"
repo=$(git rev-parse --show-toplevel)
cd "$repo"
readonly FILES=(lib.sh check.sh stage.sh activate.sh rollback.sh preflight.py envcheck.py bytecheck.py production.env README.md)
conf=deploy/release/releases/$short.env

if [[ ${KALILLAC_PACKAGE_FROM_WORKTREE:-} == 1 ]]; then
    tooling=UNCOMMITTED
    get() { cat -- "$1"; }
else
    [[ -z "$(git status --porcelain -- deploy/release)" ]] || die "deploy/release has uncommitted changes; commit first"
    tooling=$(git rev-parse HEAD)
    get() { git cat-file blob "$tooling:$1"; }
fi
commit=$(get "$conf" | sed -n 's/^RELEASE_COMMIT=//p')
[[ $commit =~ ^[0-9a-f]{40}$ && $commit == "$short"* ]] || die "$conf does not name a commit starting with $short"
git cat-file -e "$commit^{commit}" || die "commit $commit is not in this repository"
[[ ! -e $out ]] || die "$out already exists"
mkdir -p -- "$out"

for f in "${FILES[@]}"; do get "deploy/release/$f" >"$out/$f"; done
get "$conf" >"$out/release.env"
grep -l $'\r' "$out"/* >/dev/null 2>&1 && die "a packaged file contains CR characters"

ref=refs/heads/kalillac-package-$short-$$
trap 'git update-ref -d "$ref" 2>/dev/null || true' EXIT
git update-ref "$ref" "$commit"
git bundle create -q "$out/kalillac-source-$short.bundle" "$ref" 2>/dev/null || git bundle create "$out/kalillac-source-$short.bundle" "$ref"
git update-ref -d "$ref"

{
    echo "tooling_commit=$tooling"
    echo "release_commit=$commit"
    echo "release_short=$short"
} >"$out/PACKAGE.txt"
(cd "$out" && sha256sum -- * | grep -v ' SHA256SUMS$' >SHA256SUMS)
echo "package: $out"
cat "$out/PACKAGE.txt"
