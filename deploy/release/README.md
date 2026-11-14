# Kalillac AI release tooling (`deploy/release`)

Reviewed, version-controlled tooling that stages, activates and rolls back one backend release on
the production host. It keeps the existing architecture: immutable release directories
`/opt/kalillac-release-<short>` (a Git checkout of exactly one commit), the shared Python environment
`/opt/kalillac-venv-440924a9`, the root-only production launcher `launch-production.py`, and the
systemd service `kalillac-ai-fastapi-staging.service` pointed at a release by a drop-in.

It never migrates the database, edits a unit file, environment file, flag, Nginx, Cloudflare or the
web root, and never deletes a release or rollback directory.

## Files

| File | Role |
|---|---|
| `check.sh` | Read-only status. Identifies the running release from its process and Git content, prints the launcher sha256, flag states (on/off only), database revision. Ends with `READY_TO_STAGE`, `READY_TO_ACTIVATE`, `ACTIVE_<short>` or `NOT_READY`. |
| `stage.sh` | Creates the new release directory beside production. No effect on the service. |
| `activate.sh` | Points the service at the staged release, restarts once, verifies, and rolls back automatically on failure. |
| `rollback.sh` | Manual rollback after a successful activation. |
| `lib.sh` | Shared checks (sourced). |
| `preflight.py` | Imports the staged application with the live environment, in a separate process, and checks the resulting objects. |
| `production.env` | Production environment facts (paths, service, port). No secrets. |
| `releases/<short>.env` | Release facts: commits, reviewed blob, unchanged paths, migration head, required flag state. Packaged as `release.env`. |
| `package.sh` | Builds the upload package from Git on a workstation. |
| `tests/` | Disposable-environment tests (real systemd) and preflight tests (real application). |

Configuration files are `KEY=VALUE`, parsed, never sourced; unknown, duplicate or missing keys are refused.

## How Git-tracked integrity and the launcher are handled

A release directory may hold exactly three kinds of files, and each is checked separately:

1. **Tracked files**: every one is re-hashed and compared with the release commit through a throwaway
   index (`git read-tree <commit>` into a temporary `GIT_INDEX_FILE`, then `update-index --refresh` and
   `diff-files`). Neither the stat cache nor the repository's own index is trusted or written. Any
   modified, deleted or mode-changed tracked file is refused.
2. **Untracked files**: must be exactly the allowlist `DEPLOY_ARTIFACTS` (`launch-production.py`). A
   freshly cloned release must have none at all (it is verified *pristine* before the launcher is
   installed). Any other untracked file is refused.
3. **Ignored files**: only Python bytecode under `__pycache__/` is accepted. A fresh checkout must have none.

The launcher is a **deployment artifact**, not source: it is never added to Git, must not be tracked,
must be `root:root 600`, and is copied from the running release and compared by content. `stage.sh`
also requires the operator to pin its sha256 (`LAUNCHER_SHA256`, printed by `check.sh`).
`activate.sh` accepts only a directory produced by `stage.sh` (a marker inside `.git`, outside the
worktree, records the commit, launcher hash and ownership).

## Process identity (the launcher execs the server)

systemd starts `<venv>/bin/python3 <release>/launch-production.py`; the launcher replaces itself with
the server (`exec`), keeping the PID, so the unit's main process carries the server's argv, not the
launcher's. The running release is proven by all of the following, never by a health response alone:

* the unit's effective `WorkingDirectory` is `<release>/candidate` and its single `ExecStart` is
  `<venv>/bin/python3 <release>/launch-production.py` (the approved launcher);
* the main process's argv is **exactly** `PROCESS_ARGV` (`production.env`: the observed
  `… uvicorn app_fastapi_candidate:api --host 127.0.0.1 --port 8001 --workers 1`), compared argument by
  argument, never as a substring; its executable is the resolved venv interpreter; its cwd is
  `<release>/candidate`; no environment variable points into another release directory;
* the port's listener belongs to that process;
* the release's Git HEAD and every tracked file, and the launcher's content, mode and owner.

## The release pointer drop-in

Earlier releases each added a new drop-in (`z…z-release-<a>.conf` with a longer z prefix each time),
so overrides accumulated. Production also loads `z…z-provider-ceilings.conf` (z-run 96) **after** the
current pointer (z-run 80), so the pointer is not the last drop-in. `activate.sh`:

* finds the **effective** pointer by content: the last loaded drop-in that assigns `WorkingDirectory`
  or `ExecStart`. It must be **exactly** the pointer for the running release (`[Service]`,
  `WorkingDirectory=<release>/candidate`, `ExecStart=`, `ExecStart=<venv python> <release>/launch-production.py`);
  anything else (for example a later override that assigns either key, even to the same value) is an
  unknown override and is refused. Drop-ins after it that assign neither key, such as
  `provider-ceilings.conf`, are reported by key name and kept byte-for-byte;
