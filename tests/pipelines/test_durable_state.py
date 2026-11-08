"""State-file isolation and durable queue publication across lock contention."""

import os
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "_lib"))
import common  # noqa: F401 (select the source tree under test)

from muse_discord.durable_state import QueuePublication, StateFile
from muse_discord.message_ledger import MessageLedger
from muse_discord.message_queue import QueueFiles
from muse_discord.receiving.direct import DirectChannels
from muse_discord.storage import SharedLockTimeout, atomic_write, read_json


class DurableState(unittest.TestCase):
    def test_saved_content_cache_is_bound_to_its_destination(self):
        with tempfile.TemporaryDirectory() as path:
            first, second = [os.path.join(path, name) for name in ("one.json", "two.json")]
            destination = [first]
            store = StateFile(lambda: destination[0])
            state = {"replied_to": [], "value": "current"}
            store.save(state)
            atomic_write(second, {"replied_to": [], "value": "stale"})
            destination[0] = second
            store.save(state)
            self.assertEqual(read_json(second), state)
            self.assertEqual(read_json(first), state)

    def test_restart_replays_backlog_without_losing_workers_or_reviving_settled_items(self):
        with tempfile.TemporaryDirectory() as path:
            queue = QueueFiles(
                os.path.join(path, "queue.json"), os.path.join(path, "reply.lock"), 0
            )
            store = StateFile(lambda: os.path.join(path, "state.json"))
            ledger = MessageLedger(
                lambda: os.path.join(path, "ledger.json"), {}, DirectChannels(), Mock()
            )
            publisher = QueuePublication(queue, store, ledger)
            first, settled, worker, concurrent = [{"message_id": str(mid)} for mid in range(4)]
            atomic_write(queue.path, [worker])
            state = store.load()
            state["reply_nonces"] = {"0": {"nonce": "private-capability"}}
            with queue.lock():
                with self.assertRaises(SharedLockTimeout):
                    publisher.publish(state, [first, settled])
                self.assertEqual(queue.load(), [worker])
            restored = store.load()
            self.assertEqual(restored["queue_backlog"], [first, settled])
            self.assertEqual(restored["reply_nonces"], state["reply_nonces"])
            queue.edit(add=[concurrent])
            ledger.record(settled["message_id"], "replied")
            publisher.publish(restored)
            self.assertNotIn("queue_backlog", restored)
            self.assertEqual(queue.load(), [worker, concurrent, first])
            # A crash before the next state checkpoint leaves the old backlog
            # on disk. Its replay must still be idempotent.
            publisher.publish(store.load())
            self.assertEqual(queue.load(), [worker, concurrent, first])


if __name__ == "__main__":
    unittest.main()
