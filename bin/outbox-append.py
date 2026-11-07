#!/usr/bin/env python3
"""outbox-append.py -- the reply worker's only write to the outbox.

The worker runs it once per queue item, as soon as that item's reply is
drafted. Reads a JSON array of outbox items (or a single item) from
stdin, or from the file named as the only argument, and appends them to
the package's reply_outbox.json under the shared lock (reply.lock),
re-reading the file inside the lock. The monitor takes items out of the
outbox while it sends them, so a worker that rewrites the whole file from
a copy it read earlier can delete replies or resurrect sent ones;
appending through this tool cannot.

    python3 bin/outbox-append.py <<'EOF'
    [{"item": "123", "channel_id": "456", "reply_to": "123", "text": "hi"}]
    EOF

Nothing is appended if any item has an obvious mistake (the monitor
would set it aside unsent); the errors are printed so the worker can fix
them and run it again. Settings actions are rejected; use /settings or
bin/settings.py. A `schedule` action uses muse_watches.shape_problems;
a `proactive` opt-out / opt-in is "opt-out" or "opt-in" on an item with
`item`. An item for an owner's DM
carries that DM's `owner_dm_nonce` from the wake payload, passed
through as is. A `reply_nonce` or `owner_dm_nonce` that differs from
the live one in the monitor's nonce files is refused here, so a worker
that mis-copied it finds out at once. The monitor re-validates every
item on its side. Never needs the bot token and never touches the
network.
"""

import fcntl
import json
import os
import re
import sys
import tempfile

PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PACKAGE_DIR, "src"))
from muse_discord import muse_watches, reply_protocol  # noqa: E402 (local import path bootstrap)

OUTBOX = os.path.join(PACKAGE_DIR, "reply_outbox.json")
LOCK = os.path.join(PACKAGE_DIR, "reply.lock")


NONCE_RE = re.compile(r"[0-9a-f]{32,64}")


def _live(name):
    """The monitor's live nonces from one of its files ({message id:
    nonce}); {} when unreadable. Every session can read these files
    anyway; checking here only tells a worker it mis-copied, at once,
    instead of the monitor refusing the reply later."""
    try:
        with open(os.path.join(PACKAGE_DIR, name)) as f:
            data = json.load(f)
    except OSError, ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def nonce_problems(item):
    """A reply_nonce or owner-DM nonce that differs from the live one for
    its item. A missing one is left to the monitor (an out-of-date worker
    prompt omits it, and the monitor tells the owners); so are items the
    files don't list."""
    out = []
    mid = str(item.get("item") or item.get("reply_to") or "")
    dm = item.get("owner_dm_nonce")
    for field, given, name in (
        ("reply_nonce", item.get("reply_nonce"), "item_nonces.json"),
        ("owner_dm_nonce", dm, "owner_dm_nonces.json"),
    ):
        live = _live(name).get(mid)
        if isinstance(live, str) and given is not None and given != live:
            out.append(
                f"{field} does not match item {mid}'s: copy its "
                f'"{field}" from the wake payload again, exactly '
                f"({len(live)} characters)"
            )
    return out


def problems(item):
    """Mistakes the monitor would reject the item for (a subset of its
    checks, so nothing valid is ever refused here)."""
    out = reply_protocol.shape_problems(item)
    if out:
        return out
    if "proactive" in item and (
        item["proactive"] not in ("opt-out", "opt-in") or not item.get("item")
    ):
        out.append(
            'proactive is "opt-out" or "opt-in", on the reply to the '
            'person\'s own message (with its "item")'
        )
    nonce = item.get("owner_dm_nonce")
    if nonce is not None:
        if not (isinstance(nonce, str) and NONCE_RE.fullmatch(nonce)):
            out.append(
                'owner_dm_nonce: copy the item\'s "owner_dm_nonce" from the wake payload exactly'
            )
    out += nonce_problems(item)
    if out:
        return out
    if "schedule" in item:
        out = muse_watches.shape_problems(item)
        if out:
            return out
    problem = reply_protocol.content_problem(item)
    return [problem] if problem else []


def main():
    if len(sys.argv) > 2:
        print("usage: outbox-append.py [FILE]  (JSON on stdin by default)", file=sys.stderr)
        return 2
    try:
        if len(sys.argv) == 2:
            with open(sys.argv[1]) as f:
                raw = f.read()
        else:
            raw = sys.stdin.read()
        data = json.loads(raw)
    except (OSError, ValueError) as e:
        print(f"error: could not read a JSON item list ({e}); nothing appended", file=sys.stderr)
        return 1
    items = data if isinstance(data, list) else [data]
    errors = [f"item {i + 1}: {p}" for i, item in enumerate(items) for p in problems(item)]
    if errors:
        print("error: nothing appended; fix these and run it again:", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1
    if not items:
        print("nothing to append")
        return 0
    fd = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        outbox = []
        if os.path.exists(OUTBOX):
            try:
                with open(OUTBOX) as f:
                    outbox = json.load(f)
            except ValueError:
                outbox = None
            if not isinstance(outbox, list):
                print(
                    "error: reply_outbox.json is not a JSON array; the "
                    "monitor sets a bad file aside within seconds. "
                    "Run this again in a moment.",
                    file=sys.stderr,
                )
                return 1
        outbox += items
        tmp_fd, tmp = tempfile.mkstemp(dir=PACKAGE_DIR, prefix=".reply_outbox.json.", suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w") as f:
                os.fchmod(f.fileno(), 0o644)
                json.dump(outbox, f)
            os.replace(tmp, OUTBOX)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    finally:
        os.close(fd)
    missing = sum(1 for item in items if not item.get("item"))
    print(f"appended {len(items)} item(s) to the outbox")
    if missing:
        print(
            f'note: {missing} item(s) without "item" (the queue item\'s '
            f"message_id); the monitor can only match those by "
            f"reply_to",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
