#!/usr/bin/env bash
# Read-only assessment of a host before staging is placed on it (docs/STAGING-HOST-ASSESSMENT.md).
#
# Run it FROM YOUR OWN MACHINE, streamed over SSH, so nothing is copied to or written on the host:
#
#     ssh <user>@<host> 'sudo -n bash -s' < deploy/staging/assess_host.sh > host-assessment.txt
#
# (without root it still runs; the sections that need root say so). It changes nothing: no package,
# file, unit, firewall rule, database or setting. It takes about 70 seconds, most of it a one-minute
# CPU and memory sample. It never prints a unit's environment, a crontab command line, a password, a
# key or an nginx secret: units are read by named properties, nginx by named directives only.
set -u
export LC_ALL=C PGOPTIONS='-c default_transaction_read_only=on'
PROD_UNIT=kalillac-ai-fastapi-staging.service   # production's unit (a historical name)

section() { printf '\n===== %s =====\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }
root() { [ "$(id -u)" -eq 0 ]; }
needs_root() { root || { echo "(skipped: needs root)"; return 1; }; }

section "0. host"
date -u +%FT%TZ
hostname
. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME"
uname -r
uptime
have systemctl && systemctl --version | head -1
[ -f /var/run/reboot-required ] && echo "REBOOT REQUIRED (pending updates)" || echo "no reboot pending"
stat -fc 'cgroup filesystem: %T' /sys/fs/cgroup
# Provider facts (DigitalOcean metadata, link-local, read-only; absent elsewhere).
for key in region droplet_id; do
  value=$(curl -fs --max-time 2 "http://169.254.169.254/metadata/v1/$key" 2>/dev/null) && echo "provider $key: $value"
done
echo "provider size (vCPU/memory/disk, from metadata if present):"
curl -fs --max-time 2 http://169.254.169.254/metadata/v1.json 2>/dev/null \
  | grep -o '"\(vcpus\|memory\|disk\)":[0-9]*' | tr '\n' ' '; echo

section "1. CPU"
nproc
grep -m1 'model name' /proc/cpuinfo
cat /proc/loadavg
for kind in cpu memory io; do printf '%s pressure: ' "$kind"; tr '\n' ' ' < "/proc/pressure/$kind" 2>/dev/null; echo; done
echo "-- vmstat, 12 samples x 5 s (r = runnable, wa = I/O wait, si/so = swap):"
vmstat -w 5 13 | sed '3d'

section "2. memory"
free -m
grep -E '^(MemTotal|MemAvailable|SwapTotal|SwapFree|Committed_AS|CommitLimit):' /proc/meminfo
echo "-- largest processes by resident memory:"
ps -eo pid,user,rss,pcpu,etime,comm --sort=-rss | head -15
echo "-- OOM events in the kernel log, last 30 days:"
if root && have journalctl; then
  journalctl -k --since -30d --no-pager 2>/dev/null | grep -ciE 'out of memory|oom-kill|killed process'
  journalctl -k --since -30d --no-pager 2>/dev/null | grep -iE 'out of memory|oom-kill|killed process' | tail -5
else echo "(skipped: needs root)"; fi
if have sar && [ -d /var/log/sysstat ]; then
  echo "-- sysstat history, yesterday and today (CPU, then memory):"
  sar -u -f "/var/log/sysstat/sa$(date -d yesterday +%d)" 2>/dev/null | tail -3
  sar -u 2>/dev/null | tail -3; sar -r 2>/dev/null | tail -3
else echo "(no sysstat history on this host)"; fi

section "3. disk"
df -hT -x tmpfs -x devtmpfs -x squashfs -x overlay
df -i -x tmpfs -x devtmpfs -x squashfs -x overlay | awk 'NR==1 || $5+0 > 50'
echo "-- I/O wait is the vmstat 'wa' column above"

section "4. production's footprint ($PROD_UNIT)"
if have systemctl; then
  systemctl show "$PROD_UNIT" -p ActiveState,SubState,User,MainPID,NRestarts,ActiveEnterTimestamp \
    -p MemoryCurrent,MemoryPeak,MemoryHigh,MemoryMax,CPUUsageNSec,CPUQuotaPerSecUSec,CPUWeight,TasksCurrent,TasksMax
  pid=$(systemctl show "$PROD_UNIT" -p MainPID --value)
  if [ -n "$pid" ] && [ "$pid" != 0 ]; then
    echo "-- the service's processes (main and children):"
    ps -o pid,rss,pcpu,etime,comm --ppid "$pid" -p "$pid"
  fi
  for unit in nginx.service postgresql.service; do
    systemctl show "$unit" -p ActiveState,MemoryCurrent,MemoryPeak,CPUUsageNSec 2>/dev/null | tr '\n' ' '; echo " ($unit)"
  done
fi

section "5. ports and listeners"
if root; then ss -ltnpH; else ss -ltnH; echo "(process names need root)"; fi
for port in 8001 8002 8003 5432 5433 5434; do
  if ss -ltnH "( sport = :$port )" | grep -q .; then echo "port $port: IN USE"; else echo "port $port: free"; fi
done

section "6. Kalillac and staging units, files and nginx"
if have systemctl; then
  systemctl list-units --all --no-pager --plain 'kalillac*' 'postgresql*' 'nginx*' 2>/dev/null
  systemctl list-unit-files --no-pager --plain 'kalillac*' 2>/dev/null
  systemctl list-timers --all --no-pager 2>/dev/null | head -20
fi
ls -ld /etc/systemd/system/kalillac* /opt/kalillac* /var/lib/kalillac* /etc/kalillac* /etc/credstore/kalillac* 2>/dev/null
if needs_root && have nginx; then
  echo "-- nginx: only the directives that matter here (no secrets are in these):"
  nginx -T 2>/dev/null | grep -nE '^\s*(# configuration file|server_name|listen|proxy_pass|root|ssl_verify_client|ssl_client_certificate|client_max_body_size|proxy_read_timeout|add_header X-Robots-Tag|real_ip_header)\b'
  printf 'set_real_ip_from lines (Cloudflare ranges): '; nginx -T 2>/dev/null | grep -cE '^\s*set_real_ip_from'
  printf 'allow/deny lines: '; nginx -T 2>/dev/null | grep -cE '^\s*(allow|deny)\b'
fi
echo "-- root's crontab: program names only (command lines can hold secrets):"
if root; then crontab -l -u root 2>/dev/null | grep -vE '^\s*(#|$)' | awk '{print $6}' | sort | uniq -c; fi

section "7. PostgreSQL"
have pg_lsclusters && pg_lsclusters
ls /usr/lib/postgresql 2>/dev/null | tr '\n' ' '; echo "(installed major versions)"
if needs_root && have psql; then
  q() { sudo -n -u postgres psql -XAtq -c "$1" 2>&1; }
  q "SELECT version()"
  q "SELECT name || '=' || setting || coalesce(unit, '') FROM pg_settings WHERE name IN ('port','max_connections','shared_buffers','work_mem','effective_cache_size','data_directory','listen_addresses','password_encryption')"
  echo "-- connections now, by database:"
  q "SELECT coalesce(datname,'(background)') || ': ' || count(*) FROM pg_stat_activity GROUP BY 1 ORDER BY 1"
  echo "-- databases: size and who may connect (empty or =Tc/ means PUBLIC may connect):"
  q "SELECT datname || '  ' || pg_size_pretty(pg_database_size(datname)) || '  ' || coalesce(datacl::text, '(default: PUBLIC may connect)') FROM pg_database WHERE NOT datistemplate ORDER BY 1"
  echo "-- pg_hba rules (no passwords are stored here):"
  q "SELECT line_number || ' ' || type || ' ' || array_to_string(database, ',') || ' ' || array_to_string(user_name, ',') || ' ' || coalesce(address, '') || ' ' || auth_method FROM pg_hba_file_rules ORDER BY line_number"
  echo "-- roles that can log in (names only):"
  q "SELECT string_agg(rolname, ' ' ORDER BY rolname) FROM pg_roles WHERE rolcanlogin"
fi

section "8. security posture"
if root && have sshd; then sshd -T 2>/dev/null | grep -E '^(permitrootlogin|passwordauthentication|pubkeyauthentication|port) '; fi
if root && have ufw; then ufw status verbose 2>/dev/null | head -25; fi
if root && have nft; then printf 'nftables rules: '; nft list ruleset 2>/dev/null | grep -c . ; fi
if root && have fail2ban-client; then fail2ban-client status 2>/dev/null | head -5; fi
have systemctl && printf 'unattended-upgrades: %s\n' "$(systemctl is-enabled unattended-upgrades 2>/dev/null || echo absent)"
echo "-- accounts with a login shell:"
getent passwd | awk -F: '$7 !~ /(nologin|false|sync)$/ {print $1}' | tr '\n' ' '; echo
echo "-- last reboots:"
last -x reboot 2>/dev/null | head -3

section "done"
echo "read-only assessment complete; nothing was changed"
