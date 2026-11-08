"""Directory services work with explicit registries and no monitor initialization."""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "_lib"))
import common  # noqa: F401 (select the source tree under test)

from muse_discord.channel_access import ChannelAccess
from muse_discord.conversation import now_snowflake
from muse_discord.directory.discovery import Discovery, DiscoveryState
from muse_discord.directory.snapshot import DirectorySnapshot


class Services(unittest.TestCase):
    def test_incremental_discovery_returns_permissions_and_seeds_new_thread_without_fetch(self):
        thread_id = now_snowflake()
        routes = {
            "/users/@me/guilds": [{"id": "100", "name": "Lounge"}],
            "/guilds/100?with_counts=true": {
                "owner_id": "900",
                "roles": [{"id": "100", "permissions": str(1024 | 2048)}],
            },
            "/guilds/100/members/400": {"roles": []},
            "/guilds/100/channels": [{"id": "200", "name": "general", "type": 0}],
            "/channels/200/messages?limit=1": [],
            "/guilds/100/threads/active": {
                "threads": [
                    {"id": thread_id, "name": "new", "parent_id": "200", "type": 11},
                ]
            },
            f"/channels/{thread_id}/messages?limit=1": [],
        }
        calls = []

        def get(path):
            calls.append(path)
            return routes[path]

        registries = DiscoveryState(started_at=time.time() - 60)
        discovery = Discovery(
            get,
            lambda: False,
            lambda: "400",
            set(),
            registries,
            ChannelAccess({}, get, lambda: False),
        )
        work = discovery.channels_iter()
        while True:
            try:
                self.assertIsNone(next(work))
            except StopIteration as done:
                channels, guilds = done.value
                break
        self.assertTrue(channels["200"]["can_send"])
        self.assertEqual(channels[thread_id]["name"], "general > new")
        self.assertEqual(guilds["100"]["name"], "Lounge")
        self.assertIn(thread_id, registries.new_threads)
        before = list(calls)
        state = {"last_seen": {}}
        discovery.seed(thread_id, "general > new", state)
        self.assertEqual(state["last_seen"][thread_id], str(int(thread_id) - 1))
        self.assertEqual(calls, before)

    def test_snapshot_copies_readers_and_preserves_thread_location_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "directory.json"
            view = DirectorySnapshot(lambda: str(path))
            monitored = {
                "200": {"guild_id": "100", "can_send": True},
                "300": {"guild_id": "100", "thread": True, "parent_id": "200"},
            }
            listed = {
                "100": [
                    {"id": "200", "name": "general", "type": 0},
                    {"id": "201", "name": "unreadable", "type": 0},
                ]
            }
            view.update({"100": {"name": "Lounge"}}, monitored, {"100": 50}, listed)
            returned = view.channels("100")
            returned[0]["name"] = "changed by reader"
            returned.append({"id": "injected"})
            self.assertEqual(view.channels("100")[0]["name"], "general")
            self.assertEqual(len(view.channels("100")), 2)
            self.assertEqual(view.locations()["300"], ("100", "200"))
            self.assertNotIn("201", view.locations())
            self.assertEqual(view.locations(include_unmonitored=True)["201"], ("100", None))
            self.assertFalse(view.channels("100")[1]["readable"])
            self.assertTrue(path.is_file())


if __name__ == "__main__":
    unittest.main()
