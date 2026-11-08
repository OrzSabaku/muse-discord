"""Authorize, act on and deliver one worker reply in a fixed stage order.

The component depends on reply policy, effects and a batch transaction, never on
monitor globals. Rejection must precede deduplication and every side effect.
"""

import time
from http import HTTPStatus
from typing import Any, Protocol

from muse_discord.message_models import (
    DISCORD_MAX_CHARS,
    AuthorizedReply,
    DeliveryOptions,
    PreparedReply,
    ReplyActions,
    SecretBlocked,
    Settlement,
)
from muse_discord.reply_protocol import is_snowflake as _is_snowflake
from muse_discord.reply_protocol import item_key as _outbox_key

SEND_RETRY_SECS = 90


class DeliveryBatch(Protocol):
    state: dict
    picked: float
    index: dict
    remaining: list
    rejects: list
    settled: list


class ReplyPolicy(Protocol):
    def _outbox_problem(self, item) -> Any: ...

    def _refuse(self, rejects, item, why, note="") -> Any: ...

    def bind_started(self, state, item, settled, remaining, rejects) -> Any: ...

    def owner_dm_gate(self, state, item, index=None) -> Any: ...

    def own_channel_problem(self, state, item, index) -> Any: ...

    def bound_elsewhere(self, state, item) -> Any: ...

    def note_reply_nonce(self, state, refusal) -> Any: ...

    def _answered_id(self, state, item) -> Any: ...

    def never_share_hit(self, state, item, qid, parts) -> Any: ...

    def hold_reply(self, state, item, qid, parts) -> Any: ...

    def _reply_attachments(self, state, item, retry) -> Any: ...


class ReplyEffects(Protocol):
    def _settle(self, state, item, outcome) -> Any: ...

    def remember_check(self, state, item, qid, index) -> Any: ...

    def watch_schedule(self, state, item, index) -> Any: ...

    def link_mentions(self, text, channel_id, state) -> Any: ...

    def normalize_parts(self, item) -> Any: ...

    def typing_clear(self, keys, sent_in=None) -> Any: ...

    def watch_fire_send(self, state, fire, parts) -> Any: ...

    def send_fragmented(
        self, channel_id, parts, reply_to=None, attachments=(), delivery=None
    ) -> Any: ...

    def add_reaction(self, channel_id, message_id, emoji) -> Any: ...

    def note_media_sent(self, state, scope) -> Any: ...

    def scope_of_channel(self, channel_id) -> Any: ...

    def save_facts(self, state, item, t) -> Any: ...

    def open_follow_up(self, state, channel_id, user_id) -> Any: ...

    def _log_latency(self, state, qid, picked, sent) -> Any: ...

    def opt_action(self, state, item, index) -> Any: ...


def _retry_later(item, retry, exc, **keep):
    """After a failed send: schedule the next try in item["_retry"]
    (growing gaps; keep: more fields to carry) and say True, or False
    once SEND_RETRY_SECS have passed since the first failure. The send
    may well have landed (a timeout after Discord created the message
    looks exactly like a failure), so it stops before the nonce window
    closes rather than risk a duplicate."""
    now = time.time()
    first = retry.get("first", now)
    if now - first >= SEND_RETRY_SECS:
        return False
    n = retry.get("n", 0) + 1
    item["_retry"] = dict(keep, first=first, n=n, next=now + min(30, 2**n))
    print(f"Send failed (attempt {n}; retrying, same nonce): {exc}", flush=True)
    return True


