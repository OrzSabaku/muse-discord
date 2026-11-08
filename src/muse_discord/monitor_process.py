"""Own the monitor's lifetime PID lock and reject a second active process.

Linux's /proc check also recognizes older monitors that did not hold a flock.
The caller retains this object until shutdown; importing it acquires no lock.
"""

import fcntl
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class MonitorLock:
    path: Callable[[], str]
    _fd: int | None = field(default=None, init=False, repr=False)

    def acquire(self):
        if self._fd is not None:
            return
        mypid = os.getpid()
        fd = os.open(self.path(), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                print("Another monitor holds the pidfile lock; exiting.", flush=True)
                sys.exit(0)
            # A monitor from before this lock existed holds no flock, so also
            # check whether the recorded pid is still a live monitor.
            try:
                oldpid = int(os.read(fd, 32).decode().strip() or 0)
            except ValueError:
                oldpid = 0
            if oldpid and oldpid != mypid:
                try:
                    with open(f"/proc/{oldpid}/cmdline", "rb") as f:
                        alive = "monitor.py" in f.read().decode(errors="replace")
                except OSError:
                    alive = False
                if alive:
                    print(
                        f"Another monitor (pid {oldpid}) is already running; exiting.", flush=True
                    )
                    sys.exit(0)
                print(f"Replacing stale monitor pidfile (pid {oldpid} gone).", flush=True)
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, str(mypid).encode())
            self._fd = fd
        except BaseException:
            os.close(fd)
            raise

    def close(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
