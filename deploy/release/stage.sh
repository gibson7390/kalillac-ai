#!/usr/bin/env bash
# Kalillac AI: STAGE a release beside production. No effect on the running service.
#
# Usage (as root, from the unpacked package directory):
#   LAUNCHER_SHA256=<the launcher sha256 printed by check.sh> bash stage.sh
#
# Holds the deploy lock (one stage/activate/rollback at a time). The release directory is created
# atomically by this run (mkdir), and on failure only that same directory is removed.
# Creates only <RELEASE_PARENT>/<RELEASE_PREFIX><short>, in this order:
#   1. a clone of the release commit from the package's Git bundle (objects fsck-verified), proven
#      PRISTINE by content: every tracked file hashes to the commit, no untracked or ignored file;
#   2. the production launcher, copied from the running release as an explicit deployment artifact
#      (root:root 600, sha256 equal to LAUNCHER_SHA256 and to the running launcher);
#   3. verified again: tracked files exact, untracked files exactly [launcher], nothing ignored.
# Then the new application is imported with the live environment (preflight; no port, no request)
# and the database revision is read read-only. On failure it removes only what this run created.
#
# A release directory that already exists (for example from a failed attempt) is refused. With
# QUARANTINE_EXISTING=1 it is moved, never deleted, to <ROLLBACK_PARENT>/quarantine/ after proving
# that it is not running and that no unit, drop-in or process references it.
source "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"
umask 022   # release files readable like every earlier release; the launcher is set to 600 explicitly
load_all_conf
banner stage

