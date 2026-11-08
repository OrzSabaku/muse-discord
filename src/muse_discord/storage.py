"""Filesystem boundaries: atomic JSON/text replacement and bounded file locks.

No monitor, settings, token or network dependency. Business transactions remain
with their callers, which must acquire the shared lock before read/modify/write.
"""

import contextlib
import fcntl
import json
import os
import tempfile
import time
from typing import Any


class SharedLockTimeout(TimeoutError):
    """The queue/outbox writer must defer its work; it never acquired the lock."""


def atomic_write(path, obj, mode=None, text=None, *, durable=False):
    """Write JSON (obj, or text already serialized) to path via a unique
    temp file and a rename (two writers on one fixed "<path>.tmp" could
    rename each other's half-written file into place). The file keeps its
    mode unless mode is given."""
    if text is None:
        text = json.dumps(obj)
    if mode is None:
        try:
            mode = os.stat(path).st_mode & 0o777
        except OSError:
            mode = 0o644
    fd, tmp = tempfile.mkstemp(
        dir=os.path.dirname(path) or ".", prefix="." + os.path.basename(path) + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), mode)
            f.write(text)
            if durable:
                f.flush()
                os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def shared_lock(path, timeout):
    """Acquire an exclusive file lock within timeout; callers defer on contention.

    Re-read shared files under the lock. Never hold it across network calls.
    """
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise SharedLockTimeout(
                        f"{os.path.basename(path)} is busy; write deferred after {timeout}s"
                    ) from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the flock


def read_json(path, kind: type = dict) -> Any:
    """path's JSON: an empty `kind` when the file is missing, None when it
    is unreadable or not a `kind` (dict or list)."""
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return kind()
    except OSError, ValueError:
        return None
    return data if isinstance(data, kind) else None


def load_queue(path):
    """The queue as it is on disk now (a snapshot: writes are atomic
    renames, so reading needs no lock)."""
    if not os.path.exists(path):
        return []
    with open(path) as f:
        queue = json.load(f)
    return queue if isinstance(queue, list) else []


def atomic_write_text(path, text):
    """Settings use private permissions and fsync before replacing their file."""
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        mode = 0o600
    atomic_write(path, None, mode=mode, text=text, durable=True)
