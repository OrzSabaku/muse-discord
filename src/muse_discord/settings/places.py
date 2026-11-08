"""Translate user-facing places and keys into layers and explanations."""

import json
import re
from datetime import datetime

from muse_discord.settings.values import (
    CHANNEL_SHORT,
    SERVER_SCOPE_PARTS,
    SOURCE_DATE_INDEX,
    Scope,
    SettingsError,
    _id_str,
    _is_enabled_key,
    check_settable,
    parse_key,
)
from muse_discord.settings_display import (
    CATEGORY_TYPE,
    LABELS,
    VALUE_LABELS,
    ServerInfo,
    chain_names,
    label,
    name_lookup,
    names_from,
    person_name,
    place_name,
    value_label,
)
from muse_discord.settings_schema import TOP


def scope_prefix(path):
    """The overlay key prefix of a scope path: "everywhere", "dms",
    "all_servers", 'servers."<id>"', 'servers."<id>".channels."<id>"'."""
    if path[0] == "servers":
        out = f'servers."{path[1]}"'
        return out + (f'.channels."{path[3]}"' if len(path) > SERVER_SCOPE_PARTS else "")
    return path[0]


def parse_scope_prefix(prefix):
    """The scope path of an overlay key prefix (inverse of scope_prefix)."""
    path, _key = parse_key(str(prefix) + ".x")
    if path == ("top",):
        raise ValueError("not a scope")
    return path


ADMIN_REPLIES = ("off", "when-addressed")


ADMIN_QUIETER = (
    "Server admins can make the bot quieter, or undo their own "
    "changes back to what the owners set; never louder."
)


_PLACE_HELP = 'use "defaults", "dms", "server:<server id>" or "channel:<server id>:<channel id>"'


class Place:
    """Where a change applies, in the words people use: defaults (every
    server and DM that does not set its own), dms, a server, or a
    channel. Defaults maps each key to its real layer: who.replies,
    media.enabled and proactive.enabled to all_servers, who.dms_from
    to dms, bot-wide keys to the top level, everything else to
    everywhere. A channel may carry its category (category_id) so values
    resolve as they do for messages there."""

    KINDS = ("defaults", "dms", "server", "channel")

    def __init__(self, kind, guild_id=None, channel_id=None, category_id=None):
        if kind not in self.KINDS:
            raise SettingsError(f"{kind!r} is not a place; {_PLACE_HELP}")
        self.kind = kind
        self.guild_id = str(guild_id) if guild_id else None
        self.channel_id = str(channel_id) if channel_id else None
        self.category_id = str(category_id) if category_id else None

    @classmethod
    def defaults(cls):
        return cls("defaults")

    @classmethod
    def dms(cls):
        return cls("dms")

    @classmethod
    def server(cls, guild_id):
        return cls("server", guild_id)

    @classmethod
    def channel(cls, guild_id, channel_id, category_id=None):
        return cls("channel", guild_id, channel_id, category_id)

    def __eq__(self, other):
        return isinstance(other, Place) and (self.kind, self.guild_id, self.channel_id) == (
            other.kind,
            other.guild_id,
            other.channel_id,
        )

    def __hash__(self):
        return hash((self.kind, self.guild_id, self.channel_id))

    def __repr__(self):
        return f"Place({format_place(self)!r})"

    def scope(self, key=None):
        """The Scope that resolves key here (None for a bot-wide key)."""
        if self.kind == "defaults":
            if key in TOP:
                return None
            kind = (
                "all_servers"
                if key == "who.replies" or _is_enabled_key(key)
                else "dms"
                if key == "who.dms_from"
                else "everywhere"
            )
            return Scope(kind)
        if self.kind == "dms":
            return Scope.dm()
        if self.kind == "server":
            return Scope.server(self.guild_id)
        return Scope.server(self.guild_id, channel_id=self.channel_id, category_id=self.category_id)

    def owns(self, path, key=None, children=False):
        """True when a change at layer path (for key) belongs to this
        place (what its Undo sees). Defaults also owns the DM keys it
        offers (who.dms_from, and who.replies for "Nobody")."""
        if self.kind == "defaults":
            return path in (("everywhere",), ("all_servers",), ("top",)) or (
                path == ("dms",) and key in ("who.dms_from", "who.replies")
            )
        if self.kind == "dms":
            return path == ("dms",)
        if self.kind == "server":
            return path == ("servers", self.guild_id) or (
                children and path[:2] == ("servers", self.guild_id)
            )
        return path == ("servers", self.guild_id, "channels", self.channel_id)


def format_place(place):
    """The one place syntax (CLI --place, outbox actions): "defaults",
    "dms", "server:<gid>", "channel:<gid>:<cid>"."""
    if place.kind in ("defaults", "dms"):
        return place.kind
    if place.kind == "server":
        return f"server:{place.guild_id}"
    return f"channel:{place.guild_id}:{place.channel_id}"


