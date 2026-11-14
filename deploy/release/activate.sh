#!/usr/bin/env bash
# Kalillac AI: ACTIVATE a staged release, with automatic and verified rollback.
#
# Usage (as root, inside tmux or screen, after stage.sh printed STAGED_OK):
#   bash activate.sh 2>&1 | tee "activate-$(date -u +%Y%m%dT%H%M%SZ).log"
#
# Production changes, and nothing else:
#   * the release pointer drop-in. The EFFECTIVE pointer is the last loaded drop-in that assigns
#     WorkingDirectory or ExecStart (not necessarily the last drop-in: production loads
#     provider-ceilings.conf after it). It must be exactly "[Service] WorkingDirectory=<previous>/
#     candidate, ExecStart=, ExecStart=<venv python> <previous>/launch-production.py". It is replaced
#     by ONE pointer to the new release at the same position in the load order: a stable name
#     (<same z prefix>-kalillac-active-release.conf) that later releases rewrite in place. A legacy
#     per-release pointer is moved into the rollback directory, so the drop-in count does not grow.
#     Every other drop-in (including provider-ceilings.conf), unit file, environment file and flag is
#     left byte-for-byte as it is and verified so.
#   * one restart of the service.
# Before the restart, systemd's drop-in list and the effective WorkingDirectory and ExecStart are read
# back. After it, the main process (the launcher has exec'd the server in place) must carry exactly
# the approved argv (PROCESS_ARGV) from the release directory, hold the port, and keep the flags and
# provider ceilings it had before.
# The web root, Nginx, the database and its schema are not touched; the previous release directory is
# kept and verified as the rollback target.
#
# Failure handling by phase:
#   preflight/backup  -> nothing in production changed: no reload, no restart, nothing to undo.
#   pointer/reloaded  -> drop-ins restored and reloaded; the original process was never restarted and
#                        is verified to be still serving.
#   restarted         -> drop-ins restored, reloaded, previous release restarted, and the running process
#                        verified (cwd, exact argv, executable, Git HEAD and every tracked file, launcher,
#                        configuration and provider-ceiling hashes, flags, listener, health).
# ROLLBACK_RECOVERED is printed only when all of that holds; otherwise ROLLBACK_INCOMPLETE.
source "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"
load_all_conf
banner activate

TS=$(date -u +%Y%m%dT%H%M%SZ)
readonly TS
ROLLBACK_DIR=$ROLLBACK_PARENT/release-$RELEASE_SHORT-$TS
readonly ROLLBACK_DIR
PHASE=preflight
ACTIVATED=0
FAILED_LINE=""
OLD_PID=""

on_exit() {
    local status=$1
    trap - EXIT ERR INT TERM HUP
    ((status == 0 && ACTIVATED)) && return 0
    ((status == 0)) && status=1
    set +e
    log "FAILED in phase $PHASE (status $status${FAILED_LINE:+, line $FAILED_LINE})"
    case $PHASE in
        preflight|backup)
            local pid; pid=$(main_pid)
            echo "FAILED_BEFORE_PRODUCTION_CHANGE: drop-ins untouched, no reload, no restart (pid ${pid:-?} still running${OLD_PID:+, was $OLD_PID})."
            [[ -d $ROLLBACK_DIR ]] && echo "(backups were written to $ROLLBACK_DIR; they change nothing)"
            ;;
        pointer|reloaded)
            if rollback_to_previous no; then echo "ROLLBACK_RECOVERED: $PREVIOUS_SHORT never stopped; drop-ins restored; no restart was needed."
            else echo "ROLLBACK_INCOMPLETE: MANUAL ACTION REQUIRED (see the messages above and $ROLLBACK_DIR)"; fi
            ;;
        *)
            if rollback_to_previous yes; then echo "ROLLBACK_RECOVERED: $PREVIOUS_SHORT restarted and verified running ($OLD_RELEASE)."
            else echo "ROLLBACK_INCOMPLETE: MANUAL ACTION REQUIRED (see the messages above and $ROLLBACK_DIR)"; fi
            ;;
    esac
    exit "$status"
}
trap 'FAILED_LINE=$LINENO' ERR
trap 'on_exit $?' EXIT
trap 'exit 130' INT; trap 'exit 143' TERM; trap 'exit 129' HUP

