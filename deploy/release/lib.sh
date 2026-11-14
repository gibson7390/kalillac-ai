# shellcheck shell=bash
# Kalillac AI release tooling: shared functions. Sourced by check.sh, stage.sh, activate.sh and
# rollback.sh; never executed directly.
#
# Principles (see README.md):
#   * Git-tracked source is verified by content, not by stat caches: every tracked file is re-hashed
#     against the release commit through a throwaway index (verify_release_tree).
#   * Deployment-only artifacts (the root-only production launcher) are an explicit allowlist
#     (DEPLOY_ARTIFACTS). Anything else untracked, and any ignored file other than Python bytecode,
#     is refused. The launcher is never treated as a source modification and never added to Git.
#   * The running release is identified from the service's main process (/proc/<pid>/cwd, cmdline)
#     and its Git tree, never from a health response alone.
#   * Nothing here prints an environment value except the allowlisted, non-secret FLAG_VALUES;
#     configuration is compared by hash only.

set -Eeuo pipefail
shopt -s inherit_errexit
umask 077

KD_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
readonly KD_DIR
readonly PRODUCTION_SERVICE=kalillac-ai-fastapi-staging.service
readonly CONFIG_VARS='^(KALILLAC_|OPENAI_|BRAVE_|TAVILY_|GROQ_|STRIPE_|RESEND_|CREDENTIALS_DIRECTORY=)'

log()  { printf '[%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
truthy() { case "${1,,}" in 1|true|yes|on) return 0 ;; *) return 1 ;; esac; }
expect_dir()  { [[ -d $1 && ! -L $1 ]] || die "expected a real directory: $1"; }
expect_file() { [[ -f $1 && ! -L $1 ]] || die "expected a regular file: $1"; }
sha256() { sha256sum -- "$1" | cut -d' ' -f1; }
g() { local dir=$1; shift; git -c safe.directory="$dir" -c core.hooksPath=/dev/null --no-optional-locks -C "$dir" "$@"; }

