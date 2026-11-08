"""Fragment delivery invariants through an explicit transport, without a monitor."""

import os
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "_lib"))
import common  # noqa: F401 (select the source tree under test)

from muse_discord.message_models import SecretBlocked
from muse_discord.messaging.parts import PartDelivery, send_nonce
from muse_discord.messaging.sending import MessageSender


def guard(data, _where):
    content = data.get("content", "")
    text = "".join(content) if isinstance(content, list) else content
    if "blocked" in text:
        raise SecretBlocked("secret")
    return data


class Delivery(unittest.TestCase):
    def test_later_secret_prevents_every_part_from_leaving(self):
        post, upload, put, typing, sleep = [Mock() for _ in range(5)]
        sender = MessageSender(guard, post, upload, put)
        delivery = PartDelivery(sender, typing, sleep)
        with self.assertRaises(SecretBlocked):
            delivery.send("200", ["first", "second", "blocked"], reply_to="100")
        post.assert_not_called()
        upload.assert_not_called()
        typing.assert_not_called()
        sleep.assert_not_called()

    def test_partial_failure_keeps_original_exception_progress_and_part_nonces(self):
        error = OSError("ambiguous send failure")
        post = Mock(side_effect=[{"id": "sent-1"}, error])
        upload, put, typing, sleep = [Mock() for _ in range(4)]
        delivery = PartDelivery(MessageSender(guard, post, upload, put), typing, sleep)
        parts = ["one", "two"]
        with self.assertRaises(OSError) as raised:
            delivery.send("200", parts, reply_to="100")
        self.assertIs(raised.exception, error)
        self.assertEqual(error.failed_part, 1)
        self.assertEqual(error.sent_ids, ["sent-1"])
        first, second = [call.args[1] for call in post.call_args_list]
        self.assertEqual(first["nonce"], send_nonce("200", "100", parts, 0))
        self.assertEqual(second["nonce"], send_nonce("200", "100", parts, 1))
        self.assertTrue(first["enforce_nonce"])
        self.assertNotIn("message_reference", second)
        typing.assert_called_once_with("200")
        sleep.assert_called_once()


if __name__ == "__main__":
    unittest.main()
