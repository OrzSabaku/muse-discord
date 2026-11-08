"""Drain realtime events through the same receiver used by REST gap repair."""

import queue
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from muse_discord.channel_access import ChannelAccess
from muse_discord.conversation import PRIVATE_THREAD_TYPE, THREAD_TYPES, _who, channel_display
from muse_discord.message_models import MessagePlace
from muse_discord.receiving.messages import CONVERSATION_TYPES, ReceivedMessages


@dataclass
class GatewayEvents:
    guild: Callable[[dict, dict], bool]
    permissions: Callable[[dict], None]
    activity: Callable[..., None]
    record_handlers: Mapping[str, Callable[[dict, dict], None]]
    refresh: Callable[[], None]

    def record(self, state, rec):
        """Apply an interaction change only when the main loop drains its record."""
        kind = rec.get("kind")
        handler = self.record_handlers.get(kind) if isinstance(kind, str) else None
        if handler is not None:
            handler(state, rec)


@dataclass
class GatewayIngress:
    receiver: ReceivedMessages
    channels: dict
    access: ChannelAccess
    events: GatewayEvents
    allowed_guilds: set[str]

    @property
    def state(self):
        return self.receiver.state

    def consume(self, inbox):
        """Stage live DMs with guild triggers once the bot's identity is known."""
        if self.receiver.conversation.bot_id is None:
            return
        mentions, dms = self.drain(inbox)
        if mentions or dms:
            print(f"Gateway delivered {len(mentions)} mention(s), {len(dms)} DM(s).", flush=True)
        for msg, cname in dms:
            self.receiver.bursts.stage(msg, cname, "dm", place=MessagePlace(guild_id=None, dm=True))

    def metadata(self, m):
        if "_guild_event" in m:
            if self.events.guild(self.state, m["_guild_event"]):
                self.events.refresh()
            return True
        if "_perm_event" in m:
            self.events.permissions(m["_perm_event"])
            return True
        if "_typing" in m:
            self.events.activity(m["_typing"].get("channel_id"), m["_typing"].get("user_id"))
            return True
        if "_record" in m:
            self.events.record(self.state, m["_record"])
            return True
        return False

    def guild_message(self, m):
        cid, gid = m["channel_id"], m["guild_id"]
        info = self.channels.get(cid)
        if info is None:
            # Channel/thread discovery hasn't registered yet: resolve it
            # once via the API so the message isn't missed. A failure
            # (e.g. 403 private channel) skips it here only; last_seen
            # is untouched, so the REST poll can still pick it up.
            try:
                ch = self.receiver.get(f"/channels/{cid}")
            except Exception:
                return None
            if not ch or not ch.get("id"):
                return None
            g = ch.get("guild_id") or gid
            info = {
                "name": ch.get("name") or "?",
                "guild_id": g,
                "guild_name": (self.receiver.admission.directory.guilds.get(g) or {}).get("name")
                or g,
                "can_send": None,
                "thread": ch.get("type") in THREAD_TYPES,
                "parent_id": ch.get("parent_id"),
                "private": ch.get("type") == PRIVATE_THREAD_TYPE,
            }
            if not info["thread"] and ch.get("parent_id"):
                self.receiver.admission.directory.categories[cid] = ch["parent_id"]
            if not info["thread"]:  # watches: who can read it
                self.access.overwrites[cid] = ch.get("permission_overwrites") or []
            self.channels[cid] = info
        cname = channel_display(info)
        self.receiver.conversation.record_history(self.state, cid, [m])
        self.receiver.cursor.note_gateway(cid, m["id"])
        kind = self.receiver.classify(m, cname, gid)
        if kind:
            self.receiver.stage(m, cname, gid, kind)
            return m, cname, gid, kind
        return None

    def dm_message(self, m):
        cid = m["channel_id"]
        # DM event (no guild_id). The DM settings decide whose DMs
        # are even read (dm_sender_allowed): under the default
        # (dms_from: owners) everyone else's DMs are dropped unread
        # and unrecorded; dms.who.replies: off drops all of them.
        author = _who(m)
        own = author.get("id") == self.receiver.conversation.bot_id
        if not own:
            self.receiver.effects.count(
                "messages_seen", int(m.get("type", 0) in CONVERSATION_TYPES)
            )
            if not self.receiver.admission.dm_sender_allowed(author):
                self.receiver.note_dm_refused(m)
                return None
        self.receiver.conversation.record_history(self.state, cid, [m])
        self.receiver.cursor.note_gateway(cid, m["id"])
        if own or m.get("type", 0) not in CONVERSATION_TYPES:
            return None  # its own messages, system notices: context only
        if self.receiver.bursts.hold(cid, m):
            return None  # folded into a burst already being coalesced
        return m, "DM with " + (author.get("username") or cid)

    def is_new(self, m):
        cid = m["channel_id"]
        last = self.state["last_seen"].get(cid)
        if last and int(m["id"]) <= int(last):
            return False  # the poll loop already handled it
        if self.receiver.cursor.seen(cid, m["id"]):
            return False  # delivered twice (e.g. replayed on resume)
        gid = m.get("guild_id")
        return not (gid and self.allowed_guilds and gid not in self.allowed_guilds)

    def drain(self, inbox):
        """Pull gateway-delivered messages (all guilds + DMs) and classify them
        exactly like polled messages. Returns (triggers, dms): guild
        triggers as (msg, cname, guild_id, kind) tuples (already staged, see
        stage_found), DMs as (msg, cname).
        Guild join/leave events are applied to state on the way, and typing
        events and message arrivals are recorded for the early burst flush
        (note_activity). Skips anything the REST poll already processed (at
        or below its last_seen watermark), and leaves that watermark alone
        (see note_gateway_seen). Messages from servers outside a non-empty
        bot.guild_ids are ignored."""
        mentions, dms = [], []
        while True:
            try:
                m = inbox.get_nowait()
            except queue.Empty:
                break
            if self.metadata(m):
                continue
            cid = m.get("channel_id")
            if not cid:
                continue
            # Recorded even when the poll got here first: the burst holding
            # this message needs to know when its author last spoke.
            self.events.activity(cid, _who(m).get("id"), m["id"])
            if not self.is_new(m):
                continue
            gid = m.get("guild_id")
            if gid:
                trigger = self.guild_message(m)
                if trigger:
                    mentions.append(trigger)
            else:
                dm = self.dm_message(m)
                if dm:
                    dms.append(dm)
        return mentions, dms
