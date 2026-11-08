"""Directory services work with explicit registries and no monitor initialization."""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "_lib"))
import common  # noqa: F401 (select the source tree under test)

from muse_discord.channel_access import ChannelAccess
from muse_discord.conversation import now_snowflake
from muse_discord.directory.discovery import Discovery
from muse_discord.directory.refresh import DirectoryRefresh
from muse_discord.directory.snapshot import DirectorySnapshot
from muse_discord.directory.state import DirectoryState


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

        registries = DirectoryState(started_at=time.time() - 60)
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


class Refresh(unittest.TestCase):
    def test_unchanged_ids_refresh_metadata_and_existing_policy_views(self):
        registry = DirectoryState(
            channels={"200": {"name": "old", "guild_id": "100", "can_send": True}},
            guilds={"100": {"name": "old server"}},
        )
        channels, guilds, scopes = registry.channels, registry.guilds, registry.scopes
        fresh = {"200": {"name": "new", "guild_id": "100", "can_send": False, "category_id": "300"}}
        discovery = Mock(spec=Discovery)

        def scan(previous):
            self.assertEqual(previous["200"]["name"], "old")
            yield
            return fresh, {"100": {"name": "renamed server"}}

        discovery.channels_iter.side_effect = scan
        published = []
        refresh = DirectoryRefresh(
            registry,
            discovery,
            lambda: published.append(dict(registry.channels)),
            Mock(),
            Mock(),
        )
        list(refresh.channels({}))
        self.assertIs(registry.channels, channels)
        self.assertIs(registry.guilds, guilds)
        self.assertFalse(channels["200"]["can_send"])
        self.assertEqual(scopes.for_channel("200").category_id, "300")
        self.assertEqual(scopes.for_channel("200").guild_name, "renamed server")
        self.assertEqual(published, [fresh])
        discovery.seed.assert_not_called()

    def test_interleaved_gateway_channel_is_retained_while_removed_channels_are_dropped(self):
        existing = {"name": "existing", "guild_id": "100"}
        registry = DirectoryState(channels={"200": existing, "removed": existing})
        discovery = Mock(spec=Discovery)

        def scan(previous):
            yield
            return {"200": existing}, {"100": {"name": "server"}}

        discovery.channels_iter.side_effect = scan
        refresh = DirectoryRefresh(registry, discovery, Mock(), Mock(), Mock())
        job = refresh.channels({})
        next(job)
        registry.channels["new-thread"] = {"name": "new thread", "guild_id": "100"}
        list(job)
        self.assertEqual(set(registry.channels), {"200", "new-thread"})
        self.assertEqual(set(refresh.sync.call_args.args[2]), {"200", "new-thread"})
        refresh.publish.assert_called_once()
        refresh.save.assert_called_once()


if __name__ == "__main__":
    unittest.main()
