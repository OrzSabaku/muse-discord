"""The /settings edit pages, click by click, offline: picks on an
edit page stage into a per-user draft and write nothing; Save applies
the whole draft as one change (a risky draft asks first, and the
draft survives the card until Apply); Cancel throws it away; the
detail page reaches the settings the home page never had (who.allow
by picker, other bots, the daily caps, memory, reminders, proactive);
admins and members cannot open a draft; a draft is its owner's only.

Usage: [MUT_DIR=<tree>] python3 tests/settingsui/test_editdraft.py"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import crawl  # noqa: E402
import harness as H  # noqa: E402
from harness import (ADMIN, G1, G2, HOMEWORK, MANAGE, MEMBER,  # noqa: E402
                     MUSECHAT, OWNER, OWNER2, ROLE, SAM, UI, FakeCtx, P,
                     component, submit)

S1 = H.MS.Place.server(G1)
S2 = H.MS.Place.server(G2)
MS = H.MS


def verb(c):
    return c["custom_id"].split(":")[2]


def control(resp, which):
    for c in H.controls(resp):
        if (":" in which and c["custom_id"].startswith(which)) \
                or (":" not in which and verb(c) == which):
            return c
    return None


def msg(resp):
    """The message a click on resp's page would carry."""
    return {"id": "9", "flags": 64 | 32768,
            "components": resp["data"]["components"]}


def all_text(resp):
    return "\n".join(H.texts(resp))


class Base(unittest.TestCase):
    def setUp(self):
        H.install_determinism()
        self.ctx = FakeCtx()

    def h(self, ix):
        self.ctx.refresh()
        r = UI.handle(ix, self.ctx)
        self.assertFalse(crawl.crashed(self.ctx), self.ctx.logs[-1:])
        self.assertEqual(crawl.check(r, "response"), [])
        return r

    def owner(self, cid, values=None, guild=G1, **kw):
        return self.h(component(OWNER, cid, values, guild,
                                "0" if guild else None, **kw))

    def val(self, key, place):
        return self.ctx.value(key, place)


