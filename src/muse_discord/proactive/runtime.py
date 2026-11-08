"""Coordinate proactive heartbeats and durable slot publication.

A slot is saved before its private nonce and queue item are published. Failed
publication retries the same item and nonce. Upkeep expires slots, prunes old
records and repairs orphan queue items; delivery and consent own their effects.
"""

import random
import time

from muse_discord.proactive.actions import ConsentActions
from muse_discord.proactive.consent import ConsentRequests
from muse_discord.proactive.delivery import SlotDelivery
from muse_discord.proactive.heartbeat import HeartbeatSchedule
from muse_discord.proactive.model import (
    FLUSH_SECS,
    KIND,
    SEND_RETRY_SECS,
    SENT_KEEP_SECS,
    SPENT_KEEP,
    TICK_SECS,
    ProactiveContext,
)
from muse_discord.proactive.prompts import SlotPrompts
from muse_discord.proactive.store import ProactiveStore
from muse_discord.proactive.targets import TargetPolicy


class Proactive:
    def __init__(self, context: ProactiveContext, clock=time.time, rng=None):
        self.context = context
        self.clock = clock
        self._next_tick = 0.0
        self.store = ProactiveStore(
            context.path, context.nonce_path, context.write_private, lambda: self.clock()
        )
        self.consent = ConsentRequests(
            self.store,
            context.settings,
            context.send_reply,
            context.rest_backing_off,
            lambda: self.clock(),
        )
        self.policy = TargetPolicy(context, self.store, lambda: self.clock())
        self.delivery = SlotDelivery(context, self.store, self.policy, lambda: self.clock())
        self.actions = ConsentActions(
            context, self.store, self.policy, self.consent, lambda: self.clock()
        )
        self.prompts = SlotPrompts(context, self.store, self.policy)
        self.heartbeat = HeartbeatSchedule(self.policy, self.store, rng or random.Random())

    def make_slot(self, state, targets, now):
        d = self.store.data()
        slot, item = self.prompts.build(state, targets, now)
        sid = slot["id"]
        d["slot"] = slot
        d["side"] = "public" if targets[0]["type"] == "channel" else "private"
        self.store.save()  # the slot (and its nonce) first,
        self.store.write_nonces()  # then the hook's nonce file,
        self.publish_slot()  # then the queue (retried by upkeep if busy)
        print(
            f"Heartbeat: {len(targets)} place(s) or people it may reach out to; queued slot {sid}.",
            flush=True,
        )
        return item

    def publish_slot(self):
        """Retry a saved slot item without changing its nonce or heartbeat deadline."""
        slot = self.store.data().get("slot") or {}
        item = slot.get("pending_item")
        if item is None:
            return
        self.context.queue_edit(add=[item])
        slot.pop("pending_item", None)
        self.store.save()

    def end_slot(self, state, why):
        d = self.store.data()
        s = d.get("slot")
        if not s:
            return
        d["spent"][s["id"]] = self.clock()
        for old in sorted(d["spent"], key=d["spent"].get)[:-SPENT_KEEP]:
            del d["spent"][old]
        d["slot"] = None
        sid = s["id"]
        try:
            self.context.queue_edit(drop=lambda q: str(q.get("message_id")) == sid)
        except Exception as e:
            print(f"Proactive: could not drop slot {sid} ({e}).", flush=True)
        self.store.save()
        self.store.write_nonces()
        print(f"Slot {sid} ended ({why}).", flush=True)

    def tick(self, state, now=None):
        """Main loop: slot upkeep, then the heartbeat when it is due."""
        now = self.clock() if now is None else now
        self.consent.send_pending(now)  # each pass: right after the reply it follows
        if now < self._next_tick:
            return
        self._next_tick = now + TICK_SECS
        d = self.store.data()
        self._tick_upkeep(state, now)
        due = self.heartbeat.due_heartbeats(now)
        if due:
            if d.get("slot"):
                print("Heartbeat deferred: the last one is still open.", flush=True)
            elif self.store.blocked():
                print(
                    "Heartbeat skipped: a bad proactive.json is set aside "
                    "(docs/PROTOCOL.md, proactive.json).",
                    flush=True,
                )
            elif self.policy.sent_total(now) < self.policy.total_cap():
                candidates = [
                    t
                    for t in self.policy.eligible(state, now)
                    if self.policy.heartbeat_key(t) in due
                ]
                targets = self.heartbeat.pick_side(candidates)
                available = {self.policy.heartbeat_key(t) for t in candidates}
                selected = {self.policy.heartbeat_key(t) for t in targets}
                self.heartbeat.advance_heartbeats((due - available) | selected, now)
                if targets:
                    self.make_slot(state, targets, now)
                    return
                print("Heartbeat: nobody and nowhere to reach out to now; no wake.", flush=True)
        if self.store.dirty and now - self.store.flushed >= FLUSH_SECS:
            self.store.save()

    def _tick_upkeep(self, state, now):
        d = self.store.data()
        s = d.get("slot")
        if s and not self.store.slot_ok(s):
            print("Proactive: a damaged slot record was dropped.", flush=True)
            d["slot"] = s = None
        if s:
            sending = [
                p
                for p in s["posts"].values()
                if p.get("status") == "sending" and now - p.get("first", now) < SEND_RETRY_SECS + 60
            ]
            if s.get("spent") and not sending:
                self.end_slot(state, "used")
            elif not s.get("spent") and now > s.get("expires", 0):
                self.end_slot(state, "expired unanswered")
        self.store.write_nonces()  # (after a restart, say; only when it changed)
        self.publish_slot()
        self.drop_orphans()
        if self.store.alert and self.context.notify_owners(self.store.alert):
            self.store.alert = None
        cut = now - SENT_KEEP_SECS
        if any(e.get("at", 0) < cut for e in d["sent"]):
            d["sent"] = [e for e in d["sent"] if e.get("at", 0) >= cut]
            self.store.dirty = True
        if not d.get("seeded"):
            # People who DMed the bot before this module kept count.
            for mid, meta in list((state.get("item_meta") or {}).items()):
                self.policy.note_item(mid, meta)
            d["seeded"] = True
            self.store.dirty = True

    def drop_orphans(self):
        """A proactive queue item that is not the live slot (its record
        was lost, or it ended) leaves the queue: it would never get a
        nonce, and the hook would hold it back for good."""
        s = self.store.data().get("slot") or {}
        live = s.get("id") if not s.get("spent") else None
        try:
            if any(
                isinstance(q, dict) and q.get("kind") == KIND and str(q.get("message_id")) != live
                for q in self.context.load_queue()
            ):
                self.context.queue_edit(
                    drop=lambda q: q.get("kind") == KIND and str(q.get("message_id")) != live
                )
                print("Proactive: dropped a slot item with no live slot.", flush=True)
        except (OSError, ValueError) as e:
            print(f"Proactive: queue unreadable ({e}).", flush=True)
