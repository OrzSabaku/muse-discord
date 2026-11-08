"""Command registration can run independently and waits for identity/backoff."""

import os
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "_lib"))
import common  # noqa: F401 (select the source tree under test)

from muse_discord.settings_commands import CommandRegistrar


class Registration(unittest.TestCase):
    def test_registration_waits_for_readiness_and_preserves_foreign_commands(self):
        ready, backing = [False], [False]
        foreign = {"id": "other", "name": "another-app-action", "type": 1}
        get, post, patch = Mock(return_value=[foreign]), Mock(), Mock()
        registrar = CommandRegistrar(
            get, post, patch, lambda: ready[0], lambda: backing[0], lambda: 100.0
        )
        status = {"application": {"id": "777"}}
        self.assertIsNone(registrar.register(status))
        ready[0], backing[0] = True, True
        self.assertIsNone(registrar.register(status))
        get.assert_not_called()
        backing[0] = False
        self.assertEqual(registrar.register(status), "created")
        get.assert_called_once_with("/applications/777/commands")
        self.assertEqual(post.call_count, 2)
        patch.assert_not_called()
        self.assertEqual(foreign, {"id": "other", "name": "another-app-action", "type": 1})
        self.assertIsNone(registrar.register(status))
        self.assertEqual(post.call_count, 2)


if __name__ == "__main__":
    unittest.main()
