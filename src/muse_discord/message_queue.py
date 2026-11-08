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

    def index(self, state, owner_ids):
        """What the queue file says, read once per outbox pass: "frags"
        {fragment id: trigger id} for every queued burst, "channels"
        {message id: channel id} for every queued item, and "owners" the
        same for every queued owner DM. The queue is the
        one record of a queued item that nothing else evicts (item_meta and
        reply_aliases are bounded, and lost with state.json)."""
        try:
            queue = self.load()
        except (OSError, ValueError):
            queue = []
        frags, owners, channels = {}, {}, {}
        for q in queue:
            if not isinstance(q, dict) or not q.get("message_id"):
                continue
            mid = str(q["message_id"])
            channels[mid] = str(q.get("channel_id") or "")
            for f in q.get("burst_fragments") or []:
                if isinstance(f, dict) and f.get("message_id"):
                    frags[str(f["message_id"])] = mid
            if queued_owner_dm(state, q, owner_ids):
                owners[mid] = str(q.get("channel_id") or "")
        return {"frags": frags, "owners": owners, "channels": channels}


def owner_dm_metadata(meta, owners):
    """item_meta says this item is an owner's own DM (an older record
    without the owner_dm flag: judged from its fields, failing closed)."""
    if not isinstance(meta, dict):
        return False
    if "owner_dm" in meta:
        return bool(meta["owner_dm"])
    return bool(meta.get("dm")) and str(meta.get("author_id")) in owners


def keep_last(d, n):
    """Bound a dict to its n newest keys (insertion order)."""
    for k in list(d)[:-n]:
        del d[k]


def trigger_of(state, mid, frags=None):
    """The queue item (trigger) a message id belongs to: a burst fragment
    maps to its trigger (reply_aliases, the queue's bursts, an owner
    DM's nonce entry), anything else to itself."""
    mid = str(mid)
    t = (state.get("reply_aliases") or {}).get(mid) or (frags or {}).get(mid)
    if t:
        return str(t)
    for k, e in (state.get("nl_nonces") or {}).items():
        if isinstance(e, dict) and mid in (e.get("fragments") or ()):
            return str(k)
    return mid


def queued_owner_dm(state, q, owners):
    """A queue item that is an owner's own DM: from the monitor's record
    of it, else (state.json lost) from the item the monitor wrote, the
    same test the wake hook uses to hand it out alone."""
    mid = str(q.get("message_id") or "")
    meta = (state.get("item_meta") or {}).get(mid)
    if meta is not None:
        return owner_dm_metadata(meta, owners)
    card = q.get("settings") if isinstance(q.get("settings"), dict) else {}
    # (A reminder or web watch firing in an owner's DM is one too.)
    dm = q.get("kind") == "dm" or (
        q.get("kind") in ("reminder", "watch-met") and card.get("where") == "dm"
    )
    return dm and card.get("speaker") == "owner" and str(q.get("author_id")) in owners
