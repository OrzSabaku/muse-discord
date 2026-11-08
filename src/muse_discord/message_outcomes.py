"""Read-only message diagnostics for settings pages and context commands."""

from collections.abc import Callable
from dataclasses import dataclass

from muse_discord.conversation import _snowflake_ts
from muse_discord.directory.snapshot import DirectorySnapshot
from muse_discord.message_ledger import MessageLedger, ledger_code
from muse_discord.receiving.direct import DirectChannels


def render_outcome(mid, e, where, channels, direct):
    """One ledger entry for /settings. Its server comes from the entry
    (old entries have none), else from discovery; an entry is a DM's
    only when it says so or its channel is a known DM channel (an
    unmapped server channel is never shown under DMs)."""
    cid = str(e.get("channel_id") or "")
    code = e.get("code") or ledger_code(e.get("status"), e.get("reason"))
    if e.get("guild_id"):
        gid, parent = e["guild_id"], e.get("parent_id")
    else:
        gid, parent = where.get(cid, (None, None))
        info = channels.get(cid)
        if gid is None and info:
            gid = info.get("guild_id")
            parent = info.get("parent_id") if info.get("thread") else None
    dm = gid is None and bool(
        e.get("dm")
        or cid in direct.names
        or cid in direct.users
        or code in ("dm-off", "dm-not-allowed")
    )
    return {
        "message_id": mid,
        "channel_id": cid or None,
        "guild_id": gid,
        "parent_id": parent,
        "dm": dm,
        "author_id": e.get("author_id"),
        "author": e.get("author"),
        "status": e.get("status"),
        "code": code,
        "detail": e.get("detail") or {},
        "reason": e.get("reason"),
        "at": e.get("updated") or _snowflake_ts(mid),
    }


@dataclass
class MessageOutcomes:
    ledger: MessageLedger
    directory: DirectorySnapshot
    direct: DirectChannels
    fired_source: Callable[[], dict]

    def recent(self, place, limit=20):
        """The newest mention-ledger entries for a place (a Place from
        muse_settings: a server, a channel and its threads, or DMs),
        newest first: message_id, channel_id, guild_id, parent_id,
        author_id (None in old entries), author,
        status, code, detail, reason, at."""
        ledger = self.ledger
        where = self.directory.locations(include_unmonitored=True)
        out = []
        fired = self.fired_source()  # reminders: not messages
        for mid, e in ledger.load()["entries"].items():
            if not isinstance(e, dict) or mid in fired:
                continue
            o = render_outcome(mid, e, where, ledger.channels, self.direct)
            kind = getattr(place, "kind", None)
            if kind == "dms":
                ok = o["guild_id"] is None and o["dm"]
            elif kind == "server":
                ok = o["guild_id"] == place.guild_id
            elif kind == "channel":
                ok = (o["parent_id"] or o["channel_id"]) == place.channel_id
            else:
                ok = True
            if ok:
                out.append(o)
        out.sort(key=lambda o: o["at"], reverse=True)
        return out[: max(0, int(limit))]

    def one(self, message_id):
        """recent_outcomes' entry for one message, or None."""
        ledger = self.ledger
        e = ledger.load()["entries"].get(str(message_id))
        if not isinstance(e, dict):
            return None
        where = self.directory.locations()
        return render_outcome(str(message_id), e, where, ledger.channels, self.direct)
