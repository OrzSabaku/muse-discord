"""Shared classification and ordered REST reception for guild messages and DMs."""

import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from muse_discord.admission import AdmissionPolicy
from muse_discord.bursts import PendingBursts
from muse_discord.conversation import _who, now_snowflake
from muse_discord.message_models import MessagePlace
from muse_discord.receiving.cursor import ReceiveCursor
from muse_discord.response_policy import predates_join

# DEFAULT and REPLY are authored messages; other types are system notices.
CONVERSATION_TYPES = (0, 19)


@dataclass
class ReceptionEffects:
    watch_scan: Callable[[dict, str | None], None]
    count: Callable[..., Any]
    refused: Callable[[dict, str, str], None]
    no_send: Callable[[dict, str], None]
    seed: Callable[[str, str, dict], None]


@dataclass
class ReceivedMessages:
    admission: AdmissionPolicy
    bursts: PendingBursts
    get: Callable[[str], Any]
    effects: ReceptionEffects

    @property
    def state(self):
        return self.bursts.state

    @property
    def conversation(self):
        return self.admission.response.conversation

    @property
    def cursor(self):
        return ReceiveCursor(self.state)

    def stage(self, msg, cname, guild_id, kind):
        """Stage one classified guild trigger the moment it is classified, in
        arrival order, so a non-trigger follow-up later in the same poll
        batch or gateway drain finds the open burst (hold_fragment) instead
        of being classified before its trigger was staged. Talk scope: a
        trigger in a channel the bot cannot send in is never staged; it is
        logged once per channel and ledgered as skipped so the sweep leaves
        it alone."""
        info = self.admission.directory.channels.get(msg["channel_id"])
        if info is not None and info.get("can_send") is False:
            self.effects.no_send(msg, cname)
            return
        self.bursts.stage(msg, cname, kind, place=MessagePlace(guild_id=guild_id))

    def classify(self, msg, channel_name, guild_id=None):
        """Classify a single message exactly the way check_channel's loop does.

        Shared by the REST poll path and the gateway fast path so both ingest
        routes behave identically. Returns "mention", "follow_up", or None
        (folded into a burst, skipped, or not actionable).
        """
        if predates_join(self.state, msg, guild_id):
            # Older than the bot's arrival in this guild: never a trigger,
            # never a fragment. It can still reach the worker as history.
            return None
        if msg.get("type", 0) not in CONVERSATION_TYPES:
            # Discord's system notices ("X pinned a message", "X started a
            # thread", ...) carry the acting user as author: never a
            # trigger, a follow-up or a fragment.
            return None
        author = _who(msg)
        if author.get("id") == self.conversation.bot_id:
            return None  # the bot's own messages: never triggers or fragments
        self.effects.watch_scan(msg, guild_id)  # Discord watches (cheap; never raises)
        self.effects.count("messages_seen")
        if (
            self.conversation.is_bot_author(author)
            and self.admission.response.settings().get(
                "who.bots", self.admission.response.scope_for(guild_id, msg.get("channel_id"))
            )
            == "ignore"
        ):
            if self.conversation.addresses_bot(msg):
                self.effects.refused(msg, channel_name, "who.bots: ignore")
            return None
        if self.bursts.hold(msg.get("channel_id"), msg):
            return None  # folded into a burst already being coalesced
        kind, refusal = self.admission.trigger_verdict(msg, guild_id, self.state)
        if refusal:
            self.effects.refused(msg, channel_name, refusal)
        return kind

    def poll_channel(self, channel_id, channel_name, guild_id=None):
        """Check for new messages in a channel and stage its triggers (see
        stage_found). Returns the (msg, kind) pairs staged (kind as in
        trigger_kind). Messages the gateway already handled are skipped."""
        last_seen = self.state["last_seen"].get(channel_id)
        if not last_seen:
            # Never seeded (discovery normally does it): history is history.
            self.effects.seed(channel_id, channel_name, self.state)
            return []

        # 100 (Discord's max) so the poll keeps up with busy channels: it now
        # re-reads every message the gateway delivered since the last poll.
        params = {"limit": 100, "after": last_seen}

        query = urllib.parse.urlencode(params)
        try:
            messages = self.get(f"/channels/{channel_id}/messages?{query}")
        except Exception as e:
            print(f"Error reading #{channel_name}: {e}", flush=True)
            return []

        if not messages:
            return []

        self.conversation.record_history(self.state, channel_id, messages)

        newest = max(messages, key=lambda m: int(m["id"]))
        gw_done = set(self.state.get("gw_seen", {}).get(channel_id, ()))
        self.cursor.advance_rest(channel_id, newest["id"])

        triggers = []
        for msg in sorted(messages, key=lambda m: int(m["id"])):
            if msg["id"] in gw_done:
                continue  # the gateway path already handled it
            kind = self.classify(msg, channel_name, guild_id)
            if kind:
                self.stage(msg, channel_name, guild_id, kind)
                triggers.append((msg, kind))

        return triggers

    def poll_dm(self, dm_id, dm_name):
        """Return unseen non-bot messages in a DM channel. In a DM every message
        is addressed to the bot, so no mention filtering is needed. The first
        time a DM channel is read (first run, or state.json deleted) its
        history counts as already seen, like a new channel's: the backlog is
        never one big fresh trigger. Messages the gateway already handled
        are skipped."""
        try:
            messages = self.get(f"/channels/{dm_id}/messages?limit=20")
        except Exception as e:
            print(f"DM poll error #{dm_name}: {e}", flush=True)
            return []
        last_seen = self.state["last_seen"].get(dm_id)
        if not messages:
            if not last_seen:
                self.state["last_seen"][dm_id] = now_snowflake()
            return []

        self.conversation.record_history(self.state, dm_id, messages)

        if not last_seen:
            newest = max(messages, key=lambda m: int(m["id"]))
            self.state["last_seen"][dm_id] = newest["id"]
            print(f"Tracking DM with {dm_name} from message {newest['id']}.", flush=True)
            return []
        new = [m for m in messages if int(m["id"]) > int(last_seen)]
        gw_done = set(self.state.get("gw_seen", {}).get(dm_id, ()))
        if new:
            newest = max(new, key=lambda m: int(m["id"]))
            self.cursor.advance_rest(dm_id, newest["id"])

        out = []
        for m in sorted(new, key=lambda m: int(m["id"])):
            if (
                m.get("author", {}).get("id") == self.conversation.bot_id
                or m["id"] in gw_done
                or m.get("type", 0) not in CONVERSATION_TYPES
            ):
                continue
            self.effects.count("messages_seen")
            if not self.admission.dm_sender_allowed(m.get("author")):
                self.note_dm_refused(m)
                continue
            if not self.bursts.hold(dm_id, m):
                out.append(m)
        return out

    def note_dm_refused(self, msg):
        """A DM the DM settings decline (dm_sender_allowed). Only an owner's
        or a who.dms_from-listed person's is ledgered (see note_refused): the
        sweep covers only their DM channels, and a stranger's DMs stay
        unrecorded."""
        author = _who(msg)
        if self.admission.dm_swept_author(author.get("id")) and not self.conversation.is_bot_author(
            author
        ):
            self.effects.refused(
                msg,
                "DM with " + (author.get("username") or "?"),
                self.admission.dm_refusal(author) or "who.replies is off for DMs",
            )
