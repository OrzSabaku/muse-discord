#!/usr/bin/env bash
# Run the offline test suites (tests/<suite>/test_*.py) against a tree.
#
# Usage: tests/run_all.sh [-j N] [--skip-slow] [TREE] [suite...]
#
#   TREE         package tree under test (a directory with monitor.py);
#                default: this checkout
#   suite...     suite names (directories under tests/); default: all
#   -j N         run N suites at a time (default: $JOBS, else 4)
#   --skip-slow  leave out suites marked slow (a SLOW file in the suite)
#
# Every tests/<suite>/test_*.py is run as
#   MUT_DIR=TREE python3 tests/<suite>/test_*.py
# A suite passes when each of its files exits 0. Prints one line per
# suite and the log of each failed one; exits non-zero on any failure.
set -u

TESTS=$(cd "$(dirname "$0")" && pwd)
REPO=$(dirname "$TESTS")
JOBS=${JOBS:-4}
SKIP_SLOW=0

while [ $# -gt 0 ]; do
  case $1 in
    -j) JOBS=$2; shift 2 ;;
    -j*) JOBS=${1#-j}; shift ;;
    --skip-slow) SKIP_SLOW=1; shift ;;
    -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) break ;;
  esac
done

TREE=$REPO
if [ $# -gt 0 ] && [ -d "$1" ] && [ ! -d "$TESTS/$1" ]; then
  TREE=$1; shift
fi
TREE=$(cd "$TREE" && pwd) || exit 2
if [ ! -f "$TREE/monitor.py" ]; then
  echo "run_all.sh: $TREE has no monitor.py" >&2
  exit 2
fi

all_suites() {
  local d
  for d in "$TESTS"/*/; do
    d=${d%/}
    case $(basename "$d") in _*) continue ;; esac
    compgen -G "$d/test_*.py" >/dev/null && basename "$d"
  done
}

if [ $# -gt 0 ]; then
  SUITES=("$@")
else
  mapfile -t SUITES < <(all_suites)
fi

# Slow suites first, so they overlap with the quick ones.
ordered=()
for s in "${SUITES[@]}"; do
  if [ ! -d "$TESTS/$s" ] || ! compgen -G "$TESTS/$s/test_*.py" >/dev/null; then
    echo "run_all.sh: no suite named '$s' (tests/$s/test_*.py)" >&2
    exit 2
  fi
  if [ -f "$TESTS/$s/SLOW" ]; then
    [ "$SKIP_SLOW" = 1 ] && { echo "skip  $s (slow)"; continue; }
    ordered=("$s" "${ordered[@]}")
  else
    ordered+=("$s")
  fi
done

LOGS=$(mktemp -d "${TMPDIR:-/tmp}/muse-run-all.XXXXXX")
trap 'rm -rf "$LOGS"' EXIT
export PYTHONDONTWRITEBYTECODE=1

# One suite: each test file in turn; writes <suite>.log and <suite>.res
# ("<rc> <tests> <seconds> <skipped>").
run_suite() {
  local s=$1 f rc=0 n=0 skipped=0 start end out
  start=$(date +%s)
  for f in "$TESTS/$s"/test_*.py; do
    echo "=== ${f#"$REPO"/}" >> "$LOGS/$s.log"
    if ! out=$(cd "$TESTS/$s" && MUT_DIR="$TREE" python3 "$f" 2>&1); then
      rc=1
    fi
    printf '%s\n' "$out" >> "$LOGS/$s.log"
    n=$((n + $(printf '%s\n' "$out" | sed -n 's/^Ran \([0-9]*\) tests\{0,1\} in .*/\1/p' | tail -1 | grep . || echo 0)))
    skipped=$((skipped + $(printf '%s\n' "$out" | sed -n 's/.*skipped=\([0-9]*\).*/\1/p' | tail -1 | grep . || echo 0)))
  done
  end=$(date +%s)
  echo "$rc $n $((end - start)) $skipped" > "$LOGS/$s.res"
}

echo "Offline suites against $TREE (${#ordered[@]} suites, $JOBS at a time)"
t0=$(date +%s)
running=0
for s in "${ordered[@]}"; do
  if [ "$running" -ge "$JOBS" ]; then
    wait -n
    running=$((running - 1))
  fi
  run_suite "$s" &
  running=$((running + 1))
done
wait

failed=()
total=0
printf '\n%-16s %6s %8s  %s\n' SUITE TESTS SECONDS RESULT
for s in "${ordered[@]}"; do
  read -r rc n secs skipped < "$LOGS/$s.res"
  total=$((total + n))
  result=ok
  [ "$skipped" -gt 0 ] && result="ok ($skipped skipped)"
  [ -f "$TESTS/$s/SLOW" ] && result="$result [slow]"
  if [ "$rc" -ne 0 ]; then
    result=FAILED
    failed+=("$s")
  fi
  printf '%-16s %6s %8s  %s\n' "$s" "$n" "$secs" "$result"
done
printf '%-16s %6s %8s\n' total "$total" "$(($(date +%s) - t0))"

for s in "${failed[@]}"; do
  echo
  echo "######## $s (log)"
  cat "$LOGS/$s.log"
done
if [ ${#failed[@]} -gt 0 ]; then
  echo
  echo "FAILED: ${failed[*]}"
  exit 1
fi
echo "All suites passed."
