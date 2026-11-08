"""Reply binding and stale-prompt diagnostics over issued private capabilities."""

import os
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "_lib"))
import common  # noqa: F401 (select the source tree under test)

from muse_discord.reply_binding import ReplyBinding, StalePrompt
from muse_discord.storage import PrivateFile


class Binding(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.marker = os.path.join(self.directory.name, "prompt_outdated.json")
        self.notify = Mock()
        self.published = PrivateFile(os.path.join(self.directory.name, "nonces.json"))
        self.binding = ReplyBinding(
            self.published,
            lambda item: False,
            StalePrompt(self.marker, self.notify, lambda: 100.0, lambda: "today"),
        )
        self.state = {
            "reply_nonces": {"100": {"nonce": "private-capability"}},
            "reply_aliases": {"101": "100", "301": "300"},
        }
        self.index = {
            "channels": {"100": "200", "300": "200"},
            "frags": {"101": "100", "301": "300"},
        }
        self.item = {
            "item": "100",
            "channel_id": "200",
            "reply_to": "101",
            "reply_nonce": "private-capability",
        }

    def test_capability_binds_the_own_channel_and_fragments_not_other_open_items(self):
        self.assertIsNone(self.binding.problem(self.state, self.item, self.index))
        for changed in (
            dict(self.item, reply_to="301"),
            dict(self.item, channel_id="201"),
            dict(self.item, reply_nonce="different"),
        ):
            problem = self.binding.problem(self.state, changed, self.index)
            self.assertIsInstance(problem, str)
            self.assertNotIn("private-capability", problem)

    def test_missing_unpublished_nonce_is_not_mistaken_for_an_obsolete_worker(self):
        item = dict(self.item)
        item.pop("reply_nonce")
        refusal = self.binding.problem(self.state, item, self.index)
        self.binding.stale_prompt.note(self.state, refusal)
        self.assertFalse(os.path.exists(self.marker))
        self.notify.assert_not_called()
        self.assertNotIn("stale_prompt_hits", self.state)
        self.published.publish({"100": "private-capability"})
        refusal = self.binding.problem(self.state, item, self.index)
        for _ in range(4):
            self.binding.stale_prompt.note(self.state, refusal)
        self.assertTrue(os.path.exists(self.marker))
        self.notify.assert_called_once()
        self.binding.stale_prompt.note(
            self.state, self.binding.problem(self.state, self.item, self.index)
        )
        self.assertFalse(os.path.exists(self.marker))
        self.assertNotIn("stale_prompt_hits", self.state)


if __name__ == "__main__":
    unittest.main()
