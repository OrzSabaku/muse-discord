"""Regression tests for the adversarial review of the /settings redesign
(review B, 2026-10-19): another server's data never shown, ids no
resolved data vouches for never written, an admin's entry holding only
the admins' additions. The monitor-side ones are in tests/settingsplumb;
the hostile-fixture crawl is test_crawl_hostile.py.

Usage: [MUT_DIR=<tree>] python3 tests/settingsui/test_review.py"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import json  # noqa: E402

import crawl  # noqa: E402
import harness as H  # noqa: E402
from harness import (ADMIN, G1, G2, G3, MANAGE, MS, OWNER, SAM,  # noqa: E402
                     UI, FakeCtx, P, component, context_command, text)


def apply(ctx, changes):
    MS.apply(ctx.cfg, changes, OWNER, plugins_dir=H.PLUGINS, role="owner")
    ctx.refresh()


class Base(unittest.TestCase):
    def setUp(self):
        H.install_determinism()
        self.ctx = FakeCtx()

    def h(self, ix):
        self.ctx.refresh()
        r = UI.handle(ix, self.ctx)
        self.assertFalse(crawl.crashed(self.ctx), self.ctx.logs[-1:])
        return r


class WhyScope(Base):
    """#1: what another server's members were refused is never shown in
    this server, nor in a DM."""

    def test_another_servers_entry_is_not_shown(self):
        ctx = FakeCtx(text=H.BASE.replace(
            "servers:\n", f'servers:\n  "{G3}":\n    name: Pixel Plaza\n'
            f'    who:\n      allow: ["{SAM}"]\n', 1))
        tid = H.OUTCOMES[16]["message_id"]      # Kai's, in Pixel Plaza
        for ix in (context_command(ADMIN, tid, guild=G1, perms=MANAGE,
                                   author=H.KAI),
                   context_command(OWNER, tid, guild=None, author=H.KAI)):
            r = UI.handle(ix, ctx)
            self.assertNotIn("Pixel Plaza", text(r))
        # The server's Why page skips other servers' entries.
        o = dict(H.OUTCOMES[0], guild_id=G3, message_id="1999")
        self.ctx.outcomes = [o]
        self.ctx.recent_outcomes = lambda place, limit=20: [dict(o)]
        r = self.h(component(OWNER, f"{P}pg:s{G1}:y", None, G1, "0"))
        self.assertNotIn("1999", json.dumps(r))


class People(Base):
    def test_forged_ids_are_dropped(self):
        """#6: ids no resolved data vouches for (another server's, a
        channel's) are never written, and never named."""
        before = self.ctx.overlay()
        r = self.h(component(ADMIN, f"{P}ppl:s{G1}:ig", [G2, H.ART],
                             G1, MANAGE))
        self.assertEqual(self.ctx.overlay(), before)
        self.assertNotIn("Study Group", text(r))
        # a person's name only comes from people lookups
        R = UI.Req(component(OWNER, f"{P}pg:d:h"), self.ctx)
        self.assertIsNone(R.person(G2))
        self.assertEqual(R.person(SAM), "Sam")

    def test_admin_entry_holds_only_admin_additions(self):
        """#5: an admin's add copies nothing the owners set elsewhere."""
        apply(self.ctx, [MS.Change(MS.Place.defaults(), "who.ignore",
                                   [H.KAI])])
        self.h(component(ADMIN, f"{P}ppl:s{G2}:ig", [SAM], G2, MANAGE,
                         H.HOMEWORK, resolved=H.resolved_for(5, [SAM])))
        e = self.ctx.overlay()["values"][f'servers."{G2}".who.ignore']
        self.assertEqual(e["value"], [SAM])
        self.assertEqual(self.ctx.value("who.ignore", MS.Place.server(G2)),
                         [SAM, H.KAI])
        apply(self.ctx, [MS.Change(MS.Place.defaults(), "who.ignore", [])])
        self.assertEqual(self.ctx.value("who.ignore", MS.Place.server(G2)),
                         [SAM])
        apply(self.ctx, [MS.Change(MS.Place.defaults(), "who.ignore",
                                   [H.KAI])])
        r = self.h(H.command(ADMIN, G2, MANAGE, H.HOMEWORK))
        rm = H.find(r, f"{P}rm:s{G2}:ig")
        self.assertEqual([o["value"] for o in rm["options"]], [SAM])
        # removing it leaves the owners' list alone
        self.h(component(ADMIN, rm["custom_id"], [SAM], G2, MANAGE,
                         H.HOMEWORK))
        self.assertEqual(self.ctx.value("who.ignore", MS.Place.server(G2)),
                         [H.KAI])
        # the owners' entry at the server layer stays in an admin's add
        r = self.h(component(ADMIN, f"{P}ppl:s{G1}:ig", [SAM], G1,
                             MANAGE, resolved=H.resolved_for(5, [SAM])))
        self.assertEqual(self.ctx.value("who.ignore", MS.Place.server(G1)),
                         [H.BOB, H.SPAM, SAM])
        self.assertIsNotNone(H.find(r, f"{P}rm:s{G1}:ig"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
