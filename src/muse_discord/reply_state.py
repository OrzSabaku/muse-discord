"""Per-request metadata, timing and settlement of whole conversation bursts.

The caller owns the transaction lock. This service never sends messages or
consumes capabilities; it updates supplied state and the shared outcome ledger.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from muse_discord.conversation import _snowflake_ts
from muse_discord.message_ledger import MessageLedger
from muse_discord.message_models import LedgerContext, Settlement
from muse_discord.message_queue import QueueFiles, keep_last, trigger_of


@dataclass
class ReplyState:
    ledger: MessageLedger
    queue: QueueFiles
    track_item: Callable[[str, dict], Any]
    latency: Callable[[float], Any]
    now: Callable[[], float] = time.time

    def queued_meta(self, state, qid, index):
        """Who and where a still-queued item answers (author_id, author,
        channel_id, kind, dm, bot, guild_id): its item_meta record, else
        (state.json lost) the queue file's own entry, which only the monitor
        writes. None when it is not in the queue (index: queue_index()).
        A schedule action and a "stop messaging me" both bind to it."""
        if qid not in index["channels"]:
            return None
        meta = (state.get("item_meta") or {}).get(qid)
        if isinstance(meta, dict):
            return meta
        q = next(
            (
                q
                for q in self.queue.load()
                if isinstance(q, dict) and str(q.get("message_id")) == qid
            ),
            {},
        )
        return {
            "author_id": q.get("author_id"),
            "author": q.get("author"),
            "channel_id": q.get("channel_id"),
            "kind": q.get("kind"),
            "dm": q.get("kind") == "dm",
            "bot": bool(q.get("from_bot")),
            "guild_id": q.get("guild_id"),
        }

    def note_item(self, state, mid, **meta):
        """Remember who a queued item answers (bounded): the send path opens
        follow-up windows and applies the never_share backstop from it."""
        m = state.setdefault("item_meta", {})
        m[mid] = meta
        self.track_item(mid, meta)
        keep_last(m, 300)

    def note_ingested(self, state, mid):
        """Remember when the monitor first processed trigger mid (staged
        it), bounded like queued_at, for the latency log's ingest/hold
        split."""
        seen = state.setdefault("ingested_at", {})
        seen.setdefault(mid, self.now())
        keep_last(seen, 200)

    def note_queued(self, state, mid):
        """Remember when mid was queued (bounded), for the latency log even
        when a worker removes the queue item itself. Returns the time."""
        now = self.now()
        queued = state.setdefault("queued_at", {})
        queued[mid] = now
        keep_last(queued, 200)
        return now

    def settle(self, state, item, outcome: Settlement):
        """Record an outbox item's outcome under its reply_to AND every other
        message of the burst it belongs to (reply_aliases): the worker
        replies under the last fragment, while the ledger and the sweep track
        the trigger, and a fragment left unsettled would be requeued alone.
        replied=True also adds all those ids to replied_to (send dedup)."""
        status = outcome.status
        reason = outcome.reason
        replied = outcome.replied
        detail = outcome.detail
        reply_to = item.get("reply_to")
        aliases = state.get("reply_aliases", {})
        # The queue item it answers (its "item", else reply_to's trigger),
        # and that item's own messages (its burst fragments) only: a
        # reply_to under some other message is not this item's to settle
        # (it may be another item's, still open).
        trigger = item.get("item") or (aliases.get(reply_to, reply_to) if reply_to else None)
        if not trigger:
            return
        trigger = trigger_of(state, trigger)  # an "item" set to a fragment
        ids = [trigger] + [f for f, t in aliases.items() if t == trigger]
        if reply_to and reply_to not in ids and trigger_of(state, reply_to) == trigger:
            ids.append(reply_to)
        for mid in ids:
            if replied and mid not in state["replied_to"]:
                state["replied_to"].append(mid)
        self.ledger.record_many(
            ids,
            status,
            reason,
            context=LedgerContext(
                channel_id=item.get("channel_id"),
                author=item.get("author"),
                author_id=item.get("author_id"),
                detail=detail,
            ),
        )

    def log_latency(self, state, qid, picked, sent):
        """One line per answered item: where the time went, from the trigger
        message (its snowflake time) to the monitor first processing it
        (ingest: the gateway or the poll delivering it, and the main loop
        getting to it), to queued (hold: the burst coalescing), to the
        outbox pickup (worker), to sent. An item that was never staged (a
        sweep recovery) has no hold. Our clock can trail Discord's
        snowflake time by a fraction of a second, so t0 is clamped to the
        first local timestamp: ingest shows 0, never negative, and total
        stays the sum of the parts."""
        t0 = _snowflake_ts(qid)
        queued = state.get("queued_at", {}).pop(qid, None)
        ingested = state.get("ingested_at", {}).pop(qid, None)
        if not t0:
            return
        if queued:
            ingested = min(ingested or queued, queued)
            t0 = min(t0, ingested)
            print(
                f"Latency {qid}: ingest {ingested - t0:.1f}s, hold "
                f"{queued - ingested:.1f}s, worker {picked - queued:.1f}s, "
                f"send {sent - picked:.1f}s, total {sent - t0:.1f}s",
                flush=True,
            )
        else:
            t0 = min(t0, picked)
            print(f"Latency {qid}: send {sent - picked:.1f}s, total {sent - t0:.1f}s", flush=True)
        self.latency(sent - t0)
