"""Work deadlines remain responsive without a real clock or Discord connection."""

import os
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "_lib"))
import common  # noqa: F401 (select the source tree under test)

from muse_discord.work_wait import WorkWait


class WakeClock:
    def __init__(self, signalled=False):
        self.at = 100.0
        self.signalled = signalled
        self.waits = []
        self.cleared = 0

    def wait(self, timeout):
        self.waits.append(timeout)
        self.at += timeout
        return self.signalled

    def clear(self):
        self.cleared += 1


class WorkDeadlines(unittest.TestCase):
    def test_burst_deadline_shortens_rest_wait_and_passes_backoff(self):
        wake = WakeClock()
        rest = Mock(return_value=2.0)
        waiter = WorkWait(
            lambda: wake.at, lambda state, now: 0.1, lambda since: False, lambda: True
        )
        waiter.wait({}, wake, rest, 90.0)
        rest.assert_called_once_with(100.0, True)
        self.assertAlmostEqual(sum(wake.waits), 0.1)
        self.assertEqual(wake.cleared, 1)

    def test_a_reply_arriving_during_wait_is_found_within_a_probe(self):
        wake = WakeClock()
        outbox = Mock(side_effect=[False, True])
        waiter = WorkWait(lambda: wake.at, lambda state, now: None, outbox, lambda: False)
        waiter.wait({}, wake, lambda now, backing: 2.0, 90.0)
        self.assertEqual(wake.waits, [0.25])
        self.assertEqual(wake.cleared, 1)

    def test_existing_work_and_gateway_events_end_the_wait(self):
        for already_waiting, signalled, expected in ((True, False, []), (False, True, [0.25])):
            with self.subTest(outbox=already_waiting, gateway=signalled):
                wake = WakeClock(signalled)
                waiter = WorkWait(
                    lambda wake=wake: wake.at,
                    lambda state, now: None,
                    lambda since, waiting=already_waiting: waiting,
                    lambda: False,
                )
                waiter.wait({}, wake, lambda now, backing: 2.0, 90.0)
                self.assertEqual(wake.waits, expected)
                self.assertEqual(wake.cleared, 1)


if __name__ == "__main__":
    unittest.main()
