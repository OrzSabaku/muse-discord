"""Turn one received conversation burst into an authorized worker request.

The pipeline owns its stage order and queue-item construction. Settings/people
policy, conversation inputs and capability recording are explicit collaborators;
there is no monitor import, global configuration lookup or namespace injection.
"""

import time
from dataclasses import dataclass

from muse_discord.bursts import alias_fragments
from muse_discord.conversation import HISTORY_ATTACHED, is_forwarded
from muse_discord.message_ledger import MessageLedger
from muse_discord.message_models import (
    LedgerContext,
    MessageMedia,
    MessageTurn,
    OwnerCommand,
    QueueContext,
    Speaker,
)
from muse_discord.owner.capabilities import CapabilityIssuer
from muse_discord.receiving.quarantine import MessageQuarantine, is_suspicious
from muse_discord.reply_state import ReplyState
from muse_discord.response_policy import ResponsePolicy
from muse_discord.worker_context import WorkerContext


@dataclass
class IngestJournal:
    ledger: MessageLedger
    quarantine: MessageQuarantine
    replies: ReplyState
    capabilities: CapabilityIssuer


@dataclass
class MessageEnqueuer:
    msg: dict
    cname: str
    state: dict
    queue: list
    context: QueueContext
    policy: ResponsePolicy
    worker: WorkerContext
    journal: IngestJournal

    @property
    def author_id(self):
        return (self.msg.get("author") or {}).get("id")

    @property
    def author_name(self):
        return (self.msg.get("author") or {}).get("username")

    def enqueue(self):
        turn = self.prepare()
        if turn is None:
            return False
        media = self.load_context(turn)
        if media is None:
            return False
        item = self.build_item(turn, media)
        return self.record_item(turn, item)

    def prepare(self) -> MessageTurn | None:
        fragments = [f for f in (self.context.fragments or []) if f["id"] != self.msg["id"]]
        if any(q["message_id"] == self.msg["id"] for q in self.queue):
            return None
        author_id = self.msg.get("author", {}).get("id")
        author_name = self.msg.get("author", {}).get("username")
        # Every outcome below is recorded for the trigger AND each fragment:
        # a fragment can be a mention too, and one with no ledger entry would
        # be requeued alone by the sweep.
        burst_ids = [self.msg["id"]] + [f["id"] for f in fragments]

        # 0. Hard gates, re-checked here because every path (poll, gateway,
        #    restart flush, sweep) funnels through this function.
        if self.policy.predates_join(self.state, self.msg, self.context.guild_id):
            self.journal.ledger.record_many(
                burst_ids,
                "skipped",
                "predates bot join",
                context=LedgerContext(
                    channel_id=self.msg.get("channel_id"),
                    author=author_name,
                    author_id=author_id,
                ),
            )
            return None
        chan_hist = self.state.get("history", {}).get(self.msg["channel_id"], [])
        from_bot = self.policy.is_bot_author(self.msg.get("author"))
        bot_turns = self.worker.conversation.bot_run_length(chan_hist, self.msg)
        scope = self.policy.scope_for(
            self.context.guild_id, self.msg["channel_id"], self.context.dm
        )
        here = self.policy.settings().resolve(scope)
        # who.bot_exchange_limit: the bot's consecutive replies to bots in
        # this channel since a human spoke.
        exchange = self.policy.own_replies_in_run(chan_hist, self.msg) if from_bot else 0
        limit = here["who.bot_exchange_limit"]
        if from_bot and limit is not None and exchange >= limit:
            self.journal.ledger.record_many(
                burst_ids,
                "skipped",
                f"bot exchange limit ({limit}) reached",
                context=LedgerContext(
                    channel_id=self.msg.get("channel_id"),
                    author=author_name,
                    author_id=author_id,
                    detail={"limit": limit},
                ),
            )
            print(
                f"Not answering bot {author_name} in {self.cname}: the bot "
                f"exchange limit ({limit}) is reached.",
                flush=True,
            )
            return None
        suspicious = here["privacy.suspicious"]

        # The worker sees the whole burst as one message: fragments joined in
        # order under the trigger's text.
        parts = [self.msg.get("content", "")]
        parts += [f.get("content", "") for f in fragments]
        content = "\n".join(p for p in parts if p)

        # 1. Injection pre-filter -> quarantine (privacy.suspicious:
        #    hold-for-review) or drop (ignore), no auto-reply (combined text).
        if suspicious != "answer":
            matched = is_suspicious(content)
            if matched:
                self.hold_suspicious(suspicious, fragments, matched)
                return None

        # 2. Rate limits (also bound token spend), one charge per burst. A
        #    capped burst is ledgered as skipped (terminal), so flush_pending
        #    drops it and the author's later messages are judged on their own.
        capped = self.policy.rate_block(self.state, author_id, self.msg["channel_id"], scope)
        if capped:
            self.journal.ledger.record_many(
                burst_ids,
                "skipped",
                "daily cap reached",
                context=LedgerContext(
                    channel_id=self.msg.get("channel_id"),
                    author=author_name,
                    author_id=author_id,
                    detail=capped,
                ),
            )
            return None
        self.policy.rate_record(self.state, author_id, scope)

        return MessageTurn(
            fragments=fragments,
            burst_ids=burst_ids,
            chan_hist=chan_hist,
            from_bot=from_bot,
            bot_turns=bot_turns,
            scope=scope,
            here=here,
            exchange=exchange,
            content=content,
        )

    def load_context(self, turn: MessageTurn) -> MessageMedia | None:
        # 3. Media and document text, for the worker to see and read.
        media, documents, unviewable = [], [], []
        for m in [self.msg, *turn.fragments]:
            md, dc, uv = self.worker.media(m)
            media += md
            documents += dc
            unviewable += uv

        # 3b. Whose message the trigger replies to, if anyone's.
        reply_ctx = self.worker.reply_context(self.msg)

        # 3c. The worker reads extracted document text and the replied-to
        #     message as well as the message itself, so they get the same
        #     injection pre-filter. The bot's own replied-to message is
        #     skipped: everyday phrasing in it ("you are now set up") would
        #     quarantine every reply to it.
        if turn.here["privacy.suspicious"] != "answer":
            extra_texts = [(f"document {d.get('name')}", d.get("text") or "") for d in documents]
            if reply_ctx and not reply_ctx.get("is_bot"):
                extra_texts.append(("replied-to message", reply_ctx.get("content") or ""))
            for where, text in extra_texts:
                matched = is_suspicious(text)
                if matched:
                    self.hold_suspicious(
                        turn.here["privacy.suspicious"], turn.fragments, f"{matched} (in {where})"
                    )
                    return None

        return MessageMedia(media, documents, unviewable, reply_ctx)

    def build_item(self, turn: MessageTurn, media: MessageMedia):
        # 4. Recent history, without the burst itself.
        history = [h for h in turn.chan_hist if h["id"] not in turn.burst_ids][-HISTORY_ATTACHED:]

        # 5. Generation permission (media.*). may_generate
        #    is permission, not a decision: the worker judges whether
        #    generation is wanted. Queue time records it, plus the single-use
        #    approval below (keyed by message id: the worker's outbox item
        #    does not carry the flag).
        may_generate = self.policy.media_permission(
            self.state, turn.scope, turn.here, self.author_matches
        )
        if may_generate:
            # Bound to the channel too: the file may only go back where the
            # request came from (an image made in a DM never posts publicly).
            self.state.setdefault("gen_approved", {})[self.msg["id"]] = {
                "ts": time.time(),
                "channel_id": self.msg["channel_id"],
            }

        # 6. Owner commands: stamped from the verified author id only; the
        #    worker decides whether the message asks the bot to do something.
        owner_command = self.author_id in self.policy.owners and bool(
            turn.here["abilities.owner_commands"]
        )
        # The worker replies under the LAST fragment, but the ledger and the
        # sweep track the trigger: remember which trigger each fragment
        # belongs to so a reply (or a silent pass) settles the whole burst.
        alias_fragments(self.state, self.msg["id"], turn.fragments)

        item = {
            "message_id": self.msg["id"],
            "channel_id": self.msg["channel_id"],
            "channel_name": self.cname,
            "guild_id": self.context.guild_id,
            "kind": "dm" if self.context.dm else "mention",
            "author": self.msg["author"].get("username"),
            "author_id": self.author_id,
            "content": turn.content,
            "timestamp": self.msg.get("timestamp"),
            "media": media.media,
            "documents": media.documents,
            "unviewable": media.unviewable,
            "history": history,
            "may_generate": may_generate,
            "owner_command": owner_command,
            "burst": len(turn.fragments) > 0,
            "burst_fragments": [
                {
                    "message_id": f["id"],
                    "content": f.get("content", ""),
                    "timestamp": f.get("timestamp"),
                }
                for f in turn.fragments
            ],
            "reply_to": media.reply_ctx,
            # Another agent addressed the bot. bot_turns counts consecutive
            # bot messages (this one included) since a human last spoke.
            "from_bot": turn.from_bot,
            "bot_turns": turn.bot_turns,
            "participants": self.worker.conversation.conversation_participants(
                turn.chan_hist, self.msg, media.reply_ctx
            ),
            # When the monitor queued it: the typing cap and the latency log
            # measure from here.
            "queued_at": self.journal.replies.note_queued(self.state, self.msg["id"]),
            # Triggered only by the conversational follow-up window.
            "follow_up": self.context.follow_up
            and not any(
                self.worker.conversation.is_mention(m)
                or self.worker.conversation.is_reply_to_bot(m)
                for m in [self.msg, *turn.fragments]
            ),
            "settings": self.worker.settings_card(
                turn.scope,
                turn.here,
                self.msg["channel_id"],
                speaker=Speaker(
                    author_id=self.author_id, from_bot=turn.from_bot, exchange=turn.exchange
                ),
            ),
        }
        # External-app capabilities are stamped from verified
        # original owner DMs only. A reply/forward/attachment cannot grant them.
        item["external_apps"] = {"eligible": False, "level": "off"}
        if turn.here["abilities.external_apps"] != "off":
            from muse_discord.external_apps.binding import DMOrigin
            from muse_discord.external_apps.binding import card as external_app_card

            item["external_apps"] = external_app_card(
                self.msg,
                turn.fragments,
                origin=DMOrigin(
                    self.context.dm, str(self.author_id) in self.policy.owners, turn.from_bot
                ),
                level=turn.here["abilities.external_apps"],
            )
        # Reminders and watches: the author's own list, and whether they may
        # set one here (abilities.reminders).
        item["watches"] = self.worker.watches.card(
            self.context.guild_id,
            self.context.dm,
            turn.here,
            self.author_matches,
            speaker=Speaker(author_id=self.author_id, from_bot=turn.from_bot),
        )
        return item

    def record_item(self, turn: MessageTurn, item):
        self.queue.append(item)
        burst_note = f" (+{len(turn.fragments)} fragment(s))" if turn.fragments else ""
        # An owner's own DM: every outbox item that answers it, settles it
        # or posts in its channel must carry its nonce (owner_dm_gate).
        # (The wake hook's test too: settings.speaker "owner" in a DM.)
        owner_dm = bool(self.context.dm and str(self.author_id) in self.policy.owners)
        self.journal.replies.note_item(
            self.state,
            self.msg["id"],
            author_id=self.author_id,
            channel_id=self.msg["channel_id"],
            bot=turn.from_bot,
            dm=self.context.dm,
            kind=item["kind"],
            owner_dm=owner_dm,
            owner_command=item["owner_command"],
            guild_id=self.context.guild_id,
            author=self.author_name,
            # Settings requests: never from a forwarded message;
            # attachments or a reply to someone else's message
            # always need the owner's Apply.
            forwarded=any(is_forwarded(m) for m in [self.msg, *turn.fragments]),
            has_inputs=bool(item["media"] or item["documents"])
            or bool(item["reply_to"] and not item["reply_to"].get("is_bot")),
        )
        if owner_dm:
            # The capability for this owner DM (written for the wake hook
            # before the item reaches the queue file).
            self.journal.capabilities.mint_owner_dm(
                self.state,
                self.msg["id"],
                self.msg["channel_id"],
                [f["id"] for f in turn.fragments],
            )
        if item["owner_command"]:
            # bin/discord-api.py's capability for this owner's command.
            self.journal.capabilities.note_command(
                self.state,
                self.msg["id"],
                dm=owner_dm,
                command=OwnerCommand(
                    channel_id=self.msg["channel_id"],
                    content=turn.content,
                    guild_id=self.context.guild_id,
                ),
            )
        # The capability to answer it at all, for the wake that holds it.
        self.journal.capabilities.mint_reply(self.state, self.msg["id"])
        print(
            f"Queued {item['kind']}{' (follow-up)' if item['follow_up'] else ''}"
            f" from {self.msg['author'].get('username')} in "
            f"{self.cname}{burst_note}: {turn.content[:80]}",
            flush=True,
        )
        # (Typing comes from the typing thread, via typing_refresh.)
        self.journal.ledger.record_many(
            turn.burst_ids,
            "queued",
            context=LedgerContext(
                channel_id=self.msg.get("channel_id"),
                author=self.msg.get("author", {}).get("username"),
                author_id=self.author_id,
            ),
        )
        return True

    def hold_suspicious(self, policy, fragments, matched):
        if policy == "ignore":
            self.journal.ledger.record_many(
                [self.msg["id"], *[f["id"] for f in fragments]],
                "skipped",
                f"suspicious, ignored: {matched}",
                context=LedgerContext(
                    channel_id=self.msg.get("channel_id"),
                    author=self.author_name,
                    author_id=self.author_id,
                ),
            )
            print(
                f"Ignored a suspicious message from {self.author_name} in "
                f"{self.cname} (privacy.suspicious: ignore).",
                flush=True,
            )
        else:
            self.journal.quarantine.hold(self.msg, self.cname, matched, fragments)

    def author_matches(self, who):
        return self.policy.people_match(
            who,
            self.author_id,
            lambda: (
                set((self.msg.get("member") or {}).get("roles") or [])
                if "roles" in (self.msg.get("member") or {})
                else self.policy.member_roles(self.context.guild_id, self.author_id)
            ),
        )
