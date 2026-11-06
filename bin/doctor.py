#!/usr/bin/env python3
"""doctor.py -- is the bot healthy? One command, plain answers.

    python3 bin/doctor.py                    # check, then DM the owners
                                             # "I'm online"
    python3 bin/doctor.py --no-dm            # check only
    python3 bin/doctor.py --wait-mention 600 # wait for an owner's
                                             # @mention to be answered

Checks: the config and token; a worker prompt too old to reply; the
monitor process; that Discord accepted the token and the realtime
connection is up (Message Content intent on); the /settings command;
that the three hooks ran recently (each leaves a .seen-<hook> stamp in
the package dir), and a note when the optional web watch hook does
not; the last reply sent.
Each problem is printed with its fix, in plain words. Then, unless
--no-dm, it DMs each owner that the bot is online (the only thing it
ever changes), with the invite to the Muse Agent server.

--wait-mention SECONDS watches the mention ledger for an owner's next
@mention (made after this command starts) and reports how it ended:
answered, or why not (for instance, the bot cannot post in that
channel). It sends no DM.

Exit status: 0 when nothing is wrong (notes allowed), 1 when something
is, 2 for a bad config (nothing else checked). --json prints one JSON
object. Never prints the token.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import setup_common as C  # noqa: E402

HOOKS = (
    # (stamp, what it is, how often it runs, older than this = problem)
    ("mention-watch", "wake hook (hooks/mention-watch.sh)", 5, 60),
    ("update-watch", "update hook (hooks/update-watch.sh)", 60, 300),
    ("watchdog-hook", "watchdog hook (hooks/watchdog-hook.sh)", 600, 1800),
    # Optional: only web watches need it (a note, never a problem).
    ("watch-due", "web watch hook (hooks/watch-due.sh)", 60, 300),
)
HEARTBEAT_STALE_SECS = 600
ONLINE_DM = ("Hi! I'm online and ready. Mention me in any channel I can "
             "post in and I'll answer. You don't need to reply to this "
             "message.\n\n" + C.COMMUNITY_LINE)


class Doctor:
    def __init__(self, pkg, as_json):
        self.pkg = pkg
        self.as_json = as_json
        self.results = []

    def add(self, status, what, fix=None):
        self.results.append({"status": status, "what": what, "fix": fix})
        if not self.as_json:
            tag = {"ok": "OK", "note": "NOTE", "problem": "PROBLEM"}[status]
            print(f"{tag}: {what}" + (f" Fix: {fix}" if fix else ""),
                  flush=True)

    @property
    def failed(self):
        return any(r["status"] == "problem" for r in self.results)

    def path(self, name):
        return os.path.join(self.pkg, name)

    def log_tail(self, name, size=200_000):
        try:
            with open(self.path(name), "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - size))
                return f.read().decode("utf-8", "replace")
        except OSError:
            return ""

    # -- process -------------------------------------------------------
    def monitor_pid(self):
        """The live monitor.py running from this package dir, or None.
        monitor.pid first, then any `python3 monitor.py` whose working
        directory is this package (never another install's)."""
        cands = []
        try:
            with open(self.path("monitor.pid")) as f:
                cands.append(f.read().split()[0])
        except (OSError, IndexError):
            pass
        try:
            out = subprocess.run(["pgrep", "-f", "^python3 [m]onitor.py"],
                                 capture_output=True, text=True,
                                 timeout=10).stdout
            cands += out.split()
        except (OSError, subprocess.SubprocessError):
            pass
        real = os.path.realpath(self.pkg)
        for pid in cands:
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    argv = f.read().split(b"\0")
                cwd = os.path.realpath(os.readlink(f"/proc/{pid}/cwd"))
            except OSError:
                continue
            if len(argv) > 1 and argv[1].endswith(b"monitor.py") \
                    and cwd == real:
                return int(pid)
        return None


def check(d, st, args):
    # Config and token.
    if not st.owners:
        d.add("problem", "config.yaml lists no owner.",
              "run bin/connect-discord.py again with the token.")
    token = C.read_token(st)
    if not token:
        d.add("problem", "No bot token found where config.yaml says.",
              "ask the owner for a fresh token (Bot page, Reset Token, "
              "Copy) and run bin/connect-discord.py with it.")
    else:
        tf = C.token_file(st)
        if tf and os.path.exists(tf) and os.stat(tf).st_mode & 0o077:
            d.add("problem", f"The token file {tf} is readable by other "
                  f"accounts.", f"chmod 600 {tf}")

    # An outdated worker prompt: every reply refused (the monitor marks
    # it, PROTOCOL.md, Replies stay with their run).
    if os.path.exists(d.path("prompt_outdated.json")):
        d.add("problem", "The worker prompt is out of date, so the bot "
              "can't send replies (they carry no reply_nonce).",
              "follow UPDATING.md: it re-renders the prompt (python3 "
              "bin/render-worker-prompt.py with the options used before).")

    # The monitor.
    log = d.log_tail("monitor.log")
    pid = d.monitor_pid()
    if pid is None:
        if "TOKEN REJECTED" in log:
            d.add("problem", "The bot is not running: Discord rejected its "
                  "token (it was probably reset).",
                  "ask the owner for a fresh token (Bot page, Reset Token, "
                  "Copy), run bin/connect-discord.py with it, then bash "
                  "watchdog.sh.")
        else:
            d.add("problem", "The bot is not running.",
                  "run bash watchdog.sh, then this check again. If it "
                  "still fails, read the first lines of monitor.log.")
        return token
    d.add("ok", f"The bot is running (process {pid}).")
    try:
        age = time.time() - os.path.getmtime(d.path("monitor.heartbeat"))
        if age > HEARTBEAT_STALE_SECS:
            d.add("problem", f"The bot looks stuck (no heartbeat for "
                  f"{int(age // 60)} minutes).",
                  "run bash watchdog.sh; it restarts a stuck bot.")
    except OSError:
        pass
    # The latest run's log only (monitor.log is rotated on each start).
    if "TOKEN REJECTED" in log:
        d.add("problem", "Discord rejected the bot's token.",
              "ask the owner for a fresh token (Bot page, Reset Token, "
              "Copy) and run bin/connect-discord.py with it.")
    if "GATEWAY DISABLED (code 4014)" in log:
        d.add("problem", "Discord won't send the bot message text: Message "
              "Content is off.",
              "ask the owner: Developer Portal, the app, Bot, Privileged "
              "Gateway Intents, switch on Message Content Intent, click "
              "Save Changes. Then restart the bot (kill the monitor.pid "
              "process, then bash watchdog.sh).")
    elif "GATEWAY DISABLED" in log:
        d.add("problem", "The bot's realtime connection is off, so replies "
              "are slow.", "see GATEWAY DISABLED in monitor.log, and "
              "TROUBLESHOOTING.md.")
    else:
        ready = [m.start() for m in re.finditer(
            r"Gateway READY|Gateway session resumed", log)]
        closed = [m.start() for m in re.finditer(
            r"Gateway closed|Gateway error", log)]
        if ready and (not closed or ready[-1] > closed[-1]):
            d.add("ok", "Connected to Discord, and Message Content is on.")
        elif not args.quick:
            d.add("note", "Not connected to Discord's realtime feed yet "
                  "(it can take a minute after a start).")
    try:
        with open(d.path("directory.json")) as f:
            dirs = json.load(f)
        chans = [c for cs in (dirs.get("channels") or {}).values()
                 for c in cs if isinstance(c, dict) and c.get("readable")]
        servers = len(dirs.get("guilds") or [])
    except (OSError, ValueError, AttributeError):
        chans, servers = None, 0
    if chans:
        post = sum(1 for c in chans if c.get("can_send"))
        d.add("ok", f"It can read {len(chans)} channels in {servers} "
              f"server(s) and post in {post} of them.")
    elif chans is not None or "no readable channels found" in log:
        d.add("problem", "The bot can't see any channels.",
              "send the owner the invite link from bin/connect-discord.py; "
              "if it is already in their server, give its role View "
              "Channels and Send Messages.")
    failures = re.findall(r"/settings command registration failed", log)
    if failures and not re.search(r"/settings command (created|updated)",
                                  log):
        d.add("note", "/settings isn't registered yet (the bot retries "
              "every minute); see monitor.log.")

    # The hooks.
    now = time.time()
    for stamp, what, every, stale in HOOKS:
        p = d.path(f".seen-{stamp}")
        waited = 0
        while not args.quick and not os.path.exists(p) \
                and waited < min(every + 10, 70) and stamp not in \
                ("watchdog-hook", "watch-due"):
            time.sleep(1)
            waited += 1
        try:
            age = now - os.path.getmtime(p)
        except OSError:
            age = None
        if stamp == "watch-due" and (age is None or age > stale):
            d.add("note", f"The {what} isn't running, so web watches are "
                  f"never checked (each ends after a few tries and tells "
                  f"whoever set it). Register it if you want them "
                  f"(SETUP.md, step 5).")
        elif age is None and stamp == "watchdog-hook":
            d.add("note", f"The {what} hasn't run yet; it runs every 10 "
                  f"minutes. If this note is still here in 15 minutes, "
                  f"register it (SETUP.md, step 5).")
        elif age is None:
            d.add("problem", f"The {what} has never run.",
                  "register it as SETUP.md step 5 says.")
        elif age > stale:
            d.add("problem", f"The {what} last ran {int(age // 60)} "
                  f"minutes ago; it should run every {every} seconds.",
                  "check that it is still registered (SETUP.md, step 5).")
        else:
            d.add("ok", f"The {what} is running.")

    # The last reply.
    led = read_ledger(d)
    replied = [e for e in led.values() if e.get("status") == "replied"]
    if replied:
        last = max(e.get("updated", 0) for e in replied)
        d.add("ok", f"Last reply sent {ago(now - last)}.")
    return token


def ago(secs):
    secs = max(0, int(secs))
    if secs < 120:
        return f"{secs} seconds ago"
    if secs < 7200:
        return f"{secs // 60} minutes ago"
    return f"{secs // 3600} hours ago"


def read_ledger(d):
    try:
        with open(d.path("mention_ledger.json")) as f:
            data = json.load(f)
        ents = data.get("entries") if isinstance(data, dict) else None
        return ents if isinstance(ents, dict) else {}
    except (OSError, ValueError):
        return {}


def online_dm(d, st, token):
    for uid in st.owners:
        try:
            ch = C.call("POST", "/users/@me/channels", token,
                        {"recipient_id": str(uid)})
            C.call("POST", f"/channels/{ch['id']}/messages", token,
                   {"content": ONLINE_DM,
                    "allowed_mentions": {"parse": []}})
            d.add("ok", f"Sent owner {uid} the \"I'm online\" DM.")
        except (C.ApiError, KeyError, TypeError) as e:
            code = getattr(e, "code", None)
            if code == 50007:
                d.add("problem", f"Couldn't DM owner {uid}: their privacy "
                      f"settings block DMs from this server's members.",
                      "ask them: in Discord, right-click the server icon, "
                      "Privacy Settings, switch on Direct Messages.")
            else:
                d.add("problem", f"Couldn't DM owner {uid} ({e}).",
                      "make sure the owner is in a server with the bot.")


REASONS = {
    "no-send": ("It can't post in that channel.",
                "give the bot Send Messages there, or mention it in "
                "another channel."),
    "replies-off": ("Replies are off there.",
                    "DM the bot \"answer when I mention you\", or use "
                    "/settings."),
}


def wait_mention(d, st, secs):
    start = time.time()
    owners = {str(o) for o in st.owners}
    deadline = start + secs
    seen_status = None
    while time.time() < deadline:
        mine = [(e.get("updated", 0), mid, e)
                for mid, e in read_ledger(d).items()
                if str(e.get("author_id")) in owners
                and e.get("updated", 0) >= start and not e.get("dm")]
        if mine:
            _, mid, e = max(mine)
            status = e.get("status")
            if status == "replied":
                d.add("ok", "The bot answered the owner's mention.")
                return
            if status in ("skipped", "quarantined", "enqueue_failed"):
                what, fix = REASONS.get(e.get("code"), (
                    f"It didn't answer ({e.get('reason') or status}).",
                    "see TROUBLESHOOTING.md."))
                d.add("problem", what, fix)
                return
            seen_status = status
        time.sleep(2)
    if seen_status:
        d.add("problem", "The bot saw the mention but hasn't answered yet.",
              "check that the wake hook is registered with the worker "
              "prompt (SETUP.md, step 5).")
    else:
        d.add("problem", "No mention from the owner reached the bot.",
              "make sure they typed @ and picked the bot from the list, in "
              "a channel it can read.")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check the bot's health.")
    ap.add_argument("--no-dm", action="store_true",
                    help="don't send the owners the \"I'm online\" DM")
    ap.add_argument("--wait-mention", type=float, metavar="SECONDS",
                    help="wait for an owner's @mention to be answered")
    ap.add_argument("--quick", action="store_true",
                    help="don't wait for the hooks to show up")
    ap.add_argument("--dir", default=C.PKG_DIR, help=argparse.SUPPRESS)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    d = Doctor(os.path.abspath(args.dir), args.json)
    cfg = os.environ.get("MUSE_DISCORD_CONFIG") \
        or d.path("config.yaml")
    MS = C.settings_module()
    try:
        st = MS.load(cfg, os.path.join(d.pkg, "plugins"))
    except MS.SettingsError as e:
        d.add("problem", f"config.yaml is not valid: {e}",
              "python3 bin/settings.py validate names each problem.")
        code = 2
    else:
        if args.wait_mention is not None:
            wait_mention(d, st, args.wait_mention)
        else:
            token = check(d, st, args)
            if token and not args.no_dm and not d.failed:
                online_dm(d, st, token)
        code = 1 if d.failed else 0
    if args.json:
        print(json.dumps({"ok": code == 0, "results": d.results}, indent=1))
    elif code == 0:
        print("All good.")
    return code


if __name__ == "__main__":
    sys.exit(main())
