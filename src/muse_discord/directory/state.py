"""Process-owned channel/guild registries shared by discovery and message policy."""

import time
from dataclasses import dataclass, field

from muse_discord.directory.scopes import DirectoryScopes


@dataclass
class DirectoryState:
    channels: dict = field(default_factory=dict)
    guilds: dict = field(default_factory=dict)
    member_counts: dict = field(default_factory=dict)
    guild_channels: dict = field(default_factory=dict)
    categories: dict = field(default_factory=dict)
    threads_listed: dict = field(default_factory=dict)
    new_threads: set[str] = field(default_factory=set)
    started_at: float = field(default_factory=time.time)

    @property
    def scopes(self):
        return DirectoryScopes(self.channels, self.guilds, self.categories)

    def adopt(self, channels, guilds):
        """Replace the directory contents while keeping shared map identities."""
        channels, guilds = dict(channels), dict(guilds)
        self.channels.clear()
        self.channels.update(channels)
        self.guilds.clear()
        self.guilds.update(guilds)