# ================================================================ preflight (read-only)
log "Preflight"
require_root_and_tools
identify_live "$OLD_RELEASE_EXPECTED" "$PREVIOUS_COMMIT" "$PREVIOUS_APP_BLOB"
OLD_PID=$LIVE_PID
OLD_RELEASE=$LIVE_RELEASE
OLD_CONFIG_HASH=$(config_hash "$OLD_PID")
OLD_LAUNCHER_SHA=$(sha256 "$OLD_RELEASE/$LAUNCHER_NAME")
log "running: pid $OLD_PID, $OLD_RELEASE at $PREVIOUS_COMMIT, verified by content"

verify_release_tree "$NEW_RELEASE" "$RELEASE_COMMIT"
verify_launcher "$NEW_RELEASE" "$OLD_LAUNCHER_SHA"
verify_staged "$NEW_RELEASE" "$OLD_LAUNCHER_SHA"
[[ "$(stat -c '%U:%G' "$NEW_CWD")" == "$(stat -c '%U:%G' "$OLD_RELEASE/candidate")" ]] || die "staged release ownership differs from the previous release"
[[ "$(g "$NEW_RELEASE" rev-parse "$RELEASE_COMMIT:candidate/app_fastapi_candidate.py")" == "$APP_BLOB" ]] || die "staged application blob is not $APP_BLOB"
g "$NEW_RELEASE" diff --quiet "$PREVIOUS_COMMIT" "$RELEASE_COMMIT" -- $IDENTICAL_PATHS || die "unchanged paths differ: $IDENTICAL_PATHS"
verify_webroot_app "$NEW_RELEASE"
OLD_WEBROOT_HASH=$(webroot_hash)
log "staged: $NEW_RELEASE at $RELEASE_COMMIT verified by content; launcher identical to the running one"
app_preflight "$OLD_PID" "$NEW_RELEASE"
db_check

# The effective release pointer (identify_live: the last drop-in assigning WorkingDirectory or
# ExecStart, recognised exactly). Drop-ins loaded after it, such as provider-ceilings.conf, assign
# neither key; they are kept byte-for-byte and must still load after the new pointer.
OLD_POINTER=$LIVE_POINTER
mapfile -t DROPINS < <(dropin_paths)
idx=-1
for i in "${!DROPINS[@]}"; do [[ ${DROPINS[$i]} == "$OLD_POINTER" ]] && idx=$i; done
((idx >= 0)) || die "the effective pointer is not among the loaded drop-ins"
for path in "${DROPINS[@]:idx+1}"; do
    ! dropin_sets_exec "$path" || die "$path assigns WorkingDirectory/ExecStart after the pointer"
    log "later drop-in kept byte-for-byte and still loaded after the pointer: $(basename "$path") (assigns: $(dropin_keys "$path"))"
