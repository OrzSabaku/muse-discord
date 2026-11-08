"""Settings edits and private UI sessions over explicit runtime services."""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from muse_discord import muse_settings as MS
from muse_discord.directory.snapshot import DirectorySnapshot
from muse_discord.message_outcomes import MessageOutcomes


@dataclass
class SettingsSessions:
    clock: Callable[[], float] = time.time
    max_entries: int = 200
    _entries: dict = field(default_factory=dict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def peek(self, user_id, key):
        with self._lock:
            entry = self._entries.get((str(user_id), str(key)))
            if entry is None or entry[1] < self.clock():
                self._entries.pop((str(user_id), str(key)), None)
                return None
            return entry[0]

    def put(self, user_id, key, payload, ttl=900):
        now = self.clock()
        with self._lock:
            for key_id in [key_id for key_id, entry in self._entries.items() if entry[1] < now]:
                del self._entries[key_id]
            while len(self._entries) >= self.max_entries:
                self._entries.pop(next(iter(self._entries)))
            self._entries[(str(user_id), str(key))] = (payload, now + ttl)

    def drop(self, user_id, key):
        with self._lock:
            self._entries.pop((str(user_id), str(key)), None)


@dataclass
class SettingsContext:
    current: Callable[[], MS.Settings]
    config_path: str
    overlay_lock: threading.RLock
    directory: DirectorySnapshot
    people: dict
    sessions: SettingsSessions
    outcomes: MessageOutcomes
    admin_change: Callable[..., Any]
    bot_id: str
    now: float = field(default_factory=time.time)
    owners: set[str] = field(init=False)

    def __post_init__(self):
        self.owners = {str(owner) for owner in self.settings.owners}

    @property
    def settings(self):
        """The current muse_settings.Settings (reloaded on change, so a
        write made through this ctx is visible right away)."""
        return self.current()

    def guilds(self):
        return self.directory.guilds()

    def channels(self, guild_id):
        return self.directory.channels(guild_id)

    def write(self, key, value, by):
        """One change by `by` (its role inferred: a non-owner is an
        admin, held to the admin rules)."""
        with self.overlay_lock:
            MS.set_value(self.config_path, key, value, actor=MS.WriteActor(by=by))
        self.current()

    def undo(self, scope_prefix, by, **kw):
        """muse_settings.undo: revert the newest change (and the rest of
        its group) at one scope (scope_prefix as muse_settings.
        scope_prefix gives it, or a Place); the key reverted, or None.
        kw: children, admin_gid, expect (see muse_settings.undo)."""
        if kw.get("admin_gid"):
            kw.setdefault("info", self)
        with self.overlay_lock:
            actor = MS.WriteActor(
                by, admin_gid=kw.pop("admin_gid", None), info=kw.pop("info", None)
            )
            out = MS.undo(self.config_path, scope_prefix, actor, **kw)
        self.current()
        return out

    def apply(self, changes, by, role, group=None, admin_gid=None):
        """muse_settings.apply: several changes in one write and one undo
        group. role ("owner" or "admin") is required; an admin's changes
        are checked against the admin rule with admin_gid and this ctx's
        channel lists."""
        with self.overlay_lock:
            out = MS.apply(
                self.config_path,
                changes,
                actor=MS.WriteActor(by=by, group=group, role=role, admin_gid=admin_gid, info=self),
            )
        self.current()
        return out

    def undo_group(self, group, by, undo_id=None):
        """muse_settings.undo_group: undo one specific group (owners
        only)."""
        with self.overlay_lock:
            out = MS.undo_group(self.config_path, group, by, role="owner", undo_id=undo_id)
        self.current()
        return out

    def display_name(self, user_id):
        """A person's name the monitor has seen (None when unknown)."""
        return self.people.get(str(user_id))

    def session_peek(self, user_id, key):
        return self.sessions.peek(user_id, key)

    def session_put(self, user_id, key, payload, ttl=900):
        return self.sessions.put(user_id, key, payload, ttl)

    def session_drop(self, user_id, key):
        return self.sessions.drop(user_id, key)

    def recent_outcomes(self, place, limit=20):
        return self.outcomes.recent(place, limit)

    def outcome(self, message_id):
        return self.outcomes.one(message_id)

    def names(self, some_id):
        return self.directory.name(some_id) or self.settings.labels.get(str(some_id))

    def reload(self):
        """A fresh Settings through the monitor's own watcher, so the
        pages and the main loop see the same state."""
        return self.current()

    def notify_owners_admin_change(self, guild_id, user_id, key, old, new):
        return self.admin_change(guild_id, user_id, key, old, new)

    def log(self, msg):
        print(f"/settings: {msg}", flush=True)
