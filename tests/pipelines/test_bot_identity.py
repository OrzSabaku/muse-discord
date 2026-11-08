"""REST failures and Gateway READY share one conservative identity state."""

import contextlib
import io
import os
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "_lib"))
import common  # noqa: F401 (select the source tree under test)

from muse_discord.bot_identity import BotIdentity
from muse_discord.monitor_health import token_rejected_exit


class HttpFailure(Exception):
    def __init__(self, code):
        self.code = code


class IdentityRecovery(unittest.TestCase):
    def test_gateway_ready_recovers_transient_boot_failure_without_a_second_rest_query(self):
        get = Mock(side_effect=HttpFailure(429))
        clean, reject = Mock(), Mock()
        identity = BotIdentity(get, clean, reject)
        log = io.StringIO()
        with contextlib.redirect_stdout(log):
            identity.start()
            self.assertIsNone(identity.user_id)
            identity.from_gateway({"user": {"id": "999", "username": "muse"}})
            identity.retry(True)
            identity.from_gateway({"user": {"id": "other", "username": "other"}})
        self.assertEqual((identity.user_id, identity.name), ("999", "muse"))
        get.assert_called_once_with("/users/@me")
        clean.assert_called_once_with()
        reject.assert_not_called()
        self.assertIn("Bot identity pending", log.getvalue())
        self.assertIn("Bot user id: 999 (from gateway READY)", log.getvalue())

    def test_rest_recovery_waits_for_completed_rotations(self):
        get = Mock(side_effect=[HttpFailure(500), {"id": "999", "username": "muse"}])
        clean = Mock()
        identity = BotIdentity(get, clean, Mock())
        identity.retry(False)
        get.assert_not_called()
        clean.assert_not_called()
        identity.retry(True)
        self.assertIsNone(identity.user_id)
        with contextlib.redirect_stdout(io.StringIO()):
            identity.retry(True)
        self.assertEqual(identity.user_id, "999")
        self.assertEqual(clean.call_count, 2)

    def test_unauthorized_start_exits_with_the_watchdog_token_status(self):
        identity = BotIdentity(Mock(side_effect=HttpFailure(401)), Mock(), token_rejected_exit)
        log = io.StringIO()
        with contextlib.redirect_stdout(log), self.assertRaises(SystemExit) as failure:
            identity.start()
        self.assertEqual(failure.exception.code, 3)
        self.assertIn("TOKEN REJECTED", log.getvalue())
        self.assertIsNone(identity.user_id)


if __name__ == "__main__":
    unittest.main()
