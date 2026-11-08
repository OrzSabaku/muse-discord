"""Render requester-bound notification summaries for worker context."""

import time
from dataclasses import dataclass

from muse_discord.watches.limits import LATE_SECS, LIST_MAX
from muse_discord.watches.store import active
from muse_discord.watches.timing import iso


def _entry(i, r, show_for=False):
    """One listed item. show_for: someone else's, listed in an owner's
    DM: who and when, but not its note or words, which that person's
    request chose (text from others never reaches an owner-DM wake)."""
    e = {"id": i, "kind": r.get("kind"), "set_in": r.get("where")}
    if not show_for:
        e["note"] = r.get("note") or ""
    if r.get("kind") == "remind":
        e["due"] = iso(r.get("due", 0))
    elif r.get("kind") == "watch-web":
        e.update(
            {
                "web": True,
                "every_secs": r.get("every"),
                "repeat": bool(r.get("repeat")),
                "until": iso(r.get("until", 0)),
            }
        )
        if not show_for:
            e["what"] = r.get("what")
    else:
        e.update(
            {
                "in": r.get("scope_name"),
                "every": bool(r.get("every")),
                "until": iso(r.get("until", 0)),
            }
        )
        if not show_for:
            e["words"] = r.get("words")
    if show_for:
        e["for"] = r.get("name") or r.get("requester")
        e["for_id"] = r.get("requester")
    return e


@dataclass
class CardContext:
    guild_id: str | None = None
    dm: bool = False
    owner_dm: bool = False
    can_set: bool = True
    can_web: bool = False


def card(data, uid, now=None, context=None):
    """The `watches` field of a queue item: the author's reminders and
    watches that may be shown where they asked (all of them in a DM,
    those set in this server in a server, with a count of the rest),
    their time zone if they told it, and whether they may set new ones
    (can_set; can_web for web watches).
    In an owner's own DM: everyone's, marked with whose."""
    context = context or CardContext()
    guild_id = context.guild_id
    dm = context.dm
    owner_dm = context.owner_dm
    can_set = context.can_set
    can_web = context.can_web
    now = time.time() if now is None else now
    uid = str(uid)
    live = sorted(
        active(data, now).items(), key=lambda kv: kv[1].get("due") or kv[1].get("until", 0)
    )
    if owner_dm:
        shown = [_entry(i, r, show_for=r.get("requester") != uid) for i, r in live]
        hidden = 0
    else:
        mine = [(i, r) for i, r in live if r.get("requester") == uid]
        ok = [(i, r) for i, r in mine if dm or (not r.get("dm") and r.get("guild_id") == guild_id)]
        shown = [_entry(i, r) for i, r in ok]
        hidden = len(mine) - len(ok)
    out = {
        "can_set": bool(can_set),
        "can_web": bool(can_set and can_web),
        "items": shown[:LIST_MAX],
    }
    if hidden:
        out["elsewhere"] = hidden  # theirs, set in other places
    if len(shown) > LIST_MAX:
        out["more"] = len(shown) - LIST_MAX
    if data["tz"].get(uid):
        out["tz"] = data["tz"][uid]
    return out


def fire_entry(i, r, now, matched=None):
    """One entry of a fire's `reminder.items`."""
    e = {
        "id": i,
        "kind": r.get("kind"),
        "note": r.get("note") or "",
        "set_at": iso(r.get("created", now)),
    }
    if r.get("kind") == "remind":
        e["due"] = iso(r.get("due", now))
        e["late"] = now - r.get("due", now) > LATE_SECS
    elif r.get("kind") == "watch-web":
        res = (matched or {}).get("result", "met")
        e.update(
            {
                "what": r.get("what"),
                "result": res,
                "ends": iso(r.get("until", now)) if r.get("repeat") and res == "met" else "now",
            }
        )
        if matched and matched.get("summary"):
            e["summary"] = matched["summary"]
    else:
        e.update({"words": r.get("words"), "in": r.get("scope_name")})
        if matched:
            e["matched"] = matched
        e["ends"] = "now" if not r.get("every") else iso(r.get("until", now))
    return e
