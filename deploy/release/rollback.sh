#!/usr/bin/env bash
# Kalillac AI: roll back a SUCCESSFUL activation to the previous release.
# (A failed activation rolls itself back; this is for a problem found afterwards.)
#
# Usage (as root):  ROLLBACK_DIR=<the directory printed by activate.sh> bash rollback.sh
#
# Holds the deploy lock. Refuses before any change unless the next start would use the configuration
# approved at activation (envcheck.py). Restores the drop-ins exactly as they were before activation
# (from the byte copy in ROLLBACK_DIR),
# reloads systemd, verifies the effective WorkingDirectory and ExecStart are the previous release's
# BEFORE restarting, restarts, and then verifies the process actually running: cwd, command line,
# Git HEAD and every tracked file, launcher, configuration hash, flags, listener and health.
# Prints ROLLED_BACK_TO_<previous> only when all of that holds. Deletes nothing: both release
# directories and every rollback directory stay in place.
source "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"
load_all_conf
banner rollback

require_root_and_tools
acquire_deploy_lock
[[ -n ${ROLLBACK_DIR:-} && -f $ROLLBACK_DIR/state/state.txt ]] || die "set ROLLBACK_DIR to the activation's rollback directory"
readonly ROLLBACK_DIR
[[ "$(state_get release_commit)" == "$RELEASE_COMMIT" && "$(state_get previous_commit)" == "$PREVIOUS_COMMIT" ]] \
    || die "$ROLLBACK_DIR is not for $PREVIOUS_SHORT -> $RELEASE_SHORT"
[[ "$(state_get service)" == "$SERVICE" ]] || die "$ROLLBACK_DIR is for another service"
OLD_RELEASE=$(state_get old_release); OLD_PID=$(state_get old_pid)
OLD_LAUNCHER_SHA=$(state_get old_launcher_sha256); OLD_CONFIG_HASH=$(state_get old_config_sha256)
OLD_CEILING_HASH=$(state_get old_ceiling_sha256)
[[ $OLD_CEILING_HASH =~ ^[0-9a-f]{64}$ ]] || die "state has no provider-ceiling fingerprint (not written by this activate.sh)"
OLD_WEBROOT_HASH=$(state_get old_webroot_sha256); BEFORE_UNIT_FP=$(state_get unit_fingerprint)
OLD_POINTER=$(state_get old_pointer); NEW_POINTER=$(state_get new_pointer)
[[ $OLD_RELEASE == "$OLD_RELEASE_EXPECTED" && "$(state_get new_release)" == "$NEW_RELEASE" ]] || die "state names unexpected release directories"
[[ $OLD_POINTER == "$DROPIN_DIR"/* && $NEW_POINTER == "$DROPIN_DIR"/* ]] || die "state names pointers outside $DROPIN_DIR"

log "Preflight (read-only)"
[[ $(need_reload) == no ]] || die "NeedDaemonReload=yes: unit files changed on disk since the last reload; investigate first"
is_pointer_for "$NEW_POINTER" "$NEW_RELEASE" || die "$NEW_POINTER is not the pointer to $NEW_RELEASE; this rollback directory does not describe the current state"
[[ "$(effective_pointer)" == "$NEW_POINTER" ]] || die "$NEW_POINTER is not the effective release pointer (the last drop-in assigning WorkingDirectory/ExecStart)"
is_pointer_for "$ROLLBACK_DIR/systemd/old-pointer.conf" "$OLD_RELEASE" || die "the saved pointer is not the pointer to $OLD_RELEASE"
verify_release_tree "$OLD_RELEASE" "$PREVIOUS_COMMIT"
verify_launcher "$OLD_RELEASE" "$OLD_LAUNCHER_SHA"
[[ "$(g "$OLD_RELEASE" rev-parse "$PREVIOUS_COMMIT:candidate/app_fastapi_candidate.py")" == "$PREVIOUS_APP_BLOB" ]] || die "previous application blob is not $PREVIOUS_APP_BLOB"
[[ "$(webroot_hash)" == "$OLD_WEBROOT_HASH" ]] || die "web root differs from the one recorded at activation"
# The previous release is restarted only under the configuration approved at activation.
expect_file "$ROLLBACK_DIR/state/env.approved"
env_check - "$ROLLBACK_DIR/state/env.approved"
pid=$(main_pid)
if [[ "$(proc_cwd "$pid" 2>/dev/null || true)" == "$NEW_CWD" ]]; then log "currently running: pid $pid from $NEW_RELEASE"
else log "note: the service is not currently running from $NEW_RELEASE (pid ${pid:-none})"; fi
log "previous release $OLD_RELEASE verified by content; launcher sha256 as recorded"

if rollback_to_previous yes; then
    log "both release directories and $ROLLBACK_DIR are kept"
    echo "ROLLED_BACK_TO_$PREVIOUS_SHORT"
else
    echo "ROLLBACK_INCOMPLETE: MANUAL ACTION REQUIRED (see the messages above and $ROLLBACK_DIR)"
    exit 1
fi
