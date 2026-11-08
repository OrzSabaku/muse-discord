"""muse-discord settings: schema, validation, scopes, the Discord overlay,
and configuration persistence.

The engine is shared by monitor.py, watchdog.sh and the bin/ tools.
PyYAML handles input; settings_schema supplies Pydantic field validation.
Display labels and server-name lookup live in settings_display.

Layers, least specific first (the most specific value that is set wins):

    built-in -> everywhere -> dms                                  (a DM)
    built-in -> everywhere -> all_servers -> servers.<id>
             -> category -> channel                         (a server)

Threads and forum posts use their parent channel. Lists replace, never
merge. Only style.notes combines: the most specific of everywhere /
dms / server, then the category's notes, then the channel's.

At every layer the overlay (config.discord.yaml, written by Discord-side
changes) replaces the file's value at that same layer, unless the file
was edited after the change (the overlay entry's base no longer matches
the file): then the file wins and the entry is retired into history.

Caps (limits.*, media.per_day) count within the scope WHERE THEY
ARE SET: everywhere = one total, a server = that server, dms = all DMs.
Every cap set along the chain is enforced (see Settings.cap_layers).

"""

import contextlib
import copy
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime

import yaml
from pydantic import ValidationError

from muse_discord.settings_display import (
    CATEGORY_TYPE,
    LABELS,
    VALUE_LABELS,
    ServerInfo,
    chain_names,
    change_location,
    describe_change,
    label,
    name_lookup,
    names_from,
    people_text,
    person_name,
    place_name,
    value_label,
)
from muse_discord.settings_display import (
    ValueChange as ValueChange,
)
from muse_discord.settings_schema import CORE, CORE_GROUPS, TOP, EngineSettings
from muse_discord.settings_schema import NOTES_MAX as NOTES_MAX
from muse_discord.settings_schema import PRESENCE_KINDS as PRESENCE_KINDS

PAIR_LENGTH = 2


SERVER_SCOPE_PARTS = 2
MIN_SERVER_KEY_PARTS = 3
MIN_CHANNEL_KEY_PARTS = 5
SOURCE_DATE_INDEX = 2

OVERLAY_NAME = "config.discord.yaml"
HISTORY_CAP = 100
DEFAULT_TOKEN_FILE = "~/.config/muse-discord/bot_token"
DEFAULT_PERSONA = "persona.md"
# Short key names a channel/category entry uses.
CHANNEL_SHORT = {"replies": "who.replies", "notes": "style.notes"}
_ID_RE = re.compile(r"^[0-9]{1,25}$")


_SAFE_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def _yload(stream):
    """yaml.safe_load, with libyaml's loader when PyYAML has it (the same
    YAML, several times faster: the settings files are read often)."""
    return yaml.load(stream, Loader=_SAFE_LOADER)


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


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


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


# Keys: overlay form 'servers."1".channels."2".replies'
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Scope of an event
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Parsing a new-layout dict into layer values
# ---------------------------------------------------------------------------
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


class _Parser:
    def __init__(self, file_label):
        self.file = file_label
        self.errors = []
        self.values = {}  # (path, key) -> value
        self.labels = {}  # server/channel id -> name label

    def err(self, where, msg):
        self.errors.append(f"{self.file}: {where}: {msg}")

    def spec(self, key):
        return spec_of(key)

    def set(self, path, key, value, where):
        spec = self.spec(key)
        kind = _scope_kind(path)
        if spec is None:
            if key.startswith("plugins."):
                self.err(where, f"unknown setting {key}: plugin support was removed")
            else:
                group = key.split(".")[0]
                valid = sorted(k for k in CORE if k.startswith(group + "."))
                self.err(
                    where,
                    "unknown setting"
                    + (f"; valid in {group}: {', '.join(valid)}" if valid else ""),
                )
            return
        if not _settable_at(spec, kind, key):
            where_ok = ", ".join(s for s in spec.scopes if _settable_at(spec, s, key))
            self.err(where, f"cannot be set at {kind} scope (it can be set in: {where_ok})")
            return
        ok, v = normalize(spec, value)
        if not ok:
            self.err(where, f"{v}; valid: {spec.valid_text()}")
            return
        self.values[(path, key)] = v

    def scope_block(self, path, block, where):
        if block is None:
            return
        if not isinstance(block, dict):
            self.err(where, "expected a block of settings")
            return
        kind = _scope_kind(path)
        for top, sub in block.items():
            w = f"{where}.{top}"
            if top == "name" and kind in ("server", "channel"):
                self.labels[path[-1]] = str(sub)
            elif top == "channels" and kind == "server":
                self.channel_blocks(path, sub, where)
            elif kind == "channel":
                self.channel_setting(path, top, sub, w)
            elif top in CORE_GROUPS:
                if not isinstance(sub, dict):
                    self.err(w, f"expected settings under {top}:")
                    continue
                for k, v in sub.items():
                    self.set(path, f"{top}.{k}", v, f"{w}.{k}")
            else:
                self.err(
                    w,
                    "unknown section; valid: "
                    + ", ".join(CORE_GROUPS + (("name", "channels") if kind == "server" else ())),
                )

    def channel_blocks(self, path, sub, where):
        if not isinstance(sub, dict):
            self.err(f"{where}.channels", "expected channel ids, each with a block")
            return
        for cid, cblock in sub.items():
            cid_s = _id_str(cid)
            cw = f'{where}.channels."{cid}"'
            if cid_s is None:
                self.err(
                    cw,
                    "expected a Discord channel or "
                    "category id (digits) as the key; put a "
                    "name in `name:` inside",
                )
                continue
            self.scope_block((*path, "channels", cid_s), cblock, cw)

    def channel_setting(self, path, top, sub, w):
        if top in CHANNEL_SHORT:
            self.set(path, CHANNEL_SHORT[top], sub, w)
        elif top in ("media", "proactive") and isinstance(sub, dict):
            for k, v in sub.items():
                if k != "enabled":
                    self.err(f"{w}.{k}", CHANNEL_ONLY)
                    continue
                self.set(path, f"{top}.enabled", v, f"{w}.{k}")
        else:
            self.err(w, CHANNEL_ONLY)

    def top_value(self, key, value, where):
        spec = TOP[key]
        ok, v = normalize(spec, value)
        if not ok:
            self.err(where, f"{v}; valid: {spec.valid_text()}")
            return
        self.values[(("top",), key)] = v


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def overlay_path_for(config_path):
    return os.path.join(os.path.dirname(os.path.abspath(config_path)), OVERLAY_NAME)


def _read_yaml(path):
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        data = _yload(f)
    return data if data is not None else {}


def server_config_paths(config_path):
    """Optional server blocks next to config.yaml, named by Discord id."""
    root = os.path.join(os.path.dirname(os.path.abspath(config_path)), "servers")
    if not os.path.isdir(root):
        return []
    return [os.path.join(root, name) for name in sorted(os.listdir(root)) if name.endswith(".yaml")]


