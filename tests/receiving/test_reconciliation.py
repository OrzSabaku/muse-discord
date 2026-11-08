"""Lost-message recovery respects in-flight work, attempt caps and rate limits."""

import os
import tempfile
import unittest
from unittest.mock import Mock

from receiving_harness import environment, message

from muse_discord.conversation import _snowflake_ts
from muse_discord.message_ledger import MessageLedger
from muse_discord.receiving.direct import DirectChannels
from muse_discord.receiving.reconciliation import MentionReconciler, RecoveryWindow
from muse_discord.storage import atomic_write


class Reconciliation(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = self.directory.name
        self.get = Mock()
        self.receiver, _gateway = environment(self.get)
        self.state = self.receiver.state
        self.channels = self.receiver.admission.directory.channels
        self.ledger = MessageLedger(
            lambda: os.path.join(path, "ledger.json"), self.channels, DirectChannels(), Mock()
        )
        self.queue = []

        def enqueue(msg, name, state, queue, *, context):
            queue.append({"message_id": msg["id"]})
            return True

        self.enqueue = Mock(side_effect=enqueue)
        self.reconciler = MentionReconciler(
            admission=self.receiver.admission,
            ledger=self.ledger,
            get=self.get,
            enqueue=self.enqueue,
            quarantined=Mock(return_value={"18"}),
            outbox_paths=(os.path.join(path, "outbox.json"), os.path.join(path, "processing.json")),
            direct=self.ledger.direct,
            window=RecoveryWindow(1, 1000, 2),
            backing_off=lambda: False,
            now=lambda: _snowflake_ts("10") + 100,
        )

    def test_only_unaccounted_messages_are_recovered_including_fragment_aliases(self):
        self.state["reply_aliases"] = {"12": "11"}
        self.state["pending"] = {"burst": {"first_id": "15", "fragments": [{"id": "16"}]}}
        self.state["replied_to"] = ["19"]
        self.queue.append({"message_id": "14"})
        atomic_write(self.reconciler.outbox_paths[0], [{"reply_to": "12"}])
        self.get.return_value = [message(str(mid)) for mid in (19, 18, 16, 15, 14, 13, 12, 11)]
        self.assertTrue(self.reconciler.run(self.state, self.queue, self.channels, dms=False))
        self.assertEqual(self.queue, [{"message_id": "14"}, {"message_id": "13"}])
        self.enqueue.assert_called_once()
        entries = self.ledger.load()["entries"]
        self.assertEqual(entries["13"]["recoveries"], 1)
        self.assertEqual(entries["19"]["status"], "replied")
        self.assertFalse(self.reconciler.run(self.state, self.queue, self.channels, dms=False))
        self.enqueue.assert_called_once()

    def test_failed_enqueues_stop_at_persisted_recovery_cap(self):
        self.get.return_value = [message("11")]
        self.enqueue.side_effect = None
        self.enqueue.return_value = False
        for _ in range(3):
            self.assertFalse(self.reconciler.run(self.state, self.queue, self.channels, dms=False))
        self.assertEqual(self.enqueue.call_count, 2)
        entry = self.ledger.load()["entries"]["11"]
        self.assertEqual(entry["status"], "skipped")
        self.assertEqual(entry["recoveries"], 2)
        self.assertEqual(entry["reason"], "recovery cap reached")

    def test_rate_limit_stops_pass_after_retaining_already_recovered_work(self):
        limited = OSError("rate limited")
        limited.code = 429
        self.get.side_effect = [[message("11")], limited]
        channels = dict(self.channels, other={"guild_id": "100"}, later={"guild_id": "100"})
        self.assertTrue(self.reconciler.run(self.state, self.queue, channels, dms=False))
        self.assertEqual(self.get.call_count, 2)
        self.assertEqual(self.queue, [{"message_id": "11"}])
        self.reconciler.backing_off = lambda: True
        self.assertFalse(self.reconciler.run(self.state, self.queue, channels, dms=False))
        self.assertEqual(self.get.call_count, 2)


if __name__ == "__main__":
    unittest.main()
