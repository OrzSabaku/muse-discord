"""Inspect configured credentials, monitor health, gateway logs, directory and hooks.

Checks report problems and fixes through a diagnostic session. The returned token
is passed explicitly to optional owner follow-up; checks send no owner messages.
"""

import json
import os
import re
import time

from muse_discord.diagnostics.context import ago, read_ledger
from muse_discord.setup import support as C

HOOKS = (
    # (stamp, what it is, how often it runs, older than this = problem)
    ("mention-watch", "wake hook (hooks/mention-watch.sh)", 5, 60),
    ("update-watch", "update hook (hooks/update-watch.sh)", 60, 300),
    ("watchdog-hook", "watchdog hook (hooks/watchdog-hook.sh)", 600, 1800),
    # Optional: only web watches need it (a note, never a problem).
    ("watch-due", "web watch hook (hooks/watch-due.sh)", 60, 300),
)


HEARTBEAT_STALE_SECS = 600


def check(d, st, args):
    token = check_configuration(d, st)
    # The monitor.
    log = d.log_tail("monitor.log")
    pid = d.monitor_pid()
    if pid is None:
        if "TOKEN REJECTED" in log:
            d.add(
                "problem",
                "The bot is not running: Discord rejected its token (it was probably reset).",
                "ask the owner for a fresh token (Bot page, Reset Token, "
                "Copy), run bin/connect-discord.py with it, then bash "
                "watchdog.sh.",
            )
        else:
            d.add(
                "problem",
                "The bot is not running.",
                "run bash bin/watchdog.sh, then this check again. If it "
                "still fails, read the first lines of monitor.log.",
            )
        return token
    d.add("ok", f"The bot is running (process {pid}).")
    try:
        age = time.time() - os.path.getmtime(d.path("monitor.heartbeat"))
        if age > HEARTBEAT_STALE_SECS:
            d.add(
                "problem",
                f"The bot looks stuck (no heartbeat for {int(age // 60)} minutes).",
                "run bash bin/watchdog.sh; it restarts a stuck bot.",
            )
    except OSError:
        pass
    check_gateway(d, log, args.quick)
    check_directory(d, log)

    now = check_hooks(d, args)

    # The last reply.
    led = read_ledger(d)
    replied = [e for e in led.values() if e.get("status") == "replied"]
    if replied:
        last = max(e.get("updated", 0) for e in replied)
        d.add("ok", f"Last reply sent {ago(now - last)}.")
    return token


def check_gateway(d, log, quick):
    # The latest run's log only (monitor.log is rotated on each start).
    if "TOKEN REJECTED" in log:
        d.add(
            "problem",
            "Discord rejected the bot's token.",
            "ask the owner for a fresh token (Bot page, Reset Token, "
            "Copy) and run bin/connect-discord.py with it.",
        )
    if "GATEWAY DISABLED (code 4014)" in log:
        d.add(
            "problem",
            "Discord won't send the bot message text: Message Content is off.",
            "ask the owner: Developer Portal, the app, Bot, Privileged "
            "Gateway Intents, switch on Message Content Intent, click "
            "Save Changes. Then restart the bot (kill the monitor.pid "
            "process, then bash bin/watchdog.sh).",
        )
    elif "GATEWAY DISABLED" in log:
        d.add(
            "problem",
            "The bot's realtime connection is off, so replies are slow.",
            "see GATEWAY DISABLED in monitor.log, and docs/UPDATING.md.",
        )
    else:
        ready = [m.start() for m in re.finditer(r"Gateway READY|Gateway session resumed", log)]
        closed = [m.start() for m in re.finditer(r"Gateway closed|Gateway error", log)]
        if ready and (not closed or ready[-1] > closed[-1]):
            d.add("ok", "Connected to Discord, and Message Content is on.")
        elif not quick:
            d.add(
                "note",
                "Not connected to Discord's realtime feed yet "
                "(it can take a minute after a start).",
            )


def check_configuration(d, st):
    # Config and token.
    if not st.owners:
        d.add(
            "problem",
            "config.yaml lists no owner.",
            "run bin/connect-discord.py again with the token.",
        )
    token = C.read_token(st)
    if not token:
        d.add(
            "problem",
            "No bot token found where config.yaml says.",
            "ask the owner for a fresh token (Bot page, Reset Token, "
            "Copy) and run bin/connect-discord.py with it.",
        )
    else:
        tf = C.token_file(st)
        if tf and os.path.exists(tf) and os.stat(tf).st_mode & 0o077:
            d.add(
                "problem", f"The token file {tf} is readable by other accounts.", f"chmod 600 {tf}"
            )

    # An outdated worker prompt: every reply refused (the monitor marks
    # it, docs/PROTOCOL.md, Replies stay with their run).
    if os.path.exists(d.path("prompt_outdated.json")):
        d.add(
            "problem",
            "The worker prompt is out of date, so the bot "
            "can't send replies (they carry no reply_nonce).",
            "follow docs/UPDATING.md: it re-renders the prompt (python3 "
            "bin/render-worker-prompt.py with the options used before).",
        )

    return token


def check_directory(d, log):
    try:
        with open(d.path("directory.json")) as f:
            dirs = json.load(f)
        chans = [
            c
            for cs in (dirs.get("channels") or {}).values()
            for c in cs
            if isinstance(c, dict) and c.get("readable")
        ]
        servers = len(dirs.get("guilds") or [])
    except OSError, ValueError, AttributeError:
        chans, servers = None, 0
    if chans:
        post = sum(1 for c in chans if c.get("can_send"))
        d.add(
            "ok",
            f"It can read {len(chans)} channels in {servers} server(s) and post in {post} of them.",
        )
    elif chans is not None or "no readable channels found" in log:
        d.add(
            "problem",
            "The bot can't see any channels.",
            "send the owner the invite link from bin/connect-discord.py; "
            "if it is already in their server, give its role View "
            "Channels and Send Messages.",
        )
    failures = re.findall(r"/settings command registration failed", log)
    if failures and not re.search(r"/settings command (created|updated)", log):
        d.add(
            "note",
            "/settings isn't registered yet (the bot retries every minute); see monitor.log.",
        )


def check_hooks(d, args):
    # The hooks.
    now = time.time()
    for stamp, what, every, stale in HOOKS:
        p = d.path(f".seen-{stamp}")
        waited = 0
        while (
            not args.quick
            and not os.path.exists(p)
            and waited < min(every + 10, 70)
            and stamp not in ("watchdog-hook", "watch-due")
        ):
            time.sleep(1)
            waited += 1
        try:
            age = now - os.path.getmtime(p)
        except OSError:
            age = None
        if stamp == "watch-due" and (age is None or age > stale):
            d.add(
                "note",
                f"The {what} isn't running, so web watches are "
                f"never checked (each ends after a few tries and tells "
                f"whoever set it). Register it if you want them "
                f"(docs/SETUP.md).",
            )
        elif age is None and stamp == "watchdog-hook":
            d.add(
                "note",
                f"The {what} hasn't run yet; it runs every 10 "
                f"minutes. If this note is still here in 15 minutes, "
                f"register it (docs/SETUP.md).",
            )
        elif age is None:
            d.add("problem", f"The {what} has never run.", "register it as docs/SETUP.md says.")
        elif age > stale:
            d.add(
                "problem",
                f"The {what} last ran {int(age // 60)} "
                f"minutes ago; it should run every {every} seconds.",
                "check that it is still registered (docs/SETUP.md).",
            )
        else:
            d.add("ok", f"The {what} is running.")

    return now
