#!/usr/bin/env bash
# muse-discord web watch checker hook (watch-due).
# Polls web_watches_due.json, which the monitor writes (mode 600) with
# the web watch checks that are due, each with a one-time nonce. Zero
# tokens otherwise: this script never touches the network and holds no
# secrets. When checks are listed that no checker has been handed yet,
# it wakes ONE checker session for the checks of ONE requester (their
# entries share a `group`; one without is a group of its own), so a web
# page one person points the checker at never shares a run with anyone
# else's checks and nonces. Other requesters' checks go to the next
# runs, a poll apart. The checker's whole prompt is
# the fixed watch-checker-prompt.template.md rendered by
# bin/render-checker-prompt.py, with the checks embedded as a delimited
# data block. The checker may only search and read the web and report
# each check with bin/watch-report.py; it never sees the Discord queue
# and never writes to Discord (PROTOCOL.md, Web watches).
#
# Leases: each check (its key, "<id>:<issued>") is handed out once, in
# hook state; a wake carries only checks no live lease covers, so no
# check is done twice. A lease lasts LEASE_SECS, the nonce's lifetime: a
# check nobody reports on expires in the monitor, which counts a failure
# and later issues a new check (a new key).
#
# Like hooks/mention-watch.sh, this targets Muse's hook runtime (it
# sources $HATCH_HOOK_RUNTIME for the silent/wake/log/hook_state
# helpers). Register it as SETUP.md step 5's table says: every 60 s, in
# a chat of its own (never the owner's main chat or the reply worker's).
set -euo pipefail
# shellcheck source=/dev/null
source "$HATCH_HOOK_RUNTIME"

PKG_DIR="${MUSE_DISCORD_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# The package's own Python (bin/install.sh may have made a .venv, and
# marks it .muse-ok once the packages import there).
if [ -x "$PKG_DIR/.venv/bin/python3" ] && [ -e "$PKG_DIR/.venv/.muse-ok" ]; then
    PATH="$PKG_DIR/.venv/bin:$PATH"
fi
# Proof that this hook runs (never fails the hook).
touch "$PKG_DIR/.seen-watch-due" 2>/dev/null || true
DUE="$PKG_DIR/web_watches_due.json"
LEASE_SECS=900

if [ ! -f "$DUE" ]; then
    silent "no web watch checks due" '{}'
fi
entries=$(jq -c '[.[]? | objects | select((.id | type) == "string"
        and (.key | type) == "string" and (.nonce | type) == "string"
        and (.what | type) == "string")]' "$DUE" 2>/dev/null) \
    || entries='[]'
[ -n "$entries" ] || entries='[]'
if [ "$(printf '%s' "$entries" | jq 'length')" -eq 0 ]; then
    silent "no web watch checks due" '{}'
fi

state=$(hook_state_get 2>/dev/null \
    | jq -c 'if type == "object" then . else {} end' 2>/dev/null) \
    || state='{}'
[ -n "$state" ] || state='{}'
printf -v now '%(%s)T' -1
leased=$(printf '%s' "$state" | jq -c '.leased // {}' 2>/dev/null) \
    || leased='{}'

# Due: listed checks with no live lease, of the first such requester.
due=$(jq -cn --argjson e "$entries" --argjson l "$leased" \
    --argjson now "$now" --argjson lease "$LEASE_SECS" \
    '[$e[] | select((($l[.key] // 0) + $lease) <= $now)
        | . + {g: (.group // .key)}]
    | (.[0].g // null) as $g | [.[] | select(.g == $g) | del(.g)]')
ndue=$(printf '%s' "$due" | jq 'length')
if [ "$ndue" -eq 0 ]; then
    silent "a checker holds every listed check" '{}'
fi

prompt=$(printf '%s' "$due" \
    | python3 "$PKG_DIR/bin/render-checker-prompt.py" --due - 2>/dev/null) \
    || prompt=""
if [ -z "$prompt" ]; then
    log "could not render the checker prompt" '{}'
    silent "checker prompt unavailable" '{}'
fi

# Leases: keep those of checks still listed, stamp the ones handed out.
# hook_state_set is a no-op on dry runs, so testing never consumes one.
hook_state_set "$(jq -cn --argjson st "$state" --argjson e "$entries" \
    --argjson l "$leased" --argjson due "$due" --argjson now "$now" '
    $st + {leased: (($l | with_entries(select(.key as $k
            | any($e[]; .key == $k))))
        + ($due | map({key: .key, value: $now}) | from_entries))}')"
wake "muse-discord web watch checks due" "$prompt"
