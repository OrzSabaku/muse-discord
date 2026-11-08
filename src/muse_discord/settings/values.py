"""Setting keys, scope types, validation and restrictiveness rules."""

import re

from muse_discord.settings_schema import CORE, TOP

PAIR_LENGTH = 2


SERVER_SCOPE_PARTS = 2


MIN_SERVER_KEY_PARTS = 3


MIN_CHANNEL_KEY_PARTS = 5


SOURCE_DATE_INDEX = 2


DEFAULT_PERSONA = "persona.md"


CHANNEL_SHORT = {"replies": "who.replies", "notes": "style.notes"}


_ID_RE = re.compile(r"^[0-9]{1,25}$")


class SettingsError(Exception):
    """An invalid hand-written config (the message names file, key, scope,
    reason and the valid values; several problems are one per line)."""


TOP_DISCORD_EDITABLE = (
    "presence.status",
    "presence.kind",
    "updates.notify",
    "telemetry.usage_stats",
    "memory.save_dms",
)


def _id_str(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str) and _ID_RE.match(v.strip()):
        return v.strip()
    return None


def normalize(spec, value):
    """Validate one explicitly supplied override using its Pydantic field."""
    return spec.normalize(value)


def _show(v):
    if isinstance(v, str):
        return f'"{v}"'
    if v is True:
        return "on"
    if v is False:
        return "off"
    if v is None:
        return "none"
    return str(v)


def parse_key(s) -> tuple[tuple[str, ...], str]:
    """('scope path tuple', 'canonical key') for an overlay key, or raise
    ValueError. Scope paths: ("top",) ("everywhere",) ("dms",)
    ("all_servers",) ("servers", sid) ("servers", sid, "channels", cid)."""
    parts, buf, i = [], "", 0
    s = str(s)
    while i < len(s):
        c = s[i]
        if c == '"':
            j = s.find('"', i + 1)
            if j < 0:
                raise ValueError("unbalanced quote")
            buf += s[i + 1 : j]
            i = j + 1
            continue
        if c == ".":
            parts.append(buf)
            buf = ""
        else:
            buf += c
        i += 1
    parts.append(buf)
    if not parts or any(p == "" for p in parts):
        raise ValueError("empty key part")
    head = parts[0]
    if head in ("presence", "updates", "telemetry", "memory"):
        return ("top",), ".".join(parts)
    if head in ("everywhere", "dms", "all_servers"):
        return (head,), _canon(parts[1:], False)
    if head == "servers" and len(parts) >= MIN_SERVER_KEY_PARTS:
        sid = parts[1]
        if parts[2] == "channels" and len(parts) >= MIN_CHANNEL_KEY_PARTS:
            return ("servers", sid, "channels", parts[3]), _canon(parts[4:], True)
        return ("servers", sid), _canon(parts[2:], False)
    raise ValueError("unknown scope")


def _canon(rest, channel):
    key = ".".join(rest)
    if channel and key in CHANNEL_SHORT:
        return CHANNEL_SHORT[key]
    return key


def format_key(path: tuple[str, ...], key: str) -> str:
    if path == ("top",):
        return key
    if path[0] == "servers":
        out = f'servers."{path[1]}"'
        if len(path) > SERVER_SCOPE_PARTS:
            short = {v: k for k, v in CHANNEL_SHORT.items()}.get(key, key)
            return out + f'.channels."{path[3]}".{short}'
        return out + "." + key
    if path[0] == "alias":
        return f'servers."{path[1]}".{key}'
    return f"{path[0]}.{key}"


def _scope_kind(path):
    if path[0] in ("servers", "alias"):
        return "channel" if len(path) > SERVER_SCOPE_PARTS else "server"
    return path[0]


class Scope:
    """Where something happens. kind: everywhere | dms | all_servers |
    server. A server scope may name its channel (for a thread: the
    parent) and that channel's category."""

    def __init__(self, kind, guild_id=None, guild_name=None, channel_id=None, category_id=None):
        self.kind = kind
        self.guild_id = str(guild_id) if guild_id else None
        self.guild_name = guild_name
        self.channel_id = str(channel_id) if channel_id else None
        self.category_id = str(category_id) if category_id else None

    @classmethod
    def dm(cls):
        return cls("dms")

    @classmethod
    def everywhere(cls):
        return cls("everywhere")

    @classmethod
    def server(cls, guild_id, guild_name=None, channel_id=None, category_id=None):
        return cls("server", guild_id, guild_name, channel_id, category_id)

    @property
    def is_dm(self):
        return self.kind == "dms"

    def key(self):
        return (self.kind, self.guild_id, self.guild_name, self.channel_id, self.category_id)

    def label(self):
        if self.kind != "server":
            return self.kind
        if not self.guild_id:
            return "all_servers (a server with no entry)"
        out = f"server {self.guild_id}"
        if self.guild_name:
            out += f" ({self.guild_name})"
        if self.category_id:
            out += f", category {self.category_id}"
        if self.channel_id:
            out += f", channel {self.channel_id}"
        return out


