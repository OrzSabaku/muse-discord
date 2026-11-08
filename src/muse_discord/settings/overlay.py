"""Overlay files, atomic persistence, history metadata and write serialization."""

import contextlib
import fcntl
import os
import re
import secrets
import threading
from datetime import UTC, datetime

import yaml

from muse_discord.settings.values import SettingsError, _id_str, format_key, normalize
from muse_discord.storage import atomic_write_text

OVERLAY_NAME = "config.discord.yaml"


HISTORY_CAP = 100


_SAFE_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def _yload(stream):
    """yaml.safe_load, with libyaml's loader when PyYAML has it (the same
    YAML, several times faster: the settings files are read often)."""
    return yaml.load(stream, Loader=_SAFE_LOADER)


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


def _write_overlay_doc(path, doc):
    body = yaml.safe_dump(
        {"version": 1, "values": doc.get("values") or {}, "history": doc.get("history") or []},
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=1000,
    )
    atomic_write_text(path, OVERLAY_HEADER + body)


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


_GROUP_RE = re.compile(r"[A-Za-z0-9_.:\-]{1,24}")


def _check_group(group):
    if group is None:
        return None
    group = str(group)
    if not _GROUP_RE.fullmatch(group):
        raise SettingsError(f"group id {group!r}: 1-24 letters, digits, '-', '_', '.' or ':'")
    return group