# ---------------------------------------------------------------- configuration
# KEY=VALUE files, parsed (never sourced). Unknown, duplicate or missing keys are refused, as are
# CR characters and the shell metacharacters $ ` \ in values.
load_conf() {   # load_conf <file> <key>...
    local file=$1; shift
    expect_file "$file"
    local -A allowed=() seen=()
    local key value line n=0
    for key in "$@"; do allowed[$key]=1; done
    while IFS= read -r line || [[ -n $line ]]; do
        n=$((n + 1))
        [[ $line != *$'\r'* ]] || die "$file:$n: CR character (the file must use LF line endings)"
        [[ $line =~ ^[[:space:]]*(#.*)?$ ]] && continue
        [[ $line =~ ^([A-Z][A-Z0-9_]*)=(.*)$ ]] || die "$file:$n: expected KEY=VALUE"
        key=${BASH_REMATCH[1]}; value=${BASH_REMATCH[2]}
        [[ -n ${allowed[$key]:-} ]] || die "$file:$n: unknown key $key"
        [[ -z ${seen[$key]:-} ]] || die "$file:$n: duplicate key $key"
        [[ $value != *[\$\`\\]* ]] || die "$file:$n: forbidden character in $key"
        seen[$key]=1
        printf -v "$key" '%s' "$value"
    done <"$file"
    for key in "$@"; do [[ -n ${seen[$key]:-} ]] || die "$file: missing key $key"; done
}

readonly ENV_KEYS=(ENVIRONMENT_KIND SERVICE RELEASE_PARENT RELEASE_PREFIX VENV DROPIN_DIR ROLLBACK_PARENT
    LISTEN_HOST LISTEN_PORT HEALTH_PATH HEALTH_BODY HEALTH_TIMEOUT_SECONDS HEALTH_STABLE_CHECKS
    WEBROOT LAUNCHER_NAME LAUNCHER_OWNER LAUNCHER_MODE DEPLOY_ARTIFACTS REQUIRED_UNITS PUBLIC_BASE
    APP_PREFLIGHT DB_CHECK DB_OS_USER DB_NAME DB_SCHEMA)
readonly RELEASE_KEYS=(RELEASE_COMMIT RELEASE_SHORT RELEASE_TITLE PREVIOUS_COMMIT PREVIOUS_SHORT
    APP_BLOB PREVIOUS_APP_BLOB IDENTICAL_PATHS MIGRATION_HEAD DB_REVISION EMPTY_TABLES
    FLAGS_OFF FLAGS_ON FLAG_VALUES OPENAI_MODEL_ALLOWED INACTIVE_GET INACTIVE_POST INACTIVE_PREFIXES)

load_all_conf() {
    local env_file=${KALILLAC_DEPLOY_ENV:-$KD_DIR/production.env}
    load_conf "$env_file" "${ENV_KEYS[@]}"
    load_conf "$KD_DIR/release.env" "${RELEASE_KEYS[@]}"
    ENV_FILE=$env_file
    # A test environment can never be mistaken for production and vice versa.
    if [[ -n ${KALILLAC_DEPLOY_ENV:-} ]]; then
        [[ $ENVIRONMENT_KIND == disposable-test ]] || die "KALILLAC_DEPLOY_ENV may only name a disposable-test environment"
        [[ $SERVICE =~ ^kalillac-deploytest-[a-z0-9-]+\.service$ ]] || die "a test environment must use a kalillac-deploytest-* service"
        [[ $RELEASE_PARENT != /opt && $ROLLBACK_PARENT != /opt/* && $VENV != /opt/* && $WEBROOT != /var/www/* ]] \
            || die "a test environment must not use production paths"
    else
        [[ $ENVIRONMENT_KIND == production && $SERVICE == "$PRODUCTION_SERVICE" ]] || die "production.env is not the production environment"
        [[ $APP_PREFLIGHT == import && $DB_CHECK == psql-peer && -n $PUBLIC_BASE ]] \
            || die "production requires APP_PREFLIGHT=import, DB_CHECK=psql-peer and PUBLIC_BASE"
    fi
    [[ $APP_PREFLIGHT == import || $APP_PREFLIGHT == skip ]] || die "APP_PREFLIGHT must be import or skip"
    [[ $DB_CHECK == psql-peer || $DB_CHECK == none ]] || die "DB_CHECK must be psql-peer or none"
    [[ $RELEASE_COMMIT =~ ^[0-9a-f]{40}$ && $PREVIOUS_COMMIT =~ ^[0-9a-f]{40}$ ]] || die "release commits must be full SHAs"
    [[ $RELEASE_COMMIT == "$RELEASE_SHORT"* && $PREVIOUS_COMMIT == "$PREVIOUS_SHORT"* ]] || die "short names do not match the commits"
    [[ $LISTEN_PORT =~ ^[0-9]+$ && $HEALTH_TIMEOUT_SECONDS =~ ^[0-9]+$ && $HEALTH_STABLE_CHECKS =~ ^[1-9]$ ]] || die "bad numeric setting"
    VENV_PYTHON=$VENV/bin/python3
    NEW_RELEASE=$RELEASE_PARENT/$RELEASE_PREFIX$RELEASE_SHORT
    OLD_RELEASE_EXPECTED=$RELEASE_PARENT/$RELEASE_PREFIX$PREVIOUS_SHORT
    NEW_CWD=$NEW_RELEASE/candidate
    LOCAL_BASE=http://$LISTEN_HOST:$LISTEN_PORT
    readonly ENV_FILE VENV_PYTHON NEW_RELEASE OLD_RELEASE_EXPECTED NEW_CWD LOCAL_BASE
}

banner() {   # banner <script name>
    log "$1: release $RELEASE_SHORT ($RELEASE_TITLE), previous $PREVIOUS_SHORT"
    log "environment: $ENVIRONMENT_KIND ($(basename "$ENV_FILE") sha256 $(sha256 "$ENV_FILE" | cut -c1-16)), service $SERVICE"
    [[ -f $KD_DIR/PACKAGE.txt ]] && log "package: $(tr '\n' ' ' <"$KD_DIR/PACKAGE.txt")"
    return 0
}

require_root_and_tools() {
    [[ $(id -u) -eq 0 ]] || die "must run as root"
    local tool
    for tool in git systemctl curl sha256sum cmp comm sed awk grep sort tr readlink stat ss runuser find install mktemp; do
        command -v "$tool" >/dev/null || die "missing tool: $tool"
    done
}

# ---------------------------------------------------------------- the process and the unit
main_pid()      { systemctl show -p MainPID --value "$SERVICE"; }
active_state()  { systemctl show -p ActiveState --value "$SERVICE"; }
unit_wd()       { systemctl show -p WorkingDirectory --value "$SERVICE"; }
need_reload()   { systemctl show -p NeedDaemonReload --value "$SERVICE"; }
dropin_paths()  { systemctl show -p DropInPaths --value "$SERVICE" | tr ' ' '\n' | sed '/^$/d'; }
# ExecStart argv entries, one per line, trailing blanks removed.
unit_execstart() { systemctl show -p ExecStart --value "$SERVICE" | { grep -o 'argv\[\]=[^;]*' || true; } | sed -e 's/^argv\[\]=//' -e 's/[[:space:]]*$//'; }
proc_cwd()      { [[ $1 =~ ^[1-9][0-9]*$ ]] && readlink "/proc/$1/cwd"; }
proc_cmdline()  { [[ $1 =~ ^[1-9][0-9]*$ ]] && tr '\0' ' ' <"/proc/$1/cmdline" | sed 's/ $//'; }
proc_env()      { tr '\0' '\n' <"/proc/$1/environ" | sed -n "/^$2=/{s/^$2=//p;q;}"; }
proc_has_env()  { local e; e=$(tr '\0' '\n' <"/proc/$1/environ"); [[ $'\n'$e == *$'\n'"$2="* ]]; }
config_hash()   { tr '\0' '\n' <"/proc/$1/environ" | { grep -E "$CONFIG_VARS" || true; } | LC_ALL=C sort | sha256sum | cut -d' ' -f1; }
launch_cmd()    { printf '%s %s/%s' "$VENV_PYTHON" "$1" "$LAUNCHER_NAME"; }   # launch_cmd <release dir>

# Everything about the unit except WorkingDirectory and ExecStart, hashed (Environment may hold
# secrets, so it is never printed). Exec* runtime fields (pid, times, status) are excluded.
unit_fingerprint() {
    {
        systemctl show "$SERVICE" -p FragmentPath -p User -p Group -p Type -p Restart -p EnvironmentFiles \
            -p Environment -p UMask -p KillMode -p TimeoutStopUSec -p LimitNOFILE
        local p
        for p in ExecStartPre ExecStartPost ExecStop ExecStopPost ExecReload; do
            printf '%s:' "$p"; systemctl show -p "$p" --value "$SERVICE" | grep -o 'argv\[\]=[^;]*' || true
        done
    } | sha256sum | cut -d' ' -f1
}

# Every drop-in systemd loads, in order, with its content hash.
dropin_snapshot() {
    local path
    while IFS= read -r path; do
        [[ -f $path ]] || die "drop-in listed by systemd is missing: $path"
        printf '%s  %s\n' "$(sha256 "$path")" "$path"
    done < <(dropin_paths)
}

# The release pointer drop-in: [Service] WorkingDirectory=<release>/candidate, a reset ExecStart= and
# ExecStart=<venv python> <release>/<launcher>. Comment lines are ignored.
pointer_body() { printf '[Service]\nWorkingDirectory=%s/candidate\nExecStart=\nExecStart=%s\n' "$1" "$(launch_cmd "$1")"; }
is_pointer_for() {   # is_pointer_for <file> <release dir>
    [[ -f $1 && ! -L $1 ]] && cmp -s <(grep -v '^#' "$1") <(pointer_body "$2")
}

# Assert the effective unit runs exactly <release dir>, before any restart.
verify_effective_unit() {   # verify_effective_unit <release dir>
    local release=$1 wd exec
    [[ $(need_reload) == no ]] || die "systemd reports unit files changed on disk without a reload (NeedDaemonReload=yes)"
    wd=$(unit_wd); exec=$(unit_execstart)
    [[ $wd == "$release/candidate" ]] || die "effective WorkingDirectory is '$wd', expected $release/candidate"
    [[ $(printf '%s\n' "$exec" | sed '/^$/d' | wc -l) -eq 1 ]] || die "effective ExecStart has $(printf '%s\n' "$exec" | sed '/^$/d' | wc -l) entries, expected exactly one"
    [[ $exec == "$(launch_cmd "$release")" ]] || die "effective ExecStart is '$exec', expected '$(launch_cmd "$release")'"
}

# ---------------------------------------------------------------- Git-tracked source integrity
# verify_release_tree <dir> <commit> [pristine]
#   HEAD must be <commit>; every tracked file must hash to its blob in <commit> (re-hashed through a
#   throwaway index, so neither the stat cache nor the repository's own index is trusted or written);
#   untracked files must be exactly DEPLOY_ARTIFACTS (none at all when "pristine"); ignored files
#   may only be Python bytecode under __pycache__/ (none at all when "pristine").
verify_release_tree() {
    local dir=$1 commit=$2 mode=${3:-} index status untracked ignored expected
    expect_dir "$dir"; expect_dir "$dir/.git"
    [[ "$(g "$dir" rev-parse --verify -q HEAD)" == "$commit" ]] || die "$dir HEAD is not $commit"
    index=$(mktemp)
    status=0
    GIT_INDEX_FILE=$index g "$dir" read-tree "$commit" || status=1
    GIT_INDEX_FILE=$index g "$dir" update-index -q --refresh >/dev/null || true
    GIT_INDEX_FILE=$index g "$dir" diff-files --quiet || status=2
    untracked=$(GIT_INDEX_FILE=$index g "$dir" ls-files --others --exclude-standard | LC_ALL=C sort)
    ignored=$(GIT_INDEX_FILE=$index g "$dir" ls-files --others --ignored --exclude-standard | LC_ALL=C sort)
    rm -f -- "$index"
    ((status != 1)) || die "$dir: cannot read the tree of $commit"
    ((status != 2)) || die "$dir: tracked files differ from $commit (modified, deleted or mode-changed)"
    if [[ $mode == pristine ]]; then
        expected=""
    else
        expected=$(printf '%s\n' $DEPLOY_ARTIFACTS | LC_ALL=C sort)
    fi
    [[ $untracked == "$expected" ]] || die "$dir: untracked files are [$(echo $untracked)], expected exactly [$(echo $expected)]"
    if [[ $mode == pristine ]]; then
        [[ -z $ignored ]] || die "$dir: unexpected ignored files in a fresh checkout"
    elif [[ -n $ignored ]]; then
        local bad
        bad=$(printf '%s\n' "$ignored" | grep -vE '(^|/)__pycache__/[^/]+\.pyc$' || true)
        [[ -z $bad ]] || die "$dir: ignored files other than Python bytecode: $(echo $bad | cut -c1-300)"
    fi
}

# The launcher: an explicit deployment artifact. Never tracked, root-only, compared by content.
verify_launcher() {   # verify_launcher <release dir> [expected sha256]
    local launcher=$1/$LAUNCHER_NAME
    expect_file "$launcher"
    [[ "$(stat -c '%U:%G %a' "$launcher")" == "$LAUNCHER_OWNER $LAUNCHER_MODE" ]] || die "$launcher is not $LAUNCHER_OWNER $LAUNCHER_MODE"
    ! g "$1" ls-files --error-unmatch -- "$LAUNCHER_NAME" >/dev/null 2>&1 || die "$LAUNCHER_NAME is tracked in $1 (it must be a deployment artifact)"
    [[ -z ${2:-} || "$(sha256 "$launcher")" == "$2" ]] || die "$launcher sha256 is not the approved $2"
}

# Migration head of a commit, from its revision files (revision not named as any down_revision).
migration_head() {   # migration_head <dir> <commit>
    local files revs downs
    files=$(g "$1" ls-tree --name-only "$2" candidate/migrations/versions/ | grep '\.py$')
    revs=$(for f in $files; do g "$1" show "$2:$f" | sed -nE 's/^revision = "([^"]+)".*/\1/p'; done | sort -u)
    downs=$(for f in $files; do g "$1" show "$2:$f" | sed -nE 's/^down_revision = "([^"]+)".*/\1/p'; done | sort -u)
    comm -23 <(echo "$revs") <(echo "$downs")
}

# ---------------------------------------------------------------- health and identity
health_ok() { [[ "$(curl -fsS --max-time 5 "$LOCAL_BASE$HEALTH_PATH" 2>/dev/null)" == "$HEALTH_BODY" ]]; }
is_self_or_descendant() {   # is_self_or_descendant <pid> <ancestor>
    local p=$1
    while [[ $p =~ ^[0-9]+$ && $p -gt 1 ]]; do
        [[ $p == "$2" ]] && return 0
        p=$(awk '/^PPid:/{print $2}' "/proc/$p/status" 2>/dev/null) || return 1
    done
    return 1
}
# The health port is held only by <pid> (or its children): the answer comes from that process.
listener_owned_by() {
    local pids p
    pids=$(ss -Hltnp "sport = :$LISTEN_PORT" 2>/dev/null | grep -F "$LISTEN_HOST:$LISTEN_PORT" | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u)
    [[ -n $pids ]] || return 1
    for p in $pids; do is_self_or_descendant "$p" "$1" || return 1; done
}

# wait_healthy <expected cwd> <new|same> <pid>
#   new:  wait for a main process other than <pid>; same: the main process must remain <pid>.
#   Healthy = unit active, process cwd as expected, listener owned by the process, health body exact,
#   for HEALTH_STABLE_CHECKS consecutive checks with an unchanged main PID. Fails fast if the unit
#   enters "failed"; otherwise allows HEALTH_TIMEOUT_SECONDS for application start-up.
wait_healthy() {
    local cwd=$1 mode=$2 ref=$3 deadline=$((SECONDS + HEALTH_TIMEOUT_SECONDS)) stable=0 last="" pid state
    while ((SECONDS < deadline)); do
        state=$(active_state); pid=$(main_pid)
        [[ $state == failed ]] && { log "unit entered the failed state"; return 1; }
        if [[ $state == active && $pid =~ ^[1-9][0-9]*$ ]] \
            && { [[ $mode == new && $pid != "$ref" ]] || [[ $mode == same && $pid == "$ref" ]]; } \
            && [[ "$(proc_cwd "$pid" 2>/dev/null)" == "$cwd" ]] && listener_owned_by "$pid" && health_ok; then
            if [[ $pid == "$last" ]]; then stable=$((stable + 1)); else stable=1; last=$pid; fi
            ((stable >= HEALTH_STABLE_CHECKS)) && { HEALTHY_PID=$pid; return 0; }
        else
            stable=0
            [[ $mode == same && $pid != "$ref" ]] && { log "main PID changed from $ref to $pid"; return 1; }
        fi
        sleep 2
    done
    log "not healthy within ${HEALTH_TIMEOUT_SECONDS}s (state=$state pid=$pid)"
    return 1
}

# The process actually running is <release dir> at <commit>: cwd, command line, launcher, Git tree.
verify_running() {   # verify_running <pid> <release dir> <commit> <app blob>
    local pid=$1 release=$2
    [[ "$(proc_cwd "$pid")" == "$release/candidate" ]] || die "process $pid cwd is not $release/candidate"
    [[ "$(proc_cmdline "$pid")" == "$(launch_cmd "$release")" ]] || die "process $pid command line is not the release's launcher"
    verify_release_tree "$release" "$3"
    verify_launcher "$release"
    [[ "$(g "$release" rev-parse "$3:candidate/app_fastapi_candidate.py")" == "$4" ]] || die "application blob of $3 is not $4"
}

# Feature flags: everything in FLAGS_OFF off, FLAGS_ON on, FLAG_VALUES exact, model allowed.
verify_flags() {   # verify_flags <pid>
    local pid=$1 flag pair name want model
    for flag in $FLAGS_OFF; do ! truthy "$(proc_env "$pid" "$flag")" || die "$flag is enabled (must be off)"; done
    for flag in $FLAGS_ON; do truthy "$(proc_env "$pid" "$flag")" || die "$flag is not enabled (must stay on)"; done
    for pair in $FLAG_VALUES; do
        name=${pair%%=*}; want=${pair#*=}
        [[ "$(proc_env "$pid" "$name")" == "$want" ]] || die "$name is not $want"
    done
    if proc_has_env "$pid" OPENAI_MODEL; then
        model=$(proc_env "$pid" OPENAI_MODEL)
        [[ $model == "$OPENAI_MODEL_ALLOWED" ]] || die "OPENAI_MODEL is not $OPENAI_MODEL_ALLOWED"
    fi
}

flag_report() {   # flag_report <pid>: names with on/off, and the allowlisted non-secret values
    local pid=$1 flag pair out=""
    for flag in $FLAGS_OFF $FLAGS_ON; do
        if truthy "$(proc_env "$pid" "$flag")"; then out+=" $flag=on"; else out+=" $flag=off"; fi
    done
    for pair in $FLAG_VALUES; do out+=" ${pair%%=*}=$(proc_env "$pid" "${pair%%=*}")"; done
    echo "${out# }" | tr ' ' '\n' | sed 's/^/  /'
}

webroot_hash() { (cd "$WEBROOT" && find . -type f -exec sha256sum {} + | LC_ALL=C sort -k2 | sha256sum | cut -d' ' -f1); }
verify_webroot_app() {   # /app/ is the release's frontend/, byte for byte
    local entry
    expect_dir "$WEBROOT/app"
    while IFS= read -r entry; do
        cmp -s "$1/frontend/$entry" "$WEBROOT/app/$entry" || die "live /app/$entry is not this release's frontend/$entry"
    done < <(g "$1/frontend" ls-files)
}

# ---------------------------------------------------------------- application and database checks
# The new code imported with the live service's environment, as the service user, in a separate
# short process: no port, no request, no provider call, no database connection.
app_preflight() {   # app_preflight <live pid> <release dir>
    if [[ $APP_PREFLIGHT == skip ]]; then log "application import preflight SKIPPED (disposable test environment)"; return 0; fi
    local user out
    user=$(systemctl show -p User --value "$SERVICE"); user=${user:-root}
    out=$(runuser -u "$user" -- "$VENV_PYTHON" - "$1" "$VENV_PYTHON" "$2/candidate" "$OPENAI_MODEL_ALLOWED" \
        "$MIGRATION_HEAD" "$INACTIVE_PREFIXES" <"$KD_DIR/preflight.py" 2>&1) || { log "preflight: $(tail -n 1 <<<"$out")"; die "application preflight failed"; }
    [[ $(tail -n 1 <<<"$out") == APP_PREFLIGHT_OK* ]] || die "application preflight did not confirm"
    log "$(tail -n 1 <<<"$out")"
}

# Read-only database facts through a local peer-authenticated psql session as the database OS
# user: no password is read, passed or printed. Revision exact; Financial Safeguards tables empty.
db_check() {
    if [[ $DB_CHECK == none ]]; then log "database check SKIPPED (disposable test environment)"; return 0; fi
    local rev table n
    psql_ro() { runuser -u "$DB_OS_USER" -- psql -X -A -t -q -v ON_ERROR_STOP=1 -d "$DB_NAME" \
        -c 'SET default_transaction_read_only = on' -c "$1" 2>/dev/null; }
    rev=$(psql_ro "SELECT string_agg(version_num, ',') FROM $DB_SCHEMA.alembic_version") || die "cannot read $DB_SCHEMA.alembic_version (read-only psql as $DB_OS_USER)"
    [[ $rev == "$DB_REVISION" ]] || die "database revision is '$rev', expected $DB_REVISION"
    for table in $EMPTY_TABLES; do
        n=$(psql_ro "SELECT count(*) FROM $DB_SCHEMA.$table") || die "cannot read $DB_SCHEMA.$table"
        [[ $n == 0 ]] || die "$DB_SCHEMA.$table has $n rows (expected 0 while Financial Safeguards are off)"
    done
    log "database: revision $rev; $(echo $EMPTY_TABLES | wc -w) Financial Safeguards tables empty (read-only check)"
}

# Inactive routes must not be mounted (loopback only).
verify_inactive_routes() {
    local path code
    for path in $INACTIVE_GET; do
        code=$(curl -sS --max-time 10 -o /dev/null -w '%{http_code}' "$LOCAL_BASE$path") || true
        [[ $code == 404 ]] || die "GET $path returned $code (expected 404 while inactive)"
    done
    for path in $INACTIVE_POST; do
        code=$(curl -sS --max-time 10 -X POST -H 'Content-Type: application/json' --data '{}' -o /dev/null -w '%{http_code}' "$LOCAL_BASE$path") || true
        [[ $code == 404 ]] || die "POST $path returned $code (expected 404 while inactive)"
    done
}

verify_public() {   # public health and /app/ through the edge (GET only, cache-busted)
    [[ -n $PUBLIC_BASE ]] || { log "public checks SKIPPED (disposable test environment)"; return 0; }
    local bust body key
    bust=kcv=$(date -u +%Y%m%dT%H%M%SZ)
    body=$(curl -sS --proto '=https' --max-time 20 -H 'Cache-Control: no-cache' "$PUBLIC_BASE$HEALTH_PATH?$bust") || die "public health request failed"
    [[ $body == "$HEALTH_BODY" ]] || die "public health body is not $HEALTH_BODY"
    key=$(grep -m1 -oE 'app\.js\?v=[A-Za-z0-9._-]+' "$WEBROOT/app/index.html")
    curl -sS --proto '=https' --max-time 20 -H 'Cache-Control: no-cache' "$PUBLIC_BASE/app/?$bust" | grep -qF "$key" \
        || die "public /app/ does not reference $key"
}

# ---------------------------------------------------------------- the live release
# identify_live <expected release dir> <commit> <app blob>
# Sets LIVE_PID, LIVE_RELEASE. Proves, from the running process and its Git tree (not from health
# alone), that the service runs exactly the expected release, healthy, with the required flags.
identify_live() {
    local unit
    systemctl is-active --quiet "$SERVICE" || die "$SERVICE is not active"
    for unit in $REQUIRED_UNITS; do systemctl is-active --quiet "$unit" || die "$unit is not active"; done
    LIVE_PID=$(main_pid)
    [[ $LIVE_PID =~ ^[1-9][0-9]*$ ]] || die "$SERVICE has no main process"
    LIVE_RELEASE=$(dirname "$(proc_cwd "$LIVE_PID")")
    [[ $LIVE_RELEASE == "$1" ]] || die "the running release is $LIVE_RELEASE, expected $1"
    verify_effective_unit "$LIVE_RELEASE"
    verify_running "$LIVE_PID" "$LIVE_RELEASE" "$2" "$3"
    listener_owned_by "$LIVE_PID" || die "port $LISTEN_PORT is not held by the main process $LIVE_PID"
    health_ok || die "local health is not $HEALTH_BODY"
    verify_flags "$LIVE_PID"
}

# ---------------------------------------------------------------- the release pointer and rollback
readonly POINTER_SUFFIX=-kalillac-active-release.conf
state_get() { sed -n "/^$1=/{s/^$1=//p;q;}" "$ROLLBACK_DIR/state/state.txt"; }

# Put the drop-ins back exactly as recorded before activation (from the byte copy in ROLLBACK_DIR).
# Needs OLD_POINTER, NEW_POINTER, ROLLBACK_DIR.
restore_pointer() {
    local saved=$ROLLBACK_DIR/systemd/old-pointer.conf tmp=$DROPIN_DIR/.kalillac-restore-$$.tmp
    [[ -f $saved ]] || { log "ROLLBACK: saved pointer $saved missing"; return 1; }
    if [[ $NEW_POINTER != "$OLD_POINTER" ]]; then rm -f -- "$NEW_POINTER" || return 1; fi
    rm -f -- "$DROPIN_DIR"/.kalillac-active-release-*.tmp
    cp -p -- "$saved" "$tmp" && mv -T -- "$tmp" "$OLD_POINTER" || return 1
    cmp -s -- "$saved" "$OLD_POINTER" && [[ ! -e $NEW_POINTER || $NEW_POINTER == "$OLD_POINTER" ]]
}

# rollback_to_previous <restart: yes|no>
# Restores the previous drop-ins, reloads systemd, proves the effective unit is the previous release
# again and then either restarts and proves the previous release is what runs (yes), or proves the
# original, never-restarted process is still the one serving (no). Returns 0 only when every check
# holds. Needs OLD_* state, ROLLBACK_DIR, OLD_POINTER, NEW_POINTER.
rollback_to_previous() {
    local restart=$1 ok=1 pid ref
    log "ROLLBACK: restoring drop-ins from $ROLLBACK_DIR"
    if restore_pointer; then log "ROLLBACK: pointer restored: $OLD_POINTER"; else log "ROLLBACK: pointer restore FAILED"; ok=0; fi
    systemctl daemon-reload || { log "ROLLBACK: daemon-reload FAILED"; ok=0; }
    if [[ "$(dropin_snapshot 2>/dev/null)" == "$(cat "$ROLLBACK_DIR/state/dropins.before")" ]]; then
        log "ROLLBACK: drop-ins identical to before activation"
    else log "ROLLBACK: drop-ins differ from before activation"; ok=0; fi
    if (verify_effective_unit "$OLD_RELEASE"); then log "ROLLBACK: effective WorkingDirectory and ExecStart are $OLD_RELEASE again"
    else ok=0; fi
    [[ "$(unit_fingerprint)" == "$BEFORE_UNIT_FP" ]] || { log "ROLLBACK: other unit settings differ from before"; ok=0; }
    if [[ $restart == yes ]]; then
        if ((ok)); then
            ref=$(main_pid)
            log "ROLLBACK: restarting $SERVICE"
            systemctl restart "$SERVICE" || { log "ROLLBACK: restart command failed"; ok=0; }
            if wait_healthy "$OLD_RELEASE/candidate" new "$ref"; then pid=$HEALTHY_PID; log "ROLLBACK: healthy, pid $pid"
            else ok=0; pid=$(main_pid); fi
        else
            log "ROLLBACK: NOT restarting: the unit is not verifiably back on $OLD_RELEASE"; pid=$(main_pid)
        fi
    else
        pid=$OLD_PID
        if wait_healthy "$OLD_RELEASE/candidate" same "$OLD_PID"; then log "ROLLBACK: no restart was needed; original pid $OLD_PID still serving"
        else ok=0; fi
    fi
    if ((ok)) && (verify_running "$pid" "$OLD_RELEASE" "$PREVIOUS_COMMIT" "$PREVIOUS_APP_BLOB"); then
        log "ROLLBACK: running process $pid is $OLD_RELEASE at $PREVIOUS_COMMIT (tracked files exact)"
    else ok=0; fi
    if ((ok)) && [[ "$(sha256 "$OLD_RELEASE/$LAUNCHER_NAME")" == "$OLD_LAUNCHER_SHA" ]]; then :; else ok=0; fi
    if ((ok)) && [[ "$(config_hash "$pid")" == "$OLD_CONFIG_HASH" ]] && (verify_flags "$pid"); then
        log "ROLLBACK: configuration identical to before; feature flags as required"
    else ok=0; fi
    if ((ok)) && listener_owned_by "$pid" && health_ok; then log "ROLLBACK: local health $HEALTH_BODY from pid $pid"; else ok=0; fi
    if [[ "$(webroot_hash)" == "$OLD_WEBROOT_HASH" ]]; then log "ROLLBACK: web root unchanged"; else log "ROLLBACK: web root differs"; ok=0; fi
    ((ok))
}

# ---------------------------------------------------------------- the stage marker
readonly STAGE_MARKER=kalillac-staged
# verify_staged <release dir> <launcher sha256>: produced by stage.sh for this commit and launcher,
# with the same ownership as the previous release.
verify_staged() {
    local marker=$1/.git/$STAGE_MARKER
    [[ -f $marker && ! -L $marker ]] || die "$1 was not produced by stage.sh (no stage marker)"
    grep -qxF "release_commit=$RELEASE_COMMIT" "$marker" || die "$1 was staged for another commit"
    grep -qxF "launcher_sha256=$2" "$marker" || die "$1 was staged with another launcher"
    grep -qxF "owner=$(stat -c '%U:%G' "$1/candidate")" "$marker" || die "$1 ownership changed since staging"
}
