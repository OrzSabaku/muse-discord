"""Notification services run with explicit dependencies and no monitor bootstrap."""

import json
import os
import stat
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "_lib"))
import common  # noqa: F401 (select the source tree under test)

from muse_discord import muse_watches as W
from muse_discord.channel_access import ChannelAccess
from muse_discord.conversation import Conversation
from muse_discord.muse_settings import Scope, Settings
from muse_discord.response_policy import ResponsePolicy
from muse_discord.watches.audience import WatchAudience
from muse_discord.watches.delivery import WatchDelivery
from muse_discord.watches.fires import FirePolicy, WatchFireBuilder

OWNER = "300"
CHANNEL = "200"


class Services(unittest.TestCase):
    def test_retry_after_restart_keeps_reply_target_and_ping_audience(self):
        with tempfile.TemporaryDirectory() as directory:
            store = W.Store(directory + "/watches.json")
            fire = {
                "id": "400",
                "requester": OWNER,
                "channel_id": CHANNEL,
                "originals": ["100"],
            }
            with store.edit() as data:
                data["fires"]["400"] = fire
            get = Mock(return_value={"id": "100"})
            delivery = WatchDelivery(store, get, lambda item: [item["text"]])
            first = delivery.send({}, dict(store.read()["fires"]["400"]), ["hello"])
            get.assert_called_once_with("/channels/200/messages/100")
            # A fresh store/service sees the saved target even when the
            # original request has since been deleted.
            restarted = W.Store(store.path)
            no_network = Mock(side_effect=AssertionError("target must already be saved"))
            delivery = WatchDelivery(restarted, no_network, lambda item: [item["text"]])
            second = delivery.send({}, dict(restarted.read()["fires"]["400"]), ["hello"])
            self.assertEqual(first, second)
            self.assertEqual(second[0], "100")
            self.assertEqual(second[2], {"parse": [], "users": [OWNER], "replied_user": True})
            no_network.assert_not_called()

    def test_prechecked_fire_builds_under_store_lock_without_permission_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            store = W.Store(directory + "/watches.json")
            no_network = Mock(side_effect=AssertionError("network under the watch lock"))
            response = ResponsePolicy(
                Settings,
                lambda: {OWNER},
                Conversation("999", "muse"),
                lambda *_: Scope.dm(),
                lambda *_: Scope.dm(),
                no_network,
                Mock(),
            )
            audience = WatchAudience(
                lambda: response,
                ChannelAccess({}, no_network, lambda: False),
                {},
                {},
                lambda _: False,
                lambda _: None,
            )
            builder = WatchFireBuilder(audience, Mock(), Mock(return_value={"where": "dm"}))
            now = time.time()
            record = {
                "message_id": "100",
                "requester": OWNER,
                "channel_id": CHANNEL,
                "guild_id": None,
                "dm": True,
                "kind": "remind",
                "created": now - 60,
            }
            entries = [(OWNER, CHANNEL, (record, W.fire_entry("r1", record, now)))]
            verdicts = {(OWNER, CHANNEL, None, True): None}
            with store.edit() as data:
                items = builder.make({}, data, entries, FirePolicy(now, verdicts, {}))
            self.assertEqual(len(items), 1)
            queued_id = items[0]["message_id"]
            self.assertIn(queued_id, store.read()["fires"])
            self.assertEqual(items[0]["author_id"], OWNER)
            no_network.assert_not_called()

    def test_store_path_switch_uses_the_new_cache_and_private_file_in_each_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = directory + "/first", directory + "/second"
            os.mkdir(first)
            os.mkdir(second)
            selected = first + "/watches.json"
            store = W.Store(lambda: selected)
            with store.edit() as data:
                data["items"]["first"] = {"kind": "remind", "due": 1000}
            selected = second + "/watches.json"
            self.assertEqual(store.read()["items"], {})
            with store.edit() as data:
                data["items"]["second"] = {"kind": "remind", "due": 2000}
            for base, key in ((first, "first"), (second, "second")):
                path = base + "/watches.json"
                with open(path) as stream:
                    self.assertEqual(set(json.load(stream)["items"]), {key})
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
                self.assertTrue(os.path.isfile(base + "/watches.lock"))
            selected = first + "/watches.json"
            self.assertEqual(set(store.read()["items"]), {"first"})


if __name__ == "__main__":
    unittest.main()
