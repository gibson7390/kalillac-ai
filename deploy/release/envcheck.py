"""Kalillac release tooling: the environment systemd will give the service on its NEXT start.

The running process's /proc/<pid>/environ shows the environment of the LAST start. A reloaded
Environment= drop-in, a rewritten EnvironmentFile or a changed manager environment only takes
effect at the next start, so a restart could silently enable features or disable ceilings. This
script composes the next-start environment from systemd's loaded state (read exactly over D-Bus
with busctl --json, never by parsing text output) and the EnvironmentFile contents as they are now:

    manager environment  ->  Environment=  ->  EnvironmentFile= (in order; later wins)

Mechanisms it cannot model are refused (fail closed): PassEnvironment=, UnsetEnvironment=,
PAMName= (pam_env), ExecStartPre= (could rewrite an EnvironmentFile just before the start), glob
EnvironmentFile paths, and EnvironmentFile syntax outside a strict subset (quotes without escapes;
no backslash, $, backtick or continuation lines).

Checks, by mode:
  --pid P        the next-start environment equals process P's environment, except names systemd
                 generates per start and names the launcher is approved to add or override
                 (--launcher-vars, which can never include a flag, ceiling or model name);
  --approved F   the next-start environment equals an approved snapshot (rollback restarts);
  always         the policy holds for the NEXT start: FLAGS_OFF off, FLAGS_ON on, FLAG_VALUES
                 exact, OPENAI_MODEL unset or approved, provider ceilings on with all limits.
--snapshot-out writes the next-start snapshot: one line per variable, "NAME sha256(NAME=VALUE)",
so later comparisons can name what changed without storing or printing any value.

Output: one line, ENV_CHECK_OK ... or ENV_CHECK_FAILED ...; only variable NAMES are printed.
Run as root with the isolated interpreter (-I); standard library only.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

TRUE = {"1", "true", "yes", "on"}
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
# Set by systemd (or the kernel) per start; not part of the configuration being approved.
GENERATED = {
    "INVOCATION_ID", "JOURNAL_STREAM", "SYSTEMD_EXEC_PID", "MEMORY_PRESSURE_WATCH",
    "MEMORY_PRESSURE_WRITE", "MANAGERPID", "NOTIFY_SOCKET", "WATCHDOG_PID", "WATCHDOG_USEC",
    "LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES", "CREDENTIALS_DIRECTORY", "RUNTIME_DIRECTORY",
    "STATE_DIRECTORY", "CACHE_DIRECTORY", "LOGS_DIRECTORY", "CONFIGURATION_DIRECTORY", "HOME",
    "USER", "LOGNAME", "SHELL", "TRIGGER_UNIT", "TRIGGER_PATH", "TRIGGER_TIMER_REALTIME_USEC",
    "TRIGGER_TIMER_MONOTONIC_USEC",
}
CEILING_PROVIDERS = ("OPENAI", "BRAVE", "TAVILY")
CEILING_KINDS = ("TOTAL_ATTEMPTS", "ANONYMOUS_ATTEMPTS", "PERIOD_SECONDS")


class Refuse(Exception):
    pass


def bus(*args):
    try:
        out = subprocess.run(["busctl", "--json=short", *args], stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, timeout=30, check=True).stdout
    except (subprocess.SubprocessError, OSError):
        raise Refuse("cannot read systemd state over D-Bus (busctl)") from None
    return json.loads(out)["data"]


def assignments(entries, where):
    env = {}
    for entry in entries:
        name, sep, value = entry.partition("=")
        if not sep or not NAME.match(name):
            raise Refuse("malformed assignment in " + where)
        env[name] = value
    return env


def parse_env_file(path):
    """Strict subset of systemd's EnvironmentFile syntax; anything else is refused."""
    env = {}
    with open(path, "rb") as handle:
        data = handle.read()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise Refuse("EnvironmentFile " + path + " is not UTF-8") from None
    for number, line in enumerate(text.split("\n"), 1):
        where = "EnvironmentFile %s line %d" % (path, number)
        stripped = line.strip()
        if not stripped or stripped[0] in "#;":
            continue
        if "\r" in line or "\\" in line or "$" in line or "`" in line:
            raise Refuse("unsupported syntax (CR, backslash, $ or backtick) in " + where)
        name, sep, value = stripped.partition("=")
        name = name.strip()
        if not sep or not NAME.match(name):
            raise Refuse("unsupported syntax in " + where)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            inner = value[1:-1]
            if value[0] in inner or "\"" in inner or "'" in inner:
                raise Refuse("unsupported quoting in " + where)
            value = inner
        elif any(c in value for c in "\"'") or re.search(r"\s#", value):
            raise Refuse("unsupported quoting or inline comment in " + where)
        env[name] = value
    return env


