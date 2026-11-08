"""Plan and apply scoped or grouped undo against current overlay history."""

import hashlib
import json

from muse_discord.settings import overlay as _overlay
from muse_discord.settings.changes import Change, WriteActor, WriteStamp
from muse_discord.settings.loading import load
from muse_discord.settings.overlay import (
    _check_group,
    _infer_role,
    _read_overlay_doc,
    _trim_history,
    _write_overlay_doc,
    entry_role,
    new_group_id,
    overlay_lock,
    overlay_path_for,
    owner_layer,
)
from muse_discord.settings.permissions import admin_check
from muse_discord.settings.places import Place, _scope_for, parse_scope_prefix, place_of_path
from muse_discord.settings.planning import simulate
from muse_discord.settings.values import (
    SERVER_SCOPE_PARTS,
    SettingsError,
    _show,
    check_settable,
    format_key,
    normalize,
    parse_key,
)
from muse_discord.settings_display import (
    chain_names,
    change_location,
    label,
    names_from,
    value_label,
)


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
        done = _revert(st, doc, picks, WriteStamp(by, _overlay._now(), undo_id, role))
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
        done = _revert(st, doc, picks, WriteStamp(by, _overlay._now(), undo_id, "owner"))
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
