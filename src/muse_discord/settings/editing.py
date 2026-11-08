"""Apply authorized settings changes under the overlay lock."""

from muse_discord.settings import overlay as _overlay
from muse_discord.settings.changes import ApplyResult, Change, Planned, WriteActor, WriteStamp
from muse_discord.settings.loading import load
from muse_discord.settings.overlay import (
    _check_group,
    _infer_role,
    _layer_noop,
    _read_overlay_doc,
    _trim_history,
    _write_entry,
    _write_overlay_doc,
    new_group_id,
    overlay_lock,
    overlay_path_for,
)
from muse_discord.settings.permissions import _admin_translate, admin_changes
from muse_discord.settings.places import ADMIN_QUIETER, place_of_path
from muse_discord.settings.planning import plan_changes
from muse_discord.settings.values import (
    NO_CHANGE,
    SettingsError,
    check_settable,
    normalize,
    parse_key,
)


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
            WriteStamp(by, _overlay._now(), group, role),
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
        now = _overlay._now()
        for p in todo:
            _write_entry(st, doc, p, WriteStamp(by, now, group, role))
        doc["history"] = _trim_history(doc["history"])
        _write_overlay_doc(st.overlay_path, doc)
    return ApplyResult(group, todo, [p for p in rows if p.noop])
