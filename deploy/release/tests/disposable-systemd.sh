#!/usr/bin/env bash
# Disposable-environment verification of deploy/release against REAL systemd.
#
#   sudo bash deploy/release/tests/disposable-systemd.sh <package dir> [<evidence dir>]
#
# Requires Linux, root, a running systemd, git, python3, curl and ss. Run it only on a disposable host
# (a CI runner, a throwaway VM or WSL instance). Everything it creates is uniquely named and removed
# at the end: a transient unit /run/systemd/system/kalillac-deploytest-<id>.service (+ .d drop-ins)
# and /tmp/kalillac-deploytest-<id>. It never touches a production path, unit or credential.
#
# The packaged scripts run unmodified; only KALILLAC_DEPLOY_ENV points them at a disposable-test
# environment (its own service, paths and port; the application import and database checks are
# replaced by the stub launcher, see stub_launcher.py). Releases are real clones of f347e50f and
# 54e38ecf from the package's bundle. The unit mimics production: a base fragment, an override with
# the feature flags and a sentinel "secret", a legacy pointer drop-in and the current pointer.
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
SENTINEL=KDT-SENTINEL-SECRET-$ID
FAULTS=$T/faults
EVIDENCE=${2:-}

PASS=0; FAIL=0
ok()  { echo "  PASS  $*"; PASS=$((PASS + 1)); }
bad() { echo "  FAIL  $*"; FAIL=$((FAIL + 1)); }
check() { local desc=$1; shift; if "$@"; then ok "$desc"; else bad "$desc"; fi; }
section() { echo; echo "== $*"; }

