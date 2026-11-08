"""Startup directory recovery keeps known receive positions and gates new guilds first."""

import contextlib
import io
import os
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "_lib"))
import common  # noqa: F401 (select the source tree under test)

from muse_discord.channel_access import ChannelAccess
from muse_discord.directory.discovery import Discovery
from muse_discord.directory.refresh import DirectoryRefresh
from muse_discord.directory.state import DirectoryState


class DirectoryRecovery(unittest.TestCase):
    def test_admission_is_restored_before_only_new_channels_are_seeded(self):
        directory = DirectoryState(
            channels={"200": {"name": "known"}, "201": {"name": "new"}},
            guilds={"100": {"name": "server"}},
        )
        order = []

        def get(path):
            order.append(path)
            return [{"id": "300"}, {"id": "301"}]

        discovery = Discovery(
            get,
            lambda: False,
            lambda: "bot",
            set(),
            directory,
            ChannelAccess(directory.channels, get, lambda: False),
        )
        sync = Mock(side_effect=lambda *_args: order.append("admission"))
        refresh = DirectoryRefresh(directory, discovery, Mock(), sync, Mock())
        state = {"last_seen": {"200": "150"}}
        with contextlib.redirect_stdout(io.StringIO()):
            refresh.restore(state)
        self.assertEqual(state["last_seen"], {"200": "150", "201": "301"})
        self.assertEqual(order, ["admission", "/channels/201/messages?limit=5"])
        sync.assert_called_once_with(state, directory.guilds, directory.channels)


if __name__ == "__main__":
    unittest.main()
