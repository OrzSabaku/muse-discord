"""Resolve file and overlay layers into effective settings."""

import copy

from muse_discord.settings import overlay as _overlay
from muse_discord.settings.overlay import (
    OVERLAY_NAME,
    _entry_below,
    _read_overlay_doc,
    _same_entry,
    _trim_history,
    _write_overlay_doc,
    entry_role,
    overlay_lock,
)
from muse_discord.settings.values import (
    DEFAULT_PERSONA,
    DEFAULT_TOKEN_FILE,
    SERVER_SCOPE_PARTS,
    _scope_kind,
    _show,
    format_key,
    loosens,
    normalize,
    spec_of,
)
from muse_discord.settings_schema import CORE, TOP, EngineSettings


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
            now = _overlay._now()
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
