"""Simulate and normalize change plans without writing configuration."""

import copy

from muse_discord.settings.changes import Change, Planned, change_path
from muse_discord.settings.overlay import _below_for_write, _layer_noop, entry_role
from muse_discord.settings.places import _scope_for
from muse_discord.settings.values import (
    SERVER_SCOPE_PARTS,
    SettingsError,
    _scope_kind,
    check_settable,
    normalize,
)
from muse_discord.settings_display import label, place_name
from muse_discord.settings_schema import TOP


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
