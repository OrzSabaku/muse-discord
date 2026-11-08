"""Importing the command is inert and independent invocations cannot share authority."""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "_lib"))
from common import MUT_DIR

from muse_discord.discord_tools.cli import OwnerCommandCLI
from muse_discord.discord_tools.session import ToolSession

CHANNEL = "200000000000000001"
ITEM = "300000000000000001"


class CommandContext(unittest.TestCase):
    def test_importing_entrypoint_does_not_read_credentials(self):
        spec = importlib.util.spec_from_file_location(
            "inert_discord_cli", Path(MUT_DIR) / "bin/discord-api.py"
        )
        module = importlib.util.module_from_spec(spec)
        with patch("builtins.open", side_effect=AssertionError("import read a file")):
            spec.loader.exec_module(module)

    def test_verified_binding_in_one_instance_never_authorizes_another(self):
        with tempfile.TemporaryDirectory() as directory:
            first = OwnerCommandCLI(ToolSession(directory))
            second = OwnerCommandCLI(ToolSession(directory))
            Path(first.session.owner_items_file).write_text(
                json.dumps({ITEM: {"nonce": "a" * 32, "channel_id": CHANNEL}})
            )
            first.session.api = Mock(return_value={"id": CHANNEL, "type": 0})
            second.session.api = Mock(return_value={"id": CHANNEL, "type": 0})
            first.auth.binding.update({"for": ITEM, "nonce": "a" * 32, "cmd": "read"})
            first.auth.require_owner_command()
            first.auth.guard_target(CHANNEL)
            with self.assertRaises(SystemExit):
                second.auth.guard_target(CHANNEL)
            self.assertIsNone(second.auth.binding["entry"])
            first.auth.guard_target(CHANNEL)


if __name__ == "__main__":
    unittest.main()
