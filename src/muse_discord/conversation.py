"""Conversation interpretation and bounded history, independent of I/O.

A Conversation is a bot-identity snapshot. Mutable conversation history and
follow-up windows are supplied by its caller; no application globals are read.
"""

import re
import time
from dataclasses import dataclass, field

FOLLOW_UP_WINDOW_SECS = 120
THREAD_TYPES = (10, 11, 12)
HISTORY_ATTACHED = 8
HISTORY_PER_CHANNEL = 12
HISTORY_TEXT_CHARS = 300


def _who(msg):
    """A message's author object ({} when it has none)."""
    return msg.get("author") or {}


def is_forwarded(msg):
    """A forwarded message (Discord's message_reference type 1, with the
    forwarded content in message_snapshots)."""
    ref = msg.get("message_reference") or {}
    return ref.get("type") == 1 or bool(msg.get("message_snapshots"))


def _snowflake_ts(mid):
    try:
        return ((int(mid) >> 22) + 1420070400000) / 1000.0
    except Exception:
        return 0.0


@dataclass(frozen=True)
class Conversation:
    bot_id: str | None
    bot_name: str | None
    human_ids: set[str] = field(default_factory=set)
    respond_to: frozenset[str] = frozenset({"mention", "reply_to_bot"})

    def is_bot_author(self, author):
        """True when a message author is a bot (Discord's author.bot), unless
        testing.treat_as_human lists its id."""
        author = author or {}
        return bool(author.get("bot")) and str(author.get("id")) not in self.human_ids

    def open_follow_up(self, state, channel_id, user_id):
        fu = state.setdefault("follow_up", {})
        now = time.time()
        fu[f"{channel_id}:{user_id}"] = now
        for k in [k for k, t in fu.items() if now - t > FOLLOW_UP_WINDOW_SECS]:
            del fu[k]

    def addresses_someone_else(self, msg):
        """A Discord reply to someone else's message, or an @mention of
        another user or a role (the bot's own mention is direct address)."""
        if msg.get("message_reference"):
            rm = msg.get("referenced_message") or {}
            if _who(rm).get("id") != self.bot_id:
                return True
        if any((m or {}).get("id") != self.bot_id for m in msg.get("mentions") or []):
            return True
        return bool(msg.get("mention_roles") or msg.get("mention_everyone"))

    def follow_up_open(self, state, msg):
        """True when msg falls in its author's open follow-up window in its
        channel (and does not address someone else, which closes it)."""
        fu = (state or {}).get("follow_up") or {}
        author = _who(msg)
        key = f"{msg.get('channel_id')}:{author.get('id')}"
        opened = fu.get(key)
        if opened is None or self.is_bot_author(author):
            return False
        ts = _snowflake_ts(msg.get("id")) or time.time()
        if ts > opened + FOLLOW_UP_WINDOW_SECS:
            fu.pop(key, None)
            return False
        if ts < opened - 2:
            return False  # written before the bot's reply landed
        if self.addresses_someone_else(msg):
            fu.pop(key, None)
            return False
        return True

    def own_replies_in_run(self, history, msg):
        """How many of the bot's own messages are in the run of bot messages
        that ends just before msg (since a human last spoke in the channel):
        the bot's consecutive replies in a bot-to-bot exchange."""
        n = 0
        for h in reversed([h for h in history if int(h["id"]) < int(msg["id"])]):
            if not h.get("bot"):
                break
            if h.get("author_id") == self.bot_id:
                n += 1
        return n

    def is_mention(self, msg):
        """True when the message @mentions the bot on the platform. Plain
        text containing the bot's name is NOT a mention: in a server whose
        topic shares the bot's name, that made every casual "my muse ..." a
        summons. Whether a bot author may trigger is trigger_kind's call."""
        author = _who(msg)
        if author.get("id") == self.bot_id:
            return False
        return any(m.get("id") == self.bot_id for m in msg.get("mentions", []))

    def addresses_bot(self, msg):
        """Direct address as bot.respond_to defines it: a platform @mention
        or a Discord reply to the bot."""
        return ("mention" in self.respond_to and self.is_mention(msg)) or (
            "reply_to_bot" in self.respond_to and self.is_reply_to_bot(msg)
        )

    def is_reply_to_bot(self, msg):
        """True when the message is a Discord reply to one of the bot's own
        messages. Replying to the bot is addressing the bot, even with no
        @mention, so it counts as a mention trigger. Uses the embedded
        referenced_message only (no extra API call); the mention check still
        catches @mentions."""
        rm = msg.get("referenced_message")
        if not rm:
            return False
        return _who(rm).get("id") == self.bot_id

    def record_history(self, state, channel_id, messages):
        """Keep a rolling window of recent messages per channel/DM so the reply
        worker can draft with conversational context instead of starting cold.
        The triggering message itself is recorded here too; enqueue_message
        excludes it when attaching history to a queue item."""
        hist = state.setdefault("history", {}).setdefault(channel_id, [])
        seen_ids = {h["id"] for h in hist}
        for m in sorted(messages, key=lambda x: int(x["id"])):
            if m["id"] in seen_ids:
                continue
            author = _who(m)
            text = (m.get("content") or "")[:HISTORY_TEXT_CHARS]
            if not text and not (m.get("attachments") or m.get("embeds")):
                continue
            entry = {
                "id": m["id"],
                "author": author.get("username", "?"),
                "author_id": author.get("id"),
                "bot": self.is_bot_author(author),
                "text": text or "[shared media]",
                "ts": m.get("timestamp"),
            }
            # The name people see (server nickname or global display name),
            # when it differs from the username: what someone typing "@Name"
            # by hand is most likely to use.
            if m.get("webhook_id"):
                entry["webhook"] = True  # not a person (proactive.py)
            display = (m.get("member") or {}).get("nick") or author.get("global_name")
            if display and display != entry["author"]:
                entry["display"] = display
            hist.append(entry)
            seen_ids.add(m["id"])
        # Sorted: the poll re-reads messages the gateway already recorded,
        # and an old one re-added must not land after newer ones.
        hist.sort(key=lambda h: int(h["id"]))
        state["history"][channel_id] = hist[-HISTORY_PER_CHANNEL:]

    def bot_run_length(self, history, msg):
        """How many consecutive bot-authored messages (any bot, this one
        included) end at msg in this channel's history, with no human in
        between. 0 when msg is from a human."""
        run = 0
        older = [h for h in history if int(h["id"]) < int(msg["id"])]
        for h in reversed([*older, {"bot": self.is_bot_author(msg.get("author"))}]):
            if not h.get("bot"):
                break
            run += 1
        return run

    def conversation_participants(self, history, msg, reply_ctx):
        """Everyone visible in the conversation, as [{name, display?, id, bot,
        you?}], so the worker can address them with <@id>: a bare "@Name" in
        Discord text notifies nobody."""
        people = {}

        def add(pid, name, bot, display=None):
            if not pid or pid in people:
                return
            p = {"name": name or "?", "id": pid, "bot": bool(bot)}
            if display:
                p["display"] = display
            if pid == self.bot_id:
                p["you"] = True
            people[pid] = p

        author = _who(msg)
        add(
            author.get("id"),
            author.get("username"),
            self.is_bot_author(author),
            (msg.get("member") or {}).get("nick") or author.get("global_name"),
        )
        for h in reversed(history):
            add(h.get("author_id"), h.get("author"), h.get("bot"), h.get("display"))
        if reply_ctx:  # only when not already known from history
            add(reply_ctx.get("author_id"), reply_ctx.get("author"), reply_ctx.get("is_bot"))
        add(self.bot_id, self.bot_name, True)
        return list(people.values())

    def link_mentions(self, text, channel_id, state):
        """Safety net for addressing: turn a bare "@Name" for someone in this
        channel's recent history into a real <@id> mention, so the person or
        agent the reply addresses actually gets notified. Matches usernames
        and display names, case-insensitively, longest first."""
        names = {}
        for h in state.get("history", {}).get(channel_id, []):
            pid = h.get("author_id")
            if not pid or pid == self.bot_id:
                continue
            for n in (h.get("author"), h.get("display")):
                if n and n.lower() not in ("everyone", "here"):
                    names[n.lower()] = pid
        if not names or "@" not in text:
            return text
        pattern = re.compile(
            r"(?<![\w<])@("
            + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
            + r")(?![\w])",
            re.IGNORECASE,
        )
        return pattern.sub(lambda m: f"<@{names[m.group(1).lower()]}>", text)


def channel_display(info):
    return "{} #{}".format(info.get("guild_name", "?"), info.get("name", "?"))


def msg_link(gid, cid, mid):
    return f"https://discord.com/channels/{gid or '@me'}/{cid}/{mid}"
