"""Coalescing recovery and gateway ordering without importing the monitor."""

import json
import os
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "_lib"))
import common  # noqa: F401 (select the source tree under test)

from muse_discord.bursts import BurstEffects, CoalescePolicy, GatewayActivity, PendingBursts
from muse_discord.message_models import MessagePlace


def message(mid, author="alice"):
    return {"id": mid, "channel_id": "10", "author": {"id": author, "username": author}}


def coalescer(state, enqueue, clock, activity=None, effects=None):
    return PendingBursts(
        state,
        activity or GatewayActivity(),
        CoalescePolicy(30, 45, lambda author: bool((author or {}).get("bot"))),
        enqueue,
        effects or BurstEffects(Mock(), Mock(return_value=False), Mock()),
        lambda: clock[0],
    )


class Bursts(unittest.TestCase):
    def test_restart_preserves_pending_work_and_flushes_it_once(self):
        state, clock = {}, [100]

        def enqueue(msg, _name, _state, queue, *, context):
            queue.append({"message_id": msg["id"], "fragments": context.fragments})
            return True

        original = coalescer(state, enqueue, clock)
        original.stage(message("100"), "general", "mention", MessagePlace("server"))
        original.hold("10", message("101"))
        restored = json.loads(json.dumps(state))
        clock[0] = 131
        restarted = coalescer(restored, enqueue, clock)
        queue = []
        self.assertTrue(restarted.flush(queue))
        self.assertEqual(queue, [{"message_id": "100", "fragments": [message("101")]}])
        self.assertFalse(restarted.flush(queue))
        self.assertEqual(restored["pending"], {})

    def test_partial_enqueue_rolls_back_and_retries_survive_restart(self):
        state, clock = {}, [100]
        effects = BurstEffects(Mock(), Mock(return_value=False), Mock())

        def enqueue(msg, _name, _state, queue, *, context):
            queue.append({"message_id": msg["id"]})
            if msg["id"] == "100":
                raise OSError("ledger unavailable after append")
            return True

        bursts = coalescer(state, enqueue, clock, effects=effects)
        bursts.stage(message("100"), "general", "mention")
        bursts.hold("10", message("101"))
        bursts.stage(message("200", "bob"), "general", "mention")
        queue = [{"message_id": "old"}]
        clock[0] = 131
        self.assertTrue(bursts.flush(queue))
        self.assertEqual(queue, [{"message_id": "old"}, {"message_id": "200"}])
        self.assertEqual(state["pending"]["10:alice"]["tries"], 1)
        restored = json.loads(json.dumps(state))
        restarted = coalescer(restored, enqueue, clock, effects=effects)
        self.assertFalse(restarted.flush(queue))  # persisted backoff
        self.assertEqual(restored["pending"]["10:alice"]["tries"], 1)
        for _ in range(2):
            clock[0] += 300
            self.assertFalse(restarted.flush(queue))
        self.assertEqual(restored["pending"], {})
        self.assertEqual(queue, [{"message_id": "old"}, {"message_id": "200"}])
        self.assertEqual(effects.record_many.call_args.args[:2], (["100", "101"], "skipped"))
        effects.record_many.assert_called_once()

    def test_typing_and_unseen_fragments_prevent_early_flush(self):
        state, clock, activity = {}, [10], GatewayActivity()
        bursts = coalescer(state, Mock(), clock, activity)
        bursts.stage(message("100"), "general", "mention")
        activity.note("10", "alice", "100", now=10)
        activity.note("10", "alice", now=11)
        activity.note("10", "alice", "99", now=12)  # late event changes nothing
        entry = state["pending"]["10:alice"]
        self.assertFalse(bursts.quiet(entry, 20))
        bursts.hold("10", message("101"))
        self.assertFalse(bursts.quiet(entry, 20))
        activity.note("10", "alice", "101", now=21)
        self.assertFalse(bursts.quiet(entry, 22))
        self.assertTrue(bursts.quiet(entry, 23))


if __name__ == "__main__":
    unittest.main()