class Settings:
    """A loaded, validated configuration (immutable; reload to change)."""

    def __init__(self):
        self.path = None
        self.overlay_path: str | None = None
        self.overlay_error: str | None = None
        self.raw = {}
        self.server_files = {}
        self.file_values = {}
        self.overlay = {}  # (path, key) -> entry dict (active ones)
        self.overlay_doc = {}
        self.retire = []  # [(keystr, entry, reason)]
        self.labels = {}
        self.engine = EngineSettings()
        self.engine_warnings = []
        self.owners = []
        self.persona = DEFAULT_PERSONA
        self.token_from = DEFAULT_TOKEN_FILE
        self._cache = {}

    # -- top level ---------------------------------------------------------
    def top(self, key):
        v = self.layer(("top",), key)
        return v[1] if v[0] else TOP[key].default

    @property
    def presence_status(self):
        return self.top("presence.status")

    @property
    def presence_kind(self):
        return self.top("presence.kind")

    @property
    def presence_enabled(self):
        return self.top("presence.enabled")

    @property
    def updates_notify(self):
        return self.top("updates.notify")

    def token_sources(self):
        """[("env", NAME) | ("file", path)], tried in order."""
        srcs = self.token_from
        if isinstance(srcs, str):
            srcs = [srcs]
        out = []
        for s in srcs:
            s = str(s)
            if s.startswith("env:"):
                out.append(("env", s[4:].strip()))
            else:
                out.append(("file", s))
        return out

    def guild_ids(self):
        return list(self.engine.guild_ids)

    # -- layers ------------------------------------------------------------
    def layer(self, path, key):
        """(set?, value, source) for one layer path (see chain())."""
        if path == ("builtin",):
            spec = self.spec(key)
            default = spec.default if spec is not None else None
            return True, copy.deepcopy(default), ("built-in",)
        if path == ("builtin_all_servers",):
            return False, None, None
        ov = self.overlay.get((path, key))
        if ov is not None:
            if ov.get("value") == "inherit":
                return False, None, None
            return True, ov["_norm"], ("discord", ov.get("by"), ov.get("at"))
        if (path, key) in self.file_values:
            return True, self.file_values[(path, key)], ("file",)
        return False, None, None

    def spec(self, key):
        return spec_of(key)

    def keys(self):
        return list(CORE)

    def chain(self, scope):
        """The layer paths for a scope, least specific first, each with a
        display name."""
        out: list[tuple[tuple[str, ...], str]] = [
            (("builtin",), "built-in"),
            (("everywhere",), "everywhere"),
        ]
        if scope.kind == "everywhere":
            return out
        if scope.kind == "dms":
            return [*out, (("dms",), "dms")]
        out += [
            (("builtin_all_servers",), "all_servers (built-in)"),
            (("all_servers",), "all_servers"),
        ]
        if scope.kind == "all_servers" or not scope.guild_id:
            return out
        gid = scope.guild_id
        out.append((("servers", gid), f"server {gid}"))
        if scope.category_id and scope.category_id != scope.channel_id:
            out.append(
                (("servers", gid, "channels", scope.category_id), f"category {scope.category_id}")
            )
        if scope.channel_id:
            out.append(
                (("servers", gid, "channels", scope.channel_id), f"channel {scope.channel_id}")
            )
        return out

    def resolve(self, scope):
        """{key: value} for every core key at this scope."""
        ck = scope.key()
        if ck in self._cache:
            return self._cache[ck]
        chain = self.chain(scope)
        out = {}
        for key in self.keys():
            out[key] = self._resolve_key(chain, key)[0]
        self._cache[ck] = out
        return out

    def get(self, key, scope):
        ck = scope.key()
        if ck in self._cache:
            return self._cache[ck][key]
        if self.spec(key) is None or key in TOP:
            return self.resolve(scope)[key]
        kk = ("key", ck, key)
        if kk not in self._cache:
            self._cache[kk] = self._resolve_key(self.chain(scope), key)[0]
        return self._cache[kk]

    def _resolve_key(self, chain, key):
        """(value, [(name, set?, value, source)] for every layer)."""
        rows = []
        for path, name in chain:
            s, v, src = self.layer(path, key)
            rows.append((name, path, s, v, src))
        if key == "style.notes":
            base, extra = "", []
            for _name, path, s, v, _src in rows:
                if not s:
                    continue
                if _scope_kind(path) == "channel":
                    if v:
                        extra.append(v)
                else:
                    base = v
            return "\n".join(p for p in [base, *extra] if p), rows
        value, win = None, None
        for _name, path, s, v, _src in reversed(rows):
            if s:
                value, win = v, path
                break
        if (
            win is not None
            and win[0] == "servers"
            and entry_role(self.overlay.get((win, key))) == "admin"
        ):
            # A server admin's entry never outvotes the owners: it is the
            # quieter of its own value and what the owners' layers say
            # (an owner pausing the server reaches the admin's channel).
            value = _quieter(key, value, self._owner_value(chain, key), self.spec(key))
        return value, rows

    def _owner_layer(self, path, key):
        """(set?, value) for one layer as the owners and config.yaml set
        it: a server admin's entry is replaced by what it covers."""
        ov = self.overlay.get((path, key))
        if ov is None or entry_role(ov) != "admin":
            s, v, _src = self.layer(path, key)
            return s, v
        below = _entry_below(ov, self.overlay_doc or {}, format_key(path, key), self.owners)
        if not below:
            if (path, key) in self.file_values:
                return True, self.file_values[(path, key)]
            return False, None
        v = below.get("value")
        if v == "inherit":
            return False, None
        ok, norm = normalize(self.spec(key), v)
        return (True, norm) if ok else (False, None)

    def owner_sets_server(self, guild_id, key):
        """True when config.yaml or an owner's Discord change sets key for
        this server itself (a server admin's change does not count)."""
        return self._owner_layer(("servers", str(guild_id)), key)[0]

    def _owner_value(self, chain, key):
        for path, _name in reversed(chain):
            s, v = self._owner_layer(path, key)
            if s:
                return v
        return None

    def explain(self, scope):
        """[(key, value, source, chain rows)] for every key at scope.
        source: "built-in", "config.yaml", "Discord change by <id> at
        <time>", or "inherited from <scope> (...)"."""
        chain = self.chain(scope)
        here = chain[-1][0]
        out = []
        for key in self.keys():
            value, rows = self._resolve_key(chain, key)
            src = "built-in"
            for name, path, s, _v, srctag in reversed(rows):
                if not s:
                    continue
                desc = _source_text(srctag)
                if path == here or path[0] == "builtin":
                    src = desc
                else:
                    src = f"inherited from {name} ({desc})"
                if key == "style.notes":
                    parts = [r for r in rows if r[2] and r[3] and _scope_kind(r[1]) == "channel"]
                    base = [r for r in rows if r[2] and _scope_kind(r[1]) != "channel"]
                    if parts and value:
                        names = (
                            [base[-1][0]]
                            if base and base[-1][3] and base[-1][1][0] != "builtin"
                            else []
                        )
                        src = "combined: " + ", ".join(names + [r[0] for r in parts])
                break
            out.append((key, value, src, rows))
        return out

    def trace(self, scope, key):
        """(value, [(name, path, set?, value, source)] for every layer of
        scope's chain, least specific first)."""
        return self._resolve_key(self.chain(scope), key)

    def value_without(self, scope, key, path):
        """What key would resolve to at scope if the layer `path` did not
        set it (what a reset to inherit there would give)."""
        if path == ("top",):
            return copy.deepcopy(TOP[key].default)
        chain = [(p, n) for p, n in self.chain(scope) if p != path]
        return self._resolve_key(chain, key)[0]

    def cap_layers(self, key, scope):
        """[(counter id, cap)] for every layer along the chain that sets a
        cap (not none) for key: each counts only what happens in its own
        scope (everywhere = all of it, "server:<id>", "dms")."""
        out = []
        for path, _name in self.chain(scope):
            if path[0] in ("builtin", "builtin_all_servers"):
                continue
            s, v, _src = self.layer(path, key)
            if not s or v is None:
                continue
            if path[0] == "everywhere":
                cid = "everywhere"
            elif path[0] == "dms":
                cid = "dms"
            elif path[0] in ("servers", "alias"):
                cid = f"server:{scope.guild_id}"
            else:
                continue
            out.append((cid, v))
        return out

    @staticmethod
    def counter_ids(scope):
        """The counters a counted event in this scope adds to."""
        if scope.kind == "dms":
            return ["everywhere", "dms"]
        if scope.guild_id:
            return ["everywhere", f"server:{scope.guild_id}"]
        return ["everywhere"]

    def servers_listed(self):
        """{server id: label} for every server the files mention."""
        out = {}
        for path, _k in list(self.file_values) + list(self.overlay):
            if path[0] == "servers":
                out.setdefault(path[1], self.labels.get(path[1]))
        return out

    def channels_listed(self, gid):
        out = set()
        for path, _k in list(self.file_values) + list(self.overlay):
            if path[0] == "servers" and path[1] == gid and len(path) > SERVER_SCOPE_PARTS:
                out.add(path[3])
        return sorted(out)

    # -- overlay retirements -----------------------------------------------
    def commit_retirements(self):
        """Apply the conflict rule for real: move retired overlay entries
        into history (rewriting config.discord.yaml) and return one owner
        notice per entry. Only the monitor calls this; readers (the CLI)
        just see the file winning."""
        if not self.retire:
            return []
        with overlay_lock(self.overlay_path):
            doc = _read_overlay_doc(self.overlay_path, strict=True)
            values = doc.get("values") or {}
            notices = []
            now = _now()
            for keystr, entry, reason in self.retire:
                cur = values.get(keystr)
                if keystr not in values or not _same_entry(cur, entry):
                    continue  # replaced since this Settings was loaded
                values.pop(keystr, None)
                doc.setdefault("history", []).append(
                    {
                        "key": keystr,
                        "from": entry.get("value"),
                        "to": None,
                        "by": entry.get("by"),
                        "at": now,
                        "retired": reason,
                    }
                )
                notices.append(_retire_notice(keystr, entry, reason))
            doc["values"] = values
            doc["history"] = _trim_history(doc.get("history") or [])
            _write_overlay_doc(self.overlay_path, doc)
        self.retire = []
        return notices


def _quieter(key, own, owners, spec=None):
    """The quieter of a server admin's value and the owners' value."""
    if key == "who.replies":
        r = ("off", "when-addressed", "conversational", "freely")
        if own in r and owners in r:
            return r[min(r.index(own), r.index(owners))]
        return own
    if spec is not None and spec.type == "on-off":
        return bool(own) and bool(owners)
    if key == "who.ignore" or key.endswith(".ignore"):
        a = own if isinstance(own, list) else []
        b = owners if isinstance(owners, list) else []
        return a + [p for p in b if p not in a]
    return owners if loosens(key, owners, own, spec) else own


def _source_text(src):
    if not src:
        return "not set"
    if src[0] == "built-in":
        return "built-in" if len(src) == 1 else "built-in for servers"
    if src[0] == "file":
        return "config.yaml"
    return f"Discord change by {src[1]} at {src[2]}"


def _retire_notice(keystr, entry, reason):
    if reason == "file changed":
        return (
            f"settings: you edited `{keystr}` in config.yaml after it "
            f"was changed in Discord (to {_show(entry.get('value'))} by "
            f"{entry.get('by')} at {entry.get('at')}). The file wins; "
            f"the Discord change is undone and kept in "
            f"{OVERLAY_NAME} history."
        )
    return (
        f"settings: the Discord change to `{keystr}` "
        f"({_show(entry.get('value'))}) no longer applies ({reason}); "
        f"it was moved to {OVERLAY_NAME} history."
    )


def _now():
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _same_entry(a, b):
    if not isinstance(a, dict) or not isinstance(b, dict):
        return a == b
    return all(a.get(k) == b.get(k) for k in ("value", "by", "at", "base"))


def _read_overlay_doc(path, strict=False):
    """The overlay document. strict (every writer): raise SettingsError
    when the file exists but cannot be read or is not an overlay, so a
    write never wipes a file someone broke by hand; otherwise such a
    file reads as empty."""
    try:
        doc = _read_yaml(path)
    except (OSError, yaml.YAMLError) as e:
        if strict:
            raise SettingsError(
                f"{OVERLAY_NAME} cannot be read ({e}); nothing was changed. "
                f"Fix the file, or delete it to drop every change made "
                f"from Discord."
            ) from e
        return {"version": 1, "values": {}, "history": []}
    if strict and (
        not isinstance(doc, dict)
        or not isinstance(doc.get("values", {}) or {}, dict)
        or not isinstance(doc.get("history", []) or [], list)
    ):
        raise SettingsError(
            f"{OVERLAY_NAME} is not a settings overlay (expected values: "
            f"and history:); nothing was changed. Fix the file, or delete "
            f"it to drop every change made from Discord."
        )
    if not isinstance(doc, dict):
        doc = {}
    if not isinstance(doc.get("values"), dict):
        doc["values"] = {}
    if not isinstance(doc.get("history"), list):
        doc["history"] = []
    doc["version"] = 1
    return doc


def _trim_history(hist):
    """The last HISTORY_CAP lines, never splitting a group: when the cut
    falls inside one, the rest of that group goes too."""
    if len(hist) <= HISTORY_CAP:
        return hist
    cut = len(hist) - HISTORY_CAP

    def group(h):
        return h.get("group") if isinstance(h, dict) else None

    while (
        cut < len(hist)
        and group(hist[cut]) is not None
        and group(hist[cut]) == group(hist[cut - 1])
    ):
        cut += 1
    return hist[cut:]


_LOCKS = threading.local()


@contextlib.contextmanager
def overlay_lock(overlay_path):
    """Exclusive lock on config.discord.yaml for one read-check-write,
    across processes (the monitor, bin/settings.py): flock on
    config.discord.yaml.lock. Re-entrant within a thread."""
    key = os.path.abspath(overlay_path)
    held = getattr(_LOCKS, "held", None)
    if held is None:
        held = _LOCKS.held = set()
    if key in held:
        yield
        return
    fd = os.open(key + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        held.add(key)
        try:
            yield
        finally:
            held.discard(key)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


OVERLAY_HEADER = (
    "# Written by muse-discord for settings changed from Discord. Safe to\n"
    "# delete: everything then follows config.yaml again. Each value names\n"
    "# the file's value it replaced (base); edit config.yaml at that spot\n"
    "# and the file wins again. `bin/settings.py fold` copies these into\n"
    "# config.yaml.\n"
)


def atomic_write_text(path, text):
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        mode = 0o600
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, prefix="." + os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), mode)
            f.write(text)
            f.flush()
            os.fsync(f.fileno())  # never an empty file after a crash
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _write_overlay_doc(path, doc):
    body = yaml.safe_dump(
        {"version": 1, "values": doc.get("values") or {}, "history": doc.get("history") or []},
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=1000,
    )
    atomic_write_text(path, OVERLAY_HEADER + body)


def load(config_path, overlay_path=None, text=None):
    """Load and validate config.yaml (+ its overlay). Raises SettingsError
    on an invalid hand-written file; overlay problems never raise (the
    entries are queued for retirement instead). text: parse this instead
    of reading config_path (configuration preview)."""
    st = Settings()
    st.path = config_path
    st.overlay_path = overlay_path or overlay_path_for(config_path)
    label = os.path.basename(config_path)
    try:
        raw = _yload(text) if text is not None else _read_yaml(config_path)
    except yaml.YAMLError as e:
        raise SettingsError(f"{label}: not valid YAML ({e})") from e
    except OSError as e:
        raise SettingsError(f"{label}: unreadable ({e})") from e
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise SettingsError(
            f"{label}: expected settings (key: value lines), got {type(raw).__name__}"
        )
    st.raw = raw
    p = _Parser(label)
    _parse_new(p, raw, st)
    for path in server_config_paths(config_path):
        sid = os.path.basename(path)[:-5]
        if _id_str(sid) is None:
            p.err(path, "filename must be a Discord server id followed by .yaml")
            continue
        inline = raw.get("servers")
        if isinstance(inline, dict) and sid in {str(k) for k in inline}:
            p.err(path, f"server {sid} is also defined in config.yaml")
            continue
        try:
            block = _read_yaml(path)
        except (OSError, yaml.YAMLError) as e:
            raise SettingsError(f"{path}: unreadable ({e})") from e
        p.scope_block(("servers", sid), block, path)
        st.server_files[sid] = path
    if p.errors:
        raise SettingsError("\n".join(p.errors))
    st.file_values = p.values
    st.labels = p.labels
    _apply_overlay(st)
    return st


