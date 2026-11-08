"""Gateway and REST repair through independently assembled receiving services."""

import unittest
from unittest.mock import Mock

from receiving_harness import drain, environment, message


class Reception(unittest.TestCase):
    def test_gateway_replay_is_deduplicated_and_rest_repairs_the_gap(self):
        get = Mock(return_value=[message("13"), message("12"), message("11")])
        receiver, gateway = environment(get)
        mentions, dms = drain(gateway, message("13"), message("13"))
        self.assertEqual(len(mentions), 1)
        self.assertEqual(dms, [])
        self.assertEqual(receiver.state["last_seen"]["200"], "10")
        self.assertEqual(receiver.state["gw_seen"]["200"], ["13"])
        self.assertEqual(receiver.poll_channel("200", "general", "100"), [])
        self.assertEqual(receiver.state["last_seen"]["200"], "13")
        self.assertNotIn("200", receiver.state["gw_seen"])
        burst = receiver.state["pending"]["200:400"]
        self.assertEqual(burst["first_id"], "13")
        self.assertEqual([fragment["id"] for fragment in burst["fragments"]], ["11", "12"])
        self.assertEqual(receiver.effects.count.call_count, 3)
        receiver.bursts.effects.ingested.assert_called_once()

    def test_unpermitted_dm_leaves_no_history_watermark_or_refusal_record(self):
        receiver, gateway = environment(Mock())
        receiver.state["last_seen"].clear()
        self.assertEqual(drain(gateway, message("11", dm=True)), ([], []))
        self.assertEqual(receiver.state["history"], {})
        self.assertEqual(receiver.state["last_seen"], {})
        self.assertNotIn("gw_seen", receiver.state)
        receiver.effects.refused.assert_not_called()
        receiver.bursts.effects.ingested.assert_not_called()

    def test_failed_unknown_channel_lookup_leaves_rest_repair_possible(self):
        get = Mock(side_effect=[OSError("temporary lookup failure"), [message("11")]])
        receiver, gateway = environment(get)
        gateway.channels.clear()
        self.assertEqual(drain(gateway, message("11")), ([], []))
        self.assertEqual(receiver.state["last_seen"]["200"], "10")
        self.assertNotIn("gw_seen", receiver.state)
        triggers = receiver.poll_channel("200", "general", "100")
        self.assertEqual(triggers, [(message("11"), "mention")])
        self.assertEqual(receiver.state["last_seen"]["200"], "11")
        self.assertEqual(receiver.state["pending"]["200:400"]["first_id"], "11")


if __name__ == "__main__":
    unittest.main()
