#!/bin/bash
# Stand-in watchdog.sh: records each call (args, session id), stays busy
# for FAKE_WD_SECS, then records that it finished.
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
read -r -a st < "/proc/$$/stat"
echo "start pid=$$ sid=${st[5]} args=$*" >> "$D/wd_calls"
sleep "${FAKE_WD_SECS:-3}"
echo "done pid=$$" >> "$D/wd_calls"
