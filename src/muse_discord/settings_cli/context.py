"""Resolve CLI places and read monitor metadata used to explain setting changes.

Directory and state snapshots are read on demand. Importing command services
never reads owner configuration or edits a file.
"""

import json
import os

from muse_discord import muse_settings as MS
from muse_discord.paths import project_root

PKG = str(project_root(__file__))

MAX_DISCORD_ID_DIGITS = 25


def _cfg_path(args):
    return args.config or os.environ.get("MUSE_DISCORD_CONFIG") or os.path.join(PKG, "config.yaml")


def _state(cfg):
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(cfg)), "state.json")) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _known_guilds(cfg):
    known = _state(cfg).get("known_guilds")
    return known if isinstance(known, dict) else {}


def _directory(cfg):
    """directory.json (written by the running monitor: its servers, with
    member counts, and their channels), or None."""
    for d in (os.path.dirname(os.path.abspath(cfg)), PKG):
        try:
            with open(os.path.join(d, "directory.json")) as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("guilds"), list):
            return data
    return None


def _info(cfg):
    """What the monitor last saw: server and channel names and member
    counts (directory.json, else state.json), for the risk checks and
    the sentences."""
    state = _state(cfg)
    known = state.get("known_guilds")
    counts = state.get("guild_members")
    known = known if isinstance(known, dict) else {}
    counts = counts if isinstance(counts, dict) else {}
    guilds = {
        str(g): {"id": str(g), "name": n, "member_count": counts.get(g)} for g, n in known.items()
    }
    channels = {}
    d = _directory(cfg)
    if d is not None:
        for g in d.get("guilds") or []:
            if isinstance(g, dict) and g.get("id"):
                gid = str(g["id"])
                guilds[gid] = {
                    "id": gid,
                    "name": g.get("name") or gid,
                    "member_count": g.get("member_count")
                    if g.get("member_count") is not None
                    else counts.get(gid),
                }
        chans = d.get("channels")
        channels = chans if isinstance(chans, dict) else {}
    return {"guilds": list(guilds.values()), "channels": channels}


def _place(args, info):
    place = MS.parse_place(args.place or "defaults", info)
    if getattr(args, "category", None):
        if place.kind != "channel":
            raise MS.SettingsError("--category goes with a channel place")
        cat = str(args.category).strip()
        if not cat.isdigit() or len(cat) > MAX_DISCORD_ID_DIGITS or cat == place.channel_id:
            raise MS.SettingsError(f"--category {cat}: expected the channel's category id (digits)")
        place.category_id = cat
    return place