def _parse_identity(p, data, st):
    owners = data.get("owners")
    if owners is not None:
        if not isinstance(owners, list):
            p.err("owners", 'expected a list of user ids, like ["1234"]')
        else:
            for o in owners:
                if _id_str(o) is None:
                    p.err("owners", f"{_show(o)}: expected a Discord user id (digits)")
            st.owners = [str(o) for o in owners]
    persona = data.get("persona")
    if persona is not None:
        if not isinstance(persona, str) or not persona.strip():
            p.err("persona", "expected a file path, like persona.md")
        else:
            st.persona = persona.strip()


def _parse_token_source(p, data, st):
    tf = data.get("token_from")
    if tf is not None:
        items = tf if isinstance(tf, list) else [tf]
        if not items or not all(
            isinstance(i, str) and i.strip() and i.strip() != "env:" for i in items
        ):
            p.err("token_from", "expected env:NAME or a file path (never the token itself)")
        elif any(re.fullmatch(r"[A-Za-z0-9_.\-]{50,}", i.strip()) for i in items):
            p.err(
                "token_from",
                "this looks like a token itself; store the "
                "token in a file (mode 600) and give its path",
            )
        else:
            st.token_from = [i.strip() for i in items] if isinstance(tf, list) else tf.strip()


def _parse_bot_settings(p, data, st):
    for sect, keys in (
        ("presence", ("status", "kind", "enabled")),
        ("updates", ("notify",)),
        ("telemetry", ("usage_stats",)),
        ("memory", ("save_dms",)),
    ):
        block = data.get(sect)
        if block is None:
            continue
        if not isinstance(block, dict):
            p.err(sect, "expected a block")
            continue
        for k, val in block.items():
            if k not in keys:
                p.err(
                    f"{sect}.{k}",
                    "unknown setting; valid: " + ", ".join(x for x in keys if x != "enabled"),
                )
                continue
            p.top_value(f"{sect}.{k}", val, f"{sect}.{k}")


def _parse_servers(p, data, st):
    servers = data.get("servers")
    if servers is not None:
        if not isinstance(servers, dict):
            p.err("servers", "expected server ids, each with a block")
        else:
            for sid, block in servers.items():
                sid_s = _id_str(sid)
                where = f'servers."{sid}"'
                if sid_s is None:
                    p.err(where, "expected a Discord server id (digits) as the key")
                    continue
                p.scope_block(("servers", sid_s), block, where)


def _parse_engine(p, data, st):
    engine = data.get("engine")
    try:
        st.engine = EngineSettings.model_validate(
            {} if engine is None else engine, context={"warnings": st.engine_warnings}
        )
    except ValidationError as exc:
        for error in exc.errors(include_url=False):
            path = ".".join(str(part) for part in ("engine", *error["loc"]))
            p.err(path, error["msg"])


def _parse_new(p, data, st):
    allowed = {
        "version",
        "owners",
        "persona",
        "token_from",
        "presence",
        "updates",
        "telemetry",
        "memory",
        "everywhere",
        "dms",
        "all_servers",
        "servers",
        "engine",
        "testing",
    }
    for k in data:
        if k not in allowed:
            p.err(
                str(k), "unknown top-level key; valid: " + ", ".join(sorted(allowed - {"testing"}))
            )
    v = data.get("version")
    if v is not None and v != 1:
        p.err("version", f"{v!r} is not supported; valid: 1")
    _parse_identity(p, data, st)
    _parse_token_source(p, data, st)
    _parse_bot_settings(p, data, st)
    for scope in ("everywhere", "dms", "all_servers"):
        p.scope_block((scope,), data.get(scope), scope)
    _parse_servers(p, data, st)
    _parse_engine(p, data, st)


def _overlay_values(st):
    try:
        doc = _read_yaml(st.overlay_path)
    except (OSError, yaml.YAMLError) as e:
        st.overlay_doc = {}
        st.retire = []
        st.overlay_error = f"{OVERLAY_NAME} unreadable ({e}); ignored"
        return
    st.overlay_error = None
    if not isinstance(doc, dict):
        doc = {}
    st.overlay_doc = doc
    values = doc.get("values") or {}
    if not isinstance(values, dict):
        values = {}
    return values


def _overlay_scope_problem(st, path, key):
    spec = st.spec(key)
    if spec is None:
        return "no such setting anymore"
    if path == ("top",):
        return None if key in TOP_DISCORD_EDITABLE else "not editable from Discord"
    if not _settable_at(spec, _scope_kind(path), key):
        return f"cannot be set at {_scope_kind(path)} scope"
    if path[0] == "servers" and (
        _id_str(path[1]) is None or (len(path) > SERVER_SCOPE_PARTS and _id_str(path[3]) is None)
    ):
        return "expected Discord ids in the key"
    if not spec.discord_editable:
        return "not editable from Discord"
    return None


def _normalize_overlay_entry(spec, entry):
    if entry.get("value") == "inherit":
        return entry, None
    ok, norm = normalize(spec, entry.get("value"))
    return (dict(entry, _norm=norm), None) if ok else (entry, f"invalid value: {norm}")


def _overlay_matches_file(st, path, key, entry):
    base = entry.get("base")
    if base is None and (path, key) not in st.file_values:
        return True
    ok, normalized = normalize(st.spec(key), base) if base is not None else (True, None)
    return ok and normalized == st.file_values.get((path, key))


def _apply_overlay(st):
    values = _overlay_values(st)
    if values is None:
        return
    for keystr, entry in values.items():
        if not isinstance(entry, dict) or "value" not in entry:
            st.retire.append(
                (
                    str(keystr),
                    entry if isinstance(entry, dict) else {"value": entry},
                    "not a valid entry",
                )
            )
            continue
        reason = None
        try:
            path, key = parse_key(keystr)
        except ValueError as e:
            path, key, reason = None, None, f"not a settings key ({e})"
        if reason is None and path is not None:
            reason = _overlay_scope_problem(st, path, key)
        if reason is None:
            entry, reason = _normalize_overlay_entry(st.spec(key), entry)
        if reason is None and not _overlay_matches_file(st, path, key, entry):
            reason = "file changed"
        if reason is not None:
            st.retire.append((str(keystr), entry, reason))
            continue
        st.overlay[(path, key)] = entry


# ---------------------------------------------------------------------------
# Write API (overlay)
# ---------------------------------------------------------------------------
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


def entry_role(entry, owners=()):
    """ "admin" when a server admin (not an owner) wrote this overlay entry
    or history line: recognized only by the role: admin it records.
    Anything else (an owner, the CLI, muse.ai, an older entry with no
    role) is owner-level, which a server admin can never revert."""
    if isinstance(entry, dict) and entry.get("role") == "admin":
        return "admin"
    return "owner"


def _infer_role(by, owners):
    """The role of a write that names none (the legacy set_value / reset
    / undo entry points /settings uses): only owners and server admins
    write there, so a user id that is not an owner is a server admin. A
    name such as "muse.ai" or "cli" is owner-level."""
    if _id_str(by) is not None and str(by) not in {str(o) for o in owners}:
        return "admin"
    return "owner"


def new_group_id(prefix="g"):
    """A fresh history group id: prefix + 10 random hex characters, e.g.
    "g3f9a1c2b7d" (group ids are at most 24 characters: custom_ids carry
    them)."""
    return f"{prefix}{secrets.token_hex(5)}"


def _layer_noop(st, path, key, value, role="owner"):
    """True when the layer already holds value ("inherit": the layer
    sets nothing). An owner's write onto a server admin's entry is never
    a no-op: it makes the value the owners' own."""
    if role != "admin" and entry_role(st.overlay.get((path, key))) == "admin":
        return False
    s, cur, _src = st.layer(path, key)
    if value == "inherit":
        return not s
    return s and cur == value


def _below_from_history(doc, keystr, owners):
    """The layer's value before the run of admin changes that ends at the
    current entry ({"value": v}, v may be "inherit"), from history;
    {} when history does not say (no entry: the file shows)."""
    cand = None
    for h in reversed(doc.get("history") or []):
        if not isinstance(h, dict) or h.get("key") != keystr:
            continue
        if h.get("retired") or h.get("folded"):
            break
        if "to" not in h:
            continue
        if entry_role(h, owners) != "admin":
            break
        cand = h.get("from", "inherit")
    return {} if cand is None else {"value": cand}


def _entry_below(entry, doc, keystr, owners):
    below = entry.get("below") if isinstance(entry, dict) else None
    if isinstance(below, dict):
        return dict(below)
    return _below_from_history(doc, keystr, owners)


def _below_for_write(st, path, key):
    """What an admin's new entry at (path, key) covers: the owners' layer
    value ({"value": v}) or {} (no entry: the file shows)."""
    cur = st.overlay.get((path, key))
    if cur is None:
        return {}
    if entry_role(cur) == "admin":
        return _entry_below(cur, st.overlay_doc or {}, format_key(path, key), st.owners)
    return {"value": cur.get("value")}


def owner_layer(doc, keystr, owners):
    """What the owners (or the file) have at one layer, below any server
    admin's entry: {} (no overlay entry: the file shows) or
    {"value": v}."""
    cur = (doc.get("values") or {}).get(keystr)
    if not isinstance(cur, dict) or "value" not in cur:
        return {}
    if entry_role(cur, owners) != "admin":
        return {"value": cur["value"]}
    return _entry_below(cur, doc, keystr, owners)


@dataclass
class WriteStamp:
    by: str
    at: str
    group: str | None = None
    role: str | None = None


def _write_entry(st, doc, planned, stamp):
    """Record one overlay entry and its history line in doc (not saved).
    value: the display value, or "inherit"."""
    path, key, spec, value = planned.path, planned.key, planned.spec, planned.write
    by, now, group, role = stamp.by, stamp.at, stamp.group, stamp.role
    keystr = format_key(path, key)
    if role is None:
        role = _infer_role(by, st.owners)
    base = st.file_values.get((path, key))
    s, before, _src = st.layer(path, key)
    entry = {"value": value, "base": base, "by": str(by), "at": now}
    hist = {
        "key": keystr,
        "from": before if s else "inherit",
        "to": value,
        "by": str(by),
        "at": now,
    }
    if group:
        hist["group"] = group
    if role == "admin":
        entry["role"] = hist["role"] = "admin"
        entry["below"] = owner_layer(doc, keystr, st.owners)
    if key == "who.replies" and value == "off":
        # What Resume restores, kept with the pause (history is capped).
        prev = doc["values"].get(keystr)
        if hist["from"] != "off":
            entry["resume"] = hist["from"]
        elif isinstance(prev, dict) and "resume" in prev:
            entry["resume"] = prev["resume"]
    if (
        role != "admin"
        and value != "inherit"
        and (path, key) in st.file_values
        and normalize(spec, value) == (True, base)
    ):
        # config.yaml says this at this very layer: it stays the source
        # of truth, so the overlay entry goes instead.
        doc["values"].pop(keystr, None)
        hist["dropped"] = True
    else:
        doc["values"][keystr] = entry
    doc["history"].append(hist)
    return keystr


