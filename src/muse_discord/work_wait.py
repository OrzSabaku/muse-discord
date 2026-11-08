"""Wait for gateway events or the earliest pending REST, burst or worker reply work.

Short outbox probes keep worker replies responsive without busy-spinning on a
file that an earlier pass deferred. Clock and pending-work checks are supplied
by the caller; this module knows no Discord, settings or file layout.
"""

from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class WorkWait:
    now: Callable[[], float]
    pending_wait: Callable[[dict, float], float | None]
    outbox_waiting: Callable[[float], bool]
    backing_off: Callable[[], bool]

    def wait(self, state, wake, next_rest_wait, outbox_pass_at):
        # Event-driven wait: a gateway MESSAGE_CREATE wakes the loop at
        # once. Otherwise it idles in ticks of at most 2s, shorter while
        # REST slices are due (none while they are behind), a burst is
        # about to flush, or the outbox holds a reply.
        now = self.now()
        wait_for = next_rest_wait(now, self.backing_off())
        burst_wait = self.pending_wait(state, now)
        if burst_wait is not None:
            # A burst can flush early (burst_quiet): wake when it may.
            wait_for = min(wait_for, burst_wait)
        # A reply the worker writes goes out within ~0.25s, even when it
        # lands mid-wait: the wait is cut into 0.25s steps that stat the
        # outbox.
        until = now + (0.0 if self.outbox_waiting(outbox_pass_at) else wait_for)
        while True:
            left = until - self.now()
            if left <= 0 or wake.wait(timeout=min(0.25, left)):
                break
            if self.outbox_waiting(outbox_pass_at):
                break
        wake.clear()
