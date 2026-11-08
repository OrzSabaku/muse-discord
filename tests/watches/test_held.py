"""A held notice keeps every entry folded into it: one more reminder or
watch for the same person and place is never dropped (it was past 25).

Usage: [MUT_DIR=<tree>] python3 tests/watches/test_held.py"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_watches as tw

from muse_discord.watches.fires import PendingFire, hold


class Held(tw.Base):
    def test_folding_into_a_held_notice_drops_nothing(self):
        data = {"held": {}}
        es = [({"message_id": "1"}, {"id": f"r{i}", "kind": "remind"}) for i in range(40)]
        hid = hold(
            data,
            pending=PendingFire(
                fid="f1", uid="u", cid="c", entries=es[:1], kind="reminder", now=0, next_at=0
            ),
        )
        hold(
            data,
            pending=PendingFire(
                fid="f2", uid="u", cid="c", entries=es[1:], kind="reminder", now=0, next_at=0
            ),
        )
        self.assertEqual(len(data["held"][hid]["es"]), 40)


if __name__ == "__main__":
    unittest.main()
