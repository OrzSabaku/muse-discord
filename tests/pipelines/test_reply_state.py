"""Reply-state recovery binds outcomes and metadata to the correct live item."""

import os
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "_lib"))
import common  # noqa: F401 (select the source tree under test)

from muse_discord.message_ledger import MessageLedger
from muse_discord.message_models import Settlement
from muse_discord.message_queue import QueueFiles
from muse_discord.receiving.direct import DirectChannels
from muse_discord.reply_state import ReplyState


class ReplyTracking(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = self.directory.name
        self.queue = QueueFiles(os.path.join(path, "queue.json"), os.path.join(path, "lock"), 0)
        self.ledger = MessageLedger(
            os.path.join(path, "ledger.json"), {}, DirectChannels(), Mock(), Mock()
        )
        self.replies = ReplyState(self.ledger, self.queue, Mock(), Mock())

    def test_fragment_settlement_never_closes_an_unrelated_burst(self):
        state = {"replied_to": [], "reply_aliases": {"11": "10", "21": "20"}}
        self.replies.settle(
            state,
            {"item": "11", "reply_to": "21", "channel_id": "200"},
            Settlement("replied", replied=True),
        )
        self.assertEqual(state["replied_to"], ["10", "11"])
        self.assertEqual(set(self.ledger.load()["entries"]), {"10", "11"})
        self.assertFalse(self.ledger.terminal("20"))
        self.assertFalse(self.ledger.terminal("21"))

    def test_lost_state_metadata_comes_from_live_queue_but_stale_metadata_is_ignored(self):
        item = {
            "message_id": "100",
            "author_id": "300",
            "author": "alice",
            "channel_id": "200",
            "kind": "dm",
            "from_bot": False,
        }
        self.queue.edit(add=[item])
        meta = self.replies.queued_meta({}, "100", {"channels": {"100": "200"}})
        self.assertEqual(meta["author_id"], "300")
        self.assertEqual(meta["channel_id"], "200")
        self.assertTrue(meta["dm"])
        self.assertIsNone(
            self.replies.queued_meta({"item_meta": {"100": meta}}, "100", {"channels": {}})
        )


if __name__ == "__main__":
    unittest.main()
