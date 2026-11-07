#!/usr/bin/env bash
# install.sh -- check and install what muse-discord needs (docs/SETUP.md,
# step 1). Safe to run any number of times; it never touches git, the
# config or the token.
#
#   bash bin/install.sh
#
# 1. Python 3.10 or newer, uv, and jq (the hooks use it).
# 2. The Python packages in pyproject.toml, installed by uv into .venv.
#    When .venv is used, watchdog.sh, the hooks and the rendered worker
#    prompt all run .venv/bin/python3, so nothing runs without the packages.
# 3. Optional tools, listed if missing: ffmpeg/ffprobe (video frames),
#    pdftotext (PDF text), curl (downloads).
# 4. ~/.config/muse-discord/incoming (mode 700): where the Muse writes
#    the pasted token for bin/connect-discord.py --token-file.
#
# Prints one line per check and a last line "INSTALL OK" or
# "INSTALL FAILED: <what to do>"; exits 0 or 1.
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$DIR/.venv"
fail() { echo "INSTALL FAILED: $*"; exit 1; }

# ---- Python ----
# .venv counts only with its .muse-ok marker (written below once the
# packages import there); the watchdog, hooks and scripts check it too.
command -v python3 >/dev/null 2>&1 || fail "python3 is not installed. Install Python 3.10 or newer."
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
  fail "Python 3.10 or newer is needed; python3 is $(python3 -c 'import platform; print(platform.python_version())')."
fi
echo "OK python3 $(python3 -c 'import platform; print(platform.python_version())')"

# ---- uv ----
if command -v uv >/dev/null 2>&1; then
  echo "OK uv $(uv --version | awk '{print $2}')"
else
  fail "uv is not installed. Install it from https://docs.astral.sh/uv/getting-started/installation/, then run this again."
fi

# ---- jq ----
if command -v jq >/dev/null 2>&1; then
  echo "OK jq"
else
  fail "jq is not installed (the hooks need it). Install it with the system's package manager, e.g. apt-get install jq, then run this again."
fi

# ---- Python packages ----
have_pkgs() { "$1" -c 'import yaml; from pydantic import TypeAdapter; from discord.ui import LayoutView; import discord; assert (2,7,1) <= discord.version_info[:3] < (2,8,0)' >/dev/null 2>&1; }
if ! (cd "$DIR" && uv sync --locked --no-dev --quiet); then
  fail "uv could not install the project into $VENV; check the network, then run this again."
fi
have_pkgs "$VENV/bin/python3" || fail "the packages still don't import from $VENV."
: > "$VENV/.muse-ok"
echo "OK Python packages installed in $VENV (the watchdog, hooks and worker prompt use it)"

# ---- the token drop-off ----
INCOMING="$HOME/.config/muse-discord/incoming"
{ mkdir -p "$INCOMING" && chmod 700 "$HOME/.config/muse-discord" "$INCOMING"; } \
  || fail "could not make $INCOMING"
echo "OK $INCOMING (only this account can read it)"

# ---- optional tools ----
missing=()
for t in ffmpeg ffprobe pdftotext curl; do
  command -v "$t" >/dev/null 2>&1 || missing+=("$t")
done
if [ ${#missing[@]} -gt 0 ]; then
  echo "NOTE optional, not installed: ${missing[*]} (video frames, PDF text and downloads work better with them)"
fi
echo "INSTALL OK"
