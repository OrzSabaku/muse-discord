#!/usr/bin/env bash
# muse-discord watchdog hook: the scheduled watchdog run, as a Muse hook.
# Register it to run every 10 minutes, delivered to the owner's MAIN
# chat (SETUP.md, step 5). Each run:
#   1. starts watchdog.sh fully detached (double fork, new session), its
#      output going to a file of its own that is renamed to
#      watchdog-hook.out.<time>.<pid> only once the run has finished. A
#      hook-runtime timeout that kills this script can't take the
#      watchdog run, or the ALERT it prints, down with it;
#   2. waits up to WAIT_SECS for that run to finish;
#   3. collects every finished output file (this run's, and any an
#      earlier hook run stopped waiting for), deletes them, and wakes the
#      main chat with their ALERT lines, or stays silent when there are
#      none. So an alert is delivered now, or by the next hook run.
# The watchdog alerts once per new failure, never every run. UPDATE
# lines are not passed on: the bot already DMs the owners about a new
# release, with an Update now button (updates.notify).
#
# Zero tokens on a healthy run; never touches the token itself. Like the
# other hooks, it sources $HATCH_HOOK_RUNTIME for silent/wake/log. A
# runtime without hooks can run watchdog.sh from any scheduler (cron, a
# scheduled job) every ~10 minutes instead; its ALERT lines then need a
# reader.
set -euo pipefail
# shellcheck source=/dev/null
source "$HATCH_HOOK_RUNTIME"

PKG_DIR="${MUSE_DISCORD_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
WAIT_SECS="${MUSE_WATCHDOG_HOOK_WAIT:-50}"
# Proof for bin/doctor.py that this hook runs (never fails the hook).
touch "$PKG_DIR/.seen-watchdog-hook" 2>/dev/null || true

printf -v stamp '%(%s)T' -1
out="$PKG_DIR/watchdog-hook.out.$stamp.$$"
python3 - "$PKG_DIR" "$out" >/dev/null 2>&1 <<'PYEOF' || true
import os
import sys
d, out = sys.argv[1], sys.argv[2]
if os.fork() > 0:
    os._exit(0)
os.setsid()
if os.fork() > 0:
    os._exit(0)
os.chdir(d)
null = os.open(os.devnull, os.O_RDWR)
os.dup2(null, 0)
try:
    fds = [int(x) for x in os.listdir("/proc/self/fd")]
except OSError:
    fds = range(3, 1024)
for fd in fds:
    if fd > 2:
        try:
            os.close(fd)
        except OSError:
            pass
script = ('bash "$1/watchdog.sh" > "$2.tmp" 2>&1; mv -f "$2.tmp" "$2"')
os.execvp("bash", ["bash", "-c", script, "watchdog-hook", d, out])
PYEOF

# Leftovers of a run that was killed mid-way (a reboot): a day old.
find "$PKG_DIR" -maxdepth 1 -name 'watchdog-hook.out.*.tmp' -mmin +1440 \
    -delete 2>/dev/null || true

for _ in $(seq 1 "$WAIT_SECS"); do
    [ -e "$out" ] && break
    sleep 1
done

alerts=""
for f in "$PKG_DIR"/watchdog-hook.out.*; do
    case $f in *.tmp) continue ;; esac
    [ -f "$f" ] || continue
    alerts+=$(grep '^ALERT:' "$f" || true)$'\n'
    rm -f "$f"
done
alerts=$(printf '%s' "$alerts" | sed '/^$/d')
if [ -z "$alerts" ]; then
    silent "watchdog OK" '{}'
fi
payload=$(jq -cn --arg a "$alerts" --arg dir "$PKG_DIR" \
    '{alerts: ($a | split("\n")), package_dir: $dir,
      guide: ($dir + "/TROUBLESHOOTING.md")}')
wake "muse-discord needs attention" "$payload"
