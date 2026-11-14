#!/usr/bin/env bash
# Kalillac AI: READ-ONLY release check. Changes nothing (Git checks use a throwaway index under
# /tmp; the database is read in a read-only session). Prints no environment value, credential or
# database URL: only flag names with on/off and the allowlisted non-secret FLAG_VALUES.
#
# Usage (as root):  bash check.sh
# Ends with exactly one of: READY_TO_STAGE, READY_TO_ACTIVATE, ACTIVE_<short>, NOT_READY.
source "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"
set +e +o pipefail
trap - ERR
load_all_conf
banner check
require_root_and_tools
problems=()
note() { problems+=("$1"); echo "  !! $1"; }
try() { (set -e; "$@") 2>&1 | sed 's/^/  /'; return "${PIPESTATUS[0]}"; }

echo "== unit $SERVICE"
pid=$(main_pid)
echo "  state=$(active_state) main_pid=$pid need_daemon_reload=$(need_reload)"
echo "  WorkingDirectory=$(unit_wd)"
unit_execstart | sed 's/^/  ExecStart: /'
echo "  drop-ins (load order; key names only, never values):"
effective=$(effective_pointer)
while IFS= read -r path; do
    target=$(pointer_release "$path"); kind=${target:+pointer -> $target}
    if [[ $path == "$effective" ]]; then kind="EFFECTIVE release ${kind:-override, NOT a recognised pointer}"
    elif [[ -n $kind ]]; then kind="$kind (overridden)"
    else kind="assigns: $(dropin_keys "$path")"; fi
    echo "    $(sha256 "$path" | cut -c1-12)  $path  [$kind]"
done < <(dropin_paths)
[[ -n $effective ]] && is_pointer_for "$effective" "$(dirname "$(unit_wd)")" \
    || note "the effective WorkingDirectory/ExecStart drop-in is not a recognised release pointer"