@dataclass
class WriteActor:
    by: str
    group: str | None = None
    role: str | None = None
    admin_gid: str | None = None
    info: object = None


def set_value(config_path, keystr, value, actor):
    """Record a Discord-side change: overlay entry {value, base, by, at}
    plus a history line (last HISTORY_CAP kept). value "inherit" resets
    the key at that scope. Raises SettingsError for an invalid key/value.
    Returns the normalized display value written, or NO_CHANGE (nothing
    written) when that layer already holds the value. role: "owner" or
    "admin" (default: an admin when by is a user id that is not an
    owner); group: a history group id (see apply)."""
    by = actor.by
    group = actor.group
    role = actor.role
    group = _check_group(group)
    with overlay_lock(overlay_path_for(config_path)):
        st = load(config_path)
        try:
            path, key = parse_key(keystr)
        except ValueError as e:
            raise SettingsError(f"{keystr}: not a settings key ({e})") from e
        spec = check_settable(st, path, key)
        if role is None:
            role = _infer_role(by, st.owners)
        if role == "admin":
            if path[0] != "servers":
                raise SettingsError(ADMIN_QUIETER)
            value, _rev = _admin_translate(st, path, key, spec, value)
        norm = "inherit"
        if value != "inherit":
            ok, norm = normalize(spec, value)
            if not ok:
                raise SettingsError(f"{keystr}: {norm}; valid: {spec.valid_text()}")
            value = norm
        if _layer_noop(st, path, key, norm, role):
            return NO_CHANGE
        doc = _read_overlay_doc(st.overlay_path, strict=True)
        _write_entry(
            st,
            doc,
            Planned(Change(place_of_path(path), key, value, path=path), path, spec, value, False),
            WriteStamp(by, _now(), group, role),
        )
        doc["history"] = _trim_history(doc["history"])
        _write_overlay_doc(st.overlay_path, doc)
    return value


def reset(config_path, keystr, by, group=None, role=None):
    """Act as if this scope did not set the key (writes inherit; nothing
    when the scope sets nothing there already: NO_CHANGE)."""
    return set_value(
        config_path=config_path,
        keystr=keystr,
        value="inherit",
        actor=WriteActor(by=by, group=group, role=role),
    )


# Which way a change goes: the most permissive value last. Used by
# /settings for the server-admin view (restrict-only) and risky changes.
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


def _undo_match(prefix, children=False):
    """match(path, key) for the history lines an Undo at prefix sees.
    prefix: a scope prefix (scope_prefix; "everywhere" includes the
    bot-wide settings), "defaults" or a Place. children: a server's
    Undo also sees its channels and categories."""
    if isinstance(prefix, Place):
        place = prefix
    elif str(prefix) == "defaults":
        place = Place.defaults()
    else:
        path = parse_scope_prefix(prefix)
        paths = {path, ("top",)} if path == ("everywhere",) else {path}

        def match(p, _k):
            return p in paths or (
                children
                and path[0] == "servers"
                and len(path) == SERVER_SCOPE_PARTS
                and p[:2] == path
            )

        return match
    return lambda p, k: place.owns(p, k, children)


def _undo_group(doc, match, accept=None):
    """[(history index, key string, path, key, value to restore)] for
    the newest change matching match(path, key) that is still in effect
    and not undone, plus (when it was written as part of a group) every
    other change of its group still in effect, wherever it is. Empty
    when there is nothing to undo. accept(history line) narrows the
    first pick (an admin's own changes)."""
    hist = doc.get("history") or []
    values = doc.get("values") or {}

    def cand(i):
        h = hist[i]
        if (
            not isinstance(h, dict)
            or "to" not in h
            or h.get("undo")
            or h.get("undone")
            or h.get("retired")
            or h.get("folded")
        ):
            return None
        try:
            path, key = parse_key(h.get("key"))
        except ValueError:
            return None
        ks = format_key(path, key)
        live = _line_live(h, values)
        return (i, ks, path, key, h.get("from", "inherit")) if live else None

    for i in range(len(hist) - 1, -1, -1):
        c = cand(i)
        if (
            c is None
            or not match(c[2], c[3])
            or (accept is not None and not accept(hist[i]))
            or _touched_later(hist, i, c[1])
        ):
            continue
        gid = hist[i].get("group")
        if not gid:
            return [c]
        out, seen = [], set()
        for j in range(len(hist) - 1, -1, -1):
            if isinstance(hist[j], dict) and hist[j].get("group") == gid:
                cj = cand(j)
                if cj is None or cj[1] in seen or (j != i and _touched_later(hist, j, cj[1])):
                    continue  # changed again since: not this group's now
                seen.add(cj[1])
                out.append(cj)
        return out
    return []


def _touched_later(hist, j, ks):
    """A later history line (not an undo of something newer) changed ks."""
    for h in hist[j + 1 :]:
        if isinstance(h, dict) and h.get("key") == ks and not h.get("undo") and not h.get("undone"):
            return True
    return False


def _admin_accept(owners, guild_id):
    gid = str(guild_id)

    def accept(h):
        try:
            path, _k = parse_key(h.get("key"))
        except ValueError:
            return False
        return entry_role(h, owners) == "admin" and path[0] == "servers" and path[1] == gid

    return accept


def undo_plan(st, prefix, children=False, admin_gid=None):
    """The changes undo() would make, as [Change] (value: what each key
    goes back to at its layer, or "inherit"), newest first; [] when
    there is nothing to undo. admin_gid: a server admin's Undo, which
    sees only server admins' changes in that server."""
    accept = _admin_accept(st.owners, admin_gid) if admin_gid else None
    picks = _undo_group(st.overlay_doc or {}, _undo_match(prefix, children), accept)
    return [
        Change(place_of_path(path), key, target, path=path) for _i, _ks, path, key, target in picks
    ]


def undo_changes(config_path, prefix, actor, children=False, expect=None):
    """Revert the newest change still in effect at one place, and with
    it every change of the same group (one apply, preset or pause),
    in one file write. Old history lines without a group undo one at a
    time; undo lines are never undone (no redo). prefix: see
    _undo_match; children: a server's Undo also covers its channels.
    admin_gid: a server admin's Undo: only server admins' changes in
    that server, checked by admin_check (SettingsError when refused).
    expect: the id undo_preview gave; SettingsError when the Undo would
    now revert something else (a stale button). undo_id: the group id
    for the undo's own history lines (as undo_group). Returns the key
    strings reverted, newest first ([]: nothing to undo)."""
    by = actor.by
    admin_gid = actor.admin_gid
    info = actor.info
    role = actor.role
    undo_id = actor.group
    undo_id = _check_group(undo_id)
    with overlay_lock(overlay_path_for(config_path)):
        st = load(config_path)
        doc = _read_overlay_doc(st.overlay_path, strict=True)
        accept = _admin_accept(st.owners, admin_gid) if admin_gid else None
        picks = _undo_group(doc, _undo_match(prefix, children), accept)
        if expect is not None and _undo_ident(doc, picks) != expect:
            raise SettingsError("Something changed since; nothing was undone.")
        if not picks:
            return []
        if admin_gid:
            admin_check(
                st,
                admin_gid,
                [Change(place_of_path(p), k, t, path=p) for _i, _ks, p, k, t in picks],
                info,
                revert=True,
            )
            role = "admin"
        elif role == "admin":
            raise SettingsError("a server admin's undo needs admin_gid")
        done = _revert(st, doc, picks, WriteStamp(by, _now(), undo_id, role))
        _write_overlay_doc(st.overlay_path, doc)
    return done


def undo(config_path, prefix, actor, children=False, expect=None):
    """Revert the newest Discord change made at one scope (prefix as
    scope_prefix gives; "everywhere" includes the bot-wide settings) that
    is still in effect, with the rest of its group (see undo_changes).
    Repeated calls step further back. Returns the key string of the
    newest change reverted, or None when there is nothing to undo."""
    by = actor.by
    admin_gid = actor.admin_gid
    info = actor.info
    done = undo_changes(
        config_path=config_path,
        prefix=prefix,
        children=children,
        expect=expect,
        actor=WriteActor(by=by, admin_gid=admin_gid, info=info),
    )
    return done[0] if done else None


def backup(path):
    """Copy path to path.bak-<UTC timestamp>; returns the backup path."""
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    dest = f"{path}.bak-{stamp}"
    n = 1
    while os.path.exists(dest):
        n += 1
        dest = f"{path}.bak-{stamp}-{n}"
    shutil.copy2(path, dest)
    return dest


def dump_config(data):
    return yaml.safe_dump(
        data, sort_keys=False, default_flow_style=False, allow_unicode=True, width=78
    )


def fold(config_path):
    """Copy the overlay's values (not server admins' entries) into
    config.yaml and clear them. The file is rewritten (comments are
    lost); a timestamped backup is kept. Returns (backup path, [folded
    keys]), or (None, []) when there is nothing to fold."""
    with overlay_lock(overlay_path_for(config_path)):
        return _fold(config_path)


def _normalize_channel_ids(block):
    if isinstance(block, dict) and isinstance(block.get("channels"), dict):
        block["channels"] = _by_str_id(block["channels"])


def _fold_node_path(path, key):
    if path == ("top",):
        node_path = key.split(".")
    else:
        node_path = list(_node_prefix(path))
        if len(path) > SERVER_SCOPE_PARTS:
            short = {v: k for k, v in CHANNEL_SHORT.items()}
            node_path += short[key].split(".") if key in short else key.split(".")
        else:
            node_path += key.split(".")
    return node_path


def _fold(config_path):
    st = load(config_path)
    # A server admin's entry stays in the overlay: in config.yaml it
    # would be the owners' own value, no longer held to the quieter of
    # the two (Settings._resolve_key).
    todo = sorted(
        ((pk, e) for pk, e in st.overlay.items() if entry_role(e) != "admin"),
        key=lambda kv: format_key(*kv[0]),
    )
    if not todo:
        return None, []
    data = copy.deepcopy(st.raw)
    # The paths below use string ids.
    if isinstance(data.get("servers"), dict):
        data["servers"] = _by_str_id(data["servers"])
        for block in data["servers"].values():
            _normalize_channel_ids(block)
    external = {
        sid: _read_yaml(filename)
        for sid, filename in st.server_files.items()
        if any(path[:2] == ("servers", sid) for (path, key), entry in todo)
    }
    for block in external.values():
        _normalize_channel_ids(block)
    folded = []
    for (path, key), entry in todo:
        node_path = _fold_node_path(path, key)
        target = data
        if path[0] == "servers" and path[1] in external:
            target = external[path[1]]
            node_path = node_path[2:]
        if entry.get("value") == "inherit":
            _del_path(target, node_path)
        else:
            _set_path(target, node_path, entry["_norm"])
        folded.append(format_key(path, key))
    with overlay_lock(st.overlay_path):
        doc = _read_overlay_doc(st.overlay_path, strict=True)
        bak = backup(config_path)
        for sid, block in external.items():
            filename = st.server_files[sid]
            backup(filename)
            atomic_write_text(filename, dump_config(block))
        atomic_write_text(config_path, dump_config(data))
        now = _now()
        for k in folded:
            doc["values"].pop(k, None)
            doc["history"].append({"key": k, "folded": now})
        doc["history"] = _trim_history(doc["history"])
        _write_overlay_doc(st.overlay_path, doc)
    return bak, folded


