"""Live configuration, owner authorization state and cross-thread reload handoff.

Polling can happen on the interaction thread. Changes become visible there
immediately; owner-list updates and notifications run on the main thread.
Operational engine settings remain the startup snapshot until restart.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from muse_discord import muse_settings as MS


def _ignore_change(previous, current):
    pass


@dataclass
class LiveSettings:
    watcher: MS.Watcher
    on_change: Callable[[MS.Settings, MS.Settings], Any] = _ignore_change
    overlay_lock: threading.RLock = field(default_factory=threading.RLock, init=False)
    initial: MS.Settings = field(init=False)
    owners: set[str] = field(init=False)
    engine: MS.EngineSettings = field(init=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _pending: MS.Settings | None = field(default=None, init=False, repr=False)

    def __post_init__(self):
        self.initial = self.watcher.current
        self.engine = self.initial.engine
        self.owners = set(self.initial.owners)

    def read(self):
        """Reload valid edits; defer side effects to the main thread.

        Keep just the earliest unhandled snapshot, so repeated interaction
        edits coalesce into one notification without growing a pending list.
        Invalid edits retain the last valid snapshot and are reported once.
        """
        with self._lock:
            previous = self.watcher.current
            changed, error = self.watcher.poll()
            current = self.watcher.current
            if changed and self._pending is None:
                self._pending = previous
            pending = None
            if threading.current_thread() is threading.main_thread():
                pending, self._pending = self._pending, None
        if error:
            print(f"Config re-read failed; keeping the previous settings:\n{error}", flush=True)
        if pending is not None:
            self._changed(pending, current)
        return current

    def retire_changes(self, notify: Callable[[str], Any], backing_off: Callable[[], bool]) -> int:
        """Discord-side settings changes that config.yaml has overtaken (the
        file was edited at that spot afterwards) or that no longer validate:
        move them to the overlay's history, one owner DM each (the conflict
        rule: the newest intent wins). Waits out a REST backoff so the DMs
        can land."""
        if not self.read().retire or backing_off():
            return 0
        with self.overlay_lock:
            # Re-checked under the lock: a /settings change written since
            # may have replaced a stale entry, which must not be retired.
            notes = self.read().commit_retirements()
        for note in notes:
            print(note, flush=True)
            notify(note)
        return len(notes)

    def _changed(self, previous, current):
        print("Settings reloaded (config.yaml or config.discord.yaml changed).", flush=True)
        if sorted(current.owners) != sorted(previous.owners):
            self.owners = set(current.owners)
            print(f"Owners are now: {', '.join(sorted(self.owners)) or 'none'}", flush=True)
        self.on_change(previous, current)
