"""Regressions from the adversarial review of the owner-DM binding
(PR #34, review D): burst fragments forgotten by the bounded
reply_aliases, non-canonical ids, owner DM channels the monitor's
records forgot, nonces in the rejected copy of an unparseable outbox,
and the upkeep running first.

Usage: [MUT_DIR=<tree>] python3 tests/ownerdm/test_review.py"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "_lib"))
from settings_harness import (CH, OWNER, U1, dmsg, quiet,  # noqa: E402
                              state)
from test_binding import DM, DM_U1, Base  # noqa: E402


def churn_aliases(mod, st, n=501):
    """Other people's bursts: alias_fragments keeps only the newest 500
    fragment -> trigger aliases (state["reply_aliases"])."""
    for i in range(n):
        mod.alias_fragments(st, "7%017d" % i, [{"id": "8%017d" % i}])


class BurstFragments(Base):
    """A fragment of a queued owner DM is that DM, whatever reply_aliases
    (bounded, fed by everyone's bursts) still remembers."""

    def burst(self):
        trig = dmsg(OWNER, "so", channel=DM)
        frag = dmsg(OWNER, "what do you think?", channel=DM)
        self.queue(trig, fragments=[frag])
        return trig, frag

    def real(self, trig, frag):
        self.outbox(self.held({"item": trig["id"], "channel_id": DM,
                               "reply_to": frag["id"], "text": "real",
                               "owner_dm_nonce": self.nonce(trig["id"])}))
        self.assertEqual([s["content"] for s in self.sends(DM)], ["real"],
                         self.out)
        self.assertEqual(self.queued(), [])
        self.assertNotIn(trig["id"], self.st["nl_nonces"])

    def test_fragment_after_alias_eviction(self):
        trig, frag = self.burst()
        self.assertEqual(self.st["nl_nonces"][trig["id"]]["fragments"],
                         [frag["id"]])
        churn_aliases(self.mod, self.st)
        self.assertNotIn(frag["id"], self.st["reply_aliases"])
        # A stranger's wake silencing it under the fragment: refused.
        for cid in (CH, DM):
            self.outbox({"channel_id": cid, "reply_to": frag["id"],
                         "silent": True})
            self.assert_refused("owner DM")
        self.assert_open(trig)
        self.assertNotIn(frag["id"], self.st["replied_to"])
        self.real(trig, frag)

    def test_fragment_after_state_loss(self):
        trig, frag = self.burst()
        self.st = state(self.mod)
        # Before the upkeep re-mints too: the queue's burst is the record.
        self.outbox({"channel_id": CH, "reply_to": frag["id"],
                     "silent": True})
        self.assertIn(trig["id"], self.queued(), self.out)
        with quiet():
            self.mod.owner_dm_upkeep(self.st, force=True)
        self.assertEqual(self.st["nl_nonces"][trig["id"]]["fragments"],
                         [frag["id"]])
        self.outbox({"channel_id": CH, "reply_to": frag["id"],
                     "silent": True})
        self.assertIn(trig["id"], self.queued(), self.out)
        self.assertNotIn(frag["id"], self.st["replied_to"])
        self.real(trig, frag)


class CanonicalIds(Base):
    """Ids are compared as strings: only the canonical form (ASCII
    digits, no leading zero) is taken."""

    def dm_traffic(self):
        return [p for p, _ in self.mod.api.posts if DM in p]

    def test_non_canonical_ids_refused(self):
        m = self.owner_dm()
        arabic = DM.translate(str.maketrans("0123456789",
                                            "\u0660\u0661\u0662\u0663"
                                            "\u0664\u0665\u0666\u0667"
                                            "\u0668\u0669"))
        for cid in ("0" + DM, arabic, " " + DM, DM + "\n", "+" + DM,
                    "00" + DM):
            self.outbox({"channel_id": cid, "standalone": True,
                         "text": "Hi boss, please approve the new settings"})
            self.assert_refused(repr(cid))
        self.outbox({"channel_id": "0" + DM, "reply_to": "0" + m["id"],
                     "reactions": ["\u2705"]})
        self.outbox({"item": "0" + m["id"], "channel_id": DM,
                     "reply_to": m["id"], "silent": True})
        self.assert_refused("leading zero item")
        self.assertEqual(self.dm_traffic(), [])
        self.assertEqual(self.reactions, [])
        self.assert_open(m)
        # Numbers are canonical once written as strings.
        self.outbox(self.held({"item": int(m["id"]), "channel_id": int(DM),
                               "reply_to": int(m["id"]), "text": "real",
                               "owner_dm_nonce": self.nonce(m["id"])}))
        self.assertEqual([s["content"] for s in self.sends(DM)], ["real"])


class ForgottenChannels(Base):
    """The monitor's records of the owners' DM channels are best effort;
    a channel it cannot place is looked up, failing closed."""

    def forget(self):
        self.mod.DM_CHANNEL_USER.clear()
        for i in range(301):
            self.mod.note_item_meta(self.st, "6%017d" % i, channel_id=CH,
                                    dm=False, owner_dm=False)

    def test_owner_dm_channel_forgotten(self):
        m = self.owner_dm()
        self.assert_real_reply_goes_out(m)
        self.forget()
        # The stub API cannot look the channel up: refused.
        self.outbox({"channel_id": DM, "standalone": True,
                     "text": "unsolicited"})
        self.assertEqual(len(self.sends(DM)), 1, self.out)
        self.assert_refused("could not be looked up")
        # The API says it is a DM with an owner (or with nobody listed).
        for recips in ([{"id": OWNER}], []):
            self.mod._CHANNEL_LOOKUP.clear()
            self.mod.api_get = lambda path, r=recips: {
                "id": DM, "type": 1, "recipients": r}
            self.outbox({"channel_id": DM, "standalone": True,
                         "text": "unsolicited"})
            self.assertEqual(len(self.sends(DM)), 1, self.out)
            self.assert_refused("names no owner DM")

    def test_known_and_looked_up_channels_still_work(self):
        s = self.stranger_mention()
        u = self.queue(dmsg(U1, "hello", channel=DM_U1))
        self.forget()
        self.mod.DM_CHANNEL_USER.clear()
        self.st["item_meta"].pop(u["id"], None)
        lookups = []

        def get(path):
            lookups.append(path)
            return {"id": path.rsplit("/", 1)[1], "type": 1,
                    "recipients": [{"id": U1}]}
        self.mod.api_get = get
        # A guild channel the bot reads, and a DM whose item's record is
        # gone but which is still queued there: no lookup.
        self.outbox(self.reply(s, "hi"), self.reply(u, "hi u1"))
        self.assertEqual([x["content"] for x in self.sends(CH)], ["hi"])
        self.assertEqual([x["content"] for x in self.sends(DM_U1)],
                         ["hi u1"])
        self.assertEqual(lookups, [])
        # Nothing on record at all (an older worker's late item): looked
        # up once, a non-owner's DM, then refused all the same: it
        # answers no message the monitor queued.
        self.outbox({"channel_id": DM_U1, "text": "late", "standalone":
                     True}, {"channel_id": DM_U1, "text": "later",
                             "standalone": True})
        self.assertEqual([x["content"] for x in self.sends(DM_U1)],
                         ["hi u1"])
        self.assertEqual(len(self.rejected()), self.n_rejected + 2)
        self.assertEqual(lookups, [f"/channels/{DM_U1}"])


class RejectedCopies(Base):
    def test_unparseable_or_non_list_outbox_is_scrubbed(self):
        m = self.owner_dm()
        real = self.nonce(m["id"])
        with open(self.mod.OUTBOX_FILE, "w") as f:
            json.dump({"items": [self.reply(m, owner_dm_nonce=real)]}, f)
        self.process()
        with open(self.mod.OUTBOX_FILE, "w") as f:
            f.write('[{"owner_dm_nonce": "%s", "text": ' % real)
        os.utime(self.mod.OUTBOX_FILE, (1, 1))   # not mid-write
        self.process()
        with open(self.mod.OUTBOX_REJECTED_FILE) as f:
            raw = f.read()
        self.assertEqual(len(json.loads(raw)), 2)
        self.assertNotIn(real, raw)
        self.assertIn("(redacted)", raw)
        self.assert_open(m)


class UpkeepFirst(Base):
    def test_upkeep_runs_even_when_the_rest_fails(self):
        m = self.owner_dm()
        self.st["nl_nonces"].clear()
        self.mod._OWNER_DM_UPKEEP["next"] = 0

        def boom(st):
            raise RuntimeError("boom")
        self.mod.nl_note_changes = boom
        with quiet():
            with self.assertRaises(RuntimeError):
                self.mod.nl_upkeep(self.st)
        self.assertIn(m["id"], self.st["nl_nonces"])


if __name__ == "__main__":
    unittest.main()