CHANNEL_ONLY = (
    "a channel or category may set only replies, notes, media.enabled and proactive.enabled"
)


def spec_of(key):
    """The Pydantic field view of a core or bot-wide settings key, else None."""
    return CORE.get(key) or TOP.get(key)


def _settable_at(spec, kind, key):
    """key may be set at a layer of this kind (all_servers takes only
    who.replies and the on/off keys)."""
    return kind in spec.scopes and (
        kind != "all_servers" or key == "who.replies" or key.endswith(".enabled")
    )


def check_settable(st, path, key):
    """The spec of key if Discord may set it at the scope path, else raise
    SettingsError saying why. The one rule set_value and /settings share."""
    keystr = format_key(path, key) if path else key
    spec = st.spec(key)
    if spec is None:
        raise SettingsError(f"{keystr}: unknown setting")
    kind = _scope_kind(path)
    if path == ("top",):
        if key not in TOP_DISCORD_EDITABLE:
            raise SettingsError(f"{keystr}: not editable from Discord")
    elif key in TOP:
        raise SettingsError(f"{keystr}: a bot-wide setting has no scope")
    elif not _settable_at(spec, kind, key):
        raise SettingsError(f"{keystr}: cannot be set at {kind} scope")
    elif kind == "channel" and not (key in CHANNEL_SHORT.values() or _is_enabled_key(key)):
        raise SettingsError(f"{keystr}: {CHANNEL_ONLY}")
    if path[0] in ("servers", "alias") and (
        _id_str(path[1]) is None or (len(path) > SERVER_SCOPE_PARTS and _id_str(path[3]) is None)
    ):
        raise SettingsError(f"{keystr}: expected Discord ids")
    if not spec.discord_editable or spec.hidden:
        raise SettingsError(f"{keystr}: not editable from Discord")
    return spec


class _NoChange:
    """The result of a write that would change nothing (a sentinel: it
    equals no value; compare with `is NO_CHANGE`)."""

    def __repr__(self):
        return "NO_CHANGE"

    def __str__(self):
        return "no change"

    def __bool__(self):
        return False


NO_CHANGE = _NoChange()


_RANKS = {
    "who.replies": ("off", "when-addressed", "conversational", "freely"),
    "who.dms_from": ("owners", "shared-servers", "anyone"),
    "who.bots": ("ignore", "when-addressed"),
    "privacy.memory": ("nothing", "public-only", "everything"),
    "privacy.suspicious": ("ignore", "hold-for-review", "answer"),
}


def loosens(key, old, new, spec=None):
    """True when new lets the bot answer more people, share more, do more
    or reply more than old; False when it is the same or stricter; None
    when the two cannot be ordered (style, text, channels). Effective
    (resolved) values in, as Settings.resolve gives them."""
    if old == new:
        return False
    if key == "who.dms_from":
        return _dms_from_loosens(old, new)
    if key in _RANKS:
        r = _RANKS[key]
        if old in r and new in r:
            return r.index(new) > r.index(old)
        return None
    if spec is None:
        spec = CORE.get(key)
    if spec is None:
        return None
    return _typed_loosens(key, old, new, spec)


def _typed_loosens(key, old, new, spec):
    if spec.type == "on-off":
        return bool(new) and not bool(old)
    if spec.type == "number" and spec.none:
        # A cap: none = no cap at all.
        if new is None:
            return old is not None
        if old is None:
            return False
        return new > old
    if spec.type == "people":
        return _people_loosens(key, old, new)
    return None


def _people_loosens(key, old, new):
    if key.endswith(".ignore") or key == "who.ignore":
        if not isinstance(old, list) or not isinstance(new, list):
            return None
        return bool(set(old) - set(new))
    if new == "everyone":
        return old != "everyone"
    if old == "everyone":
        return False

    def people(v):
        return set() if v == "owners" else set(v or [])

    return bool(people(new) - people(old))


def _dms_from_loosens(old, new):
    """owners < a list of people < shared-servers < anyone, except that
    a list and shared-servers cannot be ordered (a listed person may
    share no server with the bot)."""

    def rank(v):
        if isinstance(v, list):
            return 1
        return {"owners": 0, "shared-servers": 2, "anyone": 3}.get(v)

    ro, rn = rank(old), rank(new)
    if ro is None or rn is None:
        return None
    if ro == rn == 1:
        return bool(set(new) - set(old))
    if {ro, rn} == {1, 2}:
        return None
    return rn > ro


def _is_enabled_key(key):
    """A core Extras on/off key."""
    return key in ("media.enabled", "proactive.enabled")
