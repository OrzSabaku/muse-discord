"""Shared paths and scratch dirs for the offline suites.

Every suite puts this directory on sys.path first thing:

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "_lib"))

MUT_DIR is the package tree under test (default: this checkout)."""
import atexit
import os
import shutil
import sys
import tempfile

# Never leave __pycache__ behind in the tree under test (or here).
sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
# Scratch git repos must not pick up the user's config (commit signing,
# hooks, templates); the same goes for every git the code under test runs.
os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
# Usage stats (telemetry.py) are never sent from a test, whatever the
# endpoint; the telemetry suite turns them on per Recorder, at a fake.
os.environ["MUSE_DISCORD_TELEMETRY"] = "off"
os.environ["MUSE_DISCORD_TELEMETRY_URL"] = ""   # "" = no endpoint at all

LIB = os.path.dirname(os.path.abspath(__file__))
TESTS = os.path.dirname(LIB)
REPO = os.path.dirname(TESTS)
MUT_DIR = os.path.abspath(os.environ.get("MUT_DIR") or REPO)
os.environ["MUT_DIR"] = MUT_DIR   # subprocesses see the same tree

FAKEWS = os.path.join(LIB, "fakews")          # offline `websockets`
FAKE_PROC = os.path.join(LIB, "fake_proc.py")  # monitor/presence stand-in
HOOK_RUNTIME = os.path.join(LIB, "hook_runtime.sh")  # hatch hook stubs

_KEEP = os.environ.get("MUSE_TESTS_KEEP_TMP") == "1"


def scratch(prefix="muse-test-"):
    """A fresh temp dir, removed when this process exits (unless
    MUSE_TESTS_KEEP_TMP=1, for debugging)."""
    d = tempfile.mkdtemp(prefix=prefix)
    if not _KEEP:
        atexit.register(shutil.rmtree, d, True)
    return d


def copy_watchdog(dst_dir):
    """Copy the tree's watchdog.sh into dst_dir with its 5 s health poll
    (wait_healthy) shortened to 0.2 s: the fakes are healthy at once, so
    only the waiting changes, not what is checked. Returns the path."""
    with open(os.path.join(MUT_DIR, "watchdog.sh")) as f:
        text = f.read()
    dst = os.path.join(dst_dir, "watchdog.sh")
    with open(dst, "w") as f:
        f.write(text.replace("    sleep 5\n", "    sleep 0.2\n"))
    shutil.copymode(os.path.join(MUT_DIR, "watchdog.sh"), dst)
    return dst


def redirect_files(mod, tmp):
    """Point every *_FILE path the module keeps under its own dir
    (BASE_DIR) at tmp instead, so a test never writes into the tree under
    test. Call it right after importing; set specific paths after."""
    base = getattr(mod, "BASE_DIR", None)
    if not base:
        return
    for name in dir(mod):
        val = getattr(mod, name)
        if (name.endswith("_FILE") and isinstance(val, str)
                and val.startswith(base + os.sep)):
            setattr(mod, name, os.path.join(tmp, os.path.relpath(val, base)))


def signed(mod, state, item):
    """An outbox item as the wake holding its queue item writes it: with
    that item's reply_nonce (monitor.py mints one for every item it
    queues; minted here too for an item a test queued by hand). A
    reply_nonce the item already sets is kept, and reply_nonce=None
    drops it (a run that was not handed the item)."""
    item = dict(item)
    if "reply_nonce" in item:
        if item["reply_nonce"] is None:
            del item["reply_nonce"]
        return item
    qid = mod._answered_id(state, item)
    if qid:
        frags = mod.queue_index(state)["frags"]
        item["reply_nonce"] = mod.reply_nonce_mint(
            state, mod.trigger_of(state, qid, frags))
    return item