def next_start_environment(service):
    unit = bus("call", "org.freedesktop.systemd1", "/org/freedesktop/systemd1",
               "org.freedesktop.systemd1.Manager", "GetUnit", "s", service)[0]

    def prop(name):
        return bus("get-property", "org.freedesktop.systemd1", unit, "org.freedesktop.systemd1.Service", name)

    for name in ("PassEnvironment", "UnsetEnvironment"):
        if prop(name):
            raise Refuse("unsupported: the unit sets " + name + "=")
    if prop("PAMName"):
        raise Refuse("unsupported: the unit sets PAMName= (pam_env can change the environment)")
    if prop("ExecStartPre"):
        raise Refuse("unsupported: the unit has ExecStartPre= (it could rewrite an EnvironmentFile before the start)")
    manager = bus("get-property", "org.freedesktop.systemd1", "/org/freedesktop/systemd1",
                  "org.freedesktop.systemd1.Manager", "Environment")
    env = assignments(manager, "the manager environment")
    sources = {"manager": len(env)}
    unit_env = assignments(prop("Environment"), "Environment=")
    env.update(unit_env)
    sources["Environment="] = len(unit_env)
    files = 0
    for path, ignore_missing in prop("EnvironmentFiles"):
        if any(c in path for c in "*?["):
            raise Refuse("unsupported: glob in EnvironmentFile= " + path)
        if not os.path.exists(path):
            if ignore_missing:
                continue
            raise Refuse("EnvironmentFile " + path + " is missing (the next start would fail)")
        if os.path.islink(path) or not os.path.isfile(path):
            raise Refuse("EnvironmentFile " + path + " is not a regular file")
        env.update(parse_env_file(path))
        files += 1
    sources["EnvironmentFile"] = files
    return env, sources


def running_environment(pid):
    with open("/proc/%d/environ" % int(pid), "rb") as handle:
        raw = handle.read()
    env = {}
    for item in raw.split(b"\0"):
        if b"=" in item:
            name, value = item.split(b"=", 1)
            env[os.fsdecode(name)] = os.fsdecode(value)
    return env


def snapshot(env):
    return {name: hashlib.sha256((name + "=" + value).encode("utf-8", "surrogateescape")).hexdigest()
            for name, value in env.items()}


def policy(env, args):
    problems = []
    for name in args.flags_off.split():
        if env.get(name, "").strip().lower() in TRUE:
            problems.append(name + " would be ON")
    for name in args.flags_on.split():
        if env.get(name, "").strip().lower() not in TRUE:
            problems.append(name + " would not be on")
    for pair in args.flag_values.split():
        name, _, want = pair.partition("=")
        if env.get(name) != want:
            problems.append(name + " would not be " + want)
    if "OPENAI_MODEL" in env and env["OPENAI_MODEL"] != args.model:
        problems.append("OPENAI_MODEL would not be " + args.model)
    if env.get("KALILLAC_PROVIDER_CEILINGS_ENABLED", "").strip().lower() not in TRUE:
        problems.append("provider ceilings would be disabled")
    for provider in CEILING_PROVIDERS:
        for kind in CEILING_KINDS:
            name = "KALILLAC_%s_%s" % (provider, kind)
            if not re.fullmatch(r"[0-9]{1,10}", env.get(name, "")):
                problems.append(name + " would not be an explicit integer")
    if problems:
        raise Refuse("next start: " + "; ".join(problems))


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--service", required=True)
    parser.add_argument("--pid", default="")
    parser.add_argument("--approved", default="")
    parser.add_argument("--snapshot-out", default="")
    parser.add_argument("--flags-off", default="")
    parser.add_argument("--flags-on", default="")
    parser.add_argument("--flag-values", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--launcher-vars", default="")
    args = parser.parse_args(argv)
    try:
        launcher = set(args.launcher_vars.split())
        env, sources = next_start_environment(args.service)
        policy(env, args)
        notes = []
        if args.pid:
            running = running_environment(args.pid)
            pending = sorted(n for n in env if n not in launcher and running.get(n) != env[n])
            if pending:
                raise Refuse("next-start value differs from the running process (pending change) for: " + " ".join(pending))
            extra = sorted(n for n in running if n not in env and n not in GENERATED and n not in launcher)
            if extra:
                raise Refuse("set in the running process but not in the next-start configuration "
                             "(removed, or added by the launcher without approval): " + " ".join(extra))
            supplied = sorted(n for n in running if n in launcher)
            notes.append("= running process; launcher-supplied: " + (" ".join(supplied) or "none"))
        current = snapshot(env)
        if args.approved:
            approved = {}
            with open(args.approved) as handle:
                for line in handle:
                    name, digest = line.split()
                    approved[name] = digest
            changed = sorted(n for n in set(approved) | set(current) if approved.get(n) != current.get(n))
            if changed:
                raise Refuse("next-start environment differs from the approved snapshot for: " + " ".join(changed))
            notes.append("= approved snapshot")
        if args.snapshot_out:
            fd = os.open(args.snapshot_out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.writelines("%s %s\n" % (n, current[n]) for n in sorted(current))
        print("ENV_CHECK_OK next-start environment (%d variables: manager %d, Environment= %d, %d EnvironmentFile) %s; "
              "flags and provider ceilings as approved for the next start"
              % (len(env), sources["manager"], sources["Environment="], sources["EnvironmentFile"], "; ".join(notes)))
        return 0
    except Refuse as refusal:
        print("ENV_CHECK_FAILED " + str(refusal))
    except Exception as error:   # type only: messages could echo configuration
        print("ENV_CHECK_FAILED unexpected " + type(error).__name__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
