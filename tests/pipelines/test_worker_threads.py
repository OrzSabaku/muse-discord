"""Each service starts one background consumer and retains its input bindings."""

import os
import queue
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "_lib"))
import common  # noqa: F401 (select the source tree under test)

from muse_discord.interaction_dispatch import InteractionWorker
from muse_discord.messaging.typing import TypingIndicators


class WorkerLifetime(unittest.TestCase):
    def test_starting_twice_runs_each_worker_once(self):
        typing = TypingIndicators(Mock(), Mock(), Mock())
        interactions = InteractionWorker(Mock(), Mock())
        inbox, record = queue.Queue(), Mock()
        for service, args in ((typing, ()), (interactions, (inbox, record))):
            with self.subTest(service=type(service).__name__):
                service.run = Mock()
                self.assertIsNone(service._thread)
                thread = service.start(*args)
                self.assertIs(service.start(*args), thread)
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
                self.assertTrue(thread.daemon)
                service.run.assert_called_once_with(*args)


if __name__ == "__main__":
    unittest.main()
