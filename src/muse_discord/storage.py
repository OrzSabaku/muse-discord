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
from collections.abc import Callable
from dataclasses import dataclass, field
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
    except (OSError, ValueError):
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


def private_file_ok(path, body):
    """The file still holds body (it was not lost or mangled)."""
    return os.path.exists(path) and read_json(path) == body


def publish_private(path, body, cache, check=False, create=True):
    """Write body to path (mode 600: the wake hook's private files) when
    it differs from the last write (cache: {"body"}), or (check) the file
    was lost or mangled. create=False: an empty body never creates the
    file. False when the write failed (logged)."""
    if (
        body == cache.get("body")
        and cache.get("path", path) == path
        and not (check and not private_file_ok(path, body))
    ):
        return True
    if create or body or os.path.exists(path):
        try:
            atomic_write(path, body, 0o600)
        except OSError as e:
            print(f"Could not write {os.path.basename(path)}: {e}", flush=True)
            return False
    cache.update(body=body, path=path)
    return True


@dataclass
class PrivateFile:
    """A private publication endpoint with its own change-detection cache."""

    destination: str | Callable[[], str]
    cache: dict = field(default_factory=lambda: {"body": None})

    @property
    def path(self) -> str:
        return self.destination() if callable(self.destination) else self.destination

    def publish(self, body, check=False, create=True) -> bool:
        return publish_private(self.path, body, self.cache, check, create)
