"""REST budgets, backoff and queue publication through explicit collaborators."""

import os
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "_lib"))
import common  # noqa: F401 (select the source tree under test)

from muse_discord.rest_scheduler import (
    DiscoveryRefresh,
    GeneratorJob,
    RestClock,
    RestContext,
    RestOperations,
    RestSchedule,
    RestScheduler,
    SweepPass,
)


def environment(clock, backing):
    channels = {str(i): {"name": str(i), "guild_id": "server"} for i in range(3)}
    operations = RestOperations(
        channels=lambda: channels,
        identity_ready=lambda: True,
        poll_targets=lambda: [("channel", cid) for cid in channels],
        list_dm_channels=Mock(return_value=[]),
        check_dm_channel=Mock(return_value=[]),
        check_channel=Mock(),
        load_queue=Mock(return_value=[{"message_id": "old"}]),
        sweep_mentions=Mock(return_value=False),
        publish_queue_additions=Mock(),
        save_state=Mock(),
        discovery_iter=Mock(side_effect=lambda state: iter(())),
        thread_refresh_iter=Mock(side_effect=lambda state: iter(())),
    )
    return RestContext(
        RestSchedule(10, 10, 10),
        RestClock(lambda: clock[0], lambda: backing[0]),
        operations,
    )


class Scheduling(unittest.TestCase):
    def test_slow_poll_and_background_job_alternate_without_blocking_each_other(self):
        clock, backing, order = [100.0], [False], []
        context = environment(clock, backing)

        def poll(*args):
            order.append("poll")
            clock[0] += 1

        def background():
            for _ in range(2):
                order.append("background")
                clock[0] += 1
                yield

        context.operations.check_channel.side_effect = poll
        scheduler = RestScheduler(context, DiscoveryRefresh())
        scheduler.jobs.append(GeneratorJob("maintenance", background(), context.clock))
        for _ in range(2):
            scheduler.begin_tick(clock[0], backing[0])
            poll_turn, spent = scheduler.poll_slice({}, clock[0], backing[0], [])
            scheduler.run_job({}, poll_turn, spent, True)
        self.assertEqual(order, ["poll", "background"])
        self.assertEqual(scheduler.rotation.pos, 1)
        self.assertEqual(scheduler.next_wait(clock[0], True), 2.0)

    def test_refresh_request_survives_recovery_cycle_and_does_not_duplicate_jobs(self):
        clock, backing = [100.0], [False]
        context = environment(clock, backing)
        refresh = DiscoveryRefresh()
        scheduler = RestScheduler(context, refresh)
        refresh.request()
        scheduler.cycle, scheduler.cycle_done = 1, True
        scheduler.schedule_jobs({}, recovery=True)
        self.assertTrue(refresh.pending)
        self.assertEqual(scheduler.jobs, [])
        scheduler.cycle = 2
        scheduler.schedule_jobs({}, recovery=False)
        self.assertFalse(refresh.pending)
        self.assertEqual([job.name for job in scheduler.jobs], ["Channel refresh"])
        refresh.request()
        scheduler.cycle = 3
        scheduler.schedule_jobs({}, recovery=False)
        context.operations.discovery_iter.assert_called_once()
        self.assertEqual(len(scheduler.jobs), 1)

    def test_sweep_never_uses_unknown_queue_but_publishes_completed_work_before_backoff(self):
        clock, backing = [100.0], [False]
        context = environment(clock, backing)
        skipped = SweepPass(["0"], context)
        self.assertEqual(skipped.run_slice({}, 200, queue_ok=False), 0)
        self.assertTrue(skipped.done())
        context.operations.load_queue.assert_not_called()
        context.operations.sweep_mentions.assert_not_called()

        def recover(state, queue, channels):
            queue.append({"message_id": "recovered"})
            backing[0] = True
            return True

        context.operations.sweep_mentions.side_effect = recover
        sweep = SweepPass(["0"], context)
        state = {}
        self.assertEqual(sweep.run_slice(state, 200, queue_ok=True), 1)
        self.assertTrue(sweep.done())
        context.operations.publish_queue_additions.assert_called_once_with(
            state, [{"message_id": "recovered"}]
        )
        context.operations.save_state.assert_called_once_with(state)


if __name__ == "__main__":
    unittest.main()