* replaces it with one pointer at a stable name **at the same position in the load order**: the same
  z prefix with the suffix `-kalillac-active-release.conf` (in production between the f126a245 pointer and
  `provider-ceilings.conf`). It refuses if the current order is not byte order of file names or if the
  name would move. A legacy per-release pointer is moved into the rollback directory, so the drop-in
  count does not grow; later releases rewrite the stable pointer in place;
* after the reload, requires systemd's drop-in list to be exactly the previous list with only the
  pointer replaced, and the provider ceilings (enabled, all nine limits, values by hash) to be
  unchanged in the new process;
* refuses to start if systemd reports `NeedDaemonReload=yes` (unreviewed unit changes would be applied);
* after `daemon-reload` and **before restarting**, reads back the effective `WorkingDirectory` and
  `ExecStart` (exactly one entry), confirms every other drop-in is byte-identical and every other unit
  setting unchanged (hash; `Environment` is never printed).

Older, already-overridden pointer drop-ins are left as they are; removing them is a separate change.

## Failure handling

| Failure | What happens |
|---|---|
| Any preflight check (before the first change) | `FAILED_BEFORE_PRODUCTION_CHANGE`; no reload, no restart. |
| After the pointer swap, before the restart (including Ctrl-C / SSH drop) | Drop-ins restored byte-for-byte, reload, effective unit verified; the original process was never restarted and is verified still serving. `ROLLBACK_RECOVERED`. |
| After the restart | Drop-ins restored, reload, effective unit verified before restarting, previous release restarted, and the running process verified: cwd, exact argv, executable, HEAD and every tracked file, launcher, configuration and provider-ceiling hashes, flags, listener, health. `ROLLBACK_RECOVERED` only if all hold; otherwise `ROLLBACK_INCOMPLETE`. |

Health after a restart means: unit active, a new main PID whose cwd is the release, the port held by
that PID, and `{"status":"ok"}` for 3 consecutive checks 2 s apart, within `HEALTH_TIMEOUT_SECONDS`
(120 s). A unit that enters `failed` fails immediately instead of waiting.

## Production procedure (release 54e38ecf)

On a workstation, from the reviewed tooling commit:

```bash
bash deploy/release/package.sh 54e38ecf ../kalillac-deploy-54e38ecf     # upload every file in it
```

On the server, as root, in Termius (run step 5 inside `tmux`):

```bash
D=/root/kalillac-upload-54e38ecf && cd "$D" && sha256sum -c SHA256SUMS   # 1. every line OK
bash check.sh | tee "check-$(date -u +%H%M%S).txt"                        # 2. read-only, see below
LAUNCHER_SHA256=<hash from step 2> QUARANTINE_EXISTING=1 bash stage.sh 2>&1 | tee stage.log   # 3. STAGED_OK
bash check.sh | tail -n 1                                                 # 4. READY_TO_ACTIVATE
bash activate.sh 2>&1 | tee "activate-$(date -u +%Y%m%dT%H%M%SZ).log"     # 5. owner go: ..._ACTIVATED
bash check.sh | tail -n 1                                                 # 6. ACTIVE_54e38ecf
```

Step 2 must show: running release `/opt/kalillac-release-f347e50f`, HEAD `f347e50f…`, tracked files
exact, effective unit = that release, the f347e50f drop-in marked `EFFECTIVE release pointer`, `provider-ceilings.conf`
after it marked with key names only, `argv = the approved invocation`, provider ceilings enabled,
every `FLAGS_OFF` flag off and `FLAGS_ON` flag on, database revision `0015_finance_ledger` with three
empty Financial Safeguards tables, and the launcher line `root:root 600 sha256 <hash>`; compare that
hash with the one recorded for the f347e50f launcher. The only expected problem is the leftover
`/opt/kalillac-release-54e38ecf` from the failed attempt (`NOT_READY (an unverified … exists)`); step 3
moves it to `/opt/kalillac-rollbacks/quarantine/` (kept) and stages a fresh, verified copy.
If anything else is reported, stop.

Manual rollback after a successful activation:

```bash
ROLLBACK_DIR=<printed by activate.sh> bash rollback.sh    # ROLLED_BACK_TO_f347e50f
```

## Tests

```bash
# Real systemd, disposable host only (CI runner, throwaway VM or WSL), as root:
KALILLAC_PACKAGE_FROM_WORKTREE=1 bash deploy/release/package.sh 54e38ecf /tmp/pkg   # or a committed build
sudo bash deploy/release/tests/disposable-systemd.sh /tmp/pkg /tmp/evidence
# Preflight against the real application (pinned dependencies installed):
python -m pytest deploy/release/tests/test_preflight.py -v
```

The disposable test uses a uniquely named transient unit (`/run/systemd/system/kalillac-deploytest-<id>.service`),
real Git clones of f347e50f and 54e38ecf, a launcher that execs a stand-in uvicorn, and drop-ins named
like production's with provider-ceilings.conf after the pointer; the packaged
scripts run unmodified, pointed at a `disposable-test` environment file that `lib.sh` refuses to accept
with production names or paths. `.github/workflows/deploy-tooling-verification.yml` runs both suites.
