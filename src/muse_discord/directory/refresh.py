"""Apply incremental discovery without losing channels registered during a scan."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from muse_discord.conversation import channel_display
from muse_discord.directory.discovery import Discovery, ThreadSnapshot
from muse_discord.directory.state import DirectoryState


@dataclass
class DirectoryRefresh:
    registry: DirectoryState
    discovery: Discovery
    publish: Callable[[], Any]
    sync: Callable[[dict, dict, dict], Any]
    save: Callable[[dict], Any]

    def restore(self, state):
        """Restore guild admission and seed new receive positions before workers start."""
        self.sync(state, self.registry.guilds, self.registry.channels)
        for cid, info in self.registry.channels.items():
            if cid not in state["last_seen"]:
                self.discovery.seed(cid, channel_display(info), state)

    def threads(self, state):
        """Light thread-only refresh: pick up brand-new posts and threads
        minutes after they appear instead of waiting for the full channel
        refresh. Only threads not already monitored cost an API probe."""
        guilds = self.discovery.guilds()
        yield
        for gid, gname in guilds or []:
            try:
                chans = self.discovery.get(f"/guilds/{gid}/channels")
            except Exception:
                continue
            finally:
                yield
            names_by_id = {c["id"]: c.get("name", "?") for c in chans}
            for c in chans:
                if c.get("parent_id"):
                    self.registry.categories[c["id"]] = c["parent_id"]
            new_threads = yield from self.discovery.threads_iter(
                gid,
                gname,
                snapshot=ThreadSnapshot(names=names_by_id, known_ids=self.registry.channels),
            )
            for tid, info in new_threads.items():
                self.discovery.seed(tid, channel_display(info), state)
                self.registry.channels[tid] = info
                print(f"Now monitoring {channel_display(info)}.", flush=True)
                yield
            if new_threads:
                self.save(state)

    def channels(self, state):
        """Periodic re-discovery of readable channels, so permission changes
        (grants or revocations) take effect without a restart. Applied when
        complete; a channel the gateway or the thread refresh registered
        while it ran is kept."""
        before = dict(self.registry.channels)
        fresh, fresh_guilds = yield from self.discovery.channels_iter(before)
        for cid, info in fresh.items():
            if cid not in self.registry.channels:
                # Seeds only a channel with no last_seen yet.
                self.discovery.seed(cid, channel_display(info), state)
                yield
        for cid, info in self.registry.channels.items():
            if cid not in before and cid not in fresh:
                fresh[cid] = info
        if set(fresh) != set(self.registry.channels):
            added = [
                channel_display(n) for i, n in fresh.items() if i not in self.registry.channels
            ]
            removed = [
                channel_display(n) for i, n in self.registry.channels.items() if i not in fresh
            ]
            if added:
                print(f"Now monitoring {', '.join(added)}.", flush=True)
            if removed:
                print(f"Lost access to {', '.join(removed)}; unmonitoring.", flush=True)
        self.registry.adopt(fresh, fresh_guilds)
        self.publish()
        self.sync(state, fresh_guilds, fresh)
        self.save(state)
