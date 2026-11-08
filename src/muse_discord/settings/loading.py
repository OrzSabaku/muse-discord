"""Parse configuration files and watch for validated reloads."""

import os
import re

import yaml
from pydantic import ValidationError

from muse_discord.settings.overlay import (
    OVERLAY_NAME,
    _read_yaml,
    _yload,
    overlay_path_for,
    server_config_paths,
)
from muse_discord.settings.resolution import Settings
from muse_discord.settings.values import (
    CHANNEL_ONLY,
    CHANNEL_SHORT,
    SERVER_SCOPE_PARTS,
    TOP_DISCORD_EDITABLE,
    SettingsError,
    _id_str,
    _scope_kind,
    _settable_at,
    _show,
    normalize,
    parse_key,
    spec_of,
)
from muse_discord.settings_schema import CORE, CORE_GROUPS, TOP, EngineSettings


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