done
# Stable pointer name at exactly the old pointer's place in the load order: same z prefix, fixed
# suffix. Later releases rewrite it in place, so no per-release override accumulates.
old_base=$(basename "$OLD_POINTER")
if [[ $old_base =~ ^z+${POINTER_SUFFIX//./\\.}$ ]]; then
    NEW_POINTER=$OLD_POINTER
else
    zs=${old_base%%[!z]*}
    ((${#zs} > 0)) || die "the current pointer name $old_base has no z prefix; no stable name can take its place"
    NEW_POINTER=$DROPIN_DIR/$zs$POINTER_SUFFIX
    [[ ! -e $NEW_POINTER ]] || die "$NEW_POINTER already exists but is not the effective pointer"
    for path in "${DROPINS[@]}"; do [[ $(basename "$path") != "$(basename "$NEW_POINTER")" ]] || die "a drop-in named $(basename "$NEW_POINTER") is already loaded"; done
fi
EXPECTED_DROPINS=("${DROPINS[@]}")
EXPECTED_DROPINS[idx]=$NEW_POINTER
# systemd applies drop-ins in file-name order. Refuse unless the current order is plain byte order
# and the new name keeps every drop-in where it was (verified again from systemd after the reload).
names_in_order() { local p; for p in "$@"; do basename "$p"; done; }
[[ "$(names_in_order "${DROPINS[@]}")" == "$(names_in_order "${DROPINS[@]}" | LC_ALL=C sort)" ]] \
    || die "the loaded drop-ins are not in byte order of their names; cannot predict where a new pointer would load"
[[ "$(names_in_order "${EXPECTED_DROPINS[@]}")" == "$(names_in_order "${EXPECTED_DROPINS[@]}" | LC_ALL=C sort)" ]] \
    || die "the stable pointer name $(basename "$NEW_POINTER") would not take the current pointer's place in the load order"
POINTER_TMP=$DROPIN_DIR/.kalillac-active-release-$TS.tmp
BEFORE_DROPINS=$(dropin_snapshot)
BEFORE_UNIT_FP=$(unit_fingerprint)
OLD_CEILING_HASH=$(ceiling_hash "$OLD_PID")
[[ ! -e $ROLLBACK_DIR && ! -e $POINTER_TMP ]] || die "a target path already exists"
log "effective pointer: $OLD_POINTER -> $OLD_RELEASE (drop-in $((idx + 1)) of ${#DROPINS[@]})"
log "new pointer:       $NEW_POINTER -> $NEW_RELEASE, same position ($([[ $NEW_POINTER == "$OLD_POINTER" ]] && echo 'rewritten in place' || echo 'replaces the legacy per-release pointer; drop-in count unchanged'))"
log "Preflight passed: $OLD_RELEASE ($PREVIOUS_SHORT) -> $NEW_RELEASE ($RELEASE_SHORT)"

# ================================================================ backups (no production change)
PHASE=backup
install -d -m 700 "$ROLLBACK_PARENT"
install -d -m 700 "$ROLLBACK_DIR" "$ROLLBACK_DIR/systemd" "$ROLLBACK_DIR/systemd/all" "$ROLLBACK_DIR/state"
i=0
for path in "${DROPINS[@]}"; do i=$((i + 1)); cp -p -- "$path" "$ROLLBACK_DIR/systemd/all/$(printf '%02d' $i)-$(basename "$path")"; done
cp -p -- "$OLD_POINTER" "$ROLLBACK_DIR/systemd/old-pointer.conf"
printf '%s\n' "$BEFORE_DROPINS" >"$ROLLBACK_DIR/state/dropins.before"
{
    echo "timestamp=$TS"
    echo "service=$SERVICE"
    echo "release_commit=$RELEASE_COMMIT"
    echo "previous_commit=$PREVIOUS_COMMIT"
    echo "old_pid=$OLD_PID"
    echo "old_release=$OLD_RELEASE"
    echo "old_launcher_sha256=$OLD_LAUNCHER_SHA"
    echo "old_config_sha256=$OLD_CONFIG_HASH"
    echo "old_ceiling_sha256=$OLD_CEILING_HASH"
    echo "old_webroot_sha256=$OLD_WEBROOT_HASH"
    echo "old_pointer=$OLD_POINTER"
    echo "new_pointer=$NEW_POINTER"
    echo "new_release=$NEW_RELEASE"
    echo "unit_fingerprint=$BEFORE_UNIT_FP"
} >"$ROLLBACK_DIR/state/state.txt"
{
    printf '# Kalillac AI active release pointer. Managed by deploy/release/activate.sh; do not edit by hand.\n'
    printf '# release=%s previous=%s activated=%s\n' "$RELEASE_COMMIT" "$PREVIOUS_COMMIT" "$TS"
    pointer_body "$NEW_RELEASE"
} >"$ROLLBACK_DIR/systemd/new-pointer.conf"
log "backups and state written to $ROLLBACK_DIR"

# ================================================================ pointer swap (first production change)
PHASE=pointer
cp -- "$ROLLBACK_DIR/systemd/new-pointer.conf" "$POINTER_TMP"
chown --reference="$OLD_POINTER" -- "$POINTER_TMP"
chmod --reference="$OLD_POINTER" -- "$POINTER_TMP"
if [[ $NEW_POINTER != "$OLD_POINTER" ]]; then
    install -d -m 700 "$ROLLBACK_DIR/systemd/displaced"
    mv -T -- "$OLD_POINTER" "$ROLLBACK_DIR/systemd/displaced/$(basename "$OLD_POINTER")"
fi
mv -T -- "$POINTER_TMP" "$NEW_POINTER"
cmp -s -- "$ROLLBACK_DIR/systemd/new-pointer.conf" "$NEW_POINTER" || die "installed pointer differs"
log "pointer installed: $NEW_POINTER"

PHASE=reloaded
systemctl daemon-reload
verify_effective_unit "$NEW_RELEASE"
[[ "$(dropin_paths)" == "$(printf '%s\n' "${EXPECTED_DROPINS[@]}")" ]] \
    || die "systemd's loaded drop-ins are not exactly the previous ones with the pointer replaced in place"
[[ "$(effective_pointer)" == "$NEW_POINTER" ]] || die "the effective pointer is not $NEW_POINTER"
[[ "$(dropin_snapshot | grep -vF "  $NEW_POINTER" || true)" == "$(printf '%s\n' "$BEFORE_DROPINS" | grep -vF "  $OLD_POINTER" || true)" ]] \
    || die "drop-ins other than the pointer changed"
[[ "$(unit_fingerprint)" == "$BEFORE_UNIT_FP" ]] || die "unit settings other than WorkingDirectory/ExecStart changed"
[[ "$(main_pid)" == "$OLD_PID" ]] || die "the main process changed during reload"
log "effective unit verified BEFORE restart: WorkingDirectory=$NEW_CWD; ExecStart=$(launch_cmd "$NEW_RELEASE") (single entry);"
log "  all other drop-ins byte-identical and in the same order; other unit settings unchanged"

# ================================================================ restart
PHASE=restarted
log "Restarting $SERVICE"
systemctl restart "$SERVICE"
wait_healthy "$NEW_CWD" new "$OLD_PID" || die "the new release did not become healthy"
NEW_PID=$HEALTHY_PID
log "healthy: pid $NEW_PID ($HEALTH_STABLE_CHECKS consecutive checks)"

# ================================================================ verification
verify_effective_unit "$NEW_RELEASE"
verify_running "$NEW_PID" "$NEW_RELEASE" "$RELEASE_COMMIT" "$APP_BLOB"
cmp -s "$OLD_RELEASE/$LAUNCHER_NAME" "$NEW_RELEASE/$LAUNCHER_NAME" || die "launchers differ"
[[ "$(config_hash "$NEW_PID")" == "$OLD_CONFIG_HASH" ]] || die "provider/feature configuration changed across the restart"
verify_flags "$NEW_PID"
verify_ceilings "$NEW_PID"
[[ "$(ceiling_hash "$NEW_PID")" == "$OLD_CEILING_HASH" ]] || die "provider ceiling settings changed across the restart"
listener_owned_by "$NEW_PID" || die "port $LISTEN_PORT is not held by $NEW_PID"
[[ "$(webroot_hash)" == "$OLD_WEBROOT_HASH" ]] || die "web root changed"
verify_inactive_routes
verify_public
db_check
verify_release_tree "$OLD_RELEASE" "$PREVIOUS_COMMIT"
[[ "$(sha256 "$OLD_RELEASE/$LAUNCHER_NAME")" == "$OLD_LAUNCHER_SHA" ]] || die "previous release launcher changed"
wait_healthy "$NEW_CWD" same "$NEW_PID" || die "the new release did not stay healthy"
log "verified: process $NEW_PID runs $NEW_RELEASE at $RELEASE_COMMIT (tracked files exact, launcher identical);"
log "  argv exactly the approved invocation; executable $(readlink -f -- "$VENV_PYTHON"); listener owned by $NEW_PID"
log "  provider ceilings enabled, settings unchanged (hash); configuration hash unchanged; flags:"
flag_report "$NEW_PID"
log "  inactive routes 404; web root unchanged; previous release intact and verified by content"

systemctl show "$SERVICE" -p WorkingDirectory -p DropInPaths -p MainPID -p ActiveState >"$ROLLBACK_DIR/state/systemd-show.after.txt"
echo "new_pid=$NEW_PID" >>"$ROLLBACK_DIR/state/state.txt"
PHASE=verified
ACTIVATED=1
echo "Running:          pid $NEW_PID, $NEW_CWD, HEAD $RELEASE_COMMIT"
echo "Rollback target:  $OLD_RELEASE ($PREVIOUS_COMMIT), kept"
echo "Manual rollback:  ROLLBACK_DIR=$ROLLBACK_DIR bash rollback.sh"
echo "KALILLAC_RELEASE_${RELEASE_SHORT^^}_ACTIVATED"
