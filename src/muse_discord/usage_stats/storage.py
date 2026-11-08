"""Private telemetry snapshots, installation IDs and worker wake counters."""

import contextlib
import json
import os
import uuid

from muse_discord.storage import atomic_write
from muse_discord.usage_stats.limits import _UUID_RE, MAX_WAKES_BYTES


def atomic_write_json(path, obj):
    atomic_write(path, obj, 0o600, text=json.dumps(obj, sort_keys=True))


def load_file(path):
    """telemetry.json as a dict (a missing or unreadable file: empty)."""
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except OSError, ValueError:
        return {}


def read_install_id(path, create=True):
    """The random install id (uuid4) in path; a new one when the file is
    missing or holds anything else (create=False: None instead)."""
    try:
        with open(path) as f:
            v = f.read().strip()
        if _UUID_RE.match(v):
            return v
    except OSError:
        pass
    if not create:
        return None
    return reset_install_id(path)


def reset_install_id(path):
    value = str(uuid.uuid4())
    atomic_write(path, None, 0o600, text=value + "\n")
    return value


def drain_wakes(path):
    """How many wakes hooks/mention-watch.sh noted in path (one line
    each) since the last call; the file is taken away first, so a wake
    noted meanwhile lands in a new file."""
    taken = path + ".taking"
    try:
        os.replace(path, taken)
    except OSError:
        return 0
    try:
        if os.path.getsize(taken) > MAX_WAKES_BYTES:
            return 0
        with open(taken, "rb") as f:
            return f.read().count(b"\n")
    except OSError:
        return 0
    finally:
        with contextlib.suppress(OSError):
            os.unlink(taken)
