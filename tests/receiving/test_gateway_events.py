"""Metadata dispatch preserves the interaction worker/main-loop boundary."""

import queue
import unittest
from unittest.mock import Mock

from receiving_harness import drain, environment


class Metadata(unittest.TestCase):
    def test_queued_interaction_changes_wait_for_the_main_loop_to_drain_them(self):
        receiver, gateway = environment(Mock())
        applied = []
        gateway.events.record_handlers = {
            "hold_choice": lambda state, rec: applied.append((state, rec))
        }
        record = {"kind": "hold_choice", "action": "send"}
        inbox = queue.Queue()
        inbox.put({"_record": record})
        self.assertEqual(applied, [])
        self.assertEqual(gateway.drain(inbox), ([], []))
        self.assertEqual(applied, [(receiver.state, record)])

    def test_unknown_records_do_not_prevent_later_known_changes(self):
        receiver, gateway = environment(Mock())
        apply = Mock()
        gateway.events.record_handlers = {"hold_choice": apply}
        record = {"kind": "hold_choice", "action": "discard"}
        drain(
            gateway,
            {"_record": {"kind": "obsolete"}},
            {"_record": {"kind": ["hold_choice"]}},
            {"_record": {}},
            {"_record": record},
        )
        apply.assert_called_once_with(receiver.state, record)

    def test_only_guild_changes_that_require_discovery_request_a_refresh(self):
        receiver, gateway = environment(Mock())
        first, second = {"op": "create", "id": "100"}, {"op": "delete", "id": "200"}
        gateway.events.guild.side_effect = [True, False]
        drain(gateway, {"_guild_event": first}, {"_guild_event": second})
        self.assertEqual(gateway.events.guild.call_count, 2)
        gateway.events.guild.assert_called_with(receiver.state, second)
        gateway.events.refresh.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
