"""Generated attachments stay bound to the item/channel across retries and expiry."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "_lib"))
import common  # noqa: F401 (select the source tree under test)

from muse_discord.generated_media import TTL_SECS
from muse_discord.generated_replies import GeneratedReplies


class GeneratedAttachments(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.image = self.root / "100" / "image.png"
        self.image.parent.mkdir()
        self.image.write_bytes(b"image")
        self.clock = [100.0]
        self.service = GeneratedReplies(str(self.root), lambda: self.clock[0])
        self.state = {
            "reply_aliases": {"101": "100"},
            "gen_approved": {"100": {"ts": 100.0, "channel_id": "200"}},
        }
        self.item = {"reply_to": "101", "channel_id": "200"}

    def test_burst_file_is_discovered_only_for_its_approved_channel(self):
        item_id, approval, attachments, retry_files = self.service.attachments(
            self.state, self.item, {}
        )
        self.assertEqual(item_id, "100")
        self.assertIs(approval, self.state["gen_approved"]["100"])
        self.assertEqual(attachments, [(str(self.image), "image.png")])
        self.assertEqual(retry_files, [[str(self.image), str(self.image)]])
        wrong_channel = dict(self.item, channel_id="201")
        self.assertEqual(
            self.service.attachments(self.state, wrong_channel, {"attachments": retry_files})[1:],
            (None, [], []),
        )
        wrong_item = dict(self.item, item="999")
        self.assertEqual(self.service.attachments(self.state, wrong_item, {})[1:], (None, [], []))

    def test_cached_files_are_rechecked_after_symlink_swap_and_approval_expires(self):
        _, _, _, retry_files = self.service.attachments(self.state, self.item, {})
        private = self.root / "999" / "private.png"
        private.parent.mkdir()
        private.write_bytes(b"other request")
        self.image.unlink()
        self.image.symlink_to(private)
        retry = {"attachments": retry_files}
        _, approval, attachments, selected = self.service.attachments(self.state, self.item, retry)
        self.assertIsNotNone(approval)
        self.assertEqual((attachments, selected), ([], []))
        self.clock[0] += TTL_SECS + 1
        self.assertEqual(self.service.attachments(self.state, self.item, retry)[1:], (None, [], []))
        self.service.sweep_state(self.state)
        self.assertEqual(self.state["gen_approved"], {})


if __name__ == "__main__":
    unittest.main()