class OutboxDelivery:
    """Keep one reply's authorization, actions and delivery metadata together.

    The phase order is security-sensitive: bind the requester before dedup,
    memory, schedule actions, holds or sends can settle the queue item.
    """

    def __init__(
        self, batch: DeliveryBatch, item, policy: ReplyPolicy, effects: ReplyEffects, progress: dict
    ):
        self.batch = batch
        self.item = item
        self.policy = policy
        self.effects = effects
        self.progress = progress

    def process(self):
        auth = self.authorize()
        if auth is None:
            return
        actions = self.actions(auth)
        if actions is None:
            return
        payload = self.prepare(auth, actions)
        if payload is None:
            return
        try:
            self.send_payload(auth, payload)
            self.record_success(auth, actions, payload)
        except Exception as exc:
            self.record_failure(payload, exc)

    def authorize(self) -> AuthorizedReply | None:
        self.progress["key"] = _outbox_key(self.item)  # see outbox_pass
        problem = self.policy._outbox_problem(self.item)
        if problem:
            self.policy._refuse(self.batch.rejects, self.item, problem)
            return None
        # Items the monitor started (reminder and watch fires, heartbeat
        # slots): bound to their own record before anything else looks
        # at them, whatever the item says (bind_started).
        fire, handled = self.policy.bind_started(
            self.batch.state,
            self.item,
            self.batch.settled,
            self.batch.remaining,
            self.batch.rejects,
        )
        if handled:
            return None
        # Owner DMs first, before dedup, holds or anything that settles:
        # only the owner-DM-only wake holding the DM's nonce may touch it.
        refused = self.policy.owner_dm_gate(self.batch.state, self.item, self.batch.index)
        if refused:
            self.policy._refuse(self.batch.rejects, self.item, refused, "; the owner DM stays open")
            return None
        # Then: it answers an open queue item it was handed (its
        # reply_nonce), in that item's channel; a fire's answer too.
        stray = self.policy.own_channel_problem(self.batch.state, self.item, self.batch.index)
        if not self.policy.bound_elsewhere(self.batch.state, self.item):
            self.policy.note_reply_nonce(self.batch.state, stray)
        if stray:
            self.policy._refuse(self.batch.rejects, self.item, stray)
            return None
        reply_to = self.item.get("reply_to")
        qid = self.policy._answered_id(self.batch.state, self.item)
        typing_keys = ["item:" + qid] if qid else []
        # Dedup: never touch the same message twice (overlapping workers)
        if (reply_to and reply_to in self.batch.state["replied_to"]) or (
            self.item.get("item") and self.item["item"] in self.batch.state["replied_to"]
        ):
            print(
                f"Skipping duplicate reply to message {reply_to or self.item.get('item')}",
                flush=True,
            )
            self.effects._settle(
                self.batch.state,
                self.item,
                outcome=Settlement(status="replied", reason="duplicate suppressed"),
            )
            self.batch.settled.append(self.item)
            return None
        return AuthorizedReply(fire, reply_to, qid, typing_keys)

    def actions(self, auth: AuthorizedReply) -> ReplyActions | None:
        # "Stop messaging me": recorded for the author of the message
        # this item answers (proactive.py); the reply goes out as usual.
        try:
            bad = self.effects.opt_action(self.batch.state, self.item, self.batch.index)
        except Exception as e:  # not recorded: the item stays open
            bad = f"proactive: could not record it ({e})"
        if bad:
            self.policy._refuse(self.batch.rejects, self.item, bad)
            return None
        bad, memory_of = self.effects.remember_check(
            self.batch.state, self.item, auth.qid, self.batch.index
        )
        if bad:
            self.policy._refuse(self.batch.rejects, self.item, bad)
            return None
        sched = None  # a refused schedule action: (line, reason, detail)
        if "schedule" in self.item:
            got = self.effects.watch_schedule(self.batch.state, self.item, self.batch.index)
            if got and got[0] == "reject":
                self.policy._refuse(self.batch.rejects, self.item, got[1])
                return None
            sched = got[1:] if got else None
        return ReplyActions(memory_of, sched)

    def prepare(self, auth: AuthorizedReply, actions: ReplyActions) -> PreparedReply | None:
        # Clamped to Discord's per-message limit AFTER every rewrite: the
        # @everyone neutralization and link_mentions both lengthen text
        # normalize_parts already trimmed, and a part over the limit
        # would fail (400) on every retry.
        parts = [
            self.effects.link_mentions(p, self.item.get("channel_id"), self.batch.state)[
                :DISCORD_MAX_CHARS
            ]
            for p in self.effects.normalize_parts(self.item)
        ]
        # Worker-drafted emoji reactions: applied to the reply_to
        # message. A react-only item may carry no text.
        reactions = [
            e for e in (self.item.get("reactions") or []) if isinstance(e, str) and e.strip()
        ][:5]
        if actions.sched:
            # Nothing was scheduled: the monitor says so itself, in place
            # of the worker's confirmation.
            parts, reactions = [actions.sched[0]], []
        if self.item.get("silent"):
            # The worker decided no reply is warranted (a conversation
            # that has run its course). Recording it keeps the sweep
            # from waking the worker again for the same message.
            self.effects._settle(
                self.batch.state,
                self.item,
                outcome=Settlement(
                    status="skipped", reason="no reply (worker's call)", replied=True
                ),
            )
            if not actions.sched:
                self.effects.save_facts(self.batch.state, self.item, actions.memory_of)
            self.effects.typing_clear(auth.typing_keys)
            self.batch.settled.append(self.item)
            return None
        if not _is_snowflake(self.item.get("channel_id")) or (
            auth.reply_to and not _is_snowflake(auth.reply_to)
        ):
            print(
                f"Dropping outbox item with a malformed channel_id or "
                f"reply_to: {self.item.get('channel_id')!r} "
                f"{auth.reply_to!r}",
                flush=True,
            )
            return None
        retry = self.item.get("_retry") or {}
        if not self.retry_ready(retry):
            return None
        if self.policy.never_share_hit(self.batch.state, self.item, auth.qid, parts):
            self.policy.hold_reply(self.batch.state, self.item, auth.qid, parts)
            self.effects.typing_clear(auth.typing_keys)
            self.batch.settled.append(self.item)
            return None
        gen_id, approval, attachments, gen_files = self.policy._reply_attachments(
            self.batch.state, self.item, retry
        )
        # The reply is about to land, which ends the indicator: stop
        # typing for this item now, or a ping right after the send would
        # show typing again for ~10s.
        self.effects.typing_clear(auth.typing_keys)
        return PreparedReply(
            auth.reply_to, parts, reactions, retry, gen_id, approval, attachments, gen_files
        )

    def retry_ready(self, retry):
        now = time.time()
        first = retry.get("first")
        if first is not None and now - first >= SEND_RETRY_SECS:
            self.give_up(TimeoutError("send retry window expired"))
            return False
        if retry.get("next", 0) > now:
            self.batch.remaining.append(self.item)
            return False
        return True

    def send_payload(self, auth: AuthorizedReply, payload: PreparedReply):
        if auth.fire is not None:
            # Under the requester's message (or @mentioning them),
            # pinging them and nobody else.
            payload.reply_to, payload.parts, payload.mentions = self.effects.watch_fire_send(
                self.batch.state, auth.fire, payload.parts
            )
        if payload.parts:
            # standalone: post as a plain message, not a Discord
            # reply, so it notifies nobody and does not count as
            # addressing the other side (a closing remark that
            # doesn't ask for an answer).
            # A fire's nonce is keyed on the fire: two fires answered
            # alike under one request are two messages, not a retry.
            self.effects.send_fragmented(
                self.item["channel_id"],
                payload.parts,
                None if self.item.get("standalone") else payload.reply_to,
                payload.attachments,
                delivery=DeliveryOptions(
                    mentions=payload.mentions,
                    nonce=f"fire:{auth.fire['id']}" if auth.fire else None,
                ),
            )
            print(
                f"Sent reply to {self.item.get('author')} in {self.item.get('channel_name')} "
                f"({len(payload.parts)} part(s)): {payload.parts[0][:60]}",
                flush=True,
            )
        for emoji in payload.reactions:
            if payload.reply_to and self.effects.add_reaction(
                self.item["channel_id"], payload.reply_to, emoji
            ):
                print(
                    f"Added reaction {emoji} for {self.item.get('author')} "
                    f"in #{self.item.get('channel_name')}",
                    flush=True,
                )

    def record_success(self, auth: AuthorizedReply, actions: ReplyActions, payload: PreparedReply):
        if payload.approval:
            self.batch.state.get("gen_approved", {}).pop(payload.gen_id, None)
        if payload.parts and payload.gen_files:
            # A generation really ran and its file went out: count
            # it for media.per_day here, at send time, never at
            # queue time.
            self.batch.state.setdefault("gen_sent", []).extend(rp for _, rp in payload.gen_files)
            sent_scope = self.effects.scope_of_channel(self.item["channel_id"])
            self.effects.note_media_sent(self.batch.state, sent_scope)
        if actions.sched and actions.sched[1]:
            # A reminder or watch refused for a cap or a setting:
            # ledgered like other refusals (Why no reply?).
            self.effects._settle(
                self.batch.state,
                self.item,
                outcome=Settlement(
                    status="skipped",
                    reason=actions.sched[1] or "",
                    replied=True,
                    detail=actions.sched[2],
                ),
            )
        else:
            self.effects._settle(
                self.batch.state, self.item, outcome=Settlement(status="replied", replied=True)
            )
            if not actions.sched:
                self.effects.save_facts(self.batch.state, self.item, actions.memory_of)
        meta = self.batch.state.get("item_meta", {}).get(auth.qid) if auth.qid else None
        if (
            payload.parts
            and meta
            and not meta.get("bot")
            and not meta.get("dm")
            and meta.get("channel_id") == self.item["channel_id"]
        ):
            # A reply to a human actually went out: (re)open their
            # conversational follow-up window here.
            self.effects.open_follow_up(
                self.batch.state, self.item["channel_id"], meta.get("author_id")
            )
        self.batch.settled.append(self.item)
        self.effects.typing_clear([], sent_in=self.item["channel_id"] if payload.parts else None)
        if auth.qid:
            self.effects._log_latency(
                self.batch.state,
                auth.qid,
                payload.retry.get("picked", self.batch.picked),
                time.time(),
            )

    def record_failure(self, payload: PreparedReply, e):
        code = getattr(e, "code", None)
        if isinstance(e, SecretBlocked):
            # It carried a secret (guard_outgoing logged it and told
            # the owners): never sent, never retried.
            self.effects._settle(
                self.batch.state, self.item, outcome=Settlement(status="skipped", reason=str(e))
            )
            self.batch.settled.append(self.item)
            return
        if code == HTTPStatus.FORBIDDEN:
            # Permanent permission failure (the bot cannot post in
            # that channel): drop it, don't retry forever.
            print(
                f"Dropping outbox item for #{self.item.get('channel_name')}: "
                f"403 Missing Access ({e})",
                flush=True,
            )
            self.effects._settle(
                self.batch.state,
                self.item,
                outcome=Settlement(status="skipped", reason="403 missing access"),
            )
            self.batch.settled.append(self.item)
            return
        if _retry_later(
            self.item,
            payload.retry,
            e,
            attachments=payload.gen_files,
            picked=payload.retry.get("picked", self.batch.picked),
        ):
            self.batch.remaining.append(self.item)
            return
        self.give_up(e)

    def give_up(self, e):
        print(
            f"Giving up on reply to {self.item.get('author')} in "
            f"{self.item.get('channel_name')} after "
            f"{SEND_RETRY_SECS}s of send errors: {e}",
            flush=True,
        )
        self.effects._settle(
            self.batch.state,
            self.item,
            outcome=Settlement(status="skipped", reason=f"send failed: {e}"[:200]),
        )
        self.batch.settled.append(self.item)