def parse_place(text, info=None):
    """A Place from "defaults" | "dms" | "server:<gid>" |
    "channel:<gid>:<cid>". Raises SettingsError for anything else,
    including a category (a "category:" form, or a channel id that info
    knows to be a category: categories stay valid in config.yaml but
    are not offered here). info (see assess) also fills in a channel's
    category so it resolves as messages there do."""
    t = str(text if text is not None else "").strip()
    if t in ("defaults", "dms"):
        return Place(t)
    m = re.fullmatch(r"server:(\d{1,25})", t)
    if m:
        return Place.server(m[1])
    m = re.fullmatch(r"channel:(\d{1,25}):(\d{1,25})", t)
    if m:
        inf = ServerInfo(info)
        ch = inf.channel(m[1], m[2])
        if ch is not None and ch.get("type") == CATEGORY_TYPE:
            raise SettingsError(
                f"{t}: that is a category; categories "
                f"can only be set in config.yaml. Pick "
                f"a channel, or the server."
            )
        return Place.channel(m[1], m[2], inf.category_of(m[1], m[2]))
    if t.lower().startswith("category"):
        raise SettingsError(f"{t}: categories can only be set in config.yaml; {_PLACE_HELP}")
    raise SettingsError(f"{t!r} is not a place; {_PLACE_HELP}")


def place_of_path(path):
    """The Place a layer path belongs to (None for an unsupported scope)."""
    if path[0] in ("everywhere", "all_servers", "top"):
        return Place.defaults()
    if path[0] == "dms":
        return Place.dms()
    if path[0] == "servers":
        if len(path) > SERVER_SCOPE_PARTS:
            return Place.channel(path[1], path[3])
        return Place.server(path[1])
    return None


def place_path(st, place, key):
    """The layer path a change to key at place writes, or raise
    SettingsError (plain words) when place does not offer key."""
    path = _layer_path(st, place, key)
    try:
        check_settable(st, path, key)
    except SettingsError as e:
        raise SettingsError(f"{label(key)} can't be changed in {place_name(place)} ({e})") from e
    return path


def _layer_path(st, place, key):
    """place_path without the check that Discord may set it there."""
    spec = st.spec(key)
    if spec is None:
        raise SettingsError(f"{key}: unknown setting")
    if key in TOP:
        if place.kind != "defaults":
            raise SettingsError(f"{label(key)} is bot-wide; change it in Defaults")
        path = ("top",)
    elif place.kind == "defaults":
        if key == "who.replies" or _is_enabled_key(key):
            path = ("all_servers",)
        elif "everywhere" in spec.scopes:
            path = ("everywhere",)
        elif "dms" in spec.scopes:
            path = ("dms",)
        else:
            raise SettingsError(f"{label(key)} can't be set in Defaults")
    elif place.kind == "dms":
        path = ("dms",)
    elif place.kind == "server":
        path = ("servers", place.guild_id)
    else:
        path = ("servers", place.guild_id, "channels", place.channel_id)
    return path


def place_value(st, place, key, path=None):
    """The value in force for key at place (path: an explicit layer for
    a Defaults key outside the usual mapping)."""
    sc = _scope_for(place, key, path)
    return st.top(key) if sc is None else st.get(key, sc)


def _scope_for(place, key, path=None):
    if path is not None and place.kind == "defaults":
        return {
            ("everywhere",): Scope.everywhere(),
            ("all_servers",): Scope("all_servers"),
            ("dms",): Scope.dm(),
        }.get(path)
    return place.scope(key)


BASIC_KEYS = (
    "who.replies",
    "who.dms_from",
    "privacy.memory",
    "style.notes",
    "style.length",
    "limits.per_person_per_day",
    "media.enabled",
    "proactive.enabled",
)


MORE_KEYS = (
    "who.allow",
    "who.ignore",
    "who.bots",
    "who.bot_exchange_limit",
    "limits.per_day",
    "privacy.suspicious",
    "media.who",
    "media.per_day",
    "abilities.reminders",
    "proactive.frequency",
    "proactive.per_person_per_day",
    "proactive.per_person_per_week",
    "proactive.per_channel_per_day",
    "proactive.quiet_hours",
    "proactive.timezone",
)


BOT_WIDE_KEYS = ("presence.status", "updates.notify")


HIDDEN_KEYS = ("abilities.owner_commands", "presence.kind", "privacy.never_share")


CHANNEL_KEYS = ("who.replies", "style.notes")