def _by_str_id(blocks):
    """{id: block} with string ids. An id written both unquoted (a
    number) and quoted is one block, merged as the loader reads them:
    setting by setting, the later spelling winning."""
    out = {}
    for k, v in blocks.items():
        k = _id_str(k) or str(k)
        out[k] = _merged(out[k], v) if k in out else v
    return out


def _merged(a, b):
    if not (isinstance(a, dict) and isinstance(b, dict)):
        return b
    out = dict(a)
    for k, v in b.items():
        out[k] = _merged(out[k], v) if k in out else v
    return out


def _node_prefix(path):
    if path[0] == "servers":
        out = ["servers", path[1]]
        if len(path) > SERVER_SCOPE_PARTS:
            out += ["channels", path[3]]
        return out
    return [path[0]]


def _set_path(d, parts, value):
    cur = d
    for p in parts[:-1]:
        if not isinstance(cur.get(p), dict):
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value


def _del_path(d, parts):
    stack = []
    cur = d
    for p in parts[:-1]:
        if not isinstance(cur.get(p), dict):
            return
        stack.append((cur, p))
        cur = cur[p]
    cur.pop(parts[-1], None)
    for parent, p in reversed(stack):
        if parent[p] == {}:
            del parent[p]


# ---------------------------------------------------------------------------
# Settings v2: places, words, grouped changes, risk by outcome, the
# server-admin revert rule, presets, pause. Pure (no network): callers
# pass what they know about servers and names (info, names).
# ---------------------------------------------------------------------------
BIG_FOR_MEMORY = 50  # members: memory "everything" warns harder
BIG_FOR_CONVERSATIONAL = 500
BIG_FOR_CAPS = 500  # members: removing a reply cap is risky
MEDIA_PER_DAY_RISKY = 20
BOT_EXCHANGE_RISKY = 10
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


def _is_enabled_key(key):
    """A core Extras on/off key."""
    return key in ("media.enabled", "proactive.enabled")


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


# -- what each place offers ---------------------------------------------------
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


# -- changes ------------------------------------------------------------------
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


_GROUP_RE = re.compile(r"[A-Za-z0-9_.:\-]{1,24}")


def _check_group(group):
    if group is None:
        return None
    group = str(group)
    if not _GROUP_RE.fullmatch(group):
        raise SettingsError(f"group id {group!r}: 1-24 letters, digits, '-', '_', '.' or ':'")
    return group


def simulate(st, changes, role="owner"):
    """A copy of st as if changes were written by role (nothing is
    saved)."""
    new = copy.copy(st)
    new.overlay = dict(st.overlay)
    new._cache = {}
    for ch in changes:
        path = change_path(st, ch)
        spec = st.spec(ch.key)
        extra = {}
        if role == "admin":
            extra = {"role": "admin", "below": _below_for_write(st, path, ch.key)}
        if ch.value == "inherit":
            new.overlay[(path, ch.key)] = dict(extra, value="inherit")
            continue
        ok, norm = normalize(spec, ch.value)
        if not ok:
            raise SettingsError(f"{label(ch.key)}: {norm}; valid: {spec.valid_text()}")
        new.overlay[(path, ch.key)] = dict(extra, value=norm, _norm=norm, by="", at="")
    return new


def _place_noop(st, change, path, norm, role="owner"):
    """Writing norm ("inherit" for a reset) at path changes nothing: the
    layer holds it already, or the layer sets nothing and the value in
    force there is the same (a reset when the layer sets nothing). An
    owner's write onto a server admin's entry always counts."""
    place, key = change.place, change.key
    if role != "admin" and entry_role(st.overlay.get((path, key))) == "admin":
        return False
    if _layer_noop(st, path, key, norm, role):
        return True
    if norm == "inherit" or st.layer(path, key)[0]:
        return False
    if path == ("top",):
        return TOP[key].default == norm
    if key == "style.notes" and _scope_kind(path) == "channel":
        return norm == ""
    unknown_category = (
        place.kind == "channel"
        and place.category_id is None
        and any(
            p[:2] == path[:2] and len(p) > SERVER_SCOPE_PARTS and p != path and k == key
            for p, k in list(st.file_values) + list(st.overlay)
        )
    )
    sc = None if unknown_category else _scope_for(place, key, path)
    return sc is not None and st.value_without(sc, key, path) == norm


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


def plan_changes(st, changes, role="owner"):
    """[Planned] for changes, validated (raises SettingsError: unknown
    key, not offered at that place, invalid value, the same key twice).
    Values the UI never offers (privacy.suspicious: answer) are refused."""
    out, seen = [], set()
    for ch in changes:
        if not isinstance(ch, Change):
            raise SettingsError(f"not a change: {ch!r}")
        path = change_path(st, ch)
        spec = check_settable(st, path, ch.key)
        if (path, ch.key) in seen:
            raise SettingsError(f"{label(ch.key)} appears twice for {place_name(ch.place)}")
        seen.add((path, ch.key))
        if ch.value == "inherit":
            norm = write = "inherit"
        else:
            ok, norm = normalize(spec, ch.value)
            if not ok:
                raise SettingsError(f"{label(ch.key)}: {norm}; valid: {spec.valid_text()}")
            write = norm
        out.append(Planned(ch, path, spec, write, _place_noop(st, ch, path, norm, role)))
    sim = simulate(st, [p.change for p in out if not p.noop], role)
    for p in out:
        sc = _scope_for(p.change.place, p.key, p.path)
        p.old = st.top(p.key) if sc is None else st.get(p.key, sc)
        p.new = sim.top(p.key) if sc is None else sim.get(p.key, sc)
    return out


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


def apply(config_path, changes, actor):
    """Write several changes atomically: one overlay file write, one
    history group id (group, or a fresh one; 24 characters at most) on
    every history line. A change that would change nothing is skipped
    (no-op guard); when all are, nothing is written and the result's
    status is NO_CHANGE. Setting the value config.yaml has at that very
    layer drops the overlay entry instead (the file stays the source of
    truth). role "admin" (with admin_gid, the admin's server, and info
    for its channel list) runs admin_changes first: refused changes
    raise SettingsError and nothing is written. role is required:
    "owner" (owners, the CLI, muse.ai) or "admin"."""
    by = actor.by
    group = actor.group
    role = actor.role
    admin_gid = actor.admin_gid
    info = actor.info
    group = _check_group(group)
    if role not in ("owner", "admin"):
        raise SettingsError(f"role {role!r}: say owner or admin")
    with overlay_lock(overlay_path_for(config_path)):
        st = load(config_path)
        if role == "admin":
            if not admin_gid:
                raise SettingsError("an admin change needs the admin's server")
            changes = admin_changes(st, admin_gid, changes, info)
        rows = plan_changes(st, changes, role)
        todo = [p for p in rows if not p.noop]
        if not todo:
            return ApplyResult(None, [], rows)
        group = group or new_group_id()
        doc = _read_overlay_doc(st.overlay_path, strict=True)
        now = _now()
        for p in todo:
            _write_entry(st, doc, p, WriteStamp(by, now, group, role))
        doc["history"] = _trim_history(doc["history"])
        _write_overlay_doc(st.overlay_path, doc)
    return ApplyResult(group, todo, [p for p in rows if p.noop])


# -- groups -------------------------------------------------------------------
def _line_id(h):
    return json.dumps(
        [h.get("key"), h.get("at"), h.get("by"), h.get("group"), h.get("to"), bool(h.get("undo"))],
        sort_keys=True,
        default=str,
    )


def history_since(doc, mark):
    """(the overlay history lines after mark, the new mark). With no mark
    yet (the first call) nothing counts: only the position is taken."""
    hist = [h for h in (doc or {}).get("history") or [] if isinstance(h, dict)]
    ids = [_line_id(h) for h in hist]
    last = {"id": ids[-1] if ids else None, "at": str(hist[-1].get("at") or "") if hist else None}
    if not isinstance(mark, dict):
        return [], last
    if mark.get("id") in ids:
        return hist[len(ids) - ids[::-1].index(mark["id"]) :], last
    after = str(mark.get("at") or "")
    return [h for h in hist if str(h.get("at") or "") > after], last


def _group_lines(doc, group):
    return [
        (i, h)
        for i, h in enumerate(doc.get("history") or [])
        if isinstance(h, dict) and h.get("group") == group and "to" in h and not h.get("undo")
    ]


def _line_live(h, values):
    try:
        path, key = parse_key(h.get("key"))
    except ValueError:
        return False
    cur = values.get(format_key(path, key))
    if h.get("to") == "inherit":
        return cur is None or (isinstance(cur, dict) and cur.get("value") == "inherit")
    if cur is None:
        return bool(h.get("dropped"))
    return isinstance(cur, dict) and cur.get("value") == h.get("to")


def group_current(st, group):
    """True when the changes of group are all still in effect: none undone
    and no later change (from anywhere: Discord, an undo, a file edit
    retiring it) touched any of its keys. False for an unknown group."""
    doc = st.overlay_doc or {}
    lines = _group_lines(doc, group)
    if not lines or any(h.get("undone") for _i, h in lines):
        return False
    keys = {h.get("key") for _i, h in lines}
    last = max(i for i, _h in lines)
    hist = doc.get("history") or []
    for h in hist[last + 1 :]:
        if isinstance(h, dict) and h.get("key") in keys:
            return False
    values = doc.get("values") or {}
    return all(_line_live(h, values) for _i, h in lines)


def group_undo_plan(st, group):
    """The changes undo_group would make for group, as [Change] (value:
    what each key goes back to at its layer, or "inherit"), newest
    first; [] for an unknown group. Pass them to assess: an Undo is
    judged by its outcome too."""
    doc = st.overlay_doc or {}
    out = []
    for _i, h in reversed(_group_lines(doc, group)):
        try:
            path, key = parse_key(h["key"])
        except ValueError:
            continue
        out.append(Change(place_of_path(path), key, h.get("from", "inherit"), path=path))
    return out


def _restore_overlay_value(st, doc, pick, stamp):
    _i, ks, path, key, target = pick
    by, now, role = stamp.by, stamp.at, stamp.role
    spec = check_settable(st, path, key)
    below = owner_layer(doc, ks, st.owners)
    in_file = (path, key) in st.file_values
    cur = st.file_values.get((path, key))
    entry = {"value": "inherit", "base": cur, "by": str(by), "at": now}
    if target == "inherit":
        if in_file:
            doc["values"][ks] = entry
        else:
            doc["values"].pop(ks, None)
    else:
        ok, norm = normalize(spec, target)
        if not ok:
            raise SettingsError(f"{ks}: cannot restore {_show(target)}: {norm}")
        if in_file and norm == cur:
            doc["values"].pop(ks, None)  # the file says it already
        else:
            doc["values"][ks] = dict(entry, value=norm)
    if role == "admin" and ks in doc["values"]:
        doc["values"][ks]["role"] = "admin"
        doc["values"][ks]["below"] = below


