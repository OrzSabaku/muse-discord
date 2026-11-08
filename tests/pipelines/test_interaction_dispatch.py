"""Interaction ownership stays live and one failure cannot kill the worker."""

import contextlib
import io
import os
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "_lib"))
import common  # noqa: F401 (select the source tree under test)

from muse_discord.interaction_dispatch import (
    InteractionResponder,
    InteractionRouter,
    InteractionWorker,
)


def interaction(key, ident):
    return {"id": ident, "type": 3, "data": {"custom_id": key}, "user": {"id": "owner"}}


class Dispatch(unittest.TestCase):
    def test_owner_revocation_applies_to_the_next_click(self):
        owners, records = {"owner"}, []
        router = InteractionRouter(lambda: owners)

        def approve(ix, choice, user_id, record):
            record({"choice": choice, "user_id": user_id})
            return {"type": 7, "data": {"content": "done"}}

        router.register("approve", approve)
        click = interaction("approve:yes", "first")
        self.assertEqual(router.dispatch(click, records.append)["type"], 7)
        owners.clear()
        denied = router.dispatch(click, records.append)
        self.assertEqual(denied["data"]["flags"], 64)
        self.assertEqual(records, [{"choice": "yes", "user_id": "owner"}])

    def test_handler_and_ack_failure_do_not_prevent_the_next_interaction(self):
        router = InteractionRouter(lambda: {"owner"})
        router.register("broken", Mock(side_effect=ValueError("handler failed")))
        response = {"type": 6}
        handled = Mock(return_value=response)
        router.register("next", handled)
        first, second = interaction("broken", "first"), interaction("next", "second")
        inbox = Mock()
        inbox.get.side_effect = [first, second, StopIteration]
        responder = Mock(spec=InteractionResponder)
        responder.send.side_effect = [OSError("ack failed"), None]
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(StopIteration):
            InteractionWorker(router, responder).run(inbox, Mock())
        self.assertEqual(responder.send.call_count, 2)
        fallback = responder.send.call_args_list[0].args[1]
        self.assertEqual(fallback["data"]["flags"], 64)
        responder.send.assert_called_with(second, response)
        handled.assert_called_once()


if __name__ == "__main__":
    unittest.main()
