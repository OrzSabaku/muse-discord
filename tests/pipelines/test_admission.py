"""Admission gates and membership cache behavior without a monitor or network."""

import os
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "_lib"))
import common  # noqa: F401 (select the source tree under test)

from muse_discord.admission import AdmissionPolicy, SharedServers
from muse_discord.conversation import Conversation
from muse_discord.directory.scopes import DirectoryScopes
from muse_discord.muse_settings import Settings
from muse_discord.owner.presence import OwnerPresence
from muse_discord.response_policy import ResponsePolicy


class Admission(unittest.TestCase):
    def test_owner_message_opens_known_server_and_live_settings_can_close_it(self):
        owners = {"owner"}
        resolved = {
            "who.replies": "freely",
            "who.bots": "ignore",
            "who.allow": "everyone",
            "who.ignore": [],
            "who.dms_from": "owners",
        }
        settings = Mock(spec=Settings)
        settings.resolve.return_value = resolved
        settings.owner_sets_server.return_value = False
        directory = DirectoryScopes({"channel": {"guild_id": "server"}}, {}, {})
        response = ResponsePolicy(
            lambda: settings,
            lambda: owners,
            Conversation("999", "muse"),
            directory.for_message,
            directory.for_channel,
            Mock(),
            Mock(),
        )
        presence = OwnerPresence(lambda: owners, lambda: settings, Mock(), lambda: False)
        presence.membership["server"] = None
        shared = SharedServers(Mock(), lambda: False, lambda: [], presence.absent)
        admission = AdmissionPolicy(response, presence, directory, shared)
        stranger = {"channel_id": "channel", "author": {"id": "stranger"}}
        self.assertEqual(admission.trigger_verdict(stranger), (None, None))
        owner = {"channel_id": "channel", "author": {"id": "owner"}}
        self.assertEqual(admission.trigger_verdict(owner), ("follow_up", None))
        self.assertTrue(presence.spoke["server"])
        self.assertEqual(admission.trigger_verdict(stranger), ("follow_up", None))
        resolved["who.replies"] = "off"
        stranger["mentions"] = [{"id": "999"}]
        self.assertEqual(admission.trigger_verdict(stranger), (None, "who.replies is off here"))
        self.assertEqual(admission.dm_refusal(owner["author"]), "who.replies is off for DMs")
        settings.resolve.return_value = dict(resolved, **{"who.replies": "freely"})
        self.assertIsNone(admission.dm_refusal(owner["author"]))
        owners.clear()
        self.assertEqual(admission.dm_refusal(owner["author"]), "who.dms_from is owners")

    def test_shared_membership_uses_distinct_positive_and_negative_ttls(self):
        clock, backing = [100.0], [False]
        missing = OSError("not a member")
        missing.code = 404
        get = Mock(side_effect=[missing, {}, OSError("temporary failure")])
        shared = SharedServers(
            get,
            lambda: backing[0],
            lambda: ["absent", "eligible"],
            lambda gid: gid == "absent",
            lambda: clock[0],
        )
        self.assertFalse(shared.contains("user"))
        clock[0] += 599
        self.assertFalse(shared.contains("user"))
        self.assertEqual(get.call_count, 1)
        clock[0] += 1
        self.assertTrue(shared.contains("user"))
        clock[0] += 3599
        self.assertTrue(shared.contains("user"))
        self.assertEqual(get.call_count, 2)
        clock[0] += 1
        self.assertTrue(shared.contains("user"))  # transient error retains last known result
        self.assertEqual(get.call_count, 3)
        backing[0] = True
        self.assertTrue(shared.contains("user"))
        self.assertEqual(get.call_count, 3)
        self.assertEqual(
            {call.args[0] for call in get.call_args_list}, {"/guilds/eligible/members/user"}
        )


if __name__ == "__main__":
    unittest.main()
