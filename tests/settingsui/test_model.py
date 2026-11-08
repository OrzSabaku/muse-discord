"""Screen drafts can be reused without importing the Discord SDK."""

import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "_lib"))
import common  # noqa: F401 (select the source tree under test)

from muse_discord.muse_settings import Settings
from muse_discord.settings_views.model import Screen


class DraftModel(unittest.TestCase):
    def test_stages_a_validated_draft_without_loading_discord(self):
        self.assertNotIn("discord", sys.modules)
        ctx = SimpleNamespace(settings=Settings())
        screen = Screen("100", "dms")
        screen.stage(ctx, "owner", "style.notes", "Keep replies concise.")
        self.assertEqual(screen.draft, {"style.notes": "Keep replies concise."})
        (change,) = screen.changes(ctx)
        self.assertEqual(change.place.kind, "dms")
        self.assertEqual(change.key, "style.notes")
        self.assertEqual(ctx.settings.resolve(change.place.scope(change.key))["style.notes"], "")
        self.assertNotIn("discord", sys.modules)


if __name__ == "__main__":
    unittest.main()
