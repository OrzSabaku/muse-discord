"""Stage received DMs and publish matured bursts without replacing a shared queue.

A queue snapshot is only for duplicate checks. Durable publication applies new
items to a fresh locked read; failed publication leaves a backlog for recovery.
Startup forces pending bursts whose messages were already marked seen.
"""

from collections.abc import Callable
from dataclasses import dataclass

from muse_discord.bursts import PendingBursts
from muse_discord.durable_state import QueuePublication
from muse_discord.message_models import MessagePlace
from muse_discord.storage import SharedLockTimeout


@dataclass
class QueueStaging:
    bursts_source: Callable[[dict], PendingBursts]
    publication_source: Callable[[], QueuePublication]

    def stage(self, state, dm_items):
        """Coalesce polled DMs and publish matured bursts; guild triggers are already staged."""
        publication = self.publication_source()
        bursts = self.bursts_source(state)
        # This snapshot and the backlog only serve duplicate checks. Flushing
        # can do slow media work while workers change the shared queue; publish
        # merges additions under the lock rather than replacing their changes.
        queue = publication.queue.load()
        queued = {q.get("message_id") for q in queue if isinstance(q, dict)}
        queue.extend(q for q in state.get("queue_backlog", []) if q["message_id"] not in queued)
        n_before = len(queue)
        for msg, cname in dm_items:
            bursts.stage(msg, cname, "dm", place=MessagePlace(guild_id=None, dm=True))

        bursts.flush(queue)
        publication.publish(state, queue[n_before:])
        publication.state_file.save(state)
        return queue

    def recover(self, state):
        """Publish pre-restart pending bursts, retaining work when the queue is locked."""
        if not state.get("pending"):
            return
        publication = self.publication_source()
        queue = publication.queue.load()
        n_before = len(queue)
        if self.bursts_source(state).flush(queue, force=True):
            try:
                publication.publish(state, queue[n_before:])
            except SharedLockTimeout as exc:
                print(f"Startup queue publication deferred: {exc}", flush=True)