loaded=$(dropin_paths)
for path in "$DROPIN_DIR"/* "$DROPIN_DIR"/.[!.]*; do
    [[ -e $path ]] || continue
    grep -qxF -- "$path" <<<"$loaded" || note "file in $DROPIN_DIR that systemd does not load: $(basename "$path")"
done
for unit in $REQUIRED_UNITS; do echo "  $unit: $(systemctl is-active "$unit")"; done

echo "== running process"
cwd=$(proc_cwd "$pid" 2>/dev/null); running=$(dirname "${cwd:-/none/x}")
echo "  cwd=$cwd"
echo "  argv: $(proc_cmdline "$pid" 2>/dev/null)"
proc_argv_ok "$pid" && echo "  argv = the approved invocation (the launcher exec'd it)" || note "argv is not exactly: $PROCESS_ARGV"
proc_exe_ok "$pid" && echo "  executable = $(readlink -f -- "$VENV_PYTHON")" || note "executable is not $(readlink -f -- "$VENV_PYTHON")"
# Informational: variables naming the running release directory itself (values never printed). If
# the launcher sets them from a fixed path rather than from its working directory, a new release
# would inherit the old path; activate.sh then refuses after the restart and rolls back.
self_refs=$(tr '\0' '\n' <"/proc/$pid/environ" 2>/dev/null | { grep -F -- "$running" || true; } | cut -d= -f1 | sort -u | tr '\n' ' ')
echo "  environment variables naming the running release directory: ${self_refs:-none}"
try verify_ceilings "$pid" && echo "  provider ceilings: enabled, openai/brave/tavily limits explicit" || note "provider ceilings not as required"
listener_owned_by "$pid" && echo "  port $LISTEN_PORT held by pid $pid" || note "port $LISTEN_PORT is not held by pid $pid"
health_ok && echo "  local health $HEALTH_BODY" || note "local health is not $HEALTH_BODY"
echo "  flags:"; flag_report "$pid" 2>/dev/null | sed 's/^/  /'
try verify_flags "$pid" && echo "  flag policy: OK" || note "flag policy not satisfied"
try env_check "$pid" && echo "  next start: same configuration as the running process, policy OK" || note "the next start would not use the running (approved) configuration"

echo "== running release $running"
case $running in
    "$OLD_RELEASE_EXPECTED") expect_commit=$PREVIOUS_COMMIT; expect_blob=$PREVIOUS_APP_BLOB ;;
    "$NEW_RELEASE")          expect_commit=$RELEASE_COMMIT;  expect_blob=$APP_BLOB ;;
    *)                       expect_commit=""; note "running release is neither $PREVIOUS_SHORT nor $RELEASE_SHORT" ;;
esac
echo "  HEAD=$(g "$running" rev-parse HEAD 2>/dev/null)"
if [[ -n $expect_commit ]]; then
    try verify_effective_unit "$running" && echo "  effective unit = this release" || note "effective unit does not match the running release"
    try verify_running "$pid" "$running" "$expect_commit" "$expect_blob" \
        && echo "  tracked files exact; untracked exactly [$DEPLOY_ARTIFACTS]; ignored: only bytecode" || note "running release verification failed"
fi
if [[ -f $running/$LAUNCHER_NAME ]]; then
    echo "  launcher: $(stat -c '%U:%G %a' "$running/$LAUNCHER_NAME") sha256 $(sha256 "$running/$LAUNCHER_NAME")"
else
    note "launcher missing"
fi

echo "== database (read-only)"
try db_check || note "database check failed"

echo "== staged release $NEW_RELEASE"
staged=absent
if [[ -e $NEW_RELEASE ]]; then
    echo "  HEAD=$(g "$NEW_RELEASE" rev-parse HEAD 2>/dev/null)"
    [[ "$(systemctl cat "$SERVICE" 2>/dev/null)" == *"$NEW_RELEASE"* ]] && echo "  referenced by the unit: yes" || echo "  referenced by the unit: no"
    if try verify_release_tree "$NEW_RELEASE" "$RELEASE_COMMIT" && try verify_launcher "$NEW_RELEASE" \
        && try verify_staged "$NEW_RELEASE" "$(sha256 "$running/$LAUNCHER_NAME" 2>/dev/null)"; then
        staged=verified; echo "  verified: tracked files exact, untracked exactly [$DEPLOY_ARTIFACTS]"
        cmp -s "$NEW_RELEASE/$LAUNCHER_NAME" "$running/$LAUNCHER_NAME" && echo "  launcher identical to the running one" || { staged=unverified; note "staged launcher differs from the running one"; }
    else
        staged=unverified; echo "  NOT a verified staged release (stage.sh refuses it; QUARANTINE_EXISTING=1 moves it aside)"
    fi
fi

echo "== release directories"
for r in "$RELEASE_PARENT/$RELEASE_PREFIX"*; do
    [[ -d $r ]] || continue
    echo "  $r  HEAD=$(g "$r" rev-parse --short=12 HEAD 2>/dev/null || echo '?')$([[ $r == "$running" ]] && echo '  <== RUNNING')"
done
for w in "$RELEASE_PARENT"/.kalillac-stage-*; do
    [[ -e $w ]] && echo "  leftover private staging directory (from an interrupted stage; inspect and remove by hand): $w"
done
echo "== rollback directories (latest 5)"
ls -1dt "$ROLLBACK_PARENT"/release-* 2>/dev/null | head -n 5 | sed 's/^/  /'
ls -1dt "$ROLLBACK_PARENT"/quarantine/* 2>/dev/null | head -n 5 | sed 's/^/  quarantine: /'

echo
if ((${#problems[@]})); then
    echo "NOT_READY (${#problems[@]} problem(s) above)"; exit 1
elif [[ $running == "$NEW_RELEASE" ]]; then
    echo "ACTIVE_$RELEASE_SHORT"
elif [[ $staged == verified ]]; then
    echo "READY_TO_ACTIVATE"
elif [[ $staged == absent ]]; then
    echo "READY_TO_STAGE"
else
    echo "NOT_READY (an unverified $NEW_RELEASE exists)"; exit 1
fi
