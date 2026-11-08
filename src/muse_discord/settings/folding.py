"""Fold owner overlay values into configuration with a recoverable backup."""

import copy
import os
import shutil
from datetime import UTC, datetime

import yaml

from muse_discord.settings import overlay as _overlay
from muse_discord.settings.loading import load
from muse_discord.settings.overlay import (
    _read_overlay_doc,
    _read_yaml,
    _trim_history,
    _write_overlay_doc,
    entry_role,
    overlay_lock,
    overlay_path_for,
)
from muse_discord.settings.values import CHANNEL_SHORT, SERVER_SCOPE_PARTS, _id_str, format_key
from muse_discord.storage import atomic_write_text


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
        now = _overlay._now()
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
