"""Outgoing checks preserve payloads and share daily notices across threads."""

import contextlib
import io
import os
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "_lib"))
import common  # noqa: F401 (select the source tree under test)

from muse_discord.message_models import SecretBlocked
from muse_discord.outgoing_guard import OutgoingGuard, collect_nonces

TOKEN = "test-credential-that-must-never-leave"


class Outgoing(unittest.TestCase):
    def test_nonce_collection_uses_only_private_values_and_survives_optional_source_failure(self):
        flat = ({"id": "flat-secret", "number": 1}, None)
        records = ({"tag": "public", "nested": [{"nonce": "nested-secret"}]},)
        self.assertEqual(
            collect_nonces(flat, records, lambda: ["slot-secret"]),
            {"flat-secret", "nested-secret", "slot-secret"},
        )
        self.assertEqual(
            collect_nonces(flat, records, Mock(side_effect=RuntimeError("optional source"))),
            {"flat-secret", "nested-secret"},
        )

    def test_joined_fields_are_checked_without_rewriting_or_logging_the_secret(self):
        notify = Mock()
        guard = OutgoingGuard(lambda: TOKEN, lambda: {"private-code"}, notify)
        allowed = {"content": "hello", "nonce": "derived-send-nonce"}
        self.assertIs(guard.guard(allowed, "channel"), allowed)
        payload = {"content": TOKEN[:15], "embeds": [{"description": TOKEN[15:]}]}
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SecretBlocked):
            guard.guard(payload, "channel")
        self.assertEqual(payload["content"], TOKEN[:15])
        self.assertNotIn(TOKEN, out.getvalue())
        self.assertNotIn(TOKEN, notify.call_args.args[0])
        notify.assert_called_once()

    def test_recursive_and_concurrent_checks_share_one_daily_notice(self):
        day, notices = ["day-one"], []

        def notify(text):
            notices.append(text)
            with self.assertRaises(SecretBlocked):
                guard.guard({"content": TOKEN}, "notification")

        guard = OutgoingGuard(lambda: TOKEN, lambda: set(), notify, lambda: day[0])

        def blocked_send(_):
            with self.assertRaises(SecretBlocked):
                guard.guard({"content": TOKEN}, "channel")

        with contextlib.redirect_stdout(io.StringIO()):
            with ThreadPoolExecutor(max_workers=4) as workers:
                list(workers.map(blocked_send, range(8)))
            self.assertEqual(len(notices), 1)
            day[0] = "day-two"
            blocked_send(None)
            self.assertEqual(len(notices), 2)


if __name__ == "__main__":
    unittest.main()