def place_keys(st, place, tier):
    """The keys place offers in tier "basics" (When it answers, Who can
    DM it, What it may say about you, Instructions + Reply length,
    Replies per person per day, Extras on/off), "more", or "hidden"
    (valid in config.yaml and still Discord-editable as far as the
    overlay goes, but not offered: owner requests, status kind, never
    share, hidden settings). The UI may show a hidden key
    read only."""
    if tier not in ("basics", "more", "hidden"):
        raise ValueError(tier)
    basics = [k for k in BASIC_KEYS if k != "who.dms_from" or place.kind in ("defaults", "dms")]
    more = list(MORE_KEYS)
    hidden = list(HIDDEN_KEYS)
    if place.kind == "defaults":
        more += list(BOT_WIDE_KEYS)
    if place.kind == "channel":
        more = []
        basics = [k for k in basics if k in CHANNEL_KEYS or _is_enabled_key(k)]
    want = {"basics": basics, "more": more, "hidden": hidden}[tier]
    out = []
    for key in want:
        if tier == "hidden":
            if _valid_layer(st, place, key):
                out.append(key)
            continue
        try:
            place_path(st, place, key)
        except SettingsError:
            continue
        out.append(key)
    return out


def _valid_layer(st, place, key):
    """key may appear at place's layer in config.yaml."""
    spec = st.spec(key)
    if spec is None:
        return False
    if key in TOP:
        return place.kind == "defaults"
    if place.kind == "defaults":
        return bool({"everywhere", "all_servers", "dms"} & set(spec.scopes))
    if place.kind == "dms":
        return "dms" in spec.scopes
    if place.kind == "server":
        return "server" in spec.scopes
    return "channel" in spec.scopes and (key in CHANNEL_KEYS or _is_enabled_key(key))


def _date(at):
    try:
        d = datetime.strptime(str(at), "%Y-%m-%dT%H:%M:%SZ")
        return f"{d:%b} {d.day}, {d.year}"
    except ValueError:
        return str(at)


def _local_source_tag(src, look):
    if src[0] == "file":
        return "● set here in config.yaml"
    by = src[1] if len(src) > 1 else None
    who = person_name(by, look) if _id_str(by) else (by or "someone")
    at = src[SOURCE_DATE_INDEX] if len(src) > SOURCE_DATE_INDEX else ""
    return f"● changed here by {who} on {_date(at)}"


def source_tag(st, place, key, names=None):
    """ "● changed here by @Sam on Oct 19, 2026", "● set here in
    config.yaml", "Same as <place>", or "Built-in default"."""
    names = chain_names(names, names_from(None, st))
    look = name_lookup(names)
    try:
        path = _layer_path(st, place, key)
    except SettingsError:
        path = None
    rows: list[tuple[str, tuple[str, ...], bool, object, tuple]]
    if key in TOP:
        s, _v, src = st.layer(("top",), key)
        rows = [("top", ("top",), s, _v, src)]
    else:
        sc = place.scope(key)
        _val, rows = st.trace(sc, key)
    for _name, p, s, _v, src in reversed(rows):
        if not s:
            continue
        if p == path:
            return _local_source_tag(src, look)
        if p[0] in ("builtin", "builtin_all_servers"):
            return "Built-in default" if place.kind == "defaults" else "Same as Defaults"
        owner = place_of_path(p)
        if owner is None:
            return f'Same as server "{p[1]}" (old config)'
        if owner.kind == "channel" and owner.channel_id != place.channel_id:
            n = look(owner.channel_id) if owner.channel_id is not None else None
            return f"Same as category {n or owner.channel_id}"
        return f"Same as {place_name(owner, names)}"
    return "Built-in default"


def explain_lines(st, place, keys=None, names=None):
    """["When it answers: Chatty (Same as Defaults)", ...] for keys
    (default: the place's basics and more)."""
    if keys is None:
        keys = place_keys(st, place, "basics") + place_keys(st, place, "more")
    names = chain_names(names, names_from(None, st))
    out = []
    for key in keys:
        v = place_value(st, place, key)
        out.append(
            f"{label(key)}: {value_label(key, v, names)} ({source_tag(st, place, key, names)})"
        )
    return out


def key_from_text(st, text):
    """A settings key from its key name or its label (any case)."""
    t = str(text).strip()
    if st.spec(t) is not None:
        return t
    low = t.lower()
    for key, lab in LABELS.items():
        if lab.lower() == low and st.spec(key) is not None:
            return key
    if low in CHANNEL_SHORT:
        return CHANNEL_SHORT[low]
    raise SettingsError(f"{t!r} is not a setting")


def parse_value(st, key, text):
    """A value from text as people type it: a value, its label ("Chatty"),
    none, on/off, a number, or comma-separated ids for a list."""
    spec = st.spec(key)
    if spec is None:
        raise SettingsError(f"{key}: unknown setting")
    t = str(text).strip()
    low = t.lower()
    for (k, v), lab in VALUE_LABELS.items():
        if k == key and lab.lower() == low and v not in ("list", "nobody"):
            return v
    if spec.type == "people" or (spec.type == "choice" and spec.ids):
        if low in spec.values:
            return low
        if spec.type == "people" and low in ("", "nobody", "none", "no one", "[]"):
            return []
        if re.fullmatch(r"[\s,]*((role:)?\d{1,25}[\s,]*)+", t):
            return [p.strip() for p in t.replace(" ", ",").split(",") if p.strip()]
    if spec.type == "number":
        try:
            return json.loads(t)
        except ValueError:
            pass
    return t
