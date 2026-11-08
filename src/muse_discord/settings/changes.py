"""Typed change proposals, actors and write results shared by the engine."""

from dataclasses import dataclass

from muse_discord.settings.places import format_place, parse_place, place_path
from muse_discord.settings.values import NO_CHANGE, SettingsError, format_key
from muse_discord.settings_display import ValueChange, describe_change


@dataclass
class WriteStamp:
    by: str
    at: str
    group: str | None = None
    role: str | None = None


@dataclass
class WriteActor:
    by: str
    group: str | None = None
    role: str | None = None
    admin_gid: str | None = None
    info: object = None


class Change:
    """One change: key at place becomes value ("inherit": the place stops
    setting it, as Reset). path: an explicit layer (only Undo needs one,
    for a Defaults key outside the usual mapping)."""

    def __init__(self, place, key, value, path=None, revert=False):
        if isinstance(place, str):
            place = parse_place(place)
        self.place, self.key, self.value = place, key, value
        self.path = path
        self.revert = revert  # an admin's revert (see admin_changes)

    def to_dict(self):
        return {"place": format_place(self.place), "key": self.key, "value": self.value}

    @classmethod
    def from_dict(cls, d, info=None):
        """{"place", "key", "value"} or {"place", "reset": key}."""
        if not isinstance(d, dict):
            raise SettingsError("a change must be {place, key, value}")
        place = parse_place(d.get("place"), info)
        if "reset" in d:
            return cls(place, str(d["reset"]), "inherit")
        if "key" not in d or "value" not in d:
            raise SettingsError("a change needs place, key and value")
        return cls(place, str(d["key"]), d["value"])

    def __eq__(self, other):
        return isinstance(other, Change) and (self.place, self.key, self.value, self.path) == (
            other.place,
            other.key,
            other.value,
            other.path,
        )

    def __repr__(self):
        return f"Change({format_place(self.place)!r}, {self.key!r}, {self.value!r})"


def change_path(st, change):
    return change.path or place_path(st, change.place, change.key)


class Planned:
    """One change, checked: path, keystr, the value to write, whether it
    is a no-op, and the values in force before (old) and after (new)."""

    def __init__(self, change, path, spec, write, noop):
        self.change, self.path, self.spec = change, path, spec
        self.key = change.key
        self.keystr = format_key(path, change.key)
        self.write, self.noop = write, noop
        self.old = self.new = None

    def describe(self, names=None):
        return describe_change(
            self.change.place,
            self.key,
            names=names,
            path=self.path,
            values=ValueChange(old=self.old, new=self.new),
        )


class ApplyResult:
    """What apply() did: group (None when nothing was written), written
    and unchanged ([Planned]); changed is False for "no change"."""

    def __init__(self, group, written, unchanged):
        self.group, self.written, self.unchanged = group, written, unchanged

    @property
    def changed(self):
        return bool(self.written)

    @property
    def status(self):
        return "changed" if self.written else NO_CHANGE

    def describe(self, names=None):
        return [p.describe(names) for p in self.written]
