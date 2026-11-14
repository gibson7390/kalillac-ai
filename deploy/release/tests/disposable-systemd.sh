#!/usr/bin/env bash
# Disposable-environment verification of deploy/release against REAL systemd, reproducing the
# production shape observed on the live server:
#   * the launcher replaces itself (os.execv) with the server: the unit's ExecStart names
#     <venv>/bin/python3 <release>/launch-production.py, while the main process (same PID) carries
#     "<venv>/bin/python3 <venv>/bin/uvicorn app_fastapi_candidate:api --host 127.0.0.1 --port N --workers 1";
#   * drop-ins named exactly like production's (z-runs 64, 72, 80): legacy pointers for 3ef0956a and
#     f126a245, the current f347e50f pointer, and provider-ceilings.conf (z-run 96) loaded AFTER the
#     pointer and carrying the provider attempt ceilings;
#   * configuration from Environment= drop-ins AND an EnvironmentFile, plus one variable the launcher
#     adds before exec (approved through LAUNCHER_ENV_VARS);
#   * the server imports a real release module, so releases hold interpreter-written bytecode.
#
#   sudo bash deploy/release/tests/disposable-systemd.sh <package dir> [<evidence dir>]
#
# Requires Linux, root, a running systemd (busctl), git, python3, curl, ss and flock; optionally a
# local peer-authenticated PostgreSQL (section 13). Run it only on a disposable host (CI runner,
# throwaway VM or WSL instance). Everything it creates is uniquely named and removed at the end: a
# transient unit /run/systemd/system/kalillac-deploytest-<id>.service (+ .d) and
# /tmp/kalillac-deploytest-<id>. Section 5c sets one variable in the systemd MANAGER environment
# for a few seconds and always unsets it (also on exit).
#
# The packaged scripts run unmodified; only KALILLAC_DEPLOY_ENV points them at a disposable-test
# environment (its own service, paths, port, PROCESS_ARGV and LAUNCHER_ENV_VARS; the application
# import is replaced by exec_launcher.py + stub_uvicorn.py). Releases are real Git clones of
# f347e50f and 54e38ecf. Concurrency tests pause a script at one of its own log lines with
# SIGSTOP, act, and resume it with SIGCONT; each pause point precedes a multi-second step.
set -uo pipefail
[[ $(id -u) -eq 0 ]] || { echo "run as root on a disposable host"; exit 2; }
[[ -d ${1:-} ]] || { echo "usage: disposable-systemd.sh <package dir> [<evidence dir>]"; exit 2; }
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
ID=$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')
SERVICE=kalillac-deploytest-$ID.service
T=/tmp/kalillac-deploytest-$ID
UNIT=/run/systemd/system/$SERVICE
DROPIN=/run/systemd/system/$SERVICE.d
PORT=$((20000 + RANDOM % 20000))
PKG=$T/pkg
LOGS=$T/logs
OLD=$T/opt/kalillac-release-f347e50f
NEW=$T/opt/kalillac-release-54e38ecf
OLD_COMMIT=f347e50fcec13db9e85399612ed5c2ca3b26c706
NEW_COMMIT=54e38ecf36a4a25db20946019dd298311a632c1d
APP_BLOB=9f921b4428e8a66bd2dac38bf6bbb8b5698ec38b
SENTINEL=KDT-SENTINEL-SECRET-$ID
FAULTS=$T/faults
ENVFILE=$T/service.env
EVIDENCE=${2:-}
VENV=$T/venv
ARGV="$VENV/bin/python3 $VENV/bin/uvicorn app_fastapi_candidate:api --host 127.0.0.1 --port $PORT --workers 1"
z() { printf 'z%.0s' $(seq 1 "$1"); }
P3EF=$DROPIN/$(z 64)-release-3ef0956a.conf
PF12=$DROPIN/$(z 72)-release-f126a245.conf
PF34=$DROPIN/$(z 80)-release-f347e50f.conf
PCEIL=$DROPIN/$(z 96)-provider-ceilings.conf
STABLE=$DROPIN/$(z 80)-kalillac-active-release.conf
CEILING_RE='^KALILLAC_(PROVIDER_CEILINGS_ENABLED|(OPENAI|BRAVE|TAVILY)_(TOTAL_ATTEMPTS|ANONYMOUS_ATTEMPTS|PERIOD_SECONDS))='

PASS=0; FAIL=0
ok()  { echo "  PASS  $*"; PASS=$((PASS + 1)); }
bad() { echo "  FAIL  $*"; FAIL=$((FAIL + 1)); }
check() { local desc=$1; shift; if "$@"; then ok "$desc"; else bad "$desc"; fi; }
section() { echo; echo "== $*"; }

