"""Publish an immutable directory snapshot for Discord settings and local tools."""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Lock

from muse_discord.directory.discovery import READABLE_CHANNEL_TYPES
from muse_discord.storage import atomic_write


@dataclass
class DirectorySnapshot:
    path_source: Callable[[], str]
    _data: dict = field(
        default_factory=lambda: {"guilds": [], "channels": {}, "where": {}}, init=False, repr=False
    )
    _written: dict = field(default_factory=lambda: {"body": None}, init=False, repr=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def guilds(self):
        with self._lock:
            return [dict(g) for g in self._data["guilds"]]

    def channels(self, guild_id):
        with self._lock:
            return [dict(c) for c in self._data["channels"].get(str(guild_id)) or []]

    def locations(self, include_unmonitored=False):
        with self._lock:
            where = dict(self._data["where"])
            if include_unmonitored:
                for gid, channels in self._data["channels"].items():
                    for channel in channels:
                        where.setdefault(str(channel.get("id")), (gid, None))
            return where

    def update(self, guilds_source, monitored, member_counts, guild_channels):
        """Snapshot GUILDS, member counts and channel lists for /settings
        (main loop only; the interaction thread reads the snapshot). A
        channel carries whether the bot can read it (readable: a text
        channel the monitor polls) and send there (can_send, None when
        unknown); where maps every monitored channel and thread to (guild,
        parent)."""
        guilds = []
        for gid, info in guilds_source.items():
            g = {
                "id": gid,
                "name": (info or {}).get("name") or gid,
                "member_count": member_counts.get(gid),
            }
            guilds.append(g)
        guilds.sort(key=lambda g: (str(g["name"]).lower(), g["id"]))
        channels = {}
        for gid in guilds_source:
            out = []
            for c in guild_channels.get(gid) or []:
                c = dict(c)
                mon = monitored.get(c.get("id"))
                if c.get("type") in READABLE_CHANNEL_TYPES:
                    c["readable"] = mon is not None
                c["can_send"] = (mon or {}).get("can_send")
                out.append(c)
            channels[gid] = out
        where = {
            cid: (info.get("guild_id"), info.get("parent_id") if info.get("thread") else None)
            for cid, info in monitored.items()
        }
        with self._lock:
            self._data = {"guilds": guilds, "channels": channels, "where": where}
        body = {"guilds": guilds, "channels": channels}
        if body != self._written["body"]:
            try:
                atomic_write(self.path_source(), dict(body, updated=int(time.time())))
                self._written["body"] = body
            except OSError as e:
                print(f"Could not write directory.json: {e}", flush=True)

    def name(self, some_id):
        """A server's or channel's name from discovery, else None."""
        some_id = str(some_id)
        with self._lock:
            for g in self._data["guilds"]:
                if g["id"] == some_id:
                    return g.get("name")
            for chans in self._data["channels"].values():
                for c in chans:
                    if str(c.get("id")) == some_id:
                        return c.get("name")
        return None
