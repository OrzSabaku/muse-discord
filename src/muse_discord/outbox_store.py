"""Outbox pickup, crash recovery and settlement transactions.

No network calls run under the queue lock. Capability publication is a separate
callback after settlement; an unavailable lock retains retry progress in the
caller's durable state. The sender and the state checkpoint are injected.
"""

import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from muse_discord.message_queue import QueueFiles, answered_id
from muse_discord.reply_protocol import item_key as _outbox_key
from muse_discord.storage import SharedLockTimeout, atomic_write
from muse_discord.storage import read_json as _read_json

OUTBOX_SETTLE_SECS = 10
OUTBOX_POISON_TRIES = 3
_NONCE_LIKE = re.compile(r"[0-9a-f]{32,64}")


def redact_nonces(item):
    """A copy of an outbox item for the rejected file and the log, its
    nonces blanked (a refused item may still carry a live one)."""
    if not isinstance(item, dict):
        return item
    out = dict(item)
    if "owner_dm_nonce" in out:
        out["owner_dm_nonce"] = "(redacted)"
    if "reply_nonce" in out:
        out["reply_nonce"] = "(redacted)"
    if isinstance(out.get("settings"), dict) and "nonce" in out["settings"]:
        out["settings"] = dict(out["settings"], nonce="(redacted)")
    return out


def _scrub_nonces(value):
    """value (any JSON, or raw text) with every nonce-like string (32 to
    64 hex digits) blanked: what goes to the rejected file never holds a
    live owner-DM nonce, however the outbox was mangled."""
    if isinstance(value, str):
        return _NONCE_LIKE.sub("(redacted)", value)
    if isinstance(value, list):
        return [_scrub_nonces(v) for v in value]
    if isinstance(value, dict):
        return {k: _scrub_nonces(v) for k, v in value.items()}
    return value


@dataclass(frozen=True)
class OutboxFiles:
    pending: str
    processing: str
    rejected: str
    queue: QueueFiles

    def load(self, path):
        """The items in an outbox file, or None while it is mid-write (see
        OUTBOX_SETTLE_SECS). A file that is not a JSON array (a bad write) is
        set aside in the rejected file instead of blocking every later
        reply."""
        if not os.path.exists(path):
            return []
        with open(path, errors="replace") as f:
            raw = f.read()
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return data
            reason = "outbox file is not a JSON array"
        except ValueError as e:
            try:
                if time.time() - os.path.getmtime(path) < OUTBOX_SETTLE_SECS:
                    return None
            except OSError:
                return None
            data, reason = raw[:20000], f"outbox file is not valid JSON ({e})"
        print(f"Rejected {os.path.basename(path)}: {reason}", flush=True)
        self.reject([(data, reason)])
        return []

    def reject(self, rejects):
        """Set malformed outbox items aside for the owner's review."""
        kept = _read_json(self.rejected, list) or []
        now = datetime.now(UTC).isoformat()
        kept += [
            {"rejected_at": now, "reason": reason, "item": _scrub_nonces(item)}
            for item, reason in rejects
        ]
        atomic_write(self.rejected, kept[-100:])


@dataclass
class OutboxBatch:
    """One locked take/send/settle pass; worker appends remain independent."""

    state: dict
    picked: float
    index: dict
    files: OutboxFiles
    checkpoint: Callable[[dict], None]
    publish: Callable[[dict], None]
    current: dict
    remaining: list = field(default_factory=list)
    rejects: list = field(default_factory=list)
    settled: list = field(default_factory=list)

    def defer_retries(self, originals):
        """Keep retry progress in monitor-owned state while shared files are locked."""
        remaining = {id(item) for item in self.remaining}
        self.state["outbox_retries"] = {
            key: item["_retry"]
            for item, key in originals
            if id(item) in remaining and key is not None and item.get("_retry")
        }
        self.checkpoint(self.state)

    def settled_ids(self):
        frags = self.index["frags"]
        # The queue items the settled replies answer. Matched on the outbox
        # item's "item" first; an older worker's item without it matches on
        # reply_to, its burst trigger, or a burst fragment it replied under.
        done_ids, done_frags = set(), set()
        for item in self.settled:
            qid = answered_id(self.state, item)
            if qid:
                done_ids.add(frags.get(qid, qid))
            if not item.get("item") and item.get("reply_to"):
                done_ids.add(item["reply_to"])
                done_frags.add(item["reply_to"])

        return done_ids, done_frags

    def finish(self):
        with self.files.queue.lock():
            self.current["key"] = None
            # A released hold is used up however its item settled (sent, 403,
            # a blocked secret, given up).
            for item in self.settled:
                if isinstance(item.get("_hold"), str):
                    (self.state.get("hold_released") or {}).pop(item["_hold"], None)

            done_ids, done_frags = self.settled_ids()

            # Answered: every capability those items held is spent.
            for key in ("nl_nonces", "owner_items", "reply_nonces"):
                held = self.state.get(key) or {}
                for qid in done_ids & set(held):
                    del held[qid]

            def answered(q):
                return q.get("message_id") in done_ids or any(
                    isinstance(f, dict) and f.get("message_id") in done_frags
                    for f in q.get("burst_fragments") or []
                )

            if self.rejects:
                self.files.reject(self.rejects)
            # Queue first, outbox second: the wake hook reads the outbox
            # before the queue, so it never sees an answered item in the
            # queue without its outbox entry.
            if done_ids:
                self.files.queue.apply_locked(drop=answered)
            fresh = self.files.load(self.files.pending)
            if fresh is not None:
                atomic_write(self.files.pending, self.remaining + fresh)
                os.remove(self.files.processing)
            elif self.remaining:
                # Mid-write: the retries wait in the processing file, which
                # the next pass merges back in first.
                atomic_write(self.files.processing, self.remaining)
            else:
                os.remove(self.files.processing)

        # Publishing can notify owners on an I/O failure; do not hold the
        # queue lock across that network call. The capabilities are already spent.
        self.publish(self.state)


