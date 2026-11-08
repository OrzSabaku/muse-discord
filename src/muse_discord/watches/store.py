"""Persist notification records under a shared lock and prune bounded history."""

import contextlib
import fcntl
import json
import os
import secrets
import time
from collections.abc import Callable

from muse_discord.storage import atomic_write
from muse_discord.watches.limits import _ALPHABET, DONE_KEEP_SECS, FIRED_KEEP, VERSION


def empty():
    return {
        "v": VERSION,
        "items": {},
        "fires": {},
        "done": {},
        "tz": {},
        "fired": {},
        "web": {},
        "held": {},
    }


def _clean(data):
    out = empty()
    if isinstance(data, dict):
        for k in out:
            if isinstance(data.get(k), dict):
                out[k] = data[k]
    return out


class Store:
    """watches.json. Reads are cached on the file's mtime and size (the
    monitor peeks at it every tick); every change is a read-modify-write
    under an flock on watches.lock, written atomically, so the owner's
    CLI and the monitor never lose each other's edits. A path provider lets a
    long-lived store follow its application's active data directory without
    carrying cached records into another store."""

    def __init__(self, path: str | os.PathLike[str] | Callable[[], str | os.PathLike[str]]):
        self._path_source: Callable[[], str | os.PathLike[str]] = (
            path if callable(path) else lambda: path
        )
        self._resolved_path = None
        self._sig = None
        self._data = None

    @property
    def path(self):
        path = os.fspath(self._path_source())
        if path != self._resolved_path:
            self._resolved_path = path
            self._sig = self._data = None
        return path

    @property
    def lock_path(self):
        return os.path.join(os.path.dirname(self.path) or ".", "watches.lock")

    def _stat(self):
        try:
            st = os.stat(self.path)
            return (st.st_mtime_ns, st.st_size, st.st_ino)
        except OSError:
            return None

    def _load(self):
        try:
            with open(self.path) as f:
                return _clean(json.load(f))
        except FileNotFoundError:
            return empty()
        except (OSError, ValueError) as e:
            # A damaged file is kept aside, never silently emptied.
            bad = f"{self.path}.bad-{int(time.time())}"
            with contextlib.suppress(OSError):
                os.replace(self.path, bad)
            print(f"watches.json unreadable ({e}); moved to {os.path.basename(bad)}", flush=True)
            return empty()

    def read(self):
        """The current data (do not modify it: use edit())."""
        sig = self._stat()
        if self._data is None or sig != self._sig:
            self._data = self._load()
            self._sig = sig
        return self._data

    @contextlib.contextmanager
    def edit(self):
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            data = self._load()
            before = json.dumps(data, sort_keys=True)
            yield data
            if json.dumps(data, sort_keys=True) != before:
                atomic_write(self.path, data, 0o600)
            self._data = data
            self._sig = self._stat()
        finally:
            os.close(fd)


def new_id(data, kind):
    prefix = "r" if kind == "remind" else "w"
    while True:
        i = prefix + "".join(secrets.choice(_ALPHABET) for _ in range(5))
        if i not in data["items"]:
            return i


def active(data, now=None):
    """The live items (not past their expiry)."""
    now = time.time() if now is None else now
    return {
        i: r
        for i, r in data["items"].items()
        if isinstance(r, dict) and (r.get("kind") == "remind" or r.get("until", 0) > now)
    }


def remember_done(data, qid, outcome, now=None):
    """Record a schedule outcome for queue item qid (applied once, ever:
    a retried send or a resend after a restart reads it back)."""
    now = time.time() if now is None else now
    d = data["done"]
    d[str(qid)] = dict(outcome, at=now)
    for k in [k for k, v in d.items() if now - (v or {}).get("at", 0) > DONE_KEEP_SECS]:
        del d[k]
    for k in list(d)[:-500]:
        del d[k]


def due_reminders(data, now=None):
    now = time.time() if now is None else now
    return [
        (i, r)
        for i, r in data["items"].items()
        if isinstance(r, dict) and r.get("kind") == "remind" and r.get("due", 0) <= now
    ]


def expired_watches(data, now=None):
    now = time.time() if now is None else now
    return [
        i
        for i, r in data["items"].items()
        if isinstance(r, dict) and r.get("kind") != "remind" and r.get("until", 0) <= now
    ]


def next_due(data):
    """When the next reminder or expiry is due (epoch), or None."""
    ts = [
        r.get("due") if r.get("kind") == "remind" else r.get("until")
        for r in data["items"].values()
        if isinstance(r, dict)
    ]
    ts = [t for t in ts if isinstance(t, (int, float))]
    return min(ts) if ts else None


def new_fire_id(now=None):
    """A queue-item id for a fire: a snowflake for now (it sorts and
    reads like a message id, and passes the outbox's id checks) whose
    low bits are random, so it never names a real message."""
    now = time.time() if now is None else now
    ms = int(now * 1000) - 1420070400000
    return str((ms << 22) | secrets.randbits(22) | 1)


def note_fired(data, fid, now=None):
    now = time.time() if now is None else now
    f = data["fired"]
    f[str(fid)] = now
    for k in [k for k, t in f.items() if now - t > 7 * 86400]:
        del f[k]
    for k in list(f)[:-FIRED_KEEP]:
        del f[k]
