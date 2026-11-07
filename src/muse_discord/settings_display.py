"""Names and display text shared by the settings UI, CLI and change previews.

This module reads schema metadata and caller-supplied server information.
It does not load, resolve or write configuration files.
"""

from muse_discord.settings_schema import CORE, TOP


# -- words --------------------------------------------------------------------
LABELS = {
    "who.replies": "When it answers",
    "who.dms_from": "Who can DM it",
    "who.allow": "Only answer these people",
    "who.ignore": "Never answer",
    "who.bots": "Other bots",
    "who.bot_exchange_limit": "Replies in a row to other bots",
    "style.notes": "Instructions for the Muse",
    "style.length": "Reply length",
    "privacy.memory": "What it may say about you",
    "privacy.suspicious": "Possible prompt injection",
    "privacy.never_share": "Never share these phrases",
    "limits.per_day": "Replies per day (total)",
    "limits.per_person_per_day": "Replies per person per day",
    "abilities.owner_commands": "Owner requests (pin, post, react, threads)",
    "abilities.reminders": "Who can set reminders and watches",
    "media.enabled": "Image and video generation",
    "media.who": "Who can ask for images and video",
    "media.per_day": "Generations per day",
    "proactive.enabled": "Reaching out on its own",
    "proactive.per_day": "Unprompted messages per day (all places)",
    "proactive.frequency": "How often it thinks of reaching out",
    "proactive.per_person_per_day": "Unprompted messages a person a day",
    "proactive.per_person_per_week": "Unprompted messages a person a week",
    "proactive.per_channel_per_day": "Unprompted messages a channel a day",
    "proactive.quiet_hours": "Quiet hours (no unprompted messages)",
    "proactive.timezone": "Time zone",
    "presence.status": "Status line",
    "presence.kind": "Status kind",
    "updates.notify": "Update notices",
    "telemetry.usage_stats": "Anonymous usage stats",
    "memory.save_dms": "Save what you tell it in DMs to your Muse's memory",
}
VALUE_LABELS = {
    ("who.replies", "freely"): "Freely (join conversations without a mention)",
    ("who.replies", "conversational"): "Chatty",
    ("who.replies", "when-addressed"): "Only when @mentioned",
    ("who.replies", "off"): "Off",
    ("who.dms_from", "owners"): "Only you",
    ("who.dms_from", "list"): "You and people you pick",
    ("who.dms_from", "shared-servers"): "Anyone in your servers",
    ("who.dms_from", "anyone"): "Anyone",
    ("who.dms_from", "nobody"): "Nobody",
    ("who.allow", "everyone"): "Everyone",
    ("who.bots", "when-addressed"): "Answer when they address it",
    ("who.bots", "ignore"): "Ignore other bots",
    ("style.length", "brief"): "Brief",
    ("style.length", "normal"): "Normal",
    ("style.length", "detailed"): "Detailed",
    ("privacy.memory", "everything"): "Anything it remembers",
    ("privacy.memory", "public-only"): "Public facts only",
    ("privacy.memory", "nothing"): "Nothing personal",
    ("privacy.suspicious", "hold-for-review"): "Hold for my review",
    ("privacy.suspicious", "ignore"): "Drop it",
    ("privacy.suspicious", "answer"): "Answer it (no check)",
    ("media.who", "owners"): "Only you",
    ("media.who", "everyone"): "Everyone",
    ("abilities.reminders", "owners"): "Only you",
    ("abilities.reminders", "everyone"): "Everyone",
    ("proactive.frequency", "rarely"): "Rarely (once or twice a day)",
    ("proactive.frequency", "sometimes"): "Sometimes (4-6 times a day)",
    ("proactive.frequency", "often"): "Often (about hourly)",
}
CATEGORY_TYPE = 4


def label(key):
    """A setting's name in plain words (raw key only for unknown ones)."""
    return LABELS.get(key, key)


def name_lookup(names):
    if names is None:
        return lambda _i: None
    if callable(names):
        def look(i):
            try:
                return names(str(i))
            except Exception:
                return None
        return look
    return lambda i: names.get(str(i))


def person_name(pid, look):
    pid = str(pid)
    if pid.startswith("role:"):
        n = look(pid[5:])
        return (n if str(n).startswith("@") else f"@{n}") if n \
            else f"role {pid[5:]}"
    n = look(pid)
    return (n if str(n).startswith("@") else f"@{n}") if n else f"user {pid}"


def people_text(ids, look):
    shown = [person_name(p, look) for p in ids]
    if len(shown) > 10:
        shown = shown[:10] + [f"{len(ids) - 10} more"]
    if len(shown) <= 2:
        return " and ".join(shown)
    return ", ".join(shown[:-1]) + " and " + shown[-1]


def clip(text, n):
    text = str(text)
    return text if len(text) <= n else text[:n - 1] + "…"