def _revert(st, doc, picks, stamp):
    """Restore each pick's earlier value (picks: (history index, key
    string, path, key, value to restore)), marking the lines undone and
    adding undo lines (one group when there are several, or ugroup).
    Not saved. role None: inferred from by (the legacy /settings path
    only)."""
    by, role, ugroup = stamp.by, stamp.role, stamp.group
    if role is None:
        role = _infer_role(by, st.owners)
    now = stamp.at
    ugroup = ugroup or (new_group_id("u") if len(picks) > 1 else None)
    stamp = WriteStamp(by, now, ugroup, role)
    hist = doc["history"]
    done = []
    for i, ks, path, key, target in picks:
        _restore_overlay_value(st, doc, (i, ks, path, key, target), stamp)
        hist[i] = dict(hist[i], undone=True)
        line = {
            "key": ks,
            "from": hist[i]["to"],
            "to": target,
            "by": str(by),
            "at": now,
            "undo": True,
        }
        if ugroup:
            line["group"] = ugroup
        if role == "admin":
            line["role"] = "admin"
        if target != "inherit" and ks not in doc["values"]:
            line["dropped"] = True
        hist.append(line)
        done.append(ks)
    doc["history"] = _trim_history(hist)
    return done


def undo_group(config_path, group, by, role=None, undo_id=None):
    """Undo one specific group (an "[Undo]" under a "Done: ..." reply),
    as an owner (role "owner"; server admins undo through undo() with
    admin_gid, which checks them). Refused (SettingsError) unless
    group_current: something changed those settings since, and undoing
    would overwrite it. Undo lines are never undoable. undo_id: the
    group id for the undo's own history lines (e.g. "nl-undo-<token>").
    Returns the key strings reverted."""
    if role != "owner":
        raise SettingsError(
            "undo_group is for owners; a server admin's Undo goes through undo(..., admin_gid=...)"
        )
    undo_id = _check_group(undo_id)
    with overlay_lock(overlay_path_for(config_path)):
        st = load(config_path)
        doc = _read_overlay_doc(st.overlay_path, strict=True)
        lines = _group_lines(doc, group)
        if not lines:
            raise SettingsError("There is no such change to undo.")
        if not group_current(st, group):
            raise SettingsError("Those settings changed since; nothing was undone.")
        picks = []
        for i, h in reversed(lines):
            path, key = parse_key(h["key"])
            picks.append((i, format_key(path, key), path, key, h.get("from", "inherit")))
        done = _revert(st, doc, picks, WriteStamp(by, _now(), undo_id, "owner"))
        _write_overlay_doc(st.overlay_path, doc)
    return done


def _undo_ident(doc, picks):
    """The id of what an Undo would revert: its group, or (an old line
    with no group) "h" + a short hash of that line."""
    if not picks:
        return None
    h = (doc.get("history") or [])[picks[0][0]]
    if h.get("group"):
        return h["group"]
    raw = f"{h.get('key')}|{h.get('to')}|{h.get('at')}|{h.get('by')}"
    return "h" + hashlib.sha1(raw.encode()).hexdigest()[:10]


def undo_results(st, plan):
    """[(change, the value in force after it)] for an Undo's plan."""
    sim = simulate(st, plan)
    out = []
    for ch in plan:
        sc = _scope_for(ch.place, ch.key, ch.path)
        out.append((ch, sim.top(ch.key) if sc is None else sim.get(ch.key, sc)))
    return out


def undo_lines(st, plan, names=None):
    """["In #general, When it answers → back to Chatty", ...]."""
    return [
        f"{change_location(ch.place, ch.key, names, ch.path)}, {label(ch.key)} "
        f"→ back to {value_label(ch.key, new, names)}"
        for ch, new in undo_results(st, plan)
    ]


def undo_preview(st, prefix, include_children=False, names=None, admin_gid=None):
    """(description, group id) for what Undo at prefix would do, e.g.
    ("Undo: In #general, When it answers → back to Chatty", "g3f9a..."),
    or (None, None) when there is nothing to undo. The id lets a UI
    detect a stale Undo (pass it to undo(..., expect=)). With
    include_children, a server's Undo also covers its channels, and the
    description says so when it does."""
    doc = st.overlay_doc or {}
    accept = _admin_accept(st.owners, admin_gid) if admin_gid else None
    picks = _undo_group(doc, _undo_match(prefix, include_children), accept)
    if not picks:
        return None, None
    names = chain_names(names, names_from(None, st))
    plan = [Change(place_of_path(p), k, t, path=p) for _i, _ks, p, k, t in picks]
    text = "Undo: " + "; ".join(undo_lines(st, plan, names))
    pp = (
        prefix
        if isinstance(prefix, Place)
        else (
            Place.defaults()
            if str(prefix) == "defaults"
            else place_of_path(parse_scope_prefix(prefix))
        )
    )
    if (
        include_children
        and pp is not None
        and pp.kind == "server"
        and any(ch.place is not None and ch.place.kind == "channel" for ch in plan)
    ):
        text += " (this includes a change in a channel of this server)"
    return text, _undo_ident(doc, picks)


# -- server admins ------------------------------------------------------------
def _admin_key_ok(place, key):
    if key == "who.replies" or _is_enabled_key(key):
        return place.kind in ("server", "channel")
    return key == "who.ignore" and place.kind == "server"


def owners_view(st, guild_id):
    """A copy of st as the owners (and config.yaml) set things in one
    server: every server admin's entry there and in its channels is
    replaced by what was below it."""
    gid = str(guild_id)
    doc = st.overlay_doc or {}
    new = copy.copy(st)
    new.overlay = dict(st.overlay)
    new._cache = {}
    for (path, key), entry in st.overlay.items():
        if path[0] != "servers" or path[1] != gid or entry_role(entry) != "admin":
            continue
        below = owner_layer(doc, format_key(path, key), st.owners)
        if not below:
            new.overlay.pop((path, key), None)
            continue
        v = below.get("value")
        if v == "inherit":
            new.overlay[(path, key)] = {"value": "inherit"}
            continue
        ok, norm = normalize(st.spec(key), v)
        if ok:
            new.overlay[(path, key)] = {"value": v, "_norm": norm}
        else:
            new.overlay.pop((path, key), None)
    return new


def _admin_scope_check(st, gid, ch, inf):
    pl = ch.place
    if pl is None or pl.kind not in ("server", "channel") or pl.guild_id != gid:
        raise SettingsError("Server admins can change settings only in their own server.")
    if pl.kind == "channel" and inf.channel(gid, pl.channel_id) is None:
        raise SettingsError("That channel is not in this server.")
    if not _admin_key_ok(pl, ch.key):
        raise SettingsError(
            "Server admins can change only When it "
            "answers, Extras on/off and (for the whole "
            "server) Never answer."
        )
    path = place_path(st, pl, ch.key)
    if ch.path is not None and ch.path != path:
        raise SettingsError("Server admins can change settings only in their own server.")
    return path


def _admin_translate(st, path, key, spec, value):
    """(value to write, revert?) for a server admin's write of value at
    one layer, or raise SettingsError. Over an admin's entry, a reset
    (or the owners' own value) restores what the owners or config.yaml
    had below it. Over an owner's entry or a value in config.yaml (a
    pin), only an explicit value no louder than the pin is allowed:
    never a reset, which would drop the owners' pin."""
    cur = st.overlay.get((path, key))
    if cur is not None and entry_role(cur) == "admin":
        below = _entry_below(cur, st.overlay_doc or {}, format_key(path, key), st.owners)
        if below:
            target = below.get("value")
        elif (path, key) in st.file_values:
            target = st.file_values[(path, key)]
        else:
            target = "inherit"
        if value == "inherit":
            return target, True
        if target != "inherit":
            ok1, a = normalize(spec, value)
            ok2, b = normalize(spec, target)
            if ok1 and ok2 and a == b:
                return target, True
        return value, False
    s, pin, _src = st.layer(path, key)
    if s:  # an owner's entry or config.yaml sets this layer
        if value == "inherit":
            raise SettingsError(
                "The owners set this here; server admins can make it "
                "quieter, but not remove the owners' setting."
            )
        ok, norm = normalize(spec, value)
        if ok and loosens(key, pin, norm, spec) is not False:
            raise SettingsError(ADMIN_QUIETER)
    return value, False


def admin_changes(st, guild_id, changes, info=None):
    """The changes a server admin of guild_id may make, as written
    ([Change]); raises SettingsError (plain words) when any is refused.

    The rule: an admin may (1) make the bot quieter in their server and
    its channels (When it answers: Off / Only when @mentioned, an Extra
    off, someone added to Never answer for the server), never removing
    or loosening an owner's (or config.yaml's) value at that layer; and
    (2) revert admins' own entries there: a reset of a layer that holds
    an admin's entry (or setting it to the owners' value) restores what
    the owners or config.yaml had at that layer below the admins'
    entries. An admin's entry always resolves as the quieter of its own
    value and the owners' (Settings._resolve_key). Every result is
    checked by admin_check: never louder than both the value in force
    and the owners' value. Anything else, any other server, and a
    channel that info does not list in this server are refused."""
    gid = str(guild_id)
    inf = ServerInfo(info)
    out = []
    for ch in changes:
        path = _admin_scope_check(st, gid, ch, inf)
        spec = st.spec(ch.key)
        value, revert = _admin_translate(st, path, ch.key, spec, ch.value)
        if not revert and value != "inherit":
            ok, norm = normalize(spec, value)
            if not ok:
                raise SettingsError(f"{label(ch.key)}: {norm}")
            if ch.key == "who.replies" and norm not in ADMIN_REPLIES:
                raise SettingsError(
                    "Server admins can set When it answers to Off or Only when @mentioned."
                )
            if _is_enabled_key(ch.key) and norm is not False:
                raise SettingsError("Server admins can only turn Extras off.")
        out.append(Change(ch.place, ch.key, value, revert=revert))
    admin_check(st, gid, out, info, revert=True)
    return out


def admin_check(st, guild_id, changes, info=None, revert=False):
    """Raise SettingsError unless a server admin of guild_id may make
    changes (as written; see admin_changes for the rule). For each key
    changed, at the changed places and at every channel of the server
    that config.yaml or the overlay mentions, the result must be no
    louder than the value in force now, or no louder than the owners'
    value (owners_view: the layers below the admins' entries). revert:
    skip the explicit-value limits (an undo restores earlier values)."""
    gid = str(guild_id)
    inf = ServerInfo(info)
    for ch in changes:
        _admin_scope_check(st, gid, ch, inf)
        if not revert and not ch.revert and ch.value != "inherit":
            ok, norm = normalize(st.spec(ch.key), ch.value)
            if (ok and ch.key == "who.replies" and norm not in ADMIN_REPLIES) or (
                ok and _is_enabled_key(ch.key) and norm is not False
            ):
                raise SettingsError(ADMIN_QUIETER)
    sim = simulate(st, changes, "admin")
    owners = owners_view(st, gid)
    scopes = {format_place(ch.place): ch.place.scope(ch.key) for ch in changes}
    scopes[f"server:{gid}"] = Scope.server(gid)
    for cid in st.channels_listed(gid):
        scopes.setdefault(
            f"channel:{gid}:{cid}",
            Scope.server(gid, channel_id=cid, category_id=inf.category_of(gid, cid)),
        )
    for key in {ch.key for ch in changes}:
        spec = st.spec(key)
        for sc in scopes.values():
            old, new = st.get(key, sc), sim.get(key, sc)
            if loosens(key, old, new, spec) is False:
                continue
            if loosens(key, owners.get(key, sc), new, spec) is False:
                continue
            raise SettingsError(ADMIN_QUIETER)


# -- DMs: "Who can DM it" -----------------------------------------------------
def dm_access(st):
    """The "Who can DM it" choice in force: "nobody" (DMs off),
    "owners", "list" (owners plus people), "shared-servers", "anyone"."""
    r = st.resolve(Scope.dm())
    if r["who.replies"] == "off":
        return "nobody"
    v = r["who.dms_from"]
    return "list" if isinstance(v, list) else v


