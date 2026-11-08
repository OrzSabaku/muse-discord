"""Owner services can recover and authorize work without a monitor bootstrap."""

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "_lib"))
import common  # noqa: F401 (select the source tree under test)

from muse_discord.message_queue import QueueFiles
from muse_discord.muse_settings import Settings
from muse_discord.owner.authorization import OwnerAuthorization
from muse_discord.owner.capabilities import CapabilityFiles, CapabilityIssuer
from muse_discord.owner.presence import OwnerPresence
from muse_discord.owner.targets import CommandTargets
from muse_discord.storage import PrivateFile

OWNER, DM, ITEM, FRAGMENT = (str(10**17 + n) for n in range(4))


def services(directory):
    queue = QueueFiles(str(directory / "queue.json"), str(directory / "queue.lock"))
    no_network = Mock(side_effect=AssertionError("known owner work needs no channel lookup"))
    auth = OwnerAuthorization(lambda: {OWNER}, {}, {}, {DM: OWNER}, no_network)
    files = CapabilityFiles(
        PrivateFile(str(directory / "owner_dm_nonces.json")),
        PrivateFile(str(directory / "owner_items.json")),
        PrivateFile(str(directory / "item_nonces.json")),
    )
    issuer = CapabilityIssuer(files, auth, CommandTargets({}, {}), queue.load, Mock())
    return queue, auth, issuer, no_network


def owner_item():
    return {
        "message_id": ITEM,
        "channel_id": DM,
        "author_id": OWNER,
        "kind": "dm",
        "owner_command": True,
        "content": "remember this",
        "settings": {"speaker": "owner", "where": "dm"},
        "burst_fragments": [{"message_id": FRAGMENT}],
    }


class Capabilities(unittest.TestCase):
    def test_publication_follows_destination_and_retains_secrets_after_failed_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _queue, _auth, issuer, _no_network = services(directory)
            destination = directory / "first.json"
            issuer.files.replies.destination = lambda: str(destination)
            state = {}
            reply = issuer.mint_reply(state, ITEM)
            issuer.mint_owner_dm(state, ITEM, DM)
            issuer.note_command(state, "command")
            known = {
                reply,
                state["nl_nonces"][ITEM]["nonce"],
                state["owner_items"]["command"]["nonce"],
            }
            self.assertEqual(issuer.published_nonces(), known)
            destination = directory / "second.json"
            issuer.write_replies(state)
            self.assertEqual(json.loads(destination.read_text()), {ITEM: reply})
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            with patch("muse_discord.storage.atomic_write", side_effect=OSError("disk full")):
                unpublished = issuer.mint_reply(state, "new-item")
            self.assertNotIn(unpublished, issuer.published_nonces())
            self.assertEqual(issuer.published_nonces(), known)
            self.assertEqual(json.loads(destination.read_text()), {ITEM: reply})

    def test_gate_checks_fragment_binding_without_spending_private_capabilities(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            queue, auth, issuer, no_network = services(directory)
            queue.edit(add=[owner_item()])
            state = {}
            issuer.mint_owner_dm(state, ITEM, DM, [FRAGMENT])
            nonce = state["nl_nonces"][ITEM]["nonce"]
            reply_nonce = issuer.mint_reply(state, ITEM)
            self.assertEqual(issuer.mint_reply(state, ITEM), reply_nonce)
            index = queue.index(state, {OWNER})
            item = {"item": ITEM, "reply_to": FRAGMENT, "channel_id": DM, "owner_dm_nonce": nonce}
            self.assertIsNone(auth.gate(state, item, index))
            self.assertIn("bad nonce", auth.gate(state, dict(item, owner_dm_nonce="wrong"), index))
            self.assertEqual(state["nl_nonces"][ITEM]["nonce"], nonce)
            self.assertNotIn(nonce, Path(queue.path).read_text())
            self.assertNotIn(reply_nonce, Path(queue.path).read_text())
            # A damaged private publication is repaired even when its cached
            # body has not changed, and replacement restores private permissions.
            reply_file = Path(issuer.files.replies.path)
            reply_file.write_text("{}")
            reply_file.chmod(0o644)
            issuer.write_replies(state, check=True)
            self.assertEqual(json.loads(reply_file.read_text()), {ITEM: reply_nonce})
            self.assertEqual(stat.S_IMODE(reply_file.stat().st_mode), 0o600)
            no_network.assert_not_called()

    def test_lost_state_recovers_all_capabilities_from_verified_queue_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            queue, _auth, issuer, no_network = services(Path(temporary))
            queue.edit(add=[owner_item()])
            state = {}
            issuer.upkeep(state, force=True)
            nonce = state["nl_nonces"][ITEM]["nonce"]
            commands = json.loads(Path(issuer.files.commands.path).read_text())
            self.assertEqual(commands[ITEM]["nonce"], nonce)
            self.assertEqual(commands[ITEM]["channel_id"], DM)
            self.assertEqual(state["nl_nonces"][ITEM]["fragments"], [FRAGMENT])
            self.assertTrue(state["reply_nonces"][ITEM]["nonce"])
            self.assertEqual(queue.load(), [owner_item()])
            no_network.assert_not_called()


class Presence(unittest.TestCase):
    def test_failed_lookup_keeps_server_gated_until_explicit_owner_configuration(self):
        config = Mock(spec=Settings)
        config.owner_sets_server.return_value = False
        get = Mock(side_effect=OSError("unavailable"))
        presence = OwnerPresence(lambda: {OWNER}, lambda: config, get, lambda: False)
        state = {}
        presence.joined(state, "guild")
        presence.check(state, {"guild": {}})
        self.assertTrue(presence.absent("guild"))
        self.assertIsNone(presence.membership["guild"])
        config.owner_sets_server.return_value = True
        self.assertFalse(presence.absent("guild"))
        self.assertIsNone(presence.membership["guild"])
        get.assert_called_once()


if __name__ == "__main__":
    unittest.main()