STAGED=0
MADE=0
cleanup() {
    local status=$?
    trap - EXIT
    ((status == 0 && STAGED)) && return 0
    ((status == 0)) && status=1
    # Delete only the very directory this run created (same device and inode), and never one the
    # service runs from or the unit references; otherwise keep it and say so.
    if ((MADE)); then
        if [[ -d $NEW_RELEASE && ! -L $NEW_RELEASE && "$(stat -c '%d:%i' -- "$NEW_RELEASE" 2>/dev/null || true)" == "$MADE_ID" ]]             && [[ "$(proc_cwd "$(main_pid)" 2>/dev/null || true)" != "$NEW_RELEASE"/* ]]             && [[ "$(systemctl cat "$SERVICE" 2>/dev/null || true)" != *"$NEW_RELEASE"* ]]; then
            rm -rf -- "$NEW_RELEASE"; echo "removed $NEW_RELEASE (the directory this run created)"
        else
            echo "KEPT $NEW_RELEASE: not provably the directory this run created, or referenced by the service"
        fi
    fi
    if [[ -n ${WORK:-} && -d $WORK && ! -L $WORK && "$(stat -c '%d:%i' -- "$WORK" 2>/dev/null || true)" == "$WORK_ID" ]]; then
        rm -rf -- "$WORK"
    fi
    echo "STAGE FAILED (status $status); the running service, its unit and drop-ins were not touched."
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT; trap 'exit 143' TERM; trap 'exit 129' HUP

require_root_and_tools
acquire_deploy_lock
[[ ${LAUNCHER_SHA256:-} =~ ^[0-9a-f]{64}$ ]] || die "set LAUNCHER_SHA256 to the running launcher's sha256 (printed by check.sh)"
BUNDLE=$KD_DIR/kalillac-source-$RELEASE_SHORT.bundle
expect_file "$BUNDLE"

log "Live release"
identify_live "$OLD_RELEASE_EXPECTED" "$PREVIOUS_COMMIT" "$PREVIOUS_APP_BLOB"
verify_launcher "$LIVE_RELEASE" "$LAUNCHER_SHA256"
log "running: pid $LIVE_PID, $LIVE_RELEASE at $PREVIOUS_COMMIT (tracked files exact, only [$DEPLOY_ARTIFACTS] untracked)"
log "launcher: $LAUNCHER_OWNER $LAUNCHER_MODE, sha256 $LAUNCHER_SHA256 (approved)"

# Atomic creation is the only existence test (no check-then-create window). This run owns the
# release directory only if its own mkdir created it.
log "Create $NEW_RELEASE"
if ! mkdir -m 755 -- "$NEW_RELEASE" 2>/dev/null; then
    [[ -e $NEW_RELEASE || -L $NEW_RELEASE ]] || die "cannot create $NEW_RELEASE"
    [[ ${QUARANTINE_EXISTING:-} == 1 ]] || die "$NEW_RELEASE already exists (for example from a failed attempt). Nothing was changed. To move it aside (nothing is deleted), rerun with QUARANTINE_EXISTING=1"
    [[ $NEW_RELEASE != "$LIVE_RELEASE" ]] || die "$NEW_RELEASE is the running release"
    [[ "$(systemctl cat "$SERVICE" 2>/dev/null)" != *"$NEW_RELEASE"* ]] || die "the unit or a loaded drop-in references $NEW_RELEASE"
    [[ -z "$(grep -rlF -- "$NEW_RELEASE" "$DROPIN_DIR" 2>/dev/null || true)" ]] || die "a file in $DROPIN_DIR references $NEW_RELEASE"
    for proc in /proc/[0-9]*; do
        case "$(readlink "$proc/cwd" 2>/dev/null || true)" in
            "$NEW_RELEASE"|"$NEW_RELEASE"/*) die "process ${proc#/proc/} runs inside $NEW_RELEASE" ;;
        esac
    done
    QUARANTINE=$ROLLBACK_PARENT/quarantine/$(basename "$NEW_RELEASE")-$(date -u +%Y%m%dT%H%M%SZ)
    install -d -m 700 "$ROLLBACK_PARENT" "$ROLLBACK_PARENT/quarantine"
    mv -T -- "$NEW_RELEASE" "$QUARANTINE"
    log "existing, unreferenced $NEW_RELEASE moved to $QUARANTINE (kept, not deleted)"
    mkdir -m 755 -- "$NEW_RELEASE" || die "cannot create $NEW_RELEASE after quarantine (created by someone else meanwhile?)"
fi
MADE=1
MADE_ID=$(stat -c '%d:%i' -- "$NEW_RELEASE")

# Clone into a private directory: git's own failure cleanup empties the clone path, so it must never
# be a path someone else could have taken over. The result is then renamed onto the reservation,
# only while that is still the empty directory this run created.
log "Clone $RELEASE_COMMIT"
WORK=$(mktemp -d -- "$RELEASE_PARENT/.kalillac-stage-$RELEASE_SHORT.XXXXXX")
WORK_ID=$(stat -c '%d:%i' -- "$WORK")
gx -c init.defaultBranch=release -c transfer.fsckObjects=true clone --quiet --no-checkout "$BUNDLE" "$WORK/release"
g "$WORK/release" checkout --quiet --detach "$RELEASE_COMMIT"
[[ "$(stat -c '%d:%i' -- "$NEW_RELEASE" 2>/dev/null || true)" == "$MADE_ID" && -z "$(ls -A -- "$NEW_RELEASE")" ]]     || die "$NEW_RELEASE is no longer the empty directory this run reserved"
mv -T -- "$WORK/release" "$NEW_RELEASE"
MADE_ID=$(stat -c '%d:%i' -- "$NEW_RELEASE")
rmdir -- "$WORK"; WORK=""
verify_release_tree "$NEW_RELEASE" "$RELEASE_COMMIT" pristine
[[ "$(g "$NEW_RELEASE" rev-parse "$RELEASE_COMMIT:candidate/app_fastapi_candidate.py")" == "$APP_BLOB" ]] || die "application blob is not the reviewed $APP_BLOB"
g "$NEW_RELEASE" merge-base --is-ancestor "$PREVIOUS_COMMIT" "$RELEASE_COMMIT" || die "$PREVIOUS_SHORT is not an ancestor of $RELEASE_SHORT"
g "$NEW_RELEASE" diff --quiet "$PREVIOUS_COMMIT" "$RELEASE_COMMIT" -- $IDENTICAL_PATHS \
    || die "these paths differ between $PREVIOUS_SHORT and $RELEASE_SHORT: $IDENTICAL_PATHS"
[[ "$(migration_head "$NEW_RELEASE" "$RELEASE_COMMIT")" == "$MIGRATION_HEAD" ]] || die "release migration head is not $MIGRATION_HEAD"
log "pristine checkout verified: HEAD, every tracked file, no untracked or ignored file; app blob $APP_BLOB"
log "unchanged from $PREVIOUS_SHORT: $IDENTICAL_PATHS; migration head $MIGRATION_HEAD"

log "Install the launcher (deployment artifact)"
chown -R --reference="$LIVE_RELEASE/candidate" "$NEW_RELEASE"
install -o "${LAUNCHER_OWNER%%:*}" -g "${LAUNCHER_OWNER#*:}" -m "$LAUNCHER_MODE" -- "$LIVE_RELEASE/$LAUNCHER_NAME" "$NEW_RELEASE/$LAUNCHER_NAME"
cmp -s "$LIVE_RELEASE/$LAUNCHER_NAME" "$NEW_RELEASE/$LAUNCHER_NAME" || die "launcher copy differs from the running launcher"
verify_launcher "$NEW_RELEASE" "$LAUNCHER_SHA256"
verify_release_tree "$NEW_RELEASE" "$RELEASE_COMMIT"
log "release verified: tracked files exact; untracked exactly [$DEPLOY_ARTIFACTS] ($LAUNCHER_OWNER $LAUNCHER_MODE, sha256 matches)"

log "Web root (kept unchanged)"
verify_webroot_app "$NEW_RELEASE"
log "live /app/ already equals frontend/ at $RELEASE_SHORT"

log "Preflight"
app_preflight "$LIVE_PID" "$NEW_RELEASE"
verify_release_tree "$NEW_RELEASE" "$RELEASE_COMMIT"
db_check

# Marker inside .git (outside the worktree, so the content checks are unaffected): activate.sh only
# accepts a release directory produced and verified by this script.
printf 'release_commit=%s\nlauncher_sha256=%s\nowner=%s\ntooling_commit=%s\nstaged_utc=%s\n' \
    "$RELEASE_COMMIT" "$LAUNCHER_SHA256" "$(stat -c '%U:%G' "$NEW_CWD")" \
    "$(sed -n 's/^tooling_commit=//p' "$KD_DIR/PACKAGE.txt" 2>/dev/null)" "$(date -u +%Y%m%dT%H%M%SZ)" \
    >"$NEW_RELEASE/.git/$STAGE_MARKER"
chmod 600 "$NEW_RELEASE/.git/$STAGE_MARKER"
STAGED=1
echo "STAGED_OK release=$NEW_RELEASE head=$RELEASE_COMMIT live=$LIVE_RELEASE ($PREVIOUS_SHORT, untouched)"
