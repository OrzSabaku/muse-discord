"""Resolve effective settings places from channel, parent and category snapshots."""

from dataclasses import dataclass

from muse_discord import muse_settings as MS


@dataclass
class DirectoryScopes:
    channels: dict
    guilds: dict
    categories: dict

    def for_message(self, guild_id=None, channel_id=None, dm=False):
        """The settings scope of a message or event."""
        if dm:
            return MS.Scope.dm()
        info = self.channels.get(channel_id) or {}
        gid = guild_id or info.get("guild_id")
        chan = channel_id
        if info.get("thread") and info.get("parent_id"):
            chan = info["parent_id"]  # threads and forum posts: the parent
        cat = self.categories.get(chan) or (self.channels.get(chan) or {}).get("category_id")
        gname = (self.guilds.get(gid) or {}).get("name") or info.get("guild_name")
        return MS.Scope.server(gid, gname, chan, cat)

    def for_channel(self, channel_id):
        """The scope of a channel a reply goes to: a monitored server
        channel, else a DM."""
        if channel_id in self.channels:
            return self.for_message(None, channel_id)
        return MS.Scope.dm()
