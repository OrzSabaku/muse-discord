"""Offline harness for the locking suite: imports $MUT_DIR's monitor.py
with every network call stubbed and every state file pointed at a fresh
scratch package dir."""
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import time
import contextlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "_lib"))
from common import MUT_DIR as WORKTREE, redirect_files, scratch  # noqa: E402

ARGV = list(sys.argv)   # before the monitor import overwrites it
SCRATCH = scratch("muse-locking-")
CFG = os.path.join(SCRATCH, "config.yaml")
with open(CFG, "w") as f:
    f.write("bot:\n  token_file: /nonexistent\nbehavior:\n"
            "  coalesce_seconds: 5\n")
os.environ["MUSE_DISCORD_CONFIG"] = CFG
sys.argv = ["x", "dummy-token"]

spec = importlib.util.spec_from_file_location(
    "monitor", os.path.join(WORKTREE, "monitor.py"))
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)
redirect_files(monitor, SCRATCH)

CALLS = []  # (time, kind, path, data)


def fake_post(path, data):
    CALLS.append((time.time(), "post", path, data))
    if path.endswith("/messages"):
        return {"id": str(len(CALLS))}
    return {}


def fake_get(path):
    # The owner-DM gate looks up a channel it cannot place (these tests
    # send to made-up channels): a server text channel.
    m = re.fullmatch(r"/channels/(\d+)", path)
    if m:
        return {"id": m[1], "type": 0, "guild_id": "1"}
    raise AssertionError(f"unexpected api_get {path}")


monitor.api_post = fake_post
monitor.api_get = fake_get
monitor.add_reaction = lambda cid, mid, emoji: CALLS.append(
    (time.time(), "react", mid, emoji)) or True
monitor.collect_media = lambda msg: ([], [], [])


def fresh_pkg():
    """A new scratch package dir with the monitor's files pointed at it and
    a copy of bin/outbox-append.py inside (the helper resolves the
    package dir from its own location)."""
    d = scratch("muse-locking-pkg-")
    os.makedirs(os.path.join(d, "bin"))
    shutil.copy(os.path.join(WORKTREE, "bin", "outbox-append.py"),
                os.path.join(d, "bin"))
    for attr, name in [("STATE_FILE", "state.json"),
                       ("QUEUE_FILE", "reply_queue.json"),
                       ("OUTBOX_FILE", "reply_outbox.json"),
                       ("QUARANTINE_FILE", "quarantine.json"),
                       ("LEDGER_FILE", "mention_ledger.json"),
                       ("PIDFILE", "monitor.pid"),
                       ("LOCK_FILE", "reply.lock"),
                       ("OUTBOX_PROCESSING_FILE",
                        "reply_outbox.processing.json"),
                       ("OUTBOX_REJECTED_FILE",
                        "reply_outbox.rejected.json")]:
        setattr(monitor, attr, os.path.join(d, name))
    monitor.MEDIA_DIR = os.path.join(d, "media")
    monitor.GENERATED_DIR = os.path.join(d, "generated")
    CALLS.clear()
    getattr(monitor, "_typing_sources", {}).clear()
    getattr(monitor, "_typing_last", {}).clear()
    getattr(monitor, "_activity", {}).clear()
    monitor._rl_until = 0.0
    monitor.BOT_ID = monitor.BOT_USER_ID = "999"
    monitor.BOT_NAME = "pip"
    return d


def new_state():
    st = {"last_seen": {}, "replied_to": [],
          "rate": {"day": "", "day_count": 0, "users": {}},
          "history": {}, "guild_joined": {"G": 0}}
    return st


def snowflake(t=None):
    ms = int((t if t is not None else time.time()) * 1000)
    snowflake.n = getattr(snowflake, "n", 0) + 1
    return str(((ms - 1420070400000) << 22) + snowflake.n)


def msg(mid=None, cid="101", author="U1", name="alice", content="hi @pip",
        mention=True, bot=False, gid="G"):
    m = {"id": mid or snowflake(), "channel_id": cid,
         "author": {"id": author, "username": name, "bot": bot},
         "content": content, "timestamp": "2026-10-17T00:00:00+00:00",
         "mentions": [{"id": "999"}] if mention else []}
    if gid:
        m["guild_id"] = gid
    return m


def read(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def write(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f)


@contextlib.contextmanager
def capture():
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        yield buf
    finally:
        sys.stdout = old


def sent_texts():
    return [c[3]["content"] for c in CALLS
            if c[1] == "post" and c[2].endswith("/messages")]


def typing_calls():
    return [c for c in CALLS if c[1] == "post" and c[2].endswith("/typing")]