def _restore_outbox_retries(state, items):
    retries = state.get("outbox_retries") or {}
    for item in items:
        retry = retries.get(_outbox_key(item))
        if retry is not None and isinstance(item, dict):
            item["_retry"] = retry


@dataclass
class OutboxProcessor:
    files: OutboxFiles
    queue_index: Callable[[dict], dict]
    checkpoint: Callable[[dict], None]
    publish: Callable[[dict], None]
    deliver: Callable[[OutboxBatch, Any], None]
    current: dict = field(default_factory=lambda: {"key": None})
    poison: dict = field(default_factory=dict)

    def process(self, state):
        """Send replies drafted by the worker, with output guardrails + dedup.

        Under the shared lock, the items are taken out of reply_outbox.json
        into self.files.processing (merged back on the next pass if the
        monitor dies mid-send). They are sent with no lock held, since a
        multi-part send takes several seconds. Then, under the lock again,
        only the items still retrying go back, ahead of whatever the worker
        appended meanwhile: writing back the copy read before the sends
        deleted those appends. Settled items (sent, silent, or skipped) also
        leave the queue here, so the worker never has to rewrite it."""
        # An unreadable outbox is not idle: OutboxFiles.load sets it aside.
        idle = _read_json(self.files.pending, list) == []
        if idle and not os.path.exists(self.files.processing) and not state.get("released"):
            return
        picked = time.time()
        with self.files.queue.lock():
            fresh = self.files.load(self.files.pending)
            if fresh is None:
                return  # mid-write; the next pass gets it
            # Held replies an owner chose to send ride along (see hold_reply).
            outbox = (
                (self.files.load(self.files.processing) or []) + fresh + state.pop("released", [])
            )
            _restore_outbox_retries(state, outbox)
            atomic_write(self.files.processing, outbox)
            atomic_write(self.files.pending, [])
            if state.pop("outbox_retries", None) is not None:
                self.checkpoint(state)  # restored progress is now in the locked processing file

        batch = OutboxBatch(
            state,
            picked,
            self.queue_index(state),
            self.files,
            self.checkpoint,
            self.publish,
            self.current,
        )
        originals = [(item, _outbox_key(item)) for item in outbox]
        for item, _key in originals:
            self.deliver(batch, item)
        try:
            batch.finish()
        except SharedLockTimeout:
            batch.defer_retries(originals)
            raise

    def pass_once(self, state, work=None):
        """process_outbox for the main loop: when it raises while handling
        an item, that item's failure is counted, and after
        OUTBOX_POISON_TRIES it is taken out of the outbox files and set
        aside in reply_outbox.rejected.json; then the error is raised on."""
        self.current["key"] = None
        try:
            (work or self.process)(state)
        except SharedLockTimeout:
            self.current["key"] = None
            raise  # contention is not a malformed outbox item
        except Exception as e:
            key = self.current["key"]
            self.current["key"] = None
            if key:
                n = self.poison[key] = self.poison.get(key, 0) + 1
                if n >= OUTBOX_POISON_TRIES:
                    self.set_aside_poison(key, e)
            raise

    def set_aside_poison(self, key, exc):
        gone = []
        with self.files.queue.lock():
            for path in (self.files.processing, self.files.pending):
                items = self.files.load(path)
                if not items:
                    continue
                keep = [i for i in items if _outbox_key(i) != key]
                gone += [i for i in items if _outbox_key(i) == key]
                if len(keep) != len(items):
                    atomic_write(path, keep)
        self.poison.pop(key, None)
        if gone:
            why = f"processing it failed {OUTBOX_POISON_TRIES} times ({exc.__class__.__name__})"
            print(f"Set aside an outbox item: {why}.", flush=True)
            self.files.reject([(redact_nonces(i), why) for i in gone[:1]])
