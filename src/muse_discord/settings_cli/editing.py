"""Apply CLI changes, undo and folding through the shared settings engine.

Print plans and warnings before editing; risky changes require the caller's
explicit --yes. The engine owns validation, history, backups and atomic writes.
"""

import secrets

from muse_discord import muse_settings as MS
from muse_discord.settings_assessment import assess
from muse_discord.settings_cli.context import _cfg_path, _info, _place
from muse_discord.settings_presets import preset_changes


class NeedsYes(Exception):
    pass


def _run(args, st, cfg, info, changes):
    """Print the changes and their risks; make them unless they are risky
    and --yes is missing."""
    names = MS.names_from(info, st)
    rows = MS.plan_changes(st, changes)
    todo = [r for r in rows if not r.noop]
    for r in rows:
        if r.noop:
            print("No change: " + r.describe(names) + ".")
    if not todo:
        return 0
    risk = assess(st, [r.change for r in todo], info)
    for r in todo:
        print(r.describe(names))
    for n in risk.notes:
        print("Note: " + n)
    for w in risk.warnings:
        print("Warning: " + w)
    if risk.risky and not args.yes:
        raise NeedsYes()
    MS.apply(
        cfg,
        [r.change for r in todo],
        actor=MS.WriteActor(by=args.by, group=f"cli-{secrets.token_hex(4)}", role="owner"),
    )
    print("Done.")
    return 0


def cmd_set(args, reset=False):
    cfg = _cfg_path(args)
    st = MS.load(cfg)
    info = _info(cfg)
    place = _place(args, info)
    key = MS.key_from_text(st, args.key)
    if reset:
        changes = [MS.Change(place, key, "inherit")]
    elif key == "who.dms_from" and place.kind in ("dms", "defaults"):
        raw = args.value.strip()
        if raw.lower() in ("nobody", "off"):
            changes = MS.dms_from_changes(st, "nobody")
        else:
            value = MS.parse_value(st, key, raw)
            changes = MS.dms_from_changes(
                st,
                "list" if isinstance(value, list) else value,
                value if isinstance(value, list) else None,
            )
    else:
        changes = [MS.Change(place, key, MS.parse_value(st, key, args.value))]
    return _run(args, st, cfg, info, changes)


def cmd_reset(args):
    return cmd_set(args, reset=True)


def cmd_preset(args):
    cfg = _cfg_path(args)
    st = MS.load(cfg)
    info = _info(cfg)
    place = _place(args, info)
    changes = preset_changes(st, args.name, place, info)
    if not changes:
        print(
            f"No change: {MS.place_name(place, MS.names_from(info, st))} "
            f"already matches {args.name.capitalize()}."
        )
        return 0
    return _run(args, st, cfg, info, changes)


def cmd_pause(args, resume=False):
    cfg = _cfg_path(args)
    st = MS.load(cfg)
    info = _info(cfg)
    place = _place(args, info)
    names = MS.names_from(info, st)
    if resume:
        changes = MS.resume_changes(st, place)
        if not changes:
            print(f"No change: {MS.place_name(place, names)} is not paused there.")
            return 0
    else:
        changes = MS.pause_changes(place)
    return _run(args, st, cfg, info, changes)


def cmd_resume(args):
    return cmd_pause(args, resume=True)


def cmd_undo(args):
    cfg = _cfg_path(args)
    st = MS.load(cfg)
    info = _info(cfg)
    place = _place(args, info)
    names = MS.names_from(info, st)
    children = place.kind == "server"
    text, ident = MS.undo_preview(st, place, children, names)
    if text is None:
        print(f"Nothing to undo in {MS.place_name(place, names)}.")
        return 0
    print(text)
    risk = assess(st, MS.undo_plan(st, place, children), info)
    for w in risk.warnings:
        print("Warning: " + w)
    if risk.risky and not args.yes:
        raise NeedsYes()
    MS.undo_changes(
        cfg, place, children=children, expect=ident, actor=MS.WriteActor(by=args.by, role="owner")
    )
    print("Done.")
    return 0


def cmd_fold(args):
    cfg = _cfg_path(args)
    st = MS.load(cfg)
    keys = sorted(
        MS.format_key(p, k) for (p, k), e in st.overlay.items() if MS.entry_role(e) != "admin"
    )
    if not keys:
        print(
            "Nothing to fold: no Discord changes in effect (server "
            "admins' changes stay in Discord)."
        )
        return 0
    if args.dry_run:
        print(f"Would copy into config.yaml (and clear from {MS.OVERLAY_NAME}):")
        for k in keys:
            print(f"  {k}")
        return 0
    bak, folded = MS.fold(cfg)
    print(f"Backed up config.yaml to {bak}.")
    print(
        f"Copied {len(folded)} Discord change(s) into config.yaml and "
        f"cleared them from {MS.OVERLAY_NAME}:"
    )
    for k in folded:
        print(f"  {k}")
    print("config.yaml was rewritten: its comments are gone (the backup has them).")
    return 0
