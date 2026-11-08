"""Bounded typing indicators with an independent clock and shared queue reader."""

import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Protocol

from muse_discord.conversation import _snowflake_ts
from muse_discord.message_queue import QueueFiles

TYPING_EVERY_SECS = 8
TYPING_MAX_SECS = 180
TYPING_CACHE_TTL_SECS = 60


class Clock(Protocol):
    def time(self) -> float: ...

    def sleep(self, seconds: float, /) -> None: ...


@dataclass
class TypingState:
    sources: dict = field(default_factory=dict)
    last: dict = field(default_factory=dict)
    lock: Lock = field(default_factory=Lock)


@dataclass
class TypingIndicators:
    post: Callable[[str, dict], Any]
    backing_off: Callable[[], bool]
    queue: QueueFiles
    clock: Clock = time
    state: TypingState = field(default_factory=TypingState)

    def send(self, channel_id):
        """Show the typing indicator for ~10s. Signals responsiveness while
        the reply worker drafts the real reply. Best-effort, and skipped
        during REST back-off: an indicator is never worth rate-limit
        pressure."""
        if self.backing_off():
            return
        with self.state.lock:
            self.state.last[channel_id] = self.clock.time()
        with contextlib.suppress(Exception):
            self.post(f"/channels/{channel_id}/typing", {})

    def sync(self, prefix, current):
        """Make current ({key: (channel_id, start_ts)}) the full set of typing
        sources whose keys start with prefix."""
        with self.state.lock:
            for key in [k for k in self.state.sources if k.startswith(prefix) and k not in current]:
                del self.state.sources[key]
            self.state.sources.update(current)

    def clear(self, keys, sent_in=None):
        """Drop typing sources (their reply is going out or was settled).
        sent_in: a channel a message was just posted in, which ended its
        indicator, so any other source there is re-pinged promptly."""
        with self.state.lock:
            for key in keys:
                self.state.sources.pop(key, None)
            if sent_in:
                self.state.last.pop(sent_in, None)

    def refresh(self, state, queue=None):
        """Publish the channels that are waiting on a reply (see above).
        queue: this tick's queue, when the caller has read it already."""
        now = self.clock.time()
        bursts = {}
        for key, entry in state.get("pending", {}).items():
            # A burst backing off after a failed enqueue does not type (a
            # rate cap would otherwise keep it typing for the ~15 min of
            # retries).
            if entry.get("next_try", 0) <= now:
                bursts["burst:" + key] = (entry["channel_id"], entry.get("first_seen", now))
        self.sync("burst:", bursts)
        items = {}
        for item in self.queue.load() if queue is None else queue:
            if (
                not isinstance(item, dict)
                or not item.get("channel_id")
                or not item.get("message_id")
            ):
                continue
            start = item.get("queued_at") or _snowflake_ts(item["message_id"])
            items["item:" + str(item["message_id"])] = (item["channel_id"], start)
        self.sync("item:", items)

    def run(self):
        while True:
            try:
                self.clock.sleep(1.0)
                if self.backing_off():
                    continue
                now = self.clock.time()
                with self.state.lock:
                    active = {
                        cid
                        for cid, start in self.state.sources.values()
                        if now - start < TYPING_MAX_SECS
                    }
                    due = [
                        cid
                        for cid in active
                        if now - self.state.last.get(cid, 0) >= TYPING_EVERY_SECS
                    ]
                    for cid in [
                        c for c, t in self.state.last.items() if now - t > TYPING_CACHE_TTL_SECS
                    ]:
                        del self.state.last[cid]
                for cid in due:
                    self.send(cid)
            except Exception as e:
                print(f"Typing thread error: {e}", flush=True)
