"""Publish recorded notification fires through the shared queue and capability journal."""

from collections.abc import Callable
from dataclasses import dataclass

from muse_discord.ingest import IngestJournal
from muse_discord.message_queue import QueueFiles


@dataclass
class WatchQueue:
    queue: QueueFiles
    journal: IngestJournal
    owners_source: Callable[[], set[str]]
    publish_nonces: Callable[[dict], None]

    def queued_ids(self) -> set[str]:
        return {str(q.get("message_id")) for q in self.queue.load() if isinstance(q, dict)}

    def enqueue(self, state, q):
        """Queue a fire: its record (item_meta: the owner-DM gate and the
        follow-up window read it), an owner DM's nonce (minted before the
        item reaches the queue file, as for a DM), then the queue."""
        fid, uid = q["message_id"], str(q["author_id"])
        dm = q.get("guild_id") is None
        owner_dm = dm and uid in self.owners_source()
        self.journal.note_item_meta(
            state,
            fid,
            author_id=uid,
            channel_id=q["channel_id"],
            bot=False,
            dm=dm,
            kind=q.get("kind"),
            owner_dm=owner_dm,
            guild_id=q.get("guild_id"),
            author=q.get("author"),
        )
        self.journal.note_queued(state, fid)
        if owner_dm and fid not in (state.get("nl_nonces") or {}):
            self.journal.owner_dm_mint_nonce(state, fid, q["channel_id"])
        self.journal.reply_nonce_mint(state, fid)
        self.queue.edit(add=[q])
        items = (q.get("reminder") or {}).get("items") or []
        print(
            f"Queued a reminder for {q.get('author')} in "
            f"{q.get('channel_name')} ({len(items)} item(s)"
            f"{', late' if (q.get('reminder') or {}).get('late') else ''}).",
            flush=True,
        )

    def drop_stale(self, state, stale):
        if stale:
            self.queue.edit(drop=lambda q: str(q.get("message_id")) in stale)
            nonces = state.get("nl_nonces") or {}
            for fid in stale:
                nonces.pop(fid, None)
            self.publish_nonces(state)
