"""Liveness and permanent credential failure markers for the watchdog."""

import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field

HEARTBEAT_INTERVAL_SECS = 30
EXIT_TOKEN_REJECTED = 3


@dataclass
class MonitorHealth:
    path_source: Callable[[], str]
    now: Callable[[], float] = time.time
    rejected: str | None = None
    _heartbeat_at: float = field(default=0, init=False, repr=False)

    def progress(self):
        """Mark the monitor as making progress (throttled to one touch per
        30s). Called from the main loop and after every REST call."""
        now = self.now()
        if now - self._heartbeat_at < HEARTBEAT_INTERVAL_SECS:
            return
        self._heartbeat_at = now
        try:
            with open(self.path_source(), "a"):
                pass
            os.utime(self.path_source(), None)
        except OSError:
            pass


def token_rejected_exit(where):
    print(
        f"TOKEN REJECTED: Discord refused the bot token ({where}); it "
        f"was probably reset in the developer portal. Exiting. Store a "
        f"fresh token and the watchdog starts the monitor again.",
        flush=True,
    )
    sys.exit(EXIT_TOKEN_REJECTED)
