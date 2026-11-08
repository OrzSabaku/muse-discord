"""Extract only allowed settings enums and aggregate change counts.

Configuration parsing and schema normalization stay on this side of the
boundary. The recorder receives no settings module or unfiltered values.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from muse_discord.settings.history import history_since
from muse_discord.settings.values import Scope, normalize
from muse_discord.usage_stats.limits import (
    FEATURES,
    MAX_DISCORD_ID_LENGTH,
    MIN_PRESET_CHANGES,
    MIN_SERVER_HISTORY_KEY_PARTS,
    PRESETS,
)
from muse_discord.usage_stats.payload import _bump


def features(st):
    """Bot-wide defaults in force, as enums only (FEATURES)."""
    out = {}
    if st is None:
        return out

    def get(key, scope):
        try:
            return st.get(key, scope)
        except Exception:
            return None

    def onoff(v):
        return "on" if v else "off"

    E, D, A = Scope.everywhere(), Scope.dm(), Scope("all_servers")
    dms_from = get("who.dms_from", D)
    raw = {
        "server_replies": get("who.replies", A),
        "dm_replies": get("who.replies", D),
        "dms_from": "list" if isinstance(dms_from, list) else dms_from,
        "memory": get("privacy.memory", E),
        "suspicious": get("privacy.suspicious", E),
        "bots": get("who.bots", E),
        "length": get("style.length", E),
        "daily_cap": onoff(get("limits.per_day", E) is not None),
        "per_person_cap": onoff(get("limits.per_person_per_day", E) is not None),
        "media": onoff(get("media.enabled", A) or get("media.enabled", E)),
    }
    try:
        raw["presence"] = onoff(st.presence_enabled)
        raw["update_notices"] = onoff(st.updates_notify)
    except Exception:
        pass
    for name, allowed in FEATURES.items():
        v = raw.get(name)
        if v is None:
            continue
        out[name] = v if isinstance(v, str) and v in allowed else "other"
    return out


def _is_id(v):
    return isinstance(v, str) and v.isdigit() and 1 <= len(v) <= MAX_DISCORD_ID_LENGTH


def change_source(h):
    """ "settings" (/settings: an owner or server admin by user id), "dm"
    (historical plain-language DM changes), or "cli" (bin/settings.py, the
    owners' Muse, bin/telemetry.py)."""
    g = str(h.get("group") or "")
    if g.startswith("nl-"):
        return "dm"
    if g.startswith("cli-"):
        return "cli"
    return "settings" if _is_id(str(h.get("by") or "")) else "cli"


def _matches_preset(picks, values, st):
    ok = True
    for key, to in picks:
        if key not in values:
            ok = False
            break
        want = values[key]
        if isinstance(want, tuple):
            want = want[1]
        try:
            spec = st.spec(key)
            a = normalize(spec, to)
            b = normalize(spec, want)
            same = a[0] and b[0] and a[1] == b[1]
        except Exception:
            same = to == want
        if not same:
            ok = False
            break
    return ok


def preset_of(lines, st, preset_values):
    """The preset (friends | community | work) one history group applied,
    else None: at least two changes, all at one server, each the value
    that preset gives that key."""
    if len(lines) < MIN_PRESET_CHANGES:
        return None
    server, picks = None, []
    for h in lines:
        parts = str(h.get("key") or "").split(".")
        if (
            len(parts) < MIN_SERVER_HISTORY_KEY_PARTS
            or parts[0] != "servers"
            or parts[2] == "channels"
        ):
            return None
        if server not in (None, parts[1]):
            return None
        server = parts[1]
        picks.append((".".join(parts[2:]), h.get("to")))
    for name, (_title, values) in (preset_values or {}).items():
        if name not in PRESETS:
            continue
        ok = _matches_preset(picks, values, st)
        if ok:
            return name
    return None


def count_history(rec, lines, st, preset_values):
    """Add settings changes (by source) and presets applied to rec."""
    groups = {}
    for h in lines:
        if "to" not in h or h.get("retired") or h.get("folded"):
            continue
        _bump(rec.setdefault("settings_changes", {}), change_source(h))
        if h.get("group") and not h.get("undo"):
            groups.setdefault(h["group"], []).append(h)
    for g in groups.values():
        name = preset_of(g, st, preset_values)
        if name:
            _bump(rec.setdefault("presets", {}), name)


@dataclass
class SettingsMetrics:
    preset_values: Mapping[str, tuple[str, Mapping[str, Any]]] | None = None

    def collect(self, record, settings, mark):
        lines, mark = history_since(getattr(settings, "overlay_doc", None), mark)
        count_history(record, lines, settings, self.preset_values)
        return mark

    def features(self, settings):
        return features(settings)
