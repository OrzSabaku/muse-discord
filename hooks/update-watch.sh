#!/usr/bin/env bash
# muse-discord update watcher (update wake hook).
# Wakes the owner's Muse, in its main chat, after an owner pressed
# [Update now] on the bot's update DM: the monitor then writes
# update_request.json ({version, requested_at, by}; version is a release
# tag such as v1.2.0) in the package dir.
# Zero tokens otherwise: this script only reads that one file. It never
# runs git or pulls, and it holds no secrets; the woken Muse does the
# update to that exact release by following UPDATING.md and deletes the
# file when it is done (or stops early).
#
# Each request is handed out once: a lease in hook state, keyed by the
# request's version and time. A request still on disk LEASE_SECS after
# its wake (the woken run died before deleting it) is handed out again.
#
# Like hooks/mention-watch.sh, this targets Muse's hook runtime (it
# sources $HATCH_HOOK_RUNTIME for the silent/wake/log/hook_state
# helpers). Register it with the wake prompt in SETUP.md (step 5),
# delivered to the owner's main chat.
set -euo pipefail
# shellcheck source=/dev/null
source "$HATCH_HOOK_RUNTIME"

PKG_DIR="${MUSE_DISCORD_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# Proof for bin/doctor.py that this hook runs (never fails the hook).
touch "$PKG_DIR/.seen-update-watch" 2>/dev/null || true
REQUEST="$PKG_DIR/update_request.json"
LEASE_SECS=21600

if [ ! -f "$REQUEST" ]; then
    silent "no update requested" '{}'
fi
# Only a release tag (vX.Y.Z) is handed on: the woken Muse puts it in git
# commands.
req=$(jq -c 'select(type == "object" and (.version | type) == "string"
        and (.version | test("^v[0-9]+\\.[0-9]+\\.[0-9]+$")))
    | {version, requested_at, by}' "$REQUEST" 2>/dev/null) || req=""
if [ -z "$req" ]; then
    silent "update request unreadable" '{}'
fi

key=$(printf '%s' "$req" | jq -r '"\(.version):\(.requested_at)"')
now=$(date +%s)
at=$(hook_state_get | jq -r --arg k "$key" \
    '(.handed // {})[$k] // 0 | numbers | floor' 2>/dev/null) || at=0
at=${at:-0}
if [ $((at + LEASE_SECS)) -gt "$now" ]; then
    silent "update already handed out" '{}'
fi
if [ "$at" -gt 0 ]; then
    log "re-waking: update request still open after its lease" '{}'
fi

# Only the current request's lease is kept.
# hook_state_set is a no-op on dry runs, so testing never consumes it.
hook_state_set "$(jq -cn --arg k "$key" --argjson now "$now" \
    '{handed: {($k): $now}}')"
payload=$(jq -cn --argjson r "$req" --arg dir "$PKG_DIR" \
    '{request: $r, package_dir: $dir, guide: ($dir + "/UPDATING.md")}')
wake "muse-discord update requested" "$payload"