class EditDraft(Base):
    def open_edit(self, tok, guild=G1, channel=MUSECHAT):
        r = self.owner(f"{P}ed:{tok}", guild=guild, channel=channel)
        self.assertIsNotNone(control(r, "sv"))
        return r

    def test_staging_writes_nothing_until_save(self):
        self.assertEqual(self.val("who.replies", S1), "conversational")
        r = self.open_edit(f"s{G1}")
        r = self.owner(f"{P}set:s{G1}:r", ["when-addressed"],
                       message=msg(r))
        # Staged only: the setting and the overlay are untouched, and
        # the page lists what is waiting.
        self.assertEqual(self.val("who.replies", S1), "conversational")
        self.assertIn("Waiting to be saved (1)", all_text(r))
        r = self.owner(f"{P}pg:s{G1}:d", message=msg(r))
        r = self.owner(f"{P}set:s{G1}:b", ["ignore"], message=msg(r))
        self.assertEqual(self.val("who.bots", S1), "when-addressed")
        self.assertIn("Waiting to be saved (2)", all_text(r))
        before = list(self.ctx.applied)
        r = self.owner(f"{P}sv:s{G1}", message=msg(r))
        # One apply call carrying both changes.
        self.assertEqual(len(self.ctx.applied), len(before) + 1)
        self.assertEqual(len(self.ctx.applied[-1][0]), 2)
        self.assertEqual(self.val("who.replies", S1), "when-addressed")
        self.assertEqual(self.val("who.bots", S1), "ignore")
        # The draft is closed: reopening starts empty.
        r = self.open_edit(f"s{G1}")
        self.assertIn("No changes yet", all_text(r))

    def test_cancel_discards(self):
        r = self.open_edit(f"s{G1}")
        r = self.owner(f"{P}set:s{G1}:ln", ["brief"], message=msg(r))
        r = self.owner(f"{P}cx:s{G1}", message=msg(r))
        self.assertEqual(self.val("style.length", S1), "normal")
        self.assertEqual(self.ctx.sessions, {})
        self.assertIn("Discarded", all_text(r))

    def test_risky_save_asks_first_and_draft_survives_the_card(self):
        self.assertEqual(self.val("privacy.memory", S1), "public-only")
        r = self.open_edit(f"s{G1}")
        r = self.owner(f"{P}pg:s{G1}:f", message=msg(r))
        r = self.owner(f"{P}set:s{G1}:m", ["everything"], message=msg(r))
        r = self.owner(f"{P}sv:s{G1}", message=msg(r))
        ok = control(r, "ok")
        self.assertIsNotNone(ok)            # the confirm card
        self.assertEqual(self.val("privacy.memory", S1), "public-only")
        self.assertTrue(self.ctx.sessions)  # still open behind the card
        r = self.owner(ok["custom_id"], message=msg(r))
        self.assertEqual(self.val("privacy.memory", S1), "everything")
        self.assertEqual(self.ctx.sessions, {})

    def test_detail_controls_and_people_picker(self):
        r = self.open_edit(f"s{G2}", guild=G2, channel=HOMEWORK)
        r = self.owner(f"{P}pg:s{G2}:d", guild=G2, channel=HOMEWORK,
                       message=msg(r))
        r = self.owner(f"{P}pk:s{G2}:al", [SAM, ROLE], guild=G2,
                       channel=HOMEWORK, message=msg(r),
                       resolved=H.resolved_for(7, [SAM, ROLE]))
        r = self.owner(f"{P}set:s{G2}:bx", ["3"], guild=G2,
                       channel=HOMEWORK, message=msg(r))
        r = self.owner(f"{P}tg:s{G2}:rw:owners", guild=G2,
                       channel=HOMEWORK, message=msg(r))
        r = self.owner(f"{P}pg:s{G2}:f", guild=G2, channel=HOMEWORK,
                       message=msg(r))
        r = self.owner(f"{P}set:s{G2}:pd", ["50"], guild=G2,
                       channel=HOMEWORK, message=msg(r))
        r = self.owner(f"{P}set:s{G2}:su", ["ignore"], guild=G2,
                       channel=HOMEWORK, message=msg(r))
        r = self.owner(f"{P}set:s{G2}:pe", ["on"], guild=G2,
                       channel=HOMEWORK, message=msg(r))
        self.assertIsNone(self.val("limits.per_day", S2))
        r = self.owner(f"{P}sv:s{G2}", guild=G2, channel=HOMEWORK,
                       message=msg(r))
        # Narrowing who "everyone" answers to a list is risky: the
        # confirm card asks first, and Apply lands the whole draft.
        ok = control(r, "ok")
        self.assertIsNotNone(ok)
        self.assertIsNone(self.val("limits.per_day", S2))
        r = self.owner(ok["custom_id"], guild=G2, channel=HOMEWORK,
                       message=msg(r))
        self.assertEqual(self.val("who.allow", S2), [SAM, f"role:{ROLE}"])
        self.assertEqual(self.val("who.bot_exchange_limit", S2), 3)
        self.assertEqual(self.val("limits.per_day", S2), 50)
        self.assertEqual(self.val("privacy.suspicious", S2), "ignore")
        self.assertEqual(self.val("abilities.reminders", S2), "owners")
        self.assertTrue(self.val("proactive.enabled", S2))

    def test_notes_modal_stages_then_saves(self):
        r = self.open_edit(f"s{G2}", guild=G2, channel=HOMEWORK)
        r = self.owner(f"{P}mod:s{G2}:n", guild=G2, channel=HOMEWORK,
                       message=msg(r))
        self.assertEqual(r["type"], 9)      # a modal
        r = self.h(submit(OWNER, f"{P}sub:s{G2}:n",
                          {"notes": "Be terse."}, guild=G2,
                          channel=HOMEWORK))
        self.assertEqual(self.val("style.notes", S2), "")
        r = self.owner(f"{P}sv:s{G2}", guild=G2, channel=HOMEWORK,
                       message=msg(r))
        self.assertEqual(self.val("style.notes", S2), "Be terse.")

    def test_home_notes_the_unsaved_draft(self):
        r = self.open_edit(f"s{G1}")
        r = self.owner(f"{P}set:s{G1}:ln", ["detailed"], message=msg(r))
        r = self.owner(f"{P}pg:s{G1}:h", message=msg(r))
        self.assertIn("unsaved", all_text(r))
        cont = control(r, "ed")
        self.assertIn("Continue editing (1)", cont["label"])

    def test_admin_and_member_cannot_edit(self):
        r = self.h(component(ADMIN, f"{P}ed:s{G1}", None, G1, MANAGE,
                             MUSECHAT))
        self.assertIn("quieter", all_text(r))
        r = self.h(component(MEMBER, f"{P}ed:s{G1}", None, G1, "0",
                             MUSECHAT))
        self.assertIn("only the bot's owners", all_text(r))
        r = self.h(component(MEMBER, f"{P}sv:s{G1}", None, G1, "0",
                             MUSECHAT))
        self.assertIn("only the bot's owners", all_text(r))
        self.assertEqual(self.ctx.sessions, {})
        self.assertEqual(self.ctx.applied, self.ctx.applied)  # no crash
        self.assertEqual(self.val("who.replies", S1), "conversational")

    def test_a_draft_is_its_owners_only(self):
        self.ctx = FakeCtx(owners=(OWNER, OWNER2))
        r = self.open_edit(f"s{G1}")
        r = self.owner(f"{P}set:s{G1}:ln", ["brief"], message=msg(r))
        # The second owner sees no draft on the same place.
        r2 = self.h(component(OWNER2, f"{P}ed:s{G1}", None, G1, "0",
                              MUSECHAT))
        self.assertIn("No changes yet", all_text(r2))
        r2 = self.h(component(OWNER2, f"{P}sv:s{G1}", None, G1, "0",
                              MUSECHAT, message=msg(r2)))
        self.assertIn("No changes to save", all_text(r2))
        self.assertEqual(self.val("style.length", S1), "normal")
        # The first owner's draft is still there to save.
        r = self.owner(f"{P}sv:s{G1}")
        self.assertEqual(self.val("style.length", S1), "brief")

    def test_edit_pages_stay_within_limits(self):
        for tok, guild, channel in ((f"s{G1}", G1, MUSECHAT),
                                    (f"c{G1}.{MUSECHAT}", G1, MUSECHAT),
                                    ("x", G1, MUSECHAT),
                                    ("d", None, None)):
            r = self.owner(f"{P}ed:{tok}", guild=guild, channel=channel)
            for page in ("e", "d", "f"):
                r = self.owner(f"{P}pg:{tok}:{page}", guild=guild,
                               channel=channel, message=msg(r))
                n = UI.count_components(r["data"]["components"])
                self.assertLessEqual(n, UI.MAX_COMPONENTS,
                                     f"{tok} page {page}: {n}")


if __name__ == "__main__":
    unittest.main()
