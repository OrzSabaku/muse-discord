"""Review I repros: a schedule action rides on any open queue item in
its channel, not only on the items the writing run was handed. A
stranger's run (maybe prompt-injected) borrows the owner's open item and
acts as the owner. Each test asserts the secure behaviour (it failed on
fix/bind-posts as reviewed). self.reply() is the run holding the item;
the stranger's run, not handed the owner's item, has no reply_nonce.

Usage: [MUT_DIR=<tree>] python3 tests/watches/test_review_i.py"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_watches as tw  # noqa: E402
from settings_harness import OWNER, U2  # noqa: E402


class BorrowedOwnerItem(tw.Base):
    def test_stranger_run_cancels_the_owners_reminders(self):
        self.remind(OWNER)                    # the owner's reminder
        self.assertEqual(len(self.items()), 1)
        own, _ = self.ask(OWNER, "<@bot> hey")  # open, in its own wake
        s, _ = self.ask(U2, "<@bot> hi")        # the stranger's wake
        self.outbox(self.reply(s, text="hi"),
                    self.reply(own, [{"op": "cancel", "all": True}],
                               text="Done.", reply_nonce=None))
        self.assertEqual(len(self.items()), 1, self.out)

    def test_stranger_run_preempts_the_owners_own_schedule(self):
        own, _ = self.ask(OWNER, "<@bot> remind me in 2 minutes")
        self.ask(U2, "<@bot> hi")
        # The stranger's run claims the owner's item first, with a
        # schedule the owner never asked for...
        self.outbox(self.reply(own, [{"op": "add", "kind": "remind",
                                      "after": 3600, "note": "x"}],
                               text="ok", reply_nonce=None))
        # ...then the owner's own run answers with the real request.
        self.outbox(self.reply(own, [{"op": "add", "kind": "remind",
                                      "after": 120, "note": "the real"}]))
        notes = [r.get("note") for r in self.items().values()]
        self.assertEqual(notes, ["the real"], self.out)


if __name__ == "__main__":
    unittest.main()