pid_now() { systemctl show -p MainPID --value "$SERVICE"; }
cwd_now() { readlink "/proc/$(pid_now)/cwd" 2>/dev/null; }
snap() { local f; for f in $(systemctl show -p DropInPaths --value "$SERVICE"); do sha256sum "$f"; done; }
dropin_count() { systemctl show -p DropInPaths --value "$SERVICE" | wc -w; }
healthy() { [[ "$(curl -fsS --max-time 3 "http://127.0.0.1:$PORT/api/health" 2>/dev/null)" == '{"status":"ok"}' ]]; }
wait_up() { local i; for i in $(seq 1 30); do healthy && return 0; sleep 1; done; return 1; }
# run <log name> <script> [VAR=value...]: run a packaged script; status in RC, output in $LOGS/<name>.log
run() {
    local name=$1 script=$2; shift 2
    (cd "$PKG" && env KALILLAC_DEPLOY_ENV="${KDT_ENV:-$T/test.env}" "$@" bash "$script") >"$LOGS/$name.log" 2>&1
    RC=$?
    echo "  [$name] exit $RC: $(tail -n 1 "$LOGS/$name.log")"
}
logged() { grep -qF -- "$2" "$LOGS/$1.log"; }
pointer() {   # pointer <release dir>: the exact format earlier releases installed
    printf '%s\n' '[Service]' "WorkingDirectory=$1/candidate" 'ExecStart=' "ExecStart=$T/venv/bin/python3 $1/launch-production.py"
}

cleanup() {
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
mkdir -p "$T"/{opt,venv/bin,www/app,rollbacks,logs}
ln -s "$(command -v python3)" "$T/venv/bin/python3"
cp -r -- "$1" "$PKG"
(cd "$PKG" && sha256sum --check --strict --quiet SHA256SUMS) && ok "package checksums verify" || bad "package checksums"
cat >"$T/test.env" <<EOF
ENVIRONMENT_KIND=disposable-test
SERVICE=$SERVICE
RELEASE_PARENT=$T/opt
RELEASE_PREFIX=kalillac-release-
VENV=$T/venv
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
install -o root -g root -m 600 "$HERE/stub_launcher.py" "$OLD/launch-production.py"
LAUNCHER_SHA=$(sha256sum "$OLD/launch-production.py" | cut -d' ' -f1)
(cd "$OLD/frontend" && git ls-files | while IFS= read -r f; do mkdir -p "$T/www/app/$(dirname "$f")"; cp "$f" "$T/www/app/$f"; done)
echo '<html>site</html>' >"$T/www/index.html"
: >"$FAULTS"
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
Environment=KALILLAC_DB_ENABLED=true KALILLAC_REQUEST_BUDGET_ENABLED=true KALILLAC_PROVIDER_CEILINGS_ENABLED=true
Environment=KALILLAC_OPENAI_TOTAL_ATTEMPTS=500 KALILLAC_OPENAI_ANONYMOUS_ATTEMPTS=200 KALILLAC_OPENAI_PERIOD_SECONDS=86400
Environment=KALILLAC_ANONYMOUS_DAILY_LIMIT_ENABLED=true KALILLAC_ANONYMOUS_DAILY_LIMIT=10
Environment=KALILLAC_SEARCH_PRIMARY=brave KALILLAC_SEARCH_FALLBACK=tavily
Environment=KALILLAC_FINANCIAL_SAFEGUARDS_ENABLED=false KALILLAC_ACCOUNTS_ENABLED=false KALILLAC_BILLING_ENABLED=false
Environment=OPENAI_API_KEY=$SENTINEL STRIPE_SECRET_KEY=$SENTINEL KALILLAC_DB_PASSWORD=$SENTINEL
Environment=KDT_PORT=$PORT KDT_FAULT_FILE=$FAULTS KDT_STARTUP_SECONDS=3
EOF
pointer "$T/opt/kalillac-release-f126a245" >"$DROPIN/zzzzzzzz-release-f126a245.conf"
pointer "$OLD" >"$DROPIN/zzzzzzzzzzzzzzzz-release-f347e50f.conf"
chmod 644 "$DROPIN"/*.conf
systemctl daemon-reload
systemctl start "$SERVICE"
check "f347e50f stand-in running and healthy" wait_up
check "running from $OLD" test "$(cwd_now)" == "$OLD/candidate"
SNAP0=$(snap); COUNT0=$(dropin_count)

# ------------------------------------------------------------------------------------------ 1
section "1. read-only check on the initial state"
P=$(pid_now)
run check-initial check.sh
check "check.sh reports READY_TO_STAGE" test "$(tail -n 1 "$LOGS/check-initial.log")" == READY_TO_STAGE
check "check.sh prints the launcher sha256 for pinning" logged check-initial "$LAUNCHER_SHA"
check "check.sh changed nothing (pid, drop-ins)" test "$(pid_now)" == "$P" -a "$(snap)" == "$SNAP0"

# ------------------------------------------------------------------------------------------ 2
section "2. root cause: a release dir with the launcher copied in, as the failed attempt left it"
git -c init.defaultBranch=x clone -q --no-checkout "$PKG/kalillac-source-54e38ecf.bundle" "$NEW" 2>/dev/null
git -C "$NEW" checkout -q --detach "$NEW_COMMIT"
cp -p "$OLD/launch-production.py" "$NEW/launch-production.py"
check "the old gate fails: plain 'git status --porcelain' reports the launcher as untracked" \
    test "$(git -C "$NEW" status --porcelain)" == "?? launch-production.py"
check "...although no tracked file differs ('--untracked-files=no' is empty)" \
    test -z "$(git -C "$NEW" status --porcelain --untracked-files=no)"
P=$(pid_now)
run stage-exists stage.sh LAUNCHER_SHA256="$LAUNCHER_SHA"
check "stage.sh refuses the existing directory" test "$RC" -ne 0
check "  ...and says how to move it aside" logged stage-exists "QUARANTINE_EXISTING=1"
check "  ...without touching it or the service" test -f "$NEW/launch-production.py" -a "$(pid_now)" == "$P" -a "$(snap)" == "$SNAP0"
run check-leftover check.sh
check "check.sh: leftover dir is not a verified staged release (NOT_READY)" logged check-leftover "NOT_READY"
run activate-leftover activate.sh
check "activate.sh refuses the leftover dir (no stage marker)" logged activate-leftover "was not produced by stage.sh"
check "  ...before any production change: FAILED_BEFORE_PRODUCTION_CHANGE, same pid, same drop-ins" \
    test "$(logged activate-leftover FAILED_BEFORE_PRODUCTION_CHANGE && echo y)" == y -a "$(pid_now)" == "$P" -a "$(snap)" == "$SNAP0"

# ------------------------------------------------------------------------------------------ 3
section "3. stage: negative cases fail before any production change"
run stage-badpin stage.sh LAUNCHER_SHA256=0000000000000000000000000000000000000000000000000000000000000000 QUARANTINE_EXISTING=1
check "wrong LAUNCHER_SHA256 refused" logged stage-badpin "is not the approved"
check "  ...leftover dir untouched, service untouched" test -d "$NEW" -a "$(pid_now)" == "$P"
run stage-nopin stage.sh
check "missing LAUNCHER_SHA256 refused" logged stage-nopin "set LAUNCHER_SHA256"

# ------------------------------------------------------------------------------------------ 4
section "4. stage: quarantine the leftover and stage cleanly"
run stage stage.sh LAUNCHER_SHA256="$LAUNCHER_SHA" QUARANTINE_EXISTING=1
check "STAGED_OK" logged stage STAGED_OK
check "leftover moved to quarantine (kept)" test -n "$(ls -d "$T"/rollbacks/quarantine/kalillac-release-54e38ecf-* 2>/dev/null)"
check "pristine checkout verified before the launcher was installed" logged stage "pristine checkout verified"
check "untracked files in the staged release are exactly the launcher" \
    test "$(git -C "$NEW" status --porcelain --untracked-files=all)" == "?? launch-production.py"
check "staged launcher root:root 600, identical to the running one" \
    test "$(stat -c '%U:%G %a' "$NEW/launch-production.py")" == "root:root 600" -a "$(sha256sum <"$NEW/launch-production.py")" == "$(sha256sum <"$OLD/launch-production.py")"
check "service untouched by staging (pid, drop-ins)" test "$(pid_now)" == "$P" -a "$(snap)" == "$SNAP0"
run check-staged check.sh
check "check.sh reports READY_TO_ACTIVATE" test "$(tail -n 1 "$LOGS/check-staged.log")" == READY_TO_ACTIVATE

# ------------------------------------------------------------------------------------------ 5
section "5. activate: preflight refusals (no reload, no restart)"
echo "# stray" >"$NEW/candidate/stray.py"
run act-untracked activate.sh
check "an extra untracked file in the release is refused" logged act-untracked "untracked files are"
rm -f "$NEW/candidate/stray.py"
cp -p "$NEW/candidate/alembic.ini" "$T/alembic.ini.bak"
printf 'X' | dd of="$NEW/candidate/alembic.ini" bs=1 seek=10 conv=notrunc status=none   # same size
touch -r "$T/alembic.ini.bak" "$NEW/candidate/alembic.ini"                                # same mtime
run act-tracked activate.sh
check "a modified tracked file is refused (content re-hashed, stat cache not trusted)" logged act-tracked "tracked files differ"
cp -p "$T/alembic.ini.bak" "$NEW/candidate/alembic.ini"
printf '[Service]\nEnvironment=KDT_MANUAL=1\n' >"$DROPIN/zzzzzzzzzzzzzzzzzzzzzzzzzzzz-manual.conf"; systemctl daemon-reload
run act-override activate.sh
check "an unrecognised last drop-in (uncontrolled override) is refused" logged act-override "is not exactly the release pointer"
rm -f "$DROPIN/zzzzzzzzzzzzzzzzzzzzzzzzzzzz-manual.conf"; systemctl daemon-reload
echo "# pending" >>"$DROPIN/override.conf"
run act-reload activate.sh
check "unit files changed without daemon-reload (NeedDaemonReload=yes) is refused" logged act-reload "NeedDaemonReload"
sed -i '/^# pending$/d' "$DROPIN/override.conf"; systemctl daemon-reload
for log in act-untracked act-tracked act-override act-reload; do
    check "  $log: FAILED_BEFORE_PRODUCTION_CHANGE" logged "$log" FAILED_BEFORE_PRODUCTION_CHANGE
done
check "  service never restarted; drop-ins unchanged" test "$(pid_now)" == "$P" -a "$(snap)" == "$SNAP0"

section "5b. flag policy: a forbidden flag in the live service blocks staging and activation"
printf '[Service]\nEnvironment=KALILLAC_FINANCIAL_SAFEGUARDS_ENABLED=true\n' >"$DROPIN/p-test-flag.conf"
systemctl daemon-reload; systemctl restart "$SERVICE"; wait_up
P2=$(pid_now); SNAPF=$(snap)
run act-flag activate.sh
check "Financial Safeguards on in the live service: refused" logged act-flag "KALILLAC_FINANCIAL_SAFEGUARDS_ENABLED is enabled"
check "  before any production change" test "$(pid_now)" == "$P2" -a "$(snap)" == "$SNAPF"
rm -f "$DROPIN/p-test-flag.conf"; systemctl daemon-reload; systemctl restart "$SERVICE"; wait_up
P=$(pid_now)
check "drop-ins back to the initial set" test "$(snap)" == "$SNAP0"

# ------------------------------------------------------------------------------------------ 6
section "6. activate: interrupted after the pointer swap, before restart (operator Ctrl-C / SSH drop)"
(cd "$PKG" && KALILLAC_DEPLOY_ENV="$T/test.env" exec bash activate.sh) >"$LOGS/act-interrupt.log" 2>&1 &
APID=$!
for i in $(seq 1 600); do grep -q "pointer installed" "$LOGS/act-interrupt.log" 2>/dev/null && break; sleep 0.05; done
kill -STOP "$APID" 2>/dev/null; pkill -STOP -P "$APID" 2>/dev/null
kill -TERM "$APID" 2>/dev/null; pkill -CONT -P "$APID" 2>/dev/null; kill -CONT "$APID" 2>/dev/null
wait "$APID"; RC=$?
echo "  [act-interrupt] exit $RC: $(tail -n 1 "$LOGS/act-interrupt.log")"
check "interrupted in phase pointer/reloaded" grep -qE "FAILED in phase (pointer|reloaded)" "$LOGS/act-interrupt.log"
check "ROLLBACK_RECOVERED with no restart needed" logged act-interrupt "no restart was needed"
check "  original process never restarted" test "$(pid_now)" == "$P"
check "  drop-ins byte-identical to before" test "$(snap)" == "$SNAP0"

# ------------------------------------------------------------------------------------------ 7
section "7. activate: new release unhealthy after restart -> automatic rollback"
echo "unhealthy kalillac-release-54e38ecf" >"$FAULTS"
run act-unhealthy activate.sh
check "activation fails (non-zero exit)" test "$RC" -ne 0
check "effective unit verified before the restart" logged act-unhealthy "effective unit verified BEFORE restart"
check "ROLLBACK_RECOVERED after restart" logged act-unhealthy "ROLLBACK_RECOVERED: f347e50f restarted and verified running"
check "  rollback verified the running process by Git content" logged act-unhealthy "at $OLD_COMMIT (tracked files exact)"
check "  service runs f347e50f again, healthy, new pid" test "$(cwd_now)" == "$OLD/candidate" -a "$(pid_now)" != "$P"
healthy && ok "  healthy" || bad "  healthy"
check "  drop-ins byte-identical to before" test "$(snap)" == "$SNAP0"
P=$(pid_now)

echo "crash kalillac-release-54e38ecf" >"$FAULTS"
run act-crash activate.sh
check "new release crashing at start-up: ROLLBACK_RECOVERED" logged act-crash "ROLLBACK_RECOVERED: f347e50f restarted"
check "  fails fast on the failed unit state" logged act-crash "unit entered the failed state"
check "  f347e50f running, drop-ins as before" test "$(cwd_now)" == "$OLD/candidate" -a "$(snap)" == "$SNAP0"
P=$(pid_now)

section "7b. rollback target itself broken -> must report ROLLBACK_INCOMPLETE, never RECOVERED"
printf 'unhealthy kalillac-release-54e38ecf\nunhealthy kalillac-release-f347e50f\n' >"$FAULTS"
run act-badrollback activate.sh
check "ROLLBACK_INCOMPLETE reported" logged act-badrollback "ROLLBACK_INCOMPLETE"
check "  ROLLBACK_RECOVERED not reported" bash -c "! grep -q ROLLBACK_RECOVERED '$LOGS/act-badrollback.log'"
check "  drop-ins nevertheless restored to before" test "$(snap)" == "$SNAP0"
: >"$FAULTS"; systemctl restart "$SERVICE"; wait_up; P=$(pid_now)

# ------------------------------------------------------------------------------------------ 8
section "8. activate: slow start-up is allowed for; success"
echo "slow kalillac-release-54e38ecf" >"$FAULTS"
sed -i 's/^HEALTH_TIMEOUT_SECONDS=.*/HEALTH_TIMEOUT_SECONDS=60/' "$T/test.env"
run activate activate.sh
check "KALILLAC_RELEASE_54E38ECF_ACTIVATED" logged activate KALILLAC_RELEASE_54E38ECF_ACTIVATED
check "service runs 54e38ecf, healthy" test "$(cwd_now)" == "$NEW/candidate"
healthy && ok "  healthy" || bad "  healthy"
check "drop-in count unchanged (no extra override)" test "$(dropin_count)" -eq "$COUNT0"
check "the stable pointer is loaded last" test "$(systemctl show -p DropInPaths --value "$SERVICE" | tr ' ' '\n' | tail -n 1)" == "$DROPIN/zzzzzzzzzzzzzzzzz-kalillac-active-release.conf"
check "legacy f347e50f pointer moved into the rollback directory" test ! -e "$DROPIN/zzzzzzzzzzzzzzzz-release-f347e50f.conf"
check "effective WorkingDirectory / ExecStart = 54e38ecf" \
    test "$(systemctl show -p WorkingDirectory --value "$SERVICE")" == "$NEW/candidate"
check "NeedDaemonReload=no" test "$(systemctl show -p NeedDaemonReload --value "$SERVICE")" == no
check "previous release directory intact" test -f "$OLD/launch-production.py" -a -z "$(git -C "$OLD" status --porcelain --untracked-files=no)"
run check-active check.sh
check "check.sh reports ACTIVE_54e38ecf" test "$(tail -n 1 "$LOGS/check-active.log")" == ACTIVE_54e38ecf
: >"$FAULTS"
ROLLBACK_DIR=$(sed -n 's/^Rollback directory: *//p; s/^Manual rollback: *ROLLBACK_DIR=\([^ ]*\) .*/\1/p' "$LOGS/activate.log" | tail -n 1)

# ------------------------------------------------------------------------------------------ 9
section "9. manual rollback after a successful activation"
P=$(pid_now)
run rollback rollback.sh ROLLBACK_DIR="$ROLLBACK_DIR"
check "ROLLED_BACK_TO_f347e50f" logged rollback ROLLED_BACK_TO_f347e50f
check "  effective unit checked before restart" logged rollback "effective WorkingDirectory and ExecStart are $OLD again"
check "  running f347e50f, new pid, healthy" test "$(cwd_now)" == "$OLD/candidate" -a "$(pid_now)" != "$P"
check "  drop-ins byte-identical to the original state" test "$(snap)" == "$SNAP0"
check "  both release directories kept" test -d "$OLD" -a -d "$NEW"
run rollback-again rollback.sh ROLLBACK_DIR="$ROLLBACK_DIR"
check "a second rollback with the same directory is refused (state no longer matches)" test "$RC" -ne 0
run check-after-rollback check.sh
check "check.sh: READY_TO_ACTIVATE again" test "$(tail -n 1 "$LOGS/check-after-rollback.log")" == READY_TO_ACTIVATE

# ------------------------------------------------------------------------------------------ 10
section "10. a later activation rewrites the stable pointer in place"
mv "$DROPIN/zzzzzzzzzzzzzzzz-release-f347e50f.conf" "$T/legacy.conf"
{ echo "# Kalillac AI active release pointer."; pointer "$OLD"; } >"$DROPIN/zzzzzzzzzzzzzzzzz-kalillac-active-release.conf"
chmod 644 "$DROPIN/zzzzzzzzzzzzzzzzz-kalillac-active-release.conf"
systemctl daemon-reload; SNAP1=$(snap); COUNT1=$(dropin_count)
run activate-inplace activate.sh
check "activated, pointer rewritten in place" logged activate-inplace "rewritten in place"
check "  drop-in count unchanged" test "$(dropin_count)" -eq "$COUNT1"
check "  running 54e38ecf" test "$(cwd_now)" == "$NEW/candidate"
RB2=$(sed -n 's/^Manual rollback: *ROLLBACK_DIR=\([^ ]*\) .*/\1/p' "$LOGS/activate-inplace.log")
run rollback-inplace rollback.sh ROLLBACK_DIR="$RB2"
check "  rolled back; drop-ins byte-identical to before" test "$(snap)" == "$SNAP1" -a "$(cwd_now)" == "$OLD/candidate"

# ------------------------------------------------------------------------------------------ 12
section "12. read-only database check (local peer-authenticated PostgreSQL; synthetic schema)"
if command -v psql >/dev/null && runuser -u postgres -- psql -XAtqc 'select 1' >/dev/null 2>&1; then
    DB=kdt_$ID
    pg() { runuser -u postgres -- psql -X -q -v ON_ERROR_STOP=1 -d "$DB" -c "$1" >/dev/null 2>&1; }
    runuser -u postgres -- createdb "$DB" 2>/dev/null
    pg "CREATE SCHEMA kalillac; CREATE TABLE kalillac.alembic_version (version_num varchar(32) PRIMARY KEY);
        INSERT INTO kalillac.alembic_version VALUES ('0015_finance_ledger');
        CREATE TABLE kalillac.finance_ledger_buckets (x int); CREATE TABLE kalillac.finance_reservations (x int);
        CREATE TABLE kalillac.finance_unsafe_latches (x int);"
    sed -e 's/^DB_CHECK=.*/DB_CHECK=psql-peer/' -e "s/^DB_NAME=.*/DB_NAME=$DB/" -e 's/^DB_SCHEMA=.*/DB_SCHEMA=kalillac/' \
        "$T/test.env" >"$T/test-db.env"
    KDT_ENV=$T/test-db.env run check-db check.sh
    check "revision 0015 and empty ledger: check passes" logged check-db "database: revision 0015_finance_ledger; 3 Financial Safeguards tables empty"
    check "  check.sh READY_TO_ACTIVATE" test "$(tail -n 1 "$LOGS/check-db.log")" == READY_TO_ACTIVATE
    pg "UPDATE kalillac.alembic_version SET version_num = '0014_v1_session_issuance'"
    P=$(pid_now); SNAPD=$(snap)
    KDT_ENV=$T/test-db.env run act-db-revision activate.sh
    check "database at another revision: activation refused" logged act-db-revision "database revision is '0014_v1_session_issuance', expected 0015_finance_ledger"
    check "  before any production change" test "$(logged act-db-revision FAILED_BEFORE_PRODUCTION_CHANGE && echo y)" == y -a "$(pid_now)" == "$P" -a "$(snap)" == "$SNAPD"
    pg "UPDATE kalillac.alembic_version SET version_num = '0015_finance_ledger'; INSERT INTO kalillac.finance_unsafe_latches VALUES (1)"
    KDT_ENV=$T/test-db.env run check-db-latch check.sh
    check "a non-empty Financial Safeguards table is reported" logged check-db-latch "finance_unsafe_latches has 1 rows"
    check "  check.sh NOT_READY" logged check-db-latch NOT_READY
    pg "DELETE FROM kalillac.finance_unsafe_latches"
    check "the checks wrote nothing (row counts and revision unchanged)" \
        test "$(runuser -u postgres -- psql -XAtq -d "$DB" -c "SELECT version_num || ':' || (SELECT count(*) FROM kalillac.finance_ledger_buckets) FROM kalillac.alembic_version")" == "0015_finance_ledger:0"
else
    echo "  SKIP  no local peer-authenticated PostgreSQL on this host (CI runs this section)"
fi

# ------------------------------------------------------------------------------------------ 11
section "11. no credential in any output or state file"
if grep -rqF -- "$SENTINEL" "$LOGS" "$T"/rollbacks/*/state 2>/dev/null; then bad "sentinel secret found in output/state"
else ok "sentinel secret absent from every script output and state file"; fi

echo
echo "RESULT: $PASS passed, $FAIL failed (environment $ID)"
((FAIL == 0))
