"""Restrict server-admin changes and construct pause and DM access changes."""

import copy

from muse_discord.settings.changes import Change
from muse_discord.settings.overlay import _entry_below, entry_role, owner_layer
from muse_discord.settings.places import (
    ADMIN_QUIETER,
    ADMIN_REPLIES,
    Place,
    format_place,
    place_path,
)
from muse_discord.settings.planning import simulate
from muse_discord.settings.values import (
    Scope,
    SettingsError,
    _is_enabled_key,
    format_key,
    loosens,
    normalize,
    parse_key,
)
from muse_discord.settings_display import ServerInfo, label


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
