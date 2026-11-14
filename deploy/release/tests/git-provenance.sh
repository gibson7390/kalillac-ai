#!/usr/bin/env bash
# Packaging cannot be steered by Git replacement objects (no root needed; any OS with bash + git).
#
#   bash deploy/release/tests/git-provenance.sh <repository> <tooling commit>
#
# In a throwaway clone it installs replacement objects for BOTH the tooling commit (its
# deploy/release/stage.sh altered) and the release commit 54e38ecf (candidate/kalillac_server.py
# altered; the main application blob unchanged), proves plain git now resolves the substitutes, and
# then checks that package.sh packages the genuine objects: the scripts are the tooling commit's real
# blobs and the bundle's release commit has its real tree. Repeated with the substitutes under a
# custom namespace selected by GIT_REPLACE_REF_BASE in the caller's environment.
set -uo pipefail
[[ $# -eq 2 ]] || { echo "usage: git-provenance.sh <repository> <tooling commit>"; exit 2; }
SRC=$1; TOOLING=$2
RELEASE=54e38ecf36a4a25db20946019dd298311a632c1d
APP_BLOB=9f921b4428e8a66bd2dac38bf6bbb8b5698ec38b
W=$(mktemp -d); trap 'rm -rf -- "$W"' EXIT
PASS=0; FAIL=0
check() { local d=$1; shift; if "$@"; then echo "  PASS  $d"; PASS=$((PASS + 1)); else echo "  FAIL  $d"; FAIL=$((FAIL + 1)); fi; }
G() { git -C "$W/repo" "$@"; }

git clone -q --no-local "$SRC" "$W/repo"
G checkout -q --detach "$TOOLING"
GENUINE_STAGE=$(G --no-replace-objects rev-parse "$TOOLING:deploy/release/stage.sh")
GENUINE_TREE=$(G --no-replace-objects rev-parse "$RELEASE^{tree}")
GENUINE_SERVER=$(G --no-replace-objects rev-parse "$RELEASE:candidate/kalillac_server.py")

substitute() {   # substitute <commit> <path> -> prints a commit identical but for <path>
    local idx blob tree
    idx=$(mktemp)
    GIT_INDEX_FILE=$idx G read-tree "$1"
    blob=$( { G cat-file blob "$1:$2"; printf '\n# SUBSTITUTED\n'; } | G hash-object -w --stdin)
    GIT_INDEX_FILE=$idx G update-index --cacheinfo "100644,$blob,$2"
    tree=$(GIT_INDEX_FILE=$idx G write-tree); rm -f "$idx"
    G cat-file commit "$1" | sed "s/^tree .*/tree $tree/" | G hash-object -t commit -w --stdin
}
EVIL_T=$(substitute "$TOOLING" deploy/release/stage.sh)
EVIL_R=$(substitute "$RELEASE" candidate/kalillac_server.py)

verify_package() {   # verify_package <label> <package dir>
    local label=$1 out=$2
    check "$label: package.sh succeeded" test -f "$out/SHA256SUMS"
    check "$label: packaged stage.sh is the tooling commit's genuine blob" \
        test "$(git hash-object "$out/stage.sh")" == "$GENUINE_STAGE"
    check "$label: no substituted content in the packaged scripts" bash -c "! grep -q SUBSTITUTED '$out'/*.sh"
    check "$label: PACKAGE.txt names the tooling commit" grep -qx "tooling_commit=$TOOLING" "$out/PACKAGE.txt"
    git -c init.defaultBranch=x clone -q --no-checkout "$out/kalillac-source-54e38ecf.bundle" "$W/check-$label" 2>/dev/null
    check "$label: bundled release commit has its genuine tree" \
        test "$(git -C "$W/check-$label" --no-replace-objects rev-parse "$RELEASE^{tree}")" == "$GENUINE_TREE"
    check "$label: bundled secondary module is the genuine blob" \
        test "$(git -C "$W/check-$label" --no-replace-objects rev-parse "$RELEASE:candidate/kalillac_server.py")" == "$GENUINE_SERVER"
}

echo "== substitutes under refs/replace/"
G replace "$TOOLING" "$EVIL_T"; G replace "$RELEASE" "$EVIL_R"
check "proof: plain git resolves the substituted stage.sh" bash -c "git -C '$W/repo' show '$TOOLING:deploy/release/stage.sh' | grep -q SUBSTITUTED"
check "proof: plain git resolves the substituted release tree, with the same application blob" \
    test "$(G rev-parse "$RELEASE^{tree}")" != "$GENUINE_TREE" -a "$(G rev-parse "$RELEASE:candidate/app_fastapi_candidate.py")" == "$APP_BLOB"
(cd "$W/repo" && bash deploy/release/package.sh 54e38ecf "$W/pkg-replace" >/dev/null 2>&1)
verify_package replace "$W/pkg-replace"

echo "== substitutes under a custom namespace selected by GIT_REPLACE_REF_BASE"
G replace -d "$TOOLING" "$RELEASE" >/dev/null
G update-ref "refs/kdt-alt/$TOOLING" "$EVIL_T"; G update-ref "refs/kdt-alt/$RELEASE" "$EVIL_R"
check "proof: with GIT_REPLACE_REF_BASE, plain git resolves the substitutes" \
    bash -c "GIT_REPLACE_REF_BASE=refs/kdt-alt/ git -C '$W/repo' show '$TOOLING:deploy/release/stage.sh' | grep -q SUBSTITUTED"
(cd "$W/repo" && env -u GIT_NO_REPLACE_OBJECTS GIT_REPLACE_REF_BASE=refs/kdt-alt/ bash deploy/release/package.sh 54e38ecf "$W/pkg-env" >/dev/null 2>&1)
verify_package env "$W/pkg-env"

echo "== substitutes under refs/replace/ AND a working tree matching the substituted stage.sh"
G update-ref -d "refs/kdt-alt/$TOOLING"; G update-ref -d "refs/kdt-alt/$RELEASE"
G replace "$TOOLING" "$EVIL_T"; G replace "$RELEASE" "$EVIL_R"
G show "$TOOLING:deploy/release/stage.sh" >"$W/repo/deploy/release/stage.sh"
G reset -q   # index from the (substituted) HEAD tree, as an attacker controlling the repository would leave it
check "proof: plain git now reports deploy/release clean" test -z "$(G status --porcelain -- deploy/release)"
(cd "$W/repo" && bash deploy/release/package.sh 54e38ecf "$W/pkg-worktree" >/dev/null 2>&1); RC=$?
check "worktree-matched substitute: nothing substituted is ever packaged (package refused: exit $RC)"     bash -c "! grep -rqs SUBSTITUTED '$W/pkg-worktree' && { [[ $RC -ne 0 ]] || [[ \$(git hash-object '$W/pkg-worktree/stage.sh') == $GENUINE_STAGE ]]; }"

echo
echo "RESULT: $PASS passed, $FAIL failed"
((FAIL == 0))
