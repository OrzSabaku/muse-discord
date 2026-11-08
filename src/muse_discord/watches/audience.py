"""Audience and request policy for reminders and Discord/web watches.

This layer resolves a requester's place and permissions before any store edit.
It only uses the provided directory, response policy and access service; no
monitor import or bot token is required.
"""

from collections.abc import Callable
from dataclasses import dataclass

from muse_discord import muse_watches as W
from muse_discord.channel_access import PERM_VIEW_CHANNEL, ChannelAccess
from muse_discord.conversation import _snowflake_ts
from muse_discord.message_queue import owner_dm_metadata
from muse_discord.response_policy import ResponsePolicy


def _place_refused(why):
    raise W.Refused(W.LINES["place"].format(why=why))


def _place_ids(value, gid, cid, dm):
    m = W.PLACE_RE.fullmatch(value)
    if value in ("here", "server"):
        if dm:
            _place_refused("in a DM, tell me which server or channel to watch.")
        tg, tc = gid, (cid if value == "here" else None)
    elif m and m.group(1):
        tg, tc = m.group(1), None
    elif m:
        tg, tc = m.group(2), m.group(3)
    else:
        _place_refused("I don't know that place.")
    return tg, tc


@dataclass
class WatchAudience:
    response_source: Callable[[], ResponsePolicy]
    access: ChannelAccess
    guilds: dict
    names: dict
    owners_absent: Callable[..., bool]
    dm_refusal: Callable[[dict], str | None]

    @property
    def response(self) -> ResponsePolicy:
        return self.response_source()

    def can_set(self, uid, from_bot, here, matches):
        """abilities.reminders allows uid here (owners always; bots never).
        matches(people value) tests uid against a people setting."""
        if from_bot:
            return False
        if str(uid) in self.response.owners:
            return True
        try:
            return bool(matches(here["abilities.reminders"]))
        except Exception:
            return False

    def can_web(self, uid, from_bot, dm):
        """Web watches: an owner's own DM only (no setting: each check is a
        turn of the owner's Muse reading the web). In a server, others' text
        reaches the same run and could steer what the checker visits."""
        return not from_bot and bool(dm) and str(uid) in self.response.owners

    def where(self, gid, cid, dm):
        if dm:
            return "DMs"
        info = self.access.channels.get(cid) or {}
        g = (self.guilds.get(gid) or {}).get("name") or info.get("guild_name") or gid
        return f"#{info.get('name') or cid} in {g}"

    def resolve(self, value, uid, gid, cid, dm):
        """A watch's "in", asked in (gid, cid) or a DM: (scope, its name), or
        raises muse_watches.Refused. Only a server the bot is in and the
        requester is a member of, and a channel there they can read; from a
        server, only that server."""

        def no(why):
            raise W.Refused(W.LINES["place"].format(why=why))

        tg, tc = _place_ids(value, gid, cid, dm)
        if not dm and tg != gid:
            no("from a server I only watch that server; ask me in a DM for another one.")
        if tg not in self.guilds:
            no("I'm not in that server.")
        if str(uid) not in self.response.owners and self.owners_absent(tg):
            no("I don't answer anyone in that server.")
        if tc and (self.access.channels.get(tc) or {}).get("guild_id") != tg:
            no("I can't read that channel.")
        try:
            roles = self.access.member_roles(tg, uid)
        except Exception:
            no("I couldn't check that server just now; try again in a minute.")
        if roles is None:
            no("you're not in that server.")
        if tc and not self.access.can_view(tg, uid, tc):
            no("you can't read that channel.")
        gname = (self.guilds.get(tg) or {}).get("name") or tg
        if tc:
            return (
                {"guild_id": tg, "channel_id": tc},
                f"#{(self.access.channels.get(tc) or {}).get('name') or tc} in {gname}",
            )
        return {"guild_id": tg, "channel_id": None}, f"all of {gname}"

    def request(self, meta, qid, changes):
        cid = str(meta.get("channel_id") or "")
        uid = str(meta.get("author_id") or "")
        dm = bool(meta.get("dm"))
        gid = (
            None
            if dm
            else (meta.get("guild_id") or (self.access.channels.get(cid) or {}).get("guild_id"))
        )
        if meta.get("bot"):
            raise W.Refused(W.LINES["bot"])
        here = self.response.settings().resolve(self.response.scope_for(gid, cid, dm))

        def matches(who):
            return self.response.people_match(
                who, uid, lambda: self.response.member_roles(gid, uid)
            )

        can = self.can_set(uid, False, here, matches)
        # (owner_dm_gate checked the owner DM's nonce; without the
        # right, decide refuses a web watch in plain words.)
        can_web = (
            can and self.can_web(uid, False, dm) and owner_dm_metadata(meta, self.response.owners)
        )
        if (
            can_web
            and meta.get("has_inputs")
            and any(c.get("op") == "add" and c.get("check") == "web" for c in changes)
        ):
            # Someone else's words (a document, a quoted message) could
            # have put it there.
            raise W.Refused(W.LINES["web-inputs"], W.REASON_NOT_ALLOWED, {"kind": "watch-web"})
        resolved = {}
        if can:
            for c in changes:
                if (
                    c.get("op") == "add"
                    and c.get("kind") == "watch"
                    and "match" in c
                    and c["match"]["in"] not in resolved
                ):
                    resolved[c["match"]["in"]] = self.resolve(c["match"]["in"], uid, gid, cid, dm)
        return W.Request(
            uid,
            meta.get("author") or self.names.get(uid) or uid,
            uid in self.response.owners,
            gid,
            cid,
            dm,
            qid,
            _snowflake_ts(qid),
            can,
            self.where(gid, cid, dm),
            resolved.__getitem__,
            can_web=can_web,
        )

    def view_problem(self, gid, uid, cid):
        p = self.access.permissions(gid, uid, cid)
        if p is not None and not p & PERM_VIEW_CHANNEL:
            return "they can no longer read that channel"
        return None

    def fire_allowed(self, uid, name, cid, gid, dm):
        """None when a fire may go to uid in cid now, else why not: the bot
        still answers them there (who.replies, an owner in the server,
        who.allow / who.ignore; in DMs, the DM rules) and, in a server, they
        can still read the channel."""
        owner = uid in self.response.owners
        if dm:
            return None if owner else self.dm_refusal({"id": uid, "username": name})
        if cid not in self.access.channels:
            return "the bot no longer reads that channel"
        here = self.response.settings().resolve(self.response.scope_for(gid, cid))
        if here["who.replies"] == "off":
            return "who.replies is off there"
        if not owner and self.owners_absent(gid):
            return "none of the owners is in that server"
        if not self.response.who_allows({"author": {"id": uid}, "channel_id": cid}, gid, here):
            return "not allowed by who.allow / who.ignore"
        # Unknown permissions (a failed lookup) let it through, unlike a
        # match: a fire goes back where its requester asked, and a match it
        # links was checked against their view when it matched.
        return self.view_problem(gid, uid, cid)
