# shellcheck shell=bash
# Stand-ins for the hatch hook runtime that hooks/*.sh source through
# $HATCH_HOOK_RUNTIME: each verdict is printed instead of acted on, and
# the hook's state lives in the file $HOOK_STATE.
silent() { echo "SILENT: $1 $2"; exit 0; }
wake() { echo "WAKE: $1"; echo "$2"; exit 0; }
log() { echo "LOG: $1 $2" >&2; }
hook_state_get() { cat "$HOOK_STATE" 2>/dev/null || echo '{}'; }
hook_state_set() { printf '%s' "$1" > "$HOOK_STATE"; }