def dms_from_changes(st, choice, people=None):
    """[Change] for a "Who can DM it" choice: "nobody" turns DMs off
    (dms.who.replies: off); "list" (people: user ids) and the keywords
    set dms.who.dms_from and, when DMs are off, turn them back on."""
    dms = Place.dms()
    if choice == "nobody":
        return [Change(dms, "who.replies", "off")]
    if choice == "list":
        if not people:
            raise SettingsError("Pick at least one person.")
        value = list(people)
    elif choice in ("owners", "shared-servers", "anyone"):
        value = choice
    else:
        raise SettingsError(f"{choice!r}: owners, list, shared-servers, anyone or nobody")
    out = [Change(dms, "who.dms_from", value)]
    if st.get("who.replies", Scope.dm()) == "off":
        out += resume_changes(st, dms) or [Change(dms, "who.replies", "conversational")]
    return out


# -- pause --------------------------------------------------------------------
def pause_changes(place):
    """[Change] that pause the bot at place: a channel pause pauses that
    channel, a server pause the server, DMs the DMs, and Defaults is
    Pause everywhere (DMs and all servers)."""
    if place.kind == "defaults":
        return [
            Change(Place.dms(), "who.replies", "off"),
            Change(Place.defaults(), "who.replies", "off"),
        ]
    return [Change(place, "who.replies", "off")]


def _pause_targets(place):
    if place.kind == "defaults":
        return [(Place.dms(), ("dms",)), (Place.defaults(), ("all_servers",))]
    if place.kind == "dms":
        return [(place, ("dms",))]
    if place.kind == "server":
        return [(place, ("servers", place.guild_id))]
    return [(place, ("servers", place.guild_id, "channels", place.channel_id))]


def is_paused(st, place):
    """Paused at place itself (Defaults: both DMs and all servers)."""
    return all(
        st.layer(path, "who.replies")[:2] == (True, "off") for _pl, path in _pause_targets(place)
    )


def resume_value(st, path):
    """What who.replies was at layer path before it was paused there: kept
    with the pause itself ("resume" on the entry), else from history,
    else "inherit" (the layer set nothing before)."""
    entry = st.overlay.get((path, "who.replies"))
    if isinstance(entry, dict) and entry.get("value") == "off" and "resume" in entry:
        return entry["resume"]
    ks = format_key(path, "who.replies")
    for h in reversed((st.overlay_doc or {}).get("history") or []):
        if not isinstance(h, dict) or "to" not in h:
            continue
        try:
            p, key = parse_key(h.get("key"))
        except ValueError:
            continue
        if format_key(p, key) != ks:
            continue
        if h["to"] == "off" and h.get("from") not in (None, "off"):
            return h["from"]
    return "inherit"


def resume_changes(st, place):
    """[Change] that undo a pause at place ([] when it is not paused
    there; a channel in a paused server is not paused itself)."""
    out = []
    for pl, path in _pause_targets(place):
        if st.layer(path, "who.replies")[:2] == (True, "off"):
            out.append(Change(pl, "who.replies", resume_value(st, path)))
    return out


# -- presets ------------------------------------------------------------------
_IF_MEDIA, _IF_EMPTY = "if-media-on", "if-empty"
WORK_NOTES = "Keep a professional tone. Keep replies brief."
PRESETS = {
    "friends": (
        "Friends",
        {
            "who.replies": "conversational",
            "privacy.memory": "everything",
            "limits.per_person_per_day": None,
            "limits.per_day": None,
            "who.bots": "when-addressed",
            "who.bot_exchange_limit": None,
            "media.per_day": None,
            "proactive.enabled": True,
        },
    ),
    "community": (
        "Community",
        {
            "who.replies": "when-addressed",
            "privacy.memory": "public-only",
            "limits.per_person_per_day": 20,
            "limits.per_day": 300,
            "who.bots": "when-addressed",
            "who.bot_exchange_limit": 4,
            "media.per_day": (_IF_MEDIA, 10),
            "proactive.enabled": False,
        },
    ),
    "work": (
        "Work",
        {
            "who.replies": "when-addressed",
            "privacy.memory": "nothing",
            "limits.per_person_per_day": None,
            "limits.per_day": None,
            "who.bots": "ignore",
            "media.per_day": None,
            "proactive.enabled": False,
            "style.notes": (_IF_EMPTY, WORK_NOTES),
        },
    ),
}


def _preset(name):
    n = str(name or "").strip().lower()
    if n not in PRESETS:
        raise SettingsError(f"{name!r} is not a preset: Friends, Community or Work")
    return PRESETS[n]


def preset_changes(st, name, place, info=None, big="refuse"):
    """[Change] that apply preset name (friends | community | work) to a
    server place: one undo group when applied together. Keys a preset
    leaves unchanged are absent; Community's media cap only when media is
    on there; Work's instructions only when the server has none; changes
    that would change nothing are left out. Friends (anything it
    remembers) is refused for a server known to have more than 50
    members; with big="skip" it is applied without that one key
    instead (/settings' preview says so). Presets are an owner's tool
    (admin_changes refuses them)."""
    title, values = _preset(name)
    if place.kind != "server":
        raise SettingsError("Presets apply to a whole server.")
    members = ServerInfo(info).members(place.guild_id)
    skip = set()
    if title == "Friends" and members is not None and members > BIG_FOR_MEMORY:
        if big != "skip":
            raise SettingsError(
                f"The Friends preset lets replies use anything it "
                f"remembers about you, so it is only for servers of up to "
                f"{BIG_FOR_MEMORY} members; this one has {members:,}."
            )
        skip.add("privacy.memory")
    out = []
    sc = Scope.server(place.guild_id)
    for key, value in values.items():
        if st.spec(key) is None or key in skip:
            continue  # a setting omitted by this preset
        if isinstance(value, tuple):
            cond, value = value
            if cond == _IF_MEDIA and not st.get("media.enabled", sc):
                continue
            if cond == _IF_EMPTY and st.get(key, sc):
                continue
        path = place_path(st, place, key)
        _ok, norm = normalize(st.spec(key), value)
        if not _place_noop(st, Change(place, key, norm, path=path), path, norm):
            out.append(Change(place, key, value))
    return out


def matching_preset(st, place):
    """ "Friends" / "Community" / "Work" when a server's values in force
    match that preset, else None ("Custom"). Instructions and reaching
    out on its own (a server may keep Friends with it off) are not
    compared; the media cap only when media is on there."""
    if place.kind != "server":
        return None
    sc = Scope.server(place.guild_id)
    for title, values in PRESETS.values():
        ok = True
        for key, value in values.items():
            if key in ("style.notes", "proactive.enabled") or st.spec(key) is None:
                continue
            if isinstance(value, tuple):
                cond, value = value
                if cond == _IF_MEDIA and not st.get("media.enabled", sc):
                    continue
            if st.get(key, sc) != value:
                ok = False
                break
        if ok:
            return title
    return None


# -- risk, judged by outcome --------------------------------------------------
class Assessment:
    """assess()'s answer: warnings (plain sentences about risky outcomes;
    any one makes the change risky: confirm first) and notes (plain
    sentences worth showing, not risky)."""

    def __init__(self):
        self.warnings = []
        self.notes = []

    @property
    def risky(self):
        return bool(self.warnings)

    def to_dict(self):
        return {"risky": self.risky, "warnings": list(self.warnings), "notes": list(self.notes)}

    def __repr__(self):
        return f"Assessment(risky={self.risky}, warnings={self.warnings})"


def _dm_audience(s):
    """Who besides the owners gets answers in DMs: ("none", ()),
    ("set", people), ("shared", ()) or ("any", ())."""
    r = s.resolve(Scope.dm())
    if r["who.replies"] == "off":
        return "none", frozenset()
    pol, allow = r["who.dms_from"], r["who.allow"]
    ignore = {p for p in r["who.ignore"] or [] if not p.startswith("role:")}
    people = None
    if isinstance(pol, list):
        people = set(pol)
    elif pol not in ("shared-servers", "anyone"):
        return "none", frozenset()
    if isinstance(allow, list):
        users = {a for a in allow if not str(a).startswith("role:")}
        people = users if people is None else people & users
    if people is not None:
        people = people - ignore - {str(o) for o in s.owners}
        return ("set", frozenset(people)) if people else ("none", frozenset())
    return ("shared" if pol == "shared-servers" else "any"), frozenset()


def _audience_grew(before, after):
    """The new people (a set), True (an open audience is new), or None."""
    kb, pb = before
    ka, pa = after
    if ka == "none":
        return None
    if ka == "set":
        if kb == "set":
            return set(pa - pb) or None
        # From shared-servers, a named person may share no server.
        return set(pa) if kb in ("none", "shared") else None
    if ka == "shared":
        return True if kb in ("none", "set") else None
    return True if kb != "any" else None


def _effective_cap(s, key, sc):
    caps = [v for _cid, v in s.cap_layers(key, sc)]
    return min(caps) if caps else None


def _join(items):
    items = list(items)
    if len(items) <= PAIR_LENGTH:
        return " and ".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


