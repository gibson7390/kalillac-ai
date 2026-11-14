"""Kalillac release preflight: import a staged release with the live service's environment.

Run by deploy/release/lib.sh (app_preflight) as the service user, fed on standard input so the
upload directory need not be readable by that user:

    python3 - <live pid> <venv python> <release candidate dir> <model> <migration head> "<prefixes>"

The outer process copies the live service's environment from /proc/<pid>/environ and runs CHILD in
a separate short process with cwd = the staged release's candidate directory. CHILD imports the
application exactly as the launcher would, then inspects the resulting objects. No port is bound,
no request or provider call is made and no database connection is opened (the launcher's database
singleton is not constructed). No bytecode is written into the release.

Output is one line beginning APP_PREFLIGHT_OK or APP_PREFLIGHT_FAILED. It never contains an
environment value or exception text (exception messages can echo configuration): only fixed check
names and, on an unexpected error, the exception type.
"""
import os
import subprocess
import sys

CHILD = r'''
import os, sys
model, head, prefixes = sys.argv[1], sys.argv[2], sys.argv[3].split()
sys.path.insert(0, os.getcwd())
step = "import"


def check(ok, what):
    if not ok:
        print("APP_PREFLIGHT_FAILED " + what, flush=True)
        raise SystemExit(3)


try:
    import app_fastapi_candidate as app
    from kalillac_routing import provider_controls
    step = "checks"
    # Financial Safeguards: off means no configuration is loaded and the controls carry none.
    check(app._financial_config is None, "Financial Safeguards configuration is loaded (must be off)")
    service = provider_controls.SERVICE
    check(isinstance(service, provider_controls.ProviderControls), "provider attempt ceilings are not active")
    check(service.finance is None, "provider controls carry a financial configuration")
    controls = app._provider_control_config
    check(controls is not None and service.config is controls, "provider ceiling configuration is not the active one")
    check(sorted(controls.providers) == ["brave", "openai", "tavily"], "provider ceilings do not cover openai, brave and tavily")
    # Accounts, usage metering and enforcement, billing and Spaces: nothing constructed or mounted.
    check(app._usage_meter is None, "usage metering is active")
    check(app._usage_limits is None, "account usage limits are active")
    paths = sorted({getattr(route, "path", "") for route in app.api.routes})
    for prefix in prefixes:
        check(not [p for p in paths if p == prefix or p.startswith(prefix + "/")],
              "inactive route family is mounted: " + prefix)
    check(app.OPENAI_MODEL == model, "OpenAI model is not " + model)
    policy = app.SEARCH_PROVIDER_POLICY
    check((policy.primary, policy.fallback) == ("brave", "tavily"), "search policy is not brave primary, tavily fallback")
    step = "migrations"
    from alembic.script import ScriptDirectory
    heads = list(ScriptDirectory(os.path.join(os.getcwd(), "migrations")).get_heads())
    check(heads == [head], "release migration head is not " + head)
except SystemExit:
    raise
except BaseException as exc:
    print("APP_PREFLIGHT_FAILED " + step + ": " + type(exc).__name__, flush=True)
    raise SystemExit(4)
print("APP_PREFLIGHT_OK financial_safeguards=off provider_ceilings=on(openai,brave,tavily) "
      "usage=off inactive_routes=absent model=" + model + " search=brave/tavily migration_head=" + head,
      flush=True)
'''


def live_environment(pid):
    with open("/proc/%s/environ" % int(pid), "rb") as handle:
        raw = handle.read()
    env = {}
    for item in raw.split(b"\0"):
        if b"=" in item:
            name, value = item.split(b"=", 1)
            env[os.fsdecode(name)] = os.fsdecode(value)
    return env


def run(python, cwd, args, env, timeout=300):
    env = dict(env, PYTHONDONTWRITEBYTECODE="1")
    try:
        result = subprocess.run([python, "-c", CHILD, *args], env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 1, "APP_PREFLIGHT_FAILED timeout after %ds" % timeout
    lines = [line for line in result.stdout.splitlines() if line.startswith("APP_PREFLIGHT_")]
    last = lines[-1] if lines else "APP_PREFLIGHT_FAILED no result (exit %d)" % result.returncode
    ok = result.returncode == 0 and last.startswith("APP_PREFLIGHT_OK")
    return (0 if ok else 1), last


def main(argv):
    if len(argv) != 7:
        print("APP_PREFLIGHT_FAILED usage")
        return 2
    pid, python, cwd, model, head, prefixes = argv[1:]
    status, line = run(python, cwd, [model, head, prefixes], live_environment(pid))
    print(line, flush=True)
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv))