pid_now() { systemctl show -p MainPID --value "$SERVICE"; }
cwd_now() { readlink "/proc/$(pid_now)/cwd" 2>/dev/null; }
argv_now() { tr '\0' ' ' <"/proc/$(pid_now)/cmdline" 2>/dev/null | sed 's/ $//'; }
env_of() { tr '\0' '\n' <"/proc/$1/environ" | sed -n "s/^$2=//p"; }
ceilings_of() { tr '\0' '\n' <"/proc/$1/environ" | grep -E "$CEILING_RE" | sort; }
loaded() { systemctl show -p DropInPaths --value "$SERVICE" | tr ' ' '\n' | sed '/^$/d'; }
snap() { local f; for f in $(loaded); do sha256sum "$f"; done; }
dropin_count() { loaded | wc -l; }
healthy() { [[ "$(curl -fsS --max-time 3 "http://127.0.0.1:$PORT/api/health" 2>/dev/null)" == '{"status":"ok"}' ]]; }
wait_up() { local i; for i in $(seq 1 40); do healthy && return 0; sleep 1; done; return 1; }
restart_service() { systemctl daemon-reload; systemctl restart "$SERVICE"; wait_up; }
# run <log name> <script> [VAR=value...]: run a packaged script; status in RC, output in $LOGS/<name>.log
run() {
    local name=$1 script=$2; shift 2
    (cd "$PKG" && env KALILLAC_DEPLOY_ENV="${KDT_ENV:-$T/test.env}" "$@" bash "$script") >"$LOGS/$name.log" 2>&1
    RC=$?
    echo "  [$name] exit $RC: $(tail -n 1 "$LOGS/$name.log")"
}
# run_bg <log name> <script> [VAR=value...]: same, in the background; its bash PID in BG
run_bg() {
    local name=$1 script=$2; shift 2
    (cd "$PKG" && exec env KALILLAC_DEPLOY_ENV="${KDT_ENV:-$T/test.env}" "$@" bash "$script") >"$LOGS/$name.log" 2>&1 &
    BG=$!
}
wait_log() { local i; for i in $(seq 1 1200); do grep -qF -- "$2" "$LOGS/$1.log" 2>/dev/null && return 0; sleep 0.05; done; return 1; }
freeze() { kill -STOP "$1"; pkill -STOP -P "$1" 2>/dev/null; return 0; }
thaw() { pkill -CONT -P "$1" 2>/dev/null; kill -CONT "$1"; return 0; }
finish() { wait "$1"; RC=$?; echo "  [$2] exit $RC: $(tail -n 1 "$LOGS/$2.log")"; }
logged() { grep -qF -- "$2" "$LOGS/$1.log"; }
not_logged() { ! grep -qF -- "$2" "$LOGS/$1.log"; }
untouched() { [[ "$(pid_now)" == "$1" && "$(snap)" == "$2" ]]; }   # untouched <pid> <snapshot>
refused_before_change() {   # refused_before_change <log> <pid> <snapshot>
    logged "$1" FAILED_BEFORE_PRODUCTION_CHANGE && untouched "$2" "$3"
}
pointer() {   # the exact format earlier releases' activate scripts installed
    printf '%s\n' '[Service]' "WorkingDirectory=$1/candidate" 'ExecStart=' "ExecStart=$VENV/bin/python3 $1/launch-production.py"
}
setenvfile() { sed -i "s/^$1=.*/$1=$2/" "$ENVFILE"; grep -q "^$1=" "$ENVFILE" || echo "$1=$2" >>"$ENVFILE"; }
restore_envfile() { cp -p "$T/service.env.orig" "$ENVFILE"; }
# A metadata-valid but altered pyc for <release>/candidate/kalillac_server.py (header = the source's
# mtime and size), compiled by the service's interpreter.
tamper_pyc() {
    (cd "$1/candidate" && "$VENV/bin/python3" -I - <<'PY'
import importlib._bootstrap_external as b, importlib.util, os
src = os.path.abspath("kalillac_server.py"); st = os.stat(src)
code = compile(open(src).read() + "\nKDT_TAMPERED = True\n", src, "exec", dont_inherit=True)
pyc = importlib.util.cache_from_source(src); os.makedirs(os.path.dirname(pyc), exist_ok=True)
open(pyc, "wb").write(b._code_to_timestamp_pyc(code, int(st.st_mtime), st.st_size))
PY
)
}
pyc_of() { (cd "$1/candidate" && "$VENV/bin/python3" -I -c 'import importlib.util,os; print(importlib.util.cache_from_source(os.path.abspath("kalillac_server.py")))'); }
# A replacement commit for <commit> in <repo>: the same commit with candidate/kalillac_server.py
# changed (a secondary module; the application blob stays the same). Prints its ID.
substitute_commit() {
    local repo=$1 commit=$2 idx blob tree
    idx=$(mktemp)
    printf '\n# substituted by the test\n' >>"$repo/candidate/kalillac_server.py"
    GIT_INDEX_FILE=$idx git -C "$repo" read-tree "$commit"
    blob=$(git -C "$repo" hash-object -w candidate/kalillac_server.py)
    GIT_INDEX_FILE=$idx git -C "$repo" update-index --cacheinfo "100644,$blob,candidate/kalillac_server.py"
    tree=$(GIT_INDEX_FILE=$idx git -C "$repo" write-tree); rm -f "$idx"
    git -C "$repo" cat-file commit "$commit" | sed "s/^tree .*/tree $tree/" | git -C "$repo" hash-object -t commit -w --stdin
}

cleanup() {
    systemctl unset-environment KALILLAC_USAGE_METERING_ENABLED >/dev/null 2>&1
    systemctl stop "$SERVICE" >/dev/null 2>&1
    rm -rf -- "$UNIT" "$DROPIN"
    systemctl daemon-reload
    systemctl reset-failed "$SERVICE" >/dev/null 2>&1
    [[ -n ${DB:-} ]] && runuser -u postgres -- dropdb --if-exists "$DB" >/dev/null 2>&1
    if [[ -n $EVIDENCE ]]; then mkdir -p "$EVIDENCE" && cp -r "$LOGS" "$EVIDENCE/logs-$ID" 2>/dev/null; fi
    rm -rf -- "$T"
}
trap cleanup EXIT

# ------------------------------------------------------------------------------------------ setup
section "setup: disposable environment $ID (service $SERVICE, port $PORT)"
mkdir -p "$T"/{opt,venv/bin,www/app,logs}
ln -s "$(command -v python3)" "$VENV/bin/python3"
install -m 755 "$HERE/stub_uvicorn.py" "$VENV/bin/uvicorn"
cp -r -- "$1" "$PKG"
(cd "$PKG" && sha256sum --check --strict --quiet SHA256SUMS) && ok "package checksums verify" || bad "package checksums"
cat >"$T/test.env" <<EOF
ENVIRONMENT_KIND=disposable-test
SERVICE=$SERVICE
RELEASE_PARENT=$T/opt
RELEASE_PREFIX=kalillac-release-
VENV=$VENV
DROPIN_DIR=$DROPIN
ROLLBACK_PARENT=$T/rollbacks
LISTEN_HOST=127.0.0.1
LISTEN_PORT=$PORT
HEALTH_PATH=/api/health
HEALTH_BODY={"status":"ok"}
HEALTH_TIMEOUT_SECONDS=25
HEALTH_STABLE_CHECKS=3
WEBROOT=$T/www
LAUNCHER_NAME=launch-production.py
LAUNCHER_OWNER=root:root
LAUNCHER_MODE=600
DEPLOY_ARTIFACTS=launch-production.py
PROCESS_ARGV=$ARGV
LAUNCHER_ENV_VARS=KDT_LAUNCHER_MARK
REQUIRED_UNITS=
PUBLIC_BASE=
APP_PREFLIGHT=skip
DB_CHECK=none
DB_OS_USER=postgres
DB_NAME=none
DB_SCHEMA=none
EOF
git -c init.defaultBranch=x clone -q --no-checkout "$PKG/kalillac-source-54e38ecf.bundle" "$OLD" 2>/dev/null
git -C "$OLD" checkout -q --detach "$OLD_COMMIT"
install -o root -g root -m 600 "$HERE/exec_launcher.py" "$OLD/launch-production.py"
LAUNCHER_SHA=$(sha256sum "$OLD/launch-production.py" | cut -d' ' -f1)
(cd "$OLD/frontend" && git ls-files | while IFS= read -r f; do mkdir -p "$T/www/app/$(dirname "$f")"; cp "$f" "$T/www/app/$f"; done)
echo '<html>site</html>' >"$T/www/index.html"
: >"$FAULTS"
cat >"$ENVFILE" <<EOF
# disposable test EnvironmentFile
KALILLAC_ANONYMOUS_DAILY_LIMIT=10
KDT_QUOTED="two words"
RESEND_API_KEY='$SENTINEL'
KDT_TAMPER_MARK=$T/tamper-executed
EOF
chmod 600 "$ENVFILE"; cp -p "$ENVFILE" "$T/service.env.orig"
mkdir -p "$DROPIN"
cat >"$UNIT" <<EOF
[Unit]
Description=Kalillac deploy tooling disposable test $ID
[Service]
Type=simple
WorkingDirectory=$T/opt
ExecStart=/bin/false
Restart=no
EOF
cat >"$DROPIN/override.conf" <<EOF
[Service]
Environment=KALILLAC_DB_ENABLED=true KALILLAC_REQUEST_BUDGET_ENABLED=true
Environment=KALILLAC_ANONYMOUS_DAILY_LIMIT_ENABLED=true
Environment=KALILLAC_SEARCH_PRIMARY=brave KALILLAC_SEARCH_FALLBACK=tavily
Environment=KALILLAC_FINANCIAL_SAFEGUARDS_ENABLED=false KALILLAC_ACCOUNTS_ENABLED=false KALILLAC_BILLING_ENABLED=false
Environment=OPENAI_API_KEY=$SENTINEL STRIPE_SECRET_KEY=$SENTINEL KALILLAC_DB_PASSWORD=$SENTINEL
Environment=KDT_VENV=$VENV KDT_PORT=$PORT KDT_FAULT_FILE=$FAULTS KDT_STARTUP_SECONDS=3
EnvironmentFile=$ENVFILE
EOF
pointer "$T/opt/kalillac-release-3ef0956a" >"$P3EF"
pointer "$T/opt/kalillac-release-f126a245" >"$PF12"
pointer "$OLD" >"$PF34"
cat >"$PCEIL" <<EOF
[Service]
Environment=KALILLAC_PROVIDER_CEILINGS_ENABLED=true
Environment=KALILLAC_OPENAI_TOTAL_ATTEMPTS=500 KALILLAC_OPENAI_ANONYMOUS_ATTEMPTS=200 KALILLAC_OPENAI_PERIOD_SECONDS=86400
Environment=KALILLAC_BRAVE_TOTAL_ATTEMPTS=300 KALILLAC_BRAVE_ANONYMOUS_ATTEMPTS=100 KALILLAC_BRAVE_PERIOD_SECONDS=86400
Environment=KALILLAC_TAVILY_TOTAL_ATTEMPTS=200 KALILLAC_TAVILY_ANONYMOUS_ATTEMPTS=50 KALILLAC_TAVILY_PERIOD_SECONDS=86400
Environment=BRAVE_API_KEY=$SENTINEL
EOF
chmod 644 "$DROPIN"/*.conf
CEIL_SHA=$(sha256sum <"$PCEIL")
systemctl daemon-reload
systemctl start "$SERVICE"
check "f347e50f stand-in running and healthy" wait_up
SNAP0=$(snap); COUNT0=$(dropin_count); P=$(pid_now); CEILINGS0=$(ceilings_of "$P")

section "0. production characteristics are reproduced"
check "ExecStart names the launcher" bash -c "systemctl show -p ExecStart --value '$SERVICE' | grep -qF 'argv[]=$VENV/bin/python3 $OLD/launch-production.py '"
check "the main process exec'd into the approved uvicorn argv (same PID as systemd started)" test "$(argv_now)" == "$ARGV"
check "provider-ceilings.conf loads AFTER the f347e50f pointer (and last)" test "$(loaded | tail -n 2 | tr '\n' ' ')" == "$PF34 $PCEIL "
check "the provider ceilings come from provider-ceilings.conf and are on" test "$(echo "$CEILINGS0" | wc -l)" -eq 10
check "the launcher added KDT_LAUNCHER_MARK before exec" test "$(env_of "$P" KDT_LAUNCHER_MARK)" == 1
check "EnvironmentFile values reached the process" test "$(env_of "$P" KDT_QUOTED)" == "two words"
check "the running release holds interpreter-written bytecode" test -f "$(pyc_of "$OLD")"

# ------------------------------------------------------------------------------------------ 1
section "1. initial release detection and read-only check"
run check-initial check.sh
check "check.sh: READY_TO_STAGE (with legitimate bytecode in the running release)" test "$(tail -n 1 "$LOGS/check-initial.log")" == READY_TO_STAGE
check "the f347e50f pointer is identified as the EFFECTIVE pointer (not the last drop-in)" \
    logged check-initial "$(basename "$PF34")  [EFFECTIVE release pointer -> $OLD]"
check "provider-ceilings.conf shown with key names only" logged check-initial "$(basename "$PCEIL")  [assigns: Environment]"
check "argv recognised as the approved invocation" logged check-initial "argv = the approved invocation"
check "next start = running process, launcher-supplied KDT_LAUNCHER_MARK recognised" logged check-initial "launcher-supplied: KDT_LAUNCHER_MARK"
check "launcher sha256 printed for pinning" logged check-initial "$LAUNCHER_SHA"
check "check.sh changed nothing" untouched "$P" "$SNAP0"

section "1b. migration-head discovery on real checkouts"
mh() { bash -c 'source "$1/lib.sh"; migration_head "$2" "$3"' _ "$PKG" "$@" 2>&1; }
check "f347e50f head = 0014_v1_session_issuance" test "$(mh "$OLD" "$OLD_COMMIT")" == 0014_v1_session_issuance

# ------------------------------------------------------------------------------------------ 2
section "2. the original failure: a release dir with the launcher copied in"
git -c init.defaultBranch=x clone -q --no-checkout "$PKG/kalillac-source-54e38ecf.bundle" "$NEW" 2>/dev/null
git -C "$NEW" checkout -q --detach "$NEW_COMMIT"
cp -p "$OLD/launch-production.py" "$NEW/launch-production.py"
check "54e38ecf head = 0015_finance_ledger (real checkout)" test "$(mh "$NEW" "$NEW_COMMIT")" == 0015_finance_ledger
check "old gate fails: 'git status --porcelain' lists the launcher" test "$(git -C "$NEW" status --porcelain)" == "?? launch-production.py"
run stage-exists stage.sh LAUNCHER_SHA256="$LAUNCHER_SHA"
check "stage.sh refuses the existing dir and names QUARANTINE_EXISTING=1" logged stage-exists "QUARANTINE_EXISTING=1"
check "  dir and service untouched (nothing deleted)" test -f "$NEW/launch-production.py" -a "$(pid_now)" == "$P" -a "$(snap)" == "$SNAP0"
run activate-leftover activate.sh
check "activate.sh refuses the leftover dir (no stage marker), before any change" logged activate-leftover "was not produced by stage.sh"
check "  FAILED_BEFORE_PRODUCTION_CHANGE, service untouched" refused_before_change activate-leftover "$P" "$SNAP0"

# ------------------------------------------------------------------------------------------ 3/4
section "3. stage: launcher pin, then quarantine the leftover and stage fresh"
run stage-badpin stage.sh LAUNCHER_SHA256=0000000000000000000000000000000000000000000000000000000000000000 QUARANTINE_EXISTING=1
check "a launcher hash other than the pinned one is refused" logged stage-badpin "is not the approved"
run stage stage.sh LAUNCHER_SHA256="$LAUNCHER_SHA" QUARANTINE_EXISTING=1
check "STAGED_OK" logged stage STAGED_OK
check "leftover moved to quarantine (kept)" test -n "$(ls -d "$T"/rollbacks/quarantine/kalillac-release-54e38ecf-* 2>/dev/null)"
check "the deploy lock was held" logged stage "deploy lock held"
check "staged release: untracked files exactly the launcher, no bytecode" test "$(git -C "$NEW" status --porcelain --untracked-files=all --ignored)" == "?? launch-production.py"
check "service untouched by staging" untouched "$P" "$SNAP0"
run check-staged check.sh
check "check.sh: READY_TO_ACTIVATE" test "$(tail -n 1 "$LOGS/check-staged.log")" == READY_TO_ACTIVATE

# ------------------------------------------------------------------------------------------ 5
section "5. activate refuses unsupported states before any change (earlier findings)"
echo "# stray" >"$NEW/candidate/stray.py"
run act-untracked activate.sh
check "extra untracked file in the release" logged act-untracked "untracked files are"
rm -f "$NEW/candidate/stray.py"
cp -p "$NEW/candidate/alembic.ini" "$T/alembic.ini.bak"
printf 'X' | dd of="$NEW/candidate/alembic.ini" bs=1 seek=10 conv=notrunc status=none
touch -r "$T/alembic.ini.bak" "$NEW/candidate/alembic.ini"
run act-tracked activate.sh
check "modified tracked file (same size and mtime; content re-hashed)" logged act-tracked "tracked files differ"
cp -p "$T/alembic.ini.bak" "$NEW/candidate/alembic.ini"
cp -p "$NEW/launch-production.py" "$T/launcher.bak"; echo "# changed" >>"$NEW/launch-production.py"
run act-launcher activate.sh
check "staged launcher changed after staging (hash differs)" logged act-launcher "is not the approved"
cp -p "$T/launcher.bak" "$NEW/launch-production.py"
cp -p "$T/www/app/app.js" "$T/app.js.bak"; printf 'X' | dd of="$T/www/app/app.js" bs=1 seek=5 conv=notrunc status=none
run act-webroot activate.sh
check "live /app/ differs from the release's frontend/" logged act-webroot "is not this release's frontend/app.js"
cp -p "$T/app.js.bak" "$T/www/app/app.js"
M=$DROPIN/$(z 100)-manual.conf
{ pointer "$OLD"; echo "Environment=KDT_MANUAL=1"; } >"$M"; systemctl daemon-reload
run act-override-copy activate.sh
check "unknown override after provider-ceilings re-asserting ExecStart (not an exact pointer)" logged act-override-copy "is not exactly the release pointer"
printf '[Service]\nExecStart=\nExecStart=/bin/sleep 1000\n' >"$M"; systemctl daemon-reload
run act-override-exec activate.sh
check "unknown override replacing ExecStart" logged act-override-exec "effective ExecStart is '/bin/sleep 1000'"
rm -f "$M"; systemctl daemon-reload
echo "# pending" >>"$DROPIN/override.conf"
run act-reload activate.sh
check "unit files changed without daemon-reload (NeedDaemonReload=yes)" logged act-reload "NeedDaemonReload"
sed -i '/^# pending$/d' "$DROPIN/override.conf"; systemctl daemon-reload
for log in act-untracked act-tracked act-launcher act-webroot act-override-copy act-override-exec act-reload; do
    check "  $log: FAILED_BEFORE_PRODUCTION_CHANGE, same pid, drop-ins unchanged" refused_before_change "$log" "$P" "$SNAP0"
done

section "5b. the live process must already be the approved state (earlier findings)"
echo "badargs kalillac-release-f347e50f" >"$FAULTS"; restart_service; P2=$(pid_now); SNAPB=$(snap)
run act-badargs-live activate.sh
check "live process with non-matching uvicorn arguments: refused before any change" \
    test "$(logged act-badargs-live "argv is not exactly the approved invocation" && echo y)" == y -a "$(refused_before_change act-badargs-live "$P2" "$SNAPB" && echo y)" == y
: >"$FAULTS"; restart_service; P=$(pid_now)
check "back to the initial drop-ins and ceilings" test "$(snap)" == "$SNAP0" -a "$(ceilings_of "$P")" == "$CEILINGS0"

# ------------------------------------------------------------------------------------------ 5c
section "5c. FINDING 1: the next start's environment, not only the running one"
drift_refused() {   # drift_refused <label> <expected text>: check.sh NOT_READY + activate refused, service untouched
    local label=$1 text=$2 p s
    p=$(pid_now); s=$(snap)
    run "check-$label" check.sh
    run "act-$label" activate.sh
    check "$label: check.sh NOT_READY naming it" bash -c "grep -qF -- '$text' '$LOGS/check-$label.log' && grep -q NOT_READY '$LOGS/check-$label.log'"
    check "$label: activate refused naming it" logged "act-$label" "$text"
    check "$label:   before any change (no reload, no restart, drop-ins unchanged)" refused_before_change "act-$label" "$p" "$s"
    check "$label:   the running process still has the approved value of everything" test "$(ceilings_of "$p")" == "$CEILINGS0" -a "$(env_of "$p" KALILLAC_BILLING_ENABLED)" == false
}
printf '[Service]\nEnvironment=KALILLAC_BILLING_ENABLED=true\n' >"$DROPIN/p-pending.conf"; systemctl daemon-reload
check "daemon-reloaded change: NeedDaemonReload=no, so the earlier gate would not see it" test "$(systemctl show -p NeedDaemonReload --value "$SERVICE")" == no
drift_refused reloaded-billing "KALILLAC_BILLING_ENABLED would be ON"
rm -f "$DROPIN/p-pending.conf"; systemctl daemon-reload
setenvfile KALILLAC_ANONYMOUS_DAILY_LIMIT 50
drift_refused envfile-limit "KALILLAC_ANONYMOUS_DAILY_LIMIT would not be 10"
restore_envfile
setenvfile KALILLAC_OPENAI_TOTAL_ATTEMPTS 999
drift_refused envfile-ceiling-value "pending change) for: KALILLAC_OPENAI_TOTAL_ATTEMPTS"
restore_envfile
setenvfile KALILLAC_PROVIDER_CEILINGS_ENABLED false
drift_refused envfile-ceilings-off "provider ceilings would be disabled"
restore_envfile
setenvfile KALILLAC_SPACES_ENABLED true
drift_refused envfile-spaces "KALILLAC_SPACES_ENABLED would be ON"
restore_envfile
systemctl set-environment KALILLAC_USAGE_METERING_ENABLED=true
drift_refused manager-usage "KALILLAC_USAGE_METERING_ENABLED would be ON"
systemctl unset-environment KALILLAC_USAGE_METERING_ENABLED
sed 's/^LAUNCHER_ENV_VARS=.*/LAUNCHER_ENV_VARS=/' "$T/test.env" >"$T/test-nolauncher.env"
KDT_ENV=$T/test-nolauncher.env run act-launcher-var activate.sh
check "a variable the launcher adds without approval is refused (named)" logged act-launcher-var "added by the launcher without approval): KDT_LAUNCHER_MARK"
check "  before any change" refused_before_change act-launcher-var "$P" "$SNAP0"
sed 's/^LAUNCHER_ENV_VARS=.*/LAUNCHER_ENV_VARS=KALILLAC_PROVIDER_CEILINGS_ENABLED/' "$T/test.env" >"$T/test-badlauncher.env"
KDT_ENV=$T/test-badlauncher.env run check-badlauncher check.sh
check "a ceiling name can never be declared launcher-supplied" logged check-badlauncher "LAUNCHER_ENV_VARS may not contain KALILLAC_PROVIDER_CEILINGS_ENABLED"
printf '[Service]\nPassEnvironment=LANG\n' >"$DROPIN/p-pass.conf"; systemctl daemon-reload
run act-passenv activate.sh
check "an unsupported composition mechanism (PassEnvironment=) fails closed" logged act-passenv "unsupported: the unit sets PassEnvironment="
rm -f "$DROPIN/p-pass.conf"; systemctl daemon-reload
check "  every refusal left the service untouched" untouched "$P" "$SNAP0"
check "no value was printed (sentinel absent from all logs so far)" bash -c "! grep -rqF -- '$SENTINEL' '$LOGS'"

# ------------------------------------------------------------------------------------------ 5d
section "5d. FINDING 2: bytecode must be its source, compiled"
tamper_pyc "$NEW"
check "proof: a metadata-valid altered pyc EXECUTES under python -B" \
    test "$(cd "$NEW/candidate" && "$VENV/bin/python3" -B -c 'import kalillac_server as m; print(getattr(m, "KDT_TAMPERED", False))')" == True
run act-pyc-new activate.sh
check "altered bytecode in the staged release: refused" logged act-pyc-new "code differs from its source"
check "  before any change" refused_before_change act-pyc-new "$P" "$SNAP0"
rm -rf "$NEW/candidate/__pycache__"
cp -p "$(pyc_of "$OLD")" "$T/old.pyc"; tamper_pyc "$OLD"
run check-pyc-old check.sh
run act-pyc-old activate.sh
check "altered bytecode in the RUNNING release: check.sh NOT_READY, activate refused" \
    bash -c "grep -q NOT_READY '$LOGS/check-pyc-old.log' && grep -qF 'code differs from its source' '$LOGS/act-pyc-old.log'"
check "  before any change" refused_before_change act-pyc-old "$P" "$SNAP0"
cp -p "$T/old.pyc" "$(pyc_of "$OLD")"
mkdir -p "$NEW/candidate/__pycache__"; cp -p "$T/old.pyc" "$NEW/candidate/__pycache__/kalillac_server.cpython-299.pyc"
run act-pyc-tag activate.sh
check "bytecode for another interpreter: refused" logged act-pyc-tag "not bytecode of this interpreter"
rm -rf "$NEW/candidate/__pycache__"
chmod g+w "$NEW/candidate/alembic.ini"
run act-perms activate.sh
check "a group-writable file in the release (others could add bytecode): refused" logged act-perms "writable by group/others: candidate/alembic.ini"
chmod g-w "$NEW/candidate/alembic.ini"
check "  every refusal left the service untouched" untouched "$P" "$SNAP0"

# ------------------------------------------------------------------------------------------ 5e
section "5e. FINDING 3: Git replacement objects cannot substitute the approved source"
EVIL=$(substitute_commit "$NEW" "$NEW_COMMIT")
git -C "$NEW" replace "$NEW_COMMIT" "$EVIL"
check "proof: with replace refs, plain git reports the substituted tree as clean" git -C "$NEW" diff --quiet "$NEW_COMMIT"
check "proof: ...and the main application blob is unchanged" test "$(git -C "$NEW" rev-parse "$NEW_COMMIT:candidate/app_fastapi_candidate.py")" == "$APP_BLOB"
run act-replace activate.sh
check "replacement ref in the staged release: refused" logged act-replace "Git replacement refs present"
git -C "$NEW" replace -d "$NEW_COMMIT"
git -C "$NEW" update-ref "refs/kdt-alt/$NEW_COMMIT" "$EVIL"
check "proof: GIT_REPLACE_REF_BASE makes plain git honour a custom namespace" env GIT_REPLACE_REF_BASE=refs/kdt-alt/ git -C "$NEW" diff --quiet "$NEW_COMMIT"
run act-replace-env activate.sh GIT_REPLACE_REF_BASE=refs/kdt-alt/
check "the same substitution via GIT_REPLACE_REF_BASE in the caller's environment: refused (content)" logged act-replace-env "tracked files differ"
git -C "$NEW" update-ref -d "refs/kdt-alt/$NEW_COMMIT"
printf '#!/bin/sh\ntouch %s\n' "$T/fsmonitor-ran" >"$T/fsmonitor.sh"; chmod 755 "$T/fsmonitor.sh"
git -C "$NEW" config core.fsmonitor "$T/fsmonitor.sh"
run act-fsmonitor activate.sh
check "repository configuration that runs a program (core.fsmonitor): refused" logged act-fsmonitor "unexpected repository configuration: core.fsmonitor"
check "  ...and the program never ran" test ! -e "$T/fsmonitor-ran"
git -C "$NEW" config --unset core.fsmonitor
echo "$NEW_COMMIT" >"$NEW/.git/info/grafts"
run act-grafts activate.sh
check "graft file: refused" logged act-grafts ".git/info/grafts exists"
rm -f "$NEW/.git/info/grafts"
git -C "$OLD" replace "$OLD_COMMIT" "$(git -C "$OLD" rev-parse "$OLD_COMMIT^")"
run check-replace-old check.sh
check "replacement ref in the RUNNING release: check.sh NOT_READY" bash -c "grep -qF 'Git replacement refs present' '$LOGS/check-replace-old.log' && grep -q NOT_READY '$LOGS/check-replace-old.log'"
git -C "$OLD" replace -d "$OLD_COMMIT"
for log in act-replace act-replace-env act-fsmonitor act-grafts; do
    check "  $log: FAILED_BEFORE_PRODUCTION_CHANGE, service untouched" refused_before_change "$log" "$P" "$SNAP0"
done
run stage-fresh stage.sh LAUNCHER_SHA256="$LAUNCHER_SHA" QUARANTINE_EXISTING=1
check "the tampered staged release is quarantined and a fresh one staged" logged stage-fresh STAGED_OK

# ------------------------------------------------------------------------------------------ 6
section "6. interrupted after the pointer swap, before restart (Ctrl-C / SSH drop)"
run_bg act-interrupt activate.sh; APID=$BG
wait_log act-interrupt "pointer installed"
freeze "$APID"; kill -TERM "$APID"; thaw "$APID"; finish "$APID" act-interrupt
check "interrupted in phase pointer/reloaded" grep -qE "FAILED in phase (pointer|reloaded)" "$LOGS/act-interrupt.log"
check "ROLLBACK_RECOVERED, no restart needed" logged act-interrupt "no restart was needed"
check "  original process never restarted; drop-ins byte-identical" untouched "$P" "$SNAP0"

section "6b. FINDING 1: configuration changed between verification and restart"
run_bg act-drift-window activate.sh; APID=$BG
wait_log act-drift-window "backups and state written"
freeze "$APID"; setenvfile KALILLAC_PROVIDER_CEILINGS_ENABLED false; thaw "$APID"; finish "$APID" act-drift-window
check "the pre-restart check caught it (phase reloaded), so nothing restarted" bash -c "grep -qF 'FAILED in phase reloaded' '$LOGS/act-drift-window.log' && grep -qF 'provider ceilings would be disabled' '$LOGS/act-drift-window.log'"
check "  ROLLBACK_RECOVERED without restart, with a pending-change WARNING" bash -c "grep -qF 'no restart was needed' '$LOGS/act-drift-window.log' && grep -qF 'WARNING' '$LOGS/act-drift-window.log'"
check "  original process untouched, ceilings still on in it, drop-ins identical" test "$(pid_now)" == "$P" -a "$(snap)" == "$SNAP0" -a "$(ceilings_of "$P")" == "$CEILINGS0"
restore_envfile

# ------------------------------------------------------------------------------------------ 7
section "7. failures after the restart -> automatic, verified rollback"
echo "unhealthy kalillac-release-54e38ecf" >"$FAULTS"
run act-unhealthy activate.sh
check "unhealthy new release: ROLLBACK_RECOVERED after restart" logged act-unhealthy "ROLLBACK_RECOVERED: f347e50f restarted and verified running"
check "  release and next-start configuration verified immediately before the restart" logged act-unhealthy "release and next-start configuration verified immediately before restart"
check "  rollback restarted only after the approved-configuration check" logged act-unhealthy "= approved snapshot"
check "  f347e50f runs again with the approved argv, new pid, drop-ins identical" test "$(cwd_now)" == "$OLD/candidate" -a "$(argv_now)" == "$ARGV" -a "$(pid_now)" != "$P" -a "$(snap)" == "$SNAP0"
P=$(pid_now)
echo "crash kalillac-release-54e38ecf" >"$FAULTS"
run act-crash activate.sh
check "crashing new release: fails fast, ROLLBACK_RECOVERED" bash -c "grep -qF 'unit entered the failed state' '$LOGS/act-crash.log' && grep -qF 'ROLLBACK_RECOVERED: f347e50f restarted' '$LOGS/act-crash.log'"
P=$(pid_now)
echo "badargs kalillac-release-54e38ecf" >"$FAULTS"
run act-badargs-new activate.sh
check "new release exec'd with non-matching uvicorn arguments: refused after restart, ROLLBACK_RECOVERED" \
    bash -c "grep -qF 'argv is not exactly the approved invocation' '$LOGS/act-badargs-new.log' && grep -qF 'ROLLBACK_RECOVERED: f347e50f restarted' '$LOGS/act-badargs-new.log'"
P=$(pid_now)

section "7a. FINDING 2: unverified bytecode appearing between the last check and start-up"
echo "plantpyc kalillac-release-54e38ecf" >"$FAULTS"
run act-plantpyc activate.sh
check "the planted pyc was executed by the new process (the residual window, documented)" test -f "$T/tamper-executed"
check "  post-start bytecode verification caught it" logged act-plantpyc "code differs from its source"
check "  ROLLBACK_RECOVERED: f347e50f restarted and verified" logged act-plantpyc "ROLLBACK_RECOVERED: f347e50f restarted"
check "  f347e50f running, drop-ins identical" test "$(cwd_now)" == "$OLD/candidate" -a "$(snap)" == "$SNAP0"
: >"$FAULTS"; rm -rf "$NEW/candidate/__pycache__" "$T/tamper-executed"; P=$(pid_now)

section "7b. rollback target itself broken -> ROLLBACK_INCOMPLETE, never RECOVERED"
printf 'unhealthy kalillac-release-54e38ecf\nunhealthy kalillac-release-f347e50f\n' >"$FAULTS"
run act-badrollback activate.sh
check "ROLLBACK_INCOMPLETE reported, RECOVERED not" bash -c "grep -q ROLLBACK_INCOMPLETE '$LOGS/act-badrollback.log' && ! grep -q ROLLBACK_RECOVERED '$LOGS/act-badrollback.log'"
check "  drop-ins nevertheless restored" test "$(snap)" == "$SNAP0"
: >"$FAULTS"; rm -rf "$NEW/candidate/__pycache__"; restart_service; P=$(pid_now)

section "7c. FINDING 1: rollback under configuration drift must not restart the previous release"
echo "unhealthy kalillac-release-54e38ecf" >"$FAULTS"
run_bg act-rollback-drift activate.sh; APID=$BG
for _ in $(seq 1 600); do [[ "$(pid_now)" != "$P" && "$(cwd_now)" == "$NEW/candidate" ]] && break; sleep 0.1; done
setenvfile KALILLAC_BILLING_ENABLED true
finish "$APID" act-rollback-drift
check "rollback refused to restart f347e50f under the drifted configuration" logged act-rollback-drift "NOT restarting $OLD: its next start would not use the approved configuration"
check "  ROLLBACK_INCOMPLETE (not RECOVERED)" bash -c "grep -q ROLLBACK_INCOMPLETE '$LOGS/act-rollback-drift.log' && ! grep -q ROLLBACK_RECOVERED '$LOGS/act-rollback-drift.log'"
check "  no process was started with billing on" test "$(env_of "$(pid_now)" KALILLAC_BILLING_ENABLED)" == false
check "  drop-ins were restored (the next start would be f347e50f once the configuration is fixed)" test "$(snap)" == "$SNAP0"
restore_envfile; : >"$FAULTS"; rm -rf "$NEW/candidate/__pycache__"; restart_service; P=$(pid_now)
check "  after the operator restores the configuration: f347e50f healthy" test "$(cwd_now)" == "$OLD/candidate"

# ------------------------------------------------------------------------------------------ 8
section "8. successful activation (slow start-up allowed for)"
echo "slow kalillac-release-54e38ecf" >"$FAULTS"
sed -i 's/^HEALTH_TIMEOUT_SECONDS=.*/HEALTH_TIMEOUT_SECONDS=60/' "$T/test.env"
run activate activate.sh
check "KALILLAC_RELEASE_54E38ECF_ACTIVATED" logged activate KALILLAC_RELEASE_54E38ECF_ACTIVATED
NP=$(pid_now)
check "identity: cwd 54e38ecf, approved argv, venv interpreter, listener, new pid" \
    test "$(cwd_now)" == "$NEW/candidate" -a "$(argv_now)" == "$ARGV" -a "$NP" != "$P" -a "$(readlink "/proc/$NP/exe")" == "$(readlink -f "$VENV/bin/python3")"
check "the new process's interpreter-written bytecode was verified" test -f "$(pyc_of "$NEW")"
check "stable pointer at the old pointer's position; provider-ceilings.conf still last, byte-identical" \
    test "$(loaded | tail -n 2 | tr '\n' ' ')" == "$STABLE $PCEIL " -a "$(sha256sum <"$PCEIL")" == "$CEIL_SHA"
check "provider ceiling settings in the new process identical to the old process" test "$(ceilings_of "$NP")" == "$CEILINGS0"
check "drop-in count unchanged; legacy history kept" test "$(dropin_count)" -eq "$COUNT0" -a -f "$P3EF" -a -f "$PF12"
check "approved next-start snapshot recorded (names and hashes only)" bash -c "test -s \"\$(ls -d '$T'/rollbacks/release-54e38ecf-*/state/env.approved | tail -n 1)\""
run check-active check.sh
check "check.sh: ACTIVE_54e38ecf" test "$(tail -n 1 "$LOGS/check-active.log")" == ACTIVE_54e38ecf
: >"$FAULTS"
ROLLBACK_DIR=$(sed -n 's/^Manual rollback: *ROLLBACK_DIR=\([^ ]*\) .*/\1/p' "$LOGS/activate.log")

# ------------------------------------------------------------------------------------------ 9
section "9. manual rollback, first under configuration drift (FINDING 1), then normally"
SNAPA=$(snap)
setenvfile KALILLAC_PROVIDER_CEILINGS_ENABLED false
run rollback-drift rollback.sh ROLLBACK_DIR="$ROLLBACK_DIR"
check "manual rollback refused before any change: next start not the approved configuration" \
    test "$(logged rollback-drift "provider ceilings would be disabled" && echo y)" == y -a "$(pid_now)" == "$NP" -a "$(snap)" == "$SNAPA"
restore_envfile
run rollback rollback.sh ROLLBACK_DIR="$ROLLBACK_DIR"
check "ROLLED_BACK_TO_f347e50f" logged rollback ROLLED_BACK_TO_f347e50f
check "  f347e50f, approved argv, new pid; drop-ins and ceilings as originally" \
    test "$(cwd_now)" == "$OLD/candidate" -a "$(argv_now)" == "$ARGV" -a "$(pid_now)" != "$NP" -a "$(snap)" == "$SNAP0" -a "$(ceilings_of "$(pid_now)")" == "$CEILINGS0"
run rollback-again rollback.sh ROLLBACK_DIR="$ROLLBACK_DIR"
check "a second rollback with the same directory is refused" test "$RC" -ne 0
P=$(pid_now)

# ------------------------------------------------------------------------------------------ 10
section "10. a later activation rewrites the stable pointer in place"
mv "$PF34" "$T/legacy.conf"
{ echo "# Kalillac AI active release pointer."; pointer "$OLD"; } >"$STABLE"; chmod 644 "$STABLE"
systemctl daemon-reload; SNAP1=$(snap); COUNT1=$(dropin_count)
run activate-inplace activate.sh
check "activated, pointer rewritten in place; count unchanged; provider-ceilings.conf last" \
    test "$(logged activate-inplace "rewritten in place" && echo y)" == y -a "$(dropin_count)" -eq "$COUNT1" -a "$(loaded | tail -n 1)" == "$PCEIL"
RB2=$(sed -n 's/^Manual rollback: *ROLLBACK_DIR=\([^ ]*\) .*/\1/p' "$LOGS/activate-inplace.log")
run rollback-inplace rollback.sh ROLLBACK_DIR="$RB2"
check "  rolled back; drop-ins byte-identical" test "$(snap)" == "$SNAP1" -a "$(cwd_now)" == "$OLD/candidate"
rm -f "$STABLE"; mv "$T/legacy.conf" "$PF34"; systemctl daemon-reload; P=$(pid_now)
check "  original drop-ins restored by the test" test "$(snap)" == "$SNAP0"

# ------------------------------------------------------------------------------------------ 12
section "12. FINDING 4: concurrent staging, activation and rollback"
qcount() { ls -d "$T"/rollbacks/quarantine/kalillac-release-54e38ecf-* 2>/dev/null | wc -l; }
Q0=$(qcount)
run_bg stage-race-a stage.sh LAUNCHER_SHA256="$LAUNCHER_SHA" QUARANTINE_EXISTING=1; A=$BG
run_bg stage-race-b stage.sh LAUNCHER_SHA256="$LAUNCHER_SHA" QUARANTINE_EXISTING=1; B=$BG
finish "$A" stage-race-a; finish "$B" stage-race-b
OKS=$(grep -l STAGED_OK "$LOGS/stage-race-a.log" "$LOGS/stage-race-b.log" | wc -l)
LOCKED=$(grep -l "another stage, activate or rollback is running" "$LOGS/stage-race-a.log" "$LOGS/stage-race-b.log" | wc -l)
check "two simultaneous stages: exactly one STAGED_OK, the other refused by the lock" test "$OKS" -eq 1 -a "$LOCKED" -eq 1
check "  the refused one deleted nothing" bash -c "! grep -qE '^removed ' '$LOGS/stage-race-a.log' '$LOGS/stage-race-b.log'"
check "  exactly one quarantine move" test "$(qcount)" -eq $((Q0 + 1))
run check-race check.sh
check "  the winning release is intact (READY_TO_ACTIVATE), service untouched" test "$(tail -n 1 "$LOGS/check-race.log")" == READY_TO_ACTIVATE -a "$(pid_now)" == "$P"
NEW_ID=$(stat -c '%d:%i' "$NEW")

run_bg stage-hold stage.sh LAUNCHER_SHA256="$LAUNCHER_SHA" QUARANTINE_EXISTING=1; A=$BG
wait_log stage-hold "Live release"; freeze "$A"
run act-contend activate.sh
check "stage holding the lock: activate refused before any change" \
    test "$(logged act-contend "another stage, activate or rollback is running" && echo y)" == y -a "$(refused_before_change act-contend "$P" "$SNAP0" && echo y)" == y
run rollback-contend rollback.sh ROLLBACK_DIR="$ROLLBACK_DIR"
check "stage holding the lock: rollback refused" logged rollback-contend "another stage, activate or rollback is running"
run stage-contend stage.sh LAUNCHER_SHA256="$LAUNCHER_SHA" QUARANTINE_EXISTING=1
check "stage holding the lock: a second stage refused, deleting nothing" \
    test "$(logged stage-contend "another stage, activate or rollback is running" && echo y)" == y -a "$(not_logged stage-contend "removed $NEW" && echo y)" == y
check "  the existing staged release was not touched while the first stage waited" test "$(stat -c '%d:%i' "$NEW")" == "$NEW_ID"
thaw "$A"; finish "$A" stage-hold
check "  the first stage then completes: STAGED_OK" logged stage-hold STAGED_OK

run_bg act-hold activate.sh; A=$BG
wait_log act-hold "deploy lock held"; freeze "$A"
NEW_ID=$(stat -c '%d:%i' "$NEW")
run stage-vs-activate stage.sh LAUNCHER_SHA256="$LAUNCHER_SHA" QUARANTINE_EXISTING=1
check "activate holding the lock: stage refused; the staged release untouched" \
    test "$(logged stage-vs-activate "another stage, activate or rollback is running" && echo y)" == y -a "$(stat -c '%d:%i' "$NEW")" == "$NEW_ID"
thaw "$A"; finish "$A" act-hold
check "  the activation then completes" logged act-hold KALILLAC_RELEASE_54E38ECF_ACTIVATED
RB3=$(sed -n 's/^Manual rollback: *ROLLBACK_DIR=\([^ ]*\) .*/\1/p' "$LOGS/act-hold.log")
run rollback-after-hold rollback.sh ROLLBACK_DIR="$RB3"
check "  and is rolled back (lock released after success)" logged rollback-after-hold ROLLED_BACK_TO_f347e50f
P=$(pid_now)

mv "$NEW" "$T/held-release"
run_bg stage-other-actor stage.sh LAUNCHER_SHA256="$LAUNCHER_SHA"; A=$BG
wait_log stage-other-actor "Live release"; freeze "$A"
mkdir "$NEW"; echo other >"$NEW/OTHER-ACTOR-MARKER"
thaw "$A"; finish "$A" stage-other-actor
check "another process created the destination while staging ran: stage refused" logged stage-other-actor "already exists"
check "  and deleted nothing of it" test -f "$NEW/OTHER-ACTOR-MARKER" -a "$(not_logged stage-other-actor "removed $NEW" && echo y)" == y
rm -rf "$NEW"

run_bg stage-swapped stage.sh LAUNCHER_SHA256="$LAUNCHER_SHA"; A=$BG
wait_log stage-swapped "Clone $NEW_COMMIT"; freeze "$A"
mv "$NEW" "$T/swapped-away"; mkdir "$NEW"; echo other >"$NEW/OTHER-ACTOR-MARKER"
thaw "$A"; finish "$A" stage-swapped
check "its directory replaced by another's mid-stage: stage fails and KEEPS the other directory" \
    test "$RC" -ne 0 -a -f "$NEW/OTHER-ACTOR-MARKER" -a "$(logged stage-swapped "KEPT $NEW" && echo y)" == y
rm -rf "$NEW" "$T/swapped-away"

run_bg stage-killed stage.sh LAUNCHER_SHA256="$LAUNCHER_SHA"; A=$BG
wait_log stage-killed "Clone $NEW_COMMIT"; pkill -KILL -P "$A"; kill -KILL "$A"; wait "$A" 2>/dev/null
check "SIGKILLed stage left its partial directory (no cleanup can run)" test -d "$NEW"
lock_free() { local i; for i in $(seq 1 30); do flock -n "$T/rollbacks/.deploy.lock" true && return 0; sleep 0.5; done; return 1; }
check "  the kernel released the lock once the killed process and its children were gone" lock_free
run stage-after-kill stage.sh LAUNCHER_SHA256="$LAUNCHER_SHA"
check "  the lock was released by the kernel; the next stage refuses the partial dir without deleting it" \
    test "$(logged stage-after-kill "already exists" && echo y)" == y -a -d "$NEW"
run stage-recover stage.sh LAUNCHER_SHA256="$LAUNCHER_SHA" QUARANTINE_EXISTING=1
check "  QUARANTINE_EXISTING=1 moves it aside and stages fresh" logged stage-recover STAGED_OK
check "  the killed run's private work directory was left alone by later runs (not theirs)"     bash -c "ls -d '$T'/opt/.kalillac-stage-54e38ecf.* >/dev/null 2>&1"
run check-leftover-work check.sh
check "  check.sh reports it (information only)" logged check-leftover-work "leftover private staging directory"
rm -rf "$T/held-release"
check "service untouched through all concurrency tests" test "$(pid_now)" == "$P" -a "$(snap)" == "$SNAP0" -a "$(cwd_now)" == "$OLD/candidate"

# ------------------------------------------------------------------------------------------ 13
section "13. read-only database check (local peer-authenticated PostgreSQL; synthetic schema)"
if command -v psql >/dev/null && runuser -u postgres -- psql -XAtqc 'select 1' >/dev/null 2>&1; then
    DB=kdt_$ID
    pg() { runuser -u postgres -- psql -X -q -v ON_ERROR_STOP=1 -d "$DB" -c "$1" >/dev/null 2>&1; }
    pgq() { runuser -u postgres -- psql -XAtq -d "$DB" -c "$1"; }
    state() { pgq "SELECT version_num || ':' || (SELECT count(*) FROM kalillac.finance_ledger_buckets) || ':' || (SELECT count(*) FROM kalillac.finance_reservations) || ':' || (SELECT count(*) FROM kalillac.finance_unsafe_latches) FROM kalillac.alembic_version"; }
    runuser -u postgres -- createdb "$DB" 2>/dev/null
    pg "CREATE SCHEMA kalillac; CREATE TABLE kalillac.alembic_version (version_num varchar(32) PRIMARY KEY);
        INSERT INTO kalillac.alembic_version VALUES ('0015_finance_ledger');
        CREATE TABLE kalillac.finance_ledger_buckets (x int); CREATE TABLE kalillac.finance_reservations (x int);
        CREATE TABLE kalillac.finance_unsafe_latches (x int);"
    sed -e 's/^DB_CHECK=.*/DB_CHECK=psql-peer/' -e "s/^DB_NAME=.*/DB_NAME=$DB/" -e 's/^DB_SCHEMA=.*/DB_SCHEMA=kalillac/' \
        "$T/test.env" >"$T/test-db.env"
    KDT_ENV=$T/test-db.env run check-db check.sh
    check "revision 0015 and empty ledger: check passes" logged check-db "database: revision 0015_finance_ledger; 3 Financial Safeguards tables empty"
    pg "UPDATE kalillac.alembic_version SET version_num = '0014_v1_session_issuance'"
    P=$(pid_now); SNAPD=$(snap)
    KDT_ENV=$T/test-db.env run act-db-revision activate.sh
    check "database at another revision: refused before any change" \
        test "$(logged act-db-revision "database revision is '0014_v1_session_issuance', expected 0015_finance_ledger" && echo y)" == y -a "$(refused_before_change act-db-revision "$P" "$SNAPD" && echo y)" == y
    pg "UPDATE kalillac.alembic_version SET version_num = '0015_finance_ledger'; INSERT INTO kalillac.finance_unsafe_latches VALUES (1)"
    KDT_ENV=$T/test-db.env run act-db-latch activate.sh
    check "non-empty Financial Safeguards table: refused before any change" \
        test "$(logged act-db-latch "finance_unsafe_latches has 1 rows" && echo y)" == y -a "$(refused_before_change act-db-latch "$P" "$SNAPD" && echo y)" == y
    pg "DELETE FROM kalillac.finance_unsafe_latches"
    KDT_ENV=$T/test-db.env run act-db activate.sh
    check "activation with the database checks: ACTIVATED" logged act-db KALILLAC_RELEASE_54E38ECF_ACTIVATED
    check "  database checked before and after the restart" test "$(grep -c 'database: revision 0015_finance_ledger; 3 Financial Safeguards tables empty' "$LOGS/act-db.log")" -ge 2
    check "  revision unchanged and ledger still empty after activation" test "$(state)" == "0015_finance_ledger:0:0:0"
    RB4=$(sed -n 's/^Manual rollback: *ROLLBACK_DIR=\([^ ]*\) .*/\1/p' "$LOGS/act-db.log")
    KDT_ENV=$T/test-db.env run rollback-db rollback.sh ROLLBACK_DIR="$RB4"
    check "  rolled back; revision and ledger unchanged" test "$(tail -n 1 "$LOGS/rollback-db.log")" == ROLLED_BACK_TO_f347e50f -a "$(state)" == "0015_finance_ledger:0:0:0"
else
    echo "  SKIP  no local peer-authenticated PostgreSQL on this host (CI runs this section)"
fi

# ------------------------------------------------------------------------------------------ 11
section "11. no credential in any output or state file"
if grep -rqF -- "$SENTINEL" "$LOGS" "$T"/rollbacks/*/state 2>/dev/null; then bad "sentinel secret found in output/state"
else ok "sentinel secret (Environment=, provider-ceilings.conf and the EnvironmentFile) absent from every output and state file"; fi

echo
echo "RESULT: $PASS passed, $FAIL failed (environment $ID)"
((FAIL == 0))