def value_label(key, value, names=None):
    """A value in plain words ("Chatty", "No limit", "@Sam and @Ann").
    names: {id: name} or a callable id -> name, for people and
    channels; a raw id is shown only when no name is known."""
    look = name_lookup(names)
    spec = CORE.get(key) or TOP.get(key)
    t = spec.type if spec else None
    if key == "who.dms_from" and isinstance(value, list):
        return "You and " + people_text(value, look)
    if isinstance(value, str) and (key, value) in VALUE_LABELS:
        return VALUE_LABELS[(key, value)]
    if isinstance(value, bool):
        return "On" if value else "Off"
    if value is None:
        return "No limit" if t in ("number", None) else "Not set"
    if isinstance(value, list):
        if t == "phrases":
            return f"{len(value)} phrase{'s' if len(value) != 1 else ''}"
        return people_text(value, look) if value else "Nobody"
    if isinstance(value, int):
        return f"{value:,}"
    if t == "text":
        return f'"{clip(value, 60)}"' if value else "(empty)"
    return str(value)


def place_name(place, names=None):
    """"Defaults", "DMs", the server's name, "#channel"."""
    look = name_lookup(names)
    if place.kind == "defaults":
        return "Defaults"
    if place.kind == "dms":
        return "DMs"
    gname = look(place.guild_id)
    if place.kind == "server":
        return gname or f"server {place.guild_id}"
    n = look(place.channel_id)
    if n:
        return n if str(n).startswith("#") else f"#{n}"
    return f"channel {place.channel_id}" + (f" in {gname}" if gname else "")


def change_location(place, key, names=None, path=None):
    """"In #general", "In DMs", "By default", "By default in servers",
    "Bot-wide": how a sentence about key at place starts."""
    if place.kind == "defaults":
        if key in TOP or path == ("top",):
            return "Bot-wide"
        if path == ("dms",) or (path is None and key == "who.dms_from"):
            return "In DMs"
        if path == ("all_servers",) or (path is None and (
                key == "who.replies" or key in ("media.enabled", "proactive.enabled"))):
            return "By default in servers"
        return "By default"
    return "In " + place_name(place, names)


def describe_change(place, key, old, new, names=None, path=None):
    """One plain sentence: "In #general, When it answers: Chatty → Only
    when @mentioned". old / new are the values in force before and
    after. names: {id: name} or a callable (raw ids only as a
    fallback)."""
    head = f"{change_location(place, key, names, path)}, {label(key)}"
    if old == new:
        return f"{head} is already {value_label(key, new, names)}"
    return (f"{head}: {value_label(key, old, names)} → "
            f"{value_label(key, new, names)}")


# -- info about servers (member counts, channel lists, names) ----------------
class ServerInfo:
    """What the caller knows about servers: None; a dict {"guilds":
    [{"id", "name", "member_count"}] or {id: {...}}, "channels": {gid:
    [{"id", "name", "type", "parent_id"}]}}; or an object with guilds()
    and channels(gid) (settings_ui's / the monitor's Ctx)."""

    def __init__(self, src):
        if isinstance(src, ServerInfo):
            src = src.src
        self.src = src
        self._guilds = None
        self._chans = {}

    def guilds(self):
        if self._guilds is None:
            raw = []
            src = self.src
            try:
                if isinstance(src, dict):
                    raw = src.get("guilds") or []
                elif src is not None and callable(getattr(src, "guilds",
                                                          None)):
                    raw = src.guilds() or []
            except Exception:
                raw = []
            if isinstance(raw, dict):
                raw = [dict(v or {}, id=k) for k, v in raw.items()]
            out = {}
            for g in raw:
                if isinstance(g, dict) and g.get("id"):
                    out[str(g["id"])] = g
            self._guilds = out
        return self._guilds

    def members(self, gid):
        n = (self.guilds().get(str(gid)) or {}).get("member_count")
        return n if isinstance(n, int) and not isinstance(n, bool) else None

    def channels(self, gid):
        """[channel dicts] or None when unknown."""
        gid = str(gid)
        if gid not in self._chans:
            src, raw = self.src, None
            try:
                if isinstance(src, dict):
                    raw = (src.get("channels") or {}).get(gid)
                elif src is not None and callable(getattr(src, "channels",
                                                          None)):
                    raw = src.channels(gid)
            except Exception:
                raw = None
            self._chans[gid] = None if raw is None else [
                c for c in raw if isinstance(c, dict) and c.get("id")]
        return self._chans[gid]

    def channel(self, gid, cid):
        for c in self.channels(gid) or []:
            if str(c["id"]) == str(cid):
                return c
        return None

    def category_of(self, gid, cid):
        ch = self.channel(gid, cid)
        parent = str((ch or {}).get("parent_id") or "") or None
        if parent and (self.channel(gid, parent) or {}).get(
                "type") == CATEGORY_TYPE:
            return parent
        return None

    def names(self):
        out = {}
        for gid, g in self.guilds().items():
            if g.get("name"):
                out[gid] = str(g["name"])
            for c in self.channels(gid) or []:
                if c.get("name"):
                    out[str(c["id"])] = str(c["name"])
        return out


def chain_names(first, then):
    """A names callable: first (dict or callable), then then."""
    a, b = name_lookup(first), name_lookup(then)
    return lambda i: a(i) or b(i)


def names_from(info=None, st=None, extra=None):
    """{id: name} from info (server and channel names), the `name:`
    labels in config.yaml, and extra (e.g. people's names)."""
    out = {}
    if st is not None:
        out.update({str(k): str(v) for k, v in (st.labels or {}).items()})
    out.update(ServerInfo(info).names())
    out.update({str(k): str(v) for k, v in (extra or {}).items()})
    return out