class ChangeAssessment:
    """Compare effective settings by policy, sharing one simulated result.

    Checks keep their existing order and wording. Helpers describe the
    same server/channel snapshot for every check, including unknown sizes.
    """

    def __init__(self, st, changes, info, names, named):
        self.st = st
        self.named = named
        self.inf = ServerInfo(info)
        self.look_names = chain_names(names, names_from(self.inf, self.st))
        self.look = name_lookup(self.look_names)
        self.after = simulate(self.st, changes)
        self.out = Assessment()
        self.gids = set(self.inf.guilds()) | set(self.st.servers_listed())
        for ch in changes:
            if ch.place.guild_id:
                self.gids.add(ch.place.guild_id)
        self.gids = sorted(g for g in self.gids if _id_str(g))

        self.servers = {g: Scope.server(g) for g in self.gids}
        self.channels = {}
        for g in self.gids:
            for c in self.st.channels_listed(g):
                self.channels[(g, c)] = Scope.server(
                    g, channel_id=c, category_id=self.inf.category_of(g, c)
                )
        for ch in changes:
            if ch.place is not None and ch.place.kind == "channel":
                self.channels[(ch.place.guild_id, ch.place.channel_id)] = ch.place.scope(ch.key)
        self.later = Scope("all_servers")  # servers it joins later / not listed
        self.total = sum(self.inf.members(g) or 0 for g in self.inf.guilds())

    def run(self):
        self.external_apps()
        self.dm_audience()
        self.bot_dm_access()
        self.memory_access()
        self.generation_access()
        self.restricted_audience()
        self.global_pause()
        self.reply_caps()
        self.server_conversations()
        self.bot_conversations()
        self.notes()
        return self.out

    def external_apps(self):
        levels = ("off", "read-only", "confirm", "write", "unconditional")
        scope = Scope.everywhere()
        before_apps = self.st.resolve(scope)["abilities.external_apps"]
        after_apps = self.after.resolve(scope)["abilities.external_apps"]
        if levels.index(after_apps) > levels.index(before_apps):
            self.out.warnings.append(
                f"External app access increases from {before_apps} to {after_apps}. "
                "App-specific limits still apply. Unconditional approval skips "
                "approval for registered writes, deletes and sends."
            )

    def gname(self, g):
        return self.look(g) or f"server {g}"

    def gdesc(self, g):
        n = self.inf.members(g)
        return (
            f"{self.gname(g)} ({n:,} members)"
            if n is not None
            else f"{self.gname(g)} (size unknown)"
        )

    def large(self, g, limit):
        """Known to have more than limit members, or of unknown size
        (fail closed: no member counts is no reason to stay quiet)."""
        n = self.inf.members(g)
        return n is None or n > limit

    def bot_dms(self, s):
        r = s.resolve(Scope.dm())
        return (
            r["who.replies"] != "off"
            and r["who.bots"] != "ignore"
            and r["who.dms_from"] in ("anyone", "shared-servers")
            and r["who.allow"] == "everyone"
        )

    def mem(self, s, sc):
        return s.get("privacy.memory", sc) == "everything"

    def media(self, s, sc):
        if not s.get("media.enabled", sc):
            return None, False
        who = s.get("media.who", sc)
        cap = _effective_cap(s, "media.per_day", sc)
        return who, who != "owners" and (cap is None or cap > MEDIA_PER_DAY_RISKY)

    def where(self, k):
        if k == ("DMs",):
            return "DMs"
        if k == ("later",):
            return "servers it joins later"
        if len(k) == 1:
            return self.gname(k[0])
        return place_name(Place.channel(*k), self.look_names)

    def paused_all(self, s):
        return (
            s.get("who.replies", Scope.dm()) == "off" and s.get("who.replies", self.later) == "off"
        )

    def botx(self, s, sc):
        lim = s.get("who.bot_exchange_limit", sc)
        return s.get("who.bots", sc) != "ignore" and (lim is None or lim > BOT_EXCHANGE_RISKY)

    def allow_ineffective(self, s):
        r = s.resolve(Scope.dm())
        return (
            r["who.replies"] != "off"
            and r["who.dms_from"] == "owners"
            and isinstance(r["who.allow"], list)
            and bool(r["who.allow"])
        )

    def dm_audience(self):
        # DMs open to more people.
        self.aud_b, self.aud_a = _dm_audience(self.st), _dm_audience(self.after)
        grew = _audience_grew(self.aud_b, self.aud_a)
        if isinstance(grew, set) and self.named:
            grew = grew - {str(n) for n in self.named} or None
        if grew is True and self.aud_a[0] == "any":
            self.out.warnings.append(
                "Anyone on Discord, strangers included, can DM "
                "the bot and get answers. Each answer costs a "
                "Muse turn."
            )
        elif grew is True:
            self.out.warnings.append(
                "Anyone who shares a server with the bot can DM it and get answers, not only you."
            )
        elif grew:
            self.out.warnings.append(
                f"{people_text(sorted(grew), self.look)} can DM the bot and get answers."
            )

    def bot_dm_access(self):
        # Other bots in DMs.

        if self.bot_dms(self.after) and not self.bot_dms(self.st):
            self.out.warnings.append(
                "Other bots can DM the bot and get answers; two "
                "bots can keep answering each other, each "
                "answer a Muse turn."
            )

    def memory_access(self):
        # Memory: anything it remembers.

        newly = [
            g
            for g, sc in self.servers.items()
            if self.mem(self.after, sc) and not self.mem(self.st, sc)
        ]
        if newly or (self.mem(self.after, self.later) and not self.mem(self.st, self.later)):
            places = [self.gdesc(g) for g in newly] or ["servers it joins later"]
            big = [g for g in newly if self.large(g, BIG_FOR_MEMORY)]
            text = (
                f"In {_join(places)}, replies may draw on anything it "
                f"remembers about you (your job, location, finances and "
                f"other saved memory), and anyone there can ask the bot "
                f"about you."
            )
            if big:
                text = (
                    f"More than {BIG_FOR_MEMORY} members (or size unknown): "
                    f"{_join(self.gdesc(g) for g in big)}. " + text
                )
            self.out.warnings.append(text)
        if (
            self.aud_a[0] in ("shared", "any")
            and self.mem(self.after, Scope.dm())
            and not (self.aud_b[0] in ("shared", "any") and self.mem(self.st, Scope.dm()))
        ):
            self.out.warnings.append(
                "People who DM the bot, strangers included, may "
                "get replies drawing on anything it remembers "
                "about you."
            )

    def generation_access(self):
        # Image and video generation.
        scopes: dict[tuple[str, ...], Scope] = {(g,): sc for g, sc in self.servers.items()}
        scopes[("DMs",)] = Scope.dm()
        scopes[("later",)] = self.later
        for (g, c), sc in self.channels.items():
            scopes[(g, c)] = sc

        everyone, uncapped = [], []
        for k, sc in scopes.items():
            wb, ub = self.media(self.st, sc)
            wa, ua = self.media(self.after, sc)
            if wa == "everyone" and wb != "everyone":
                everyone.append(self.where(k))
            if ua and not ub:
                uncapped.append(self.where(k))
        if everyone:
            self.out.warnings.append(
                f"Anyone in {_join(everyone)} can ask for "
                f"generated images and video. Generation "
                f"can cost real money."
            )
        if uncapped:
            self.out.warnings.append(
                f"Generations per day: more than "
                f"{MEDIA_PER_DAY_RISKY} (or no limit) for people other "
                f"than you in {_join(uncapped)}. Generation can cost real "
                f"money."
            )

    def restricted_audience(self):
        # Only answer these people: newly limiting places.
        allow_scopes = [(self.gname(g), sc) for g, sc in self.servers.items()] + [
            ("servers it joins later", self.later)
        ]
        if self.aud_b[0] != "none":  # DMs answered someone besides the owners
            allow_scopes.append(("DMs", Scope.dm()))
        silenced, who = [], None
        for name, sc in allow_scopes:
            a = self.after.get("who.allow", sc)
            if isinstance(a, list) and self.st.get("who.allow", sc) == "everyone":
                silenced.append(name)
                who = who or a
        if silenced:
            n = len(silenced)
            self.out.warnings.append(
                f"Only {people_text(who, self.look) if who else 'nobody'} and you "
                f"will get answers in {n} place{'s' if n != 1 else ''} "
                f"({_join(silenced)}); everyone else there is ignored."
            )

    def global_pause(self):
        # Pause everywhere.

        if self.paused_all(self.after) and not self.paused_all(self.st):
            own = [
                self.gname(g)
                for g, sc in self.servers.items()
                if self.after.get("who.replies", sc) != "off"
            ]
            text = (
                "Pause everywhere: the bot stops answering everyone, in DMs "
                "and in every server without its own setting, until you "
                "resume."
            )
            if own:
                text += (
                    f" {_join(own)} set{'s' if len(own) == 1 else ''} "
                    f"its own and keep{'s' if len(own) == 1 else ''} "
                    f"answering."
                )
            self.out.warnings.append(text)

    def reply_caps(self):
        # A reply cap removed where the bot is large.
        for key in ("limits.per_day", "limits.per_person_per_day"):
            lost = []
            for g, sc in self.servers.items():
                if (
                    self.large(g, BIG_FOR_CAPS)
                    and self.st.cap_layers(key, sc)
                    and not self.after.cap_layers(key, sc)
                ):
                    lost.append(self.gdesc(g))
            if (
                self.aud_a[0] in ("shared", "any")
                and self.st.cap_layers(key, Scope.dm())
                and not self.after.cap_layers(key, Scope.dm())
            ):
                lost.append("DMs (open to strangers)")
            ev = Scope.everywhere()
            unknown = not self.inf.guilds() or any(
                self.inf.members(g) is None for g in self.inf.guilds()
            )
            if (
                (self.total > BIG_FOR_CAPS or unknown)
                and not lost
                and self.st.cap_layers(key, ev)
                and not self.after.cap_layers(key, ev)
            ):
                lost.append(
                    "the bot as a whole (size unknown)"
                    if not self.total
                    else f"the bot as a whole ({self.total:,} members"
                    + (", some of unknown size)" if unknown else ")")
                )
            if lost:
                self.out.warnings.append(f"{label(key)}: no limit any more in {_join(lost)}.")

    def server_conversations(self):
        # Chatty in a big server.
        big = []
        for (g, c), sc in [((g, None), sc) for g, sc in self.servers.items()] + list(
            self.channels.items()
        ):
            if not self.large(g, BIG_FOR_CONVERSATIONAL):
                continue
            if (
                self.after.get("who.replies", sc) == "conversational"
                and self.st.get("who.replies", sc) != "conversational"
            ):
                if c:
                    n = self.look(c)
                    cname = (n if str(n).startswith("#") else f"#{n}") if n else f"channel {c}"
                    big.append(f"{cname} in {self.gdesc(g)}")
                else:
                    big.append(self.gdesc(g))
        if big:
            self.out.warnings.append(
                f"Chatty in {_join(big)}: anyone the bot answers there can keep "
                f"talking to it for 2 minutes without @mentioning it."
            )

    def bot_conversations(self):
        # Replies in a row to other bots.

        loose = [
            n
            for n, sc in [("Defaults", Scope.everywhere())]
            + [(self.gname(g), sc) for g, sc in self.servers.items()]
            if self.botx(self.after, sc) and not self.botx(self.st, sc)
        ]
        if loose:
            self.out.warnings.append(
                f"Two bots can keep answering each other in "
                f"{_join(loose)} with no limit (or more than "
                f"{BOT_EXCHANGE_RISKY} in a row), each answer "
                f"a Muse turn."
            )

    def notes(self):
        # Notes.

        if self.allow_ineffective(self.after) and not self.allow_ineffective(self.st):
            self.out.notes.append(
                "Only answer these people does not let anyone DM "
                "the bot while Who can DM it is Only you; choose "
                "You and people you pick for that."
            )


def assess(st, changes, info=None, names=None, named=()):
    """Assess newly introduced risks in effective settings, including undo.

    Caller-supplied server sizes and names describe the same snapshot for
    every policy. Unknown sizes are treated conservatively.
    """
    return ChangeAssessment(st, changes, info, names, named).run()


# ---------------------------------------------------------------------------
# Watching the files (live reload)
# ---------------------------------------------------------------------------
class Watcher:
    """Holds the current Settings and reloads when config.yaml or the
    overlay changes (an mtime/size/inode check per call; no reading).
    A reload that fails validation keeps the previous settings."""

    def __init__(self, config_path):
        self.config_path = config_path
        self.overlay_path = overlay_path_for(config_path)
        self._sig = self._signature()
        self.current = load(config_path)
        self.error = None

    def _signature(self):
        out = []
        for p in [self.config_path, self.overlay_path, *server_config_paths(self.config_path)]:
            try:
                s = os.stat(p)
                out.append((p, s.st_mtime_ns, s.st_size, s.st_ino))
            except OSError:
                out.append(None)
        return tuple(out)

    def poll(self):
        """(changed?, error text or None)."""
        sig = self._signature()
        if sig == self._sig:
            return False, None
        self._sig = sig
        try:
            new = load(self.config_path)
        except SettingsError as e:
            self.error = str(e)
            return False, self.error
        self.error = None
        self.current = new
        return True, None


# ---------------------------------------------------------------------------
# One value for watchdog.sh
# ---------------------------------------------------------------------------
def shell_value(st, name, pkg_dir=None):
    if name == "token_file":
        files = [p for k, p in st.token_sources() if k == "file"]
        return os.path.expanduser(files[0]) if files else ""
    if name == "token_env":
        envs = [p for k, p in st.token_sources() if k == "env"]
        return envs[0] if envs else ""
    if name == "presence_enabled":
        return "true" if st.presence_enabled else "false"
    if name == "updates_notify":
        return "true" if st.updates_notify else "false"
    raise KeyError(name)
