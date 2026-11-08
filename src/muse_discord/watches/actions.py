"""Authorize and apply reminder/watch requests without Discord access."""

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from muse_discord.watches.limits import (
    LINES,
    NOTE_MAX,
    PER_PERSON_MAX,
    REASON_CAP,
    REASON_NOT_ALLOWED,
    REMIND_MAX_SECS,
    REMIND_MIN_SECS,
    TOTAL_MAX,
    TZ_MAX_USERS,
    WATCH_DEFAULT_SECS,
    WATCH_MAX_SECS,
    WATCH_MIN_SECS,
)
from muse_discord.watches.store import active, new_id
from muse_discord.watches.timing import known_tz, parse_utc


class Refused(Exception):
    def __init__(self, line, reason=None, detail=None):
        super().__init__(line)
        self.line = line
        self.reason = reason  # a ledger reason, or None (replied)
        self.detail = detail


@dataclass
class Request:
    """Monitor-authored identity and permissions for one schedule request."""

    requester: str
    name: str
    owner: bool
    guild_id: str | None
    channel_id: str
    dm: bool
    message_id: str
    message_time: float
    can_set: bool
    where: str
    resolve: Callable[[str], tuple[dict[str, Any], str]]
    can_web: bool = False

    def __post_init__(self):
        self.requester = str(self.requester)
        self.channel_id = str(self.channel_id)
        self.message_id = str(self.message_id)
        self.owner = bool(self.owner)
        self.dm = bool(self.dm)

    def new_record(self, c, now):
        kind = "watch-web" if c.get("check") == "web" else c["kind"]
        rec = {
            "kind": kind,
            "requester": self.requester,
            "name": self.name,
            "guild_id": None if self.dm else self.guild_id,
            "channel_id": self.channel_id,
            "dm": self.dm,
            "message_id": self.message_id,
            "created": now,
            "where": self.where,
            "note": (c.get("note") or "").strip()[:NOTE_MAX],
        }
        if kind == "remind":
            fields = self.reminder_fields(c, now)
        elif kind == "watch-web":
            fields = self.web_fields(c, now)
        else:
            fields = self.watch_fields(c, now)
        rec.update(fields)
        return rec

    def reminder_fields(self, c, now):
        due = parse_utc(c["at"]) if "at" in c else self.message_time + c["after"]
        if due is None:
            raise Refused(LINES["time"])
        delay = due - self.message_time
        if delay < REMIND_MIN_SECS or delay > REMIND_MAX_SECS or due - now > REMIND_MAX_SECS:
            raise Refused(LINES["time"])
        return {"due": due}

    def web_fields(self, c, now):
        until = parse_utc(c["until"]) if "until" in c else now + WATCH_DEFAULT_SECS
        if until is None or until - now < WATCH_MIN_SECS or until - now > WATCH_MAX_SECS:
            raise Refused(LINES["watch-time"])
        return {
            "until": until,
            "what": " ".join(c["what"].split()),
            "every": c["every"],
            "repeat": bool(c.get("repeat")),
            "owner": self.owner,
            "next_check": now,
            "check": None,
            "fails": 0,
            "checks": 0,
        }

    def watch_fields(self, c, now):
        m = c["match"]
        until = parse_utc(c["until"]) if "until" in c else now + WATCH_DEFAULT_SECS
        if until is None or until - now < WATCH_MIN_SECS or until - now > WATCH_MAX_SECS:
            raise Refused(LINES["watch-time"])
        scope, scope_name = self.resolve(m["in"])
        return {
            "until": until,
            "scope": scope,
            "scope_name": scope_name,
            "words": [w.strip() for w in m["words"]],
            "every": bool(m.get("every")),
            "last_fired": 0,
            "fired": 0,
        }

    def cancel_ids(self, items, live, change):
        # Web watches can only be changed from the requesting owner's DM.
        def permitted(record):
            return self.can_web or record.get("kind") != "watch-web"

        if change.get("all"):
            return [
                i
                for i, record in items.items()
                if record.get("requester") == self.requester and i in live and permitted(record)
            ]
        record = items.get(change["id"])
        if (
            record is None
            or change["id"] not in live
            or not permitted(record)
            or not (self.owner or record.get("requester") == self.requester)
        ):
            raise Refused(LINES["cancel"])
        return [change["id"]]

    def set_timezone(self, tz, change):
        if change["tz"] is None:
            tz.pop(self.requester, None)
        elif not known_tz(change["tz"]):
            raise Refused(LINES["tz"])
        else:
            tz[self.requester] = change["tz"]


def _check_schedule_caps(req, live, kind):
    if not req.owner:
        mine = sum(1 for r in live.values() if r.get("requester") == req.requester)
        if mine >= PER_PERSON_MAX:
            raise Refused(
                LINES["cap-person"].format(cap=PER_PERSON_MAX),
                REASON_CAP,
                {"kind": kind, "cap": PER_PERSON_MAX, "per": "person"},
            )
        if len(live) >= TOTAL_MAX:
            raise Refused(
                LINES["cap-total"], REASON_CAP, {"kind": kind, "cap": TOTAL_MAX, "per": "total"}
            )


def decide(data, req, changes, now=None):
    """Apply changes (a valid schedule.changes list) for req to data,
    all or nothing. Returns a list of what was done ([(op, id)]); raises
    Refused, leaving data untouched."""
    now = time.time() if now is None else now
    adds = [c for c in changes if c.get("op") == "add"]
    if not req.can_set and adds:
        raise Refused(LINES["not-allowed"], REASON_NOT_ALLOWED)
    if not (req.can_web and req.owner) and any(c.get("check") == "web" for c in adds):
        raise Refused(LINES["web-not-allowed"], REASON_NOT_ALLOWED, {"kind": "watch-web"})
    items = dict(data["items"])
    live = active(data, now)
    tz = dict(data["tz"])
    done = []
    for c in changes:
        op = c["op"]
        if op == "tz":
            req.set_timezone(tz, c)
            done.append(("tz", c["tz"]))
            continue
        if op == "cancel":
            ids = req.cancel_ids(items, live, c)
            for i in ids:
                items.pop(i, None)
                live.pop(i, None)
                done.append(("cancel", i))
            continue
        rec = req.new_record(c, now)
        kind = rec["kind"]
        _check_schedule_caps(req, live, kind)
        rid = new_id({"items": items}, kind)
        items[rid] = rec
        live[rid] = rec
        done.append(("add", rid))
    data["items"] = items
    if len(tz) > TZ_MAX_USERS:
        for k in list(tz)[: len(tz) - TZ_MAX_USERS]:
            del tz[k]
    data["tz"] = tz
    return done
