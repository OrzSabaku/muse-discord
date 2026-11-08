"""Review M (lean/core): PendingBursts.flush isolates bursts.

Usage: [MUT_DIR=<tree>] python3 tests/monitorfixes/test_review_m.py"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_monitorfixes import CH, U1, Base, gmsg, quiet  # noqa: E402


class PoisonAfterAppend(Base):
    def tick(self):
        """The main loop's staging phase: flush, then write what was
        appended (only when PendingBursts.flush says it queued something)."""
        for entry in self.st.get("pending", {}).values():
            entry["first_seen"] = 0
        with quiet():
            queue = self.m._queue_files().load()
            n_before = len(queue)
            try:
                if self.m._bursts(self.st).flush(queue):
                    self.m._queue_files().edit(add=queue[n_before:])
            except Exception:
                pass  # the main loop logs it and goes on

    def test_a_failure_after_the_append_loses_nothing(self):
        """enqueue_message raises after queue.append(item) (here: the
        "queued" ledger write fails once). The burst must end up in
        reply_queue.json, or stay pending to be tried again."""
        msg = gmsg(U1, "@muse hello", mention=True)
        self.stage(msg)
        real = self.m._LEDGER.record_many
        failed = []

        def flaky(mids, status, *a, **k):
            if status == "queued" and not failed:
                failed.append(1)
                raise OSError("No space left on device")
            return real(mids, status, *a, **k)

        self.m._LEDGER.record_many = flaky
        self.tick()
        self.tick()  # a retry, if it was kept
        queued = [q["message_id"] for q in self.m._queue_files().load()]
        pending = f"{CH}:{U1}" in self.st.get("pending", {})
        self.assertTrue(msg["id"] in queued or pending, "the burst is neither queued nor pending")


if __name__ == "__main__":
    unittest.main()
