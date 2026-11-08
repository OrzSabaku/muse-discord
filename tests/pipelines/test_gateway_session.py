"""Lifetime resources work without booting a monitor or connecting to Discord."""

import contextlib
import io
import os
import sys
import unittest
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "_lib"))
import common  # noqa: F401 (select the source tree under test)

from muse_discord.gateway_session import GatewaySession


class GatewaySessionTests(unittest.TestCase):
    def test_gateway_failure_wakes_consumer_and_session_starts_only_once(self):
        session = GatewaySession("dummy", Mock(), Mock())
        session.connect = AsyncMock(side_effect=RuntimeError("offline"))
        session.status["connected"] = True
        with contextlib.redirect_stdout(io.StringIO()):
            inbox, status = session.start()
            again_inbox, again_status = session.start()
            self.assertTrue(status["wake"].wait(timeout=5))
            session._thread.join(timeout=5)
        self.assertFalse(session._thread.is_alive())
        self.assertIs(inbox, again_inbox)
        self.assertIs(status, again_status)
        self.assertIs(status["interactions"], session.interactions)
        self.assertTrue(status["dead"])
        self.assertFalse(status["connected"])
        session.connect.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
