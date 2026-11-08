"""Display resolved settings, explanations and discoverable places for the CLI.

These commands read configuration and saved monitor snapshots without changing
them. Display and filtering stay separate from mutation and argument parsing.
"""

import json
import sys
import time

from muse_discord import muse_settings as MS
from muse_discord.settings import resolution as settings_resolution
from muse_discord.settings import values as settings_values
from muse_discord.settings_cli.context import _cfg_path, _directory, _info, _known_guilds, _place
from muse_discord.settings_presets import matching_preset

DIRECTORY_STALE_SECS = 3600


CARD_TYPES = (0, 5, 15, 16)  # text, announcement, forum, media channels


def _fmt(spec, value):
    v = value
    if isinstance(v, str):
        return json.dumps(v) if v == "" or "\n" in v else v
    if isinstance(v, list):
        return "[" + ", ".join(json.dumps(x) if isinstance(x, str) else str(x) for x in v) + "]"
    return str(v)


def show_setting(spec, kinds, source):
    return (
        spec is not None
        and bool(kinds & set(spec.scopes))
        and not (spec.hidden and source.startswith("built-in"))
    )


def show_global(st):
    print(f"  owners                   {', '.join(st.owners) or '(none)'}")
    print(f"  persona                  {st.persona}")
    print(
        "  token_from               "
        + ", ".join(f"env:{w}" if k == "env" else w for k, w in st.token_sources())
    )
    for key in (
        "presence.status",
        "presence.kind",
        "updates.notify",
        "telemetry.usage_stats",
        "memory.save_dms",
    ):
        print(f"  {key:<24} {_fmt(MS.TOP[key], st.top(key))}")
    if not st.presence_enabled:
        print("  presence.enabled         off")


def cmd_validate(args):
    cfg = _cfg_path(args)
    st = MS.load(cfg)
    print(f"{cfg}: valid.")
    n = len(st.overlay)
    if n or st.retire:
        print(f"{MS.OVERLAY_NAME}: {n} Discord change(s) in effect.")
    for keystr, _entry, reason in st.retire:
        print(
            f"  {keystr}: no longer applies ({reason}); the monitor will "
            f"move it to history and tell the owners."
        )
    if st.overlay_error:
        print(st.overlay_error)
    return 0


def cmd_show(args):
    cfg = _cfg_path(args)
    st = MS.load(cfg)
    known = _known_guilds(cfg)
    if args.dms:
        scope = MS.Scope.dm()
    elif args.server:
        name = known.get(args.server) or st.labels.get(args.server)
        scope = MS.Scope.server(args.server, name, args.channel, args.category)
    else:
        if args.channel:
            print("--channel needs --server", file=sys.stderr)
            return 2
        scope = MS.Scope.everywhere()
    print(f"Settings for: {scope.label()}")
    if scope.kind == "everywhere":
        show_global(st)
    kinds = {
        settings_values._scope_kind(p)
        for p, _n in st.chain(scope)
        if p[0] not in ("builtin", "builtin_all_servers")
    }
    if scope.kind == "server":
        kinds |= {"server", "channel"}
    for key, value, source, rows in st.explain(scope):
        spec = st.spec(key)
        if not show_setting(spec, kinds, source):
            continue
        print(f"  {key:<24} {_fmt(spec, value):<18} [{source}]")
        if args.explain:
            for name, _path, is_set, v, src in rows:
                shown = _fmt(spec, v) if is_set else "(not set)"
                where = settings_resolution._source_text(src) if is_set else ""
                print(f"      {name:<28} {shown}" + (f"  ({where})" if where else ""))
    return 0


def cmd_explain(args):
    cfg = _cfg_path(args)
    st = MS.load(cfg)
    info = _info(cfg)
    place = _place(args, info)
    names = MS.names_from(info, st)
    keys = [MS.key_from_text(st, k) for k in args.keys] or None
    print(f"{MS.place_name(place, names)}:")
    if MS.is_paused(st, place):
        print("  Paused here.")
    if place.kind == "server" and not keys:
        print(f"  Matches: {matching_preset(st, place) or 'Custom'}")
    for line in MS.explain_lines(st, place, keys, names):
        print(f"  {line}")
    return 0


def cmd_places(args):
    cfg = _cfg_path(args)
    d = _directory(cfg)
    if d is None:
        print(
            "No directory.json yet: the monitor writes it once it is "
            "running. Start the monitor, then try again.",
            file=sys.stderr,
        )
        return 1
    age = time.time() - (d.get("updated") or 0)
    if age > DIRECTORY_STALE_SECS:
        print(
            f"Note: directory.json is {int(age // 3600)} hour(s) old; "
            f"start the monitor for fresh names."
        )
    find = (args.find or "").strip().lower()
    chans = d.get("channels")
    if not isinstance(chans, dict):
        chans = {}
    lines = [] if find or args.server else ["defaults", "dms"]
    for g in d.get("guilds") or []:
        if not isinstance(g, dict) or not g.get("id"):
            continue
        gid, name = str(g["id"]), str(g.get("name") or g["id"])
        if args.server and gid != str(args.server):
            continue
        n = g.get("member_count")
        size = f"{n:,} members" if isinstance(n, int) else "size unknown"
        rows = [
            c for c in chans.get(gid) or [] if isinstance(c, dict) and c.get("type") in CARD_TYPES
        ]
        hit = not find or find in name.lower()
        rows = [c for c in rows if hit or find in str(c.get("name") or "").lower()]
        if not hit and not rows:
            continue
        lines.append(f"server:{gid}  {name} ({size})")
        lines += [f"  channel:{gid}:{c['id']}  #{c.get('name') or c['id']}" for c in rows]
    if not lines:
        print(f"Nothing matches {args.find or args.server!r}.")
        return 1
    print("\n".join(lines))
    return 0
