"""Shared worker queue: read snapshots and apply deltas under one bounded lock."""

from dataclasses import dataclass

from muse_discord import storage


def answered_id(state, item):
    """The queue item (its message_id) an outbox item answers: its "item"
    field, else the burst trigger its reply_to belongs to, else reply_to.
    None when it names neither."""
    if item.get("item"):
        return item["item"]
    reply_to = item.get("reply_to")
    return state.get("reply_aliases", {}).get(reply_to) or reply_to


@dataclass(frozen=True)
class QueueFiles:
    path: str
    lock_file: str
    lock_wait: float = 20

    def lock(self):
        return storage.shared_lock(self.lock_file, self.lock_wait)

    def load(self):
        return storage.load_queue(self.path)

    def apply_locked(self, add=(), drop=None):
        """Apply a delta while the caller holds lock(); use edit() otherwise."""
        queue = self.load()
        kept = [q for q in queue if not (drop and isinstance(q, dict) and drop(q))]
        have = {q.get("message_id") for q in kept if isinstance(q, dict)}
        added = 0
        for item in add:
            if item.get("message_id") in have:
                continue
            kept.append(item)
            have.add(item.get("message_id"))
            added += 1
        if added or len(kept) != len(queue):
            storage.atomic_write(self.path, kept)
        return added

    def edit(self, add=(), drop=None):
        """Apply a delta to reply_queue.json under the shared lock: re-read
        it, remove the items drop(item) is true for, append the items in add
        whose message_id is not queued yet. Never write back a copy loaded
        before slow work (media downloads, the sweep): it would resurrect
        removed items and drop ones appended meanwhile. Returns the number
        of items appended."""
        with self.lock():
            return self.apply_locked(add, drop)
