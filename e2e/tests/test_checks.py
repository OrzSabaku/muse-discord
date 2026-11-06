"""Offline tests for e2e/checks.py: each check fails the transcripts a
broken bot would produce (and passes a good one), and a bad check never
passes or takes the run down.

Run from the repo root: python3 -m unittest discover -s e2e/tests -t .
"""
import os
import sys
import unittest

E2E = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, E2E)

import checks  # noqa: E402
import scenario  # noqa: E402

IDS = {"SUT": "900", "A": "901", "B": "902", "SUT_NAME": "muse-test"}
AUTHOR_IDS = {"sut": "900", "a": "901", "b": "902"}
T0 = 1_800_000_000  # epoch seconds of the scenario start


def snowflake(secs):
    return str((int(secs * 1000) - checks.DISCORD_EPOCH_MS) << 22)


def iso(secs):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(secs, timezone.utc).isoformat()


def msg(author, at, content="", reply_to=None, **extra):
    """A transcript message posted `at` seconds after T0 (its id is the
    matching snowflake, so ids order like times)."""
    m = {"id": snowflake(T0 + at), "author": author,
         "author_id": AUTHOR_IDS[author], "ts": iso(T0 + at),
         "content": content, "reply_to": reply_to, "attachments": [],
         "mentions": [], "mention_everyone": False, "mention_roles": []}
    m.update(extra)
    return m


def transcript(messages, labels=None, **extra):
    tr = {"scenario": "t", "channel_id": "1", "started_at": iso(T0),
          "ids": dict(IDS), "labels": labels or {}, "converse_turns": None,
          "messages": messages,
          "sut_files": {"quarantine": None, "ledger": None},
          "monitor_log_tail": ""}
    tr.update(extra)
    return tr


def ok(check, tr):
    """Evaluate one check; returns its ok flag."""
    (res,) = checks.evaluate({"expect": {"checks": [check]}}, tr)
    return res["ok"]


def basic():
    """A mentions at t=0 (q1), B chats at t=1 (q2), SUT replies to q1
    at t=4."""
    q1 = msg("a", 0, "<@900> hi")
    q2 = msg("b", 1, "unrelated")
    r1 = msg("sut", 4, "ack 1", reply_to=q1["id"])
    return transcript([q1, q2, r1], {"q1": q1["id"], "q2": q2["id"]}), \
        q1, q2, r1


class Evaluate(unittest.TestCase):
    def test_check_names_agree_with_the_scenario_schema(self):
        self.assertEqual(set(scenario.CHECK_ARGS), set(checks.CHECKS))

    def test_bad_checks_fail_and_never_raise(self):
        tr, *_ = basic()
        for chk in ({"bogus": 1}, "replies_to", {"replies_to": "q7"},
                    {"latency": "q1"}):
            (r,) = checks.evaluate({"expect": {"checks": [chk]}}, tr)
            self.assertFalse(r["ok"], chk)
        # An empty transcript (the scenario never ran) never crashes
        # the run.
        for name in checks.CHECKS:
            arg = {"latency": {"from": "q1", "max": 1},
                   "mentions": {"label": "q1", "user": "a"},
                   "ledger_status": {"label": "q1", "status": "replied"},
                   "replies_to_any": ["q1"],
                   "converse_turns": {"max": 1},
                   "monitor_restarts": {"eq": 0}}.get(name, "q1")
            checks.evaluate({"expect": {"checks": [{name: arg}]}}, {})


class Replies(unittest.TestCase):
    def test_counting_and_threading(self):
        tr, q1, q2, r1 = basic()
        self.assertTrue(ok({"sut_reply_count": {"eq": 1}}, tr))
        self.assertTrue(ok({"replies_to": "q1"}, tr))
        self.assertTrue(ok({"no_sut_reply_to": "q2"}, tr))
        self.assertFalse(ok({"no_sut_reply_to": "q1"}, tr))
        self.assertFalse(ok({"replies_to": "q2"}, tr))
        # An extra unthreaded SUT message fails replies_to(_any).
        tr["messages"].append(msg("sut", 6, "part two"))
        self.assertFalse(ok({"replies_to": "q1"}, tr))
        self.assertFalse(ok({"replies_to_any": ["q1", "q2"]}, tr))
        self.assertFalse(ok({"sut_reply_count": {"max": 1}}, tr))
        # No SUT message at all is never "all of them reply".
        tr["messages"] = [q1, q2]
        self.assertFalse(ok({"replies_to": "q1"}, tr))
        self.assertFalse(ok({"replies_to_any": ["q1"]}, tr))

    def test_no_sut_before_and_duplicates(self):
        tr, q1, q2, r1 = basic()
        self.assertTrue(ok({"no_sut_before": "q1"}, tr))
        tr["messages"].insert(1, msg("sut", 0.5, "jumping in!"))
        self.assertFalse(ok({"no_sut_before": "q2"}, tr))
        tr, q1, q2, r1 = basic()
        tr["messages"].append(msg("sut", 5, "ack 1", reply_to=q2["id"]))
        self.assertTrue(ok({"no_duplicate_replies": True}, tr))
        tr["messages"].append(msg("sut", 9, "ack 1", reply_to=q1["id"]))
        self.assertFalse(ok({"no_duplicate_replies": True}, tr))

    def test_latency_uses_the_first_reply_to_the_label(self):
        tr, q1, q2, r1 = basic()
        self.assertTrue(ok({"latency": {"from": "q1", "max": 5}}, tr))
        self.assertFalse(ok({"latency": {"from": "q1", "max": 3}}, tr))
        self.assertFalse(ok({"latency": {"from": "q2", "max": 60}}, tr))
        tr["messages"].append(msg("sut", 50, "late", reply_to=q1["id"]))
        self.assertTrue(ok({"latency": {"from": "q1", "max": 5}}, tr))
        for m in tr["messages"]:  # no ts: the snowflake's time
            m.pop("ts")
        self.assertFalse(ok({"latency": {"from": "q1", "max": 3}}, tr))

    def test_thread_replies_count_like_channel_ones(self):
        q1 = msg("a", 0, "<@900> hi")
        r1 = msg("sut", 3, "ack 1", reply_to=q1["id"])
        q2 = msg("b", 5, "<@900> in the thread", thread="t1")
        r2 = msg("sut", 8, "ack 1", reply_to=q2["id"], thread="t1")
        q3 = msg("a", 9, "<@900> again", thread="t1")
        tr = transcript([q1, r1, q2, r2, q3], {
            "q1": q1["id"], "q2": q2["id"], "q3": q3["id"]})
        for chk in ({"sut_reply_count": {"eq": 2}},
                    {"replies_to_any": ["q1", "q2"]},
                    {"latency": {"from": "q2", "max": 5}},
                    {"no_sut_reply_to": "q3"}):
            self.assertTrue(ok(chk, tr), chk)
        self.assertFalse(ok({"replies_to": "q2"}, tr))


class Content(unittest.TestCase):
    def test_content_matches_all_or_per_label(self):
        tr, q1, q2, r1 = basic()
        self.assertTrue(ok({"content_matches": "^ack 1$"}, tr))
        self.assertFalse(ok({"content_matches": "^ack 3$"}, tr))
        tr["messages"].append(msg("sut", 5, "ack 3", reply_to=q2["id"]))
        self.assertTrue(ok({"content_matches": {"label": "q2",
                                                "pattern": "^ack 3$"}}, tr))
        self.assertFalse(ok({"content_matches": {"label": "q1",
                                                 "pattern": "^ack 3$"}}, tr))
        # No reply (to the label): nothing matched, so it fails.
        tr["messages"] = [q1, q2]
        self.assertFalse(ok({"content_matches": ".*"}, tr))
        self.assertFalse(ok({"content_matches": {"label": "q1",
                                                 "pattern": ".*"}}, tr))

    def test_lengths(self):
        tr, *_ = basic()
        tr["messages"].append(msg("sut", 5, "x" * 1795))
        self.assertTrue(ok({"max_chars": 2000}, tr))
        self.assertTrue(ok({"total_chars": {"min": 1700, "max": 1800}}, tr))
        self.assertFalse(ok({"total_chars": {"max": 1000}}, tr))
        tr["messages"].append(msg("sut", 6, "x" * 2001))
        self.assertFalse(ok({"max_chars": 2000}, tr))

    def test_mass_mentions_and_user_mentions(self):
        tr, q1, *_ = basic()
        tr["messages"].append(msg("sut", 5, "hey @​everyone <@&1>"))
        tr["messages"].append(msg("a", 6, "@everyone",
                                  mention_everyone=True))  # not the SUT
        self.assertTrue(ok({"no_mass_mentions": True}, tr))
        for bad in (msg("sut", 7, "hi", mention_everyone=True),
                    msg("sut", 7, "<@&1>", mention_roles=["1"]),
                    msg("sut", 7, "listen up @here")):
            t, *_ = basic()
            t["messages"].append(bad)
            self.assertFalse(ok({"no_mass_mentions": True}, t), bad)
        # mentions: the reply both writes <@id> and really pings it. A
        # reply ping of the author alone is not a mention.
        tr["messages"].append(msg("sut", 8, "ack 1", reply_to=q1["id"],
                                  mentions=["902"]))
        self.assertFalse(ok({"mentions": {"label": "q1", "user": "b"}}, tr))
        tr["messages"].append(msg("sut", 9, "hey <@901>",
                                  reply_to=q1["id"], mentions=["902"]))
        self.assertFalse(ok({"mentions": {"label": "q1", "user": "a"}}, tr))
        tr["messages"].append(msg("sut", 10, "hey <@!901>",
                                  reply_to=q1["id"], mentions=["901"]))
        self.assertTrue(ok({"mentions": {"label": "q1", "user": "a"}}, tr))


class SutFiles(unittest.TestCase):
    def test_quarantine_ledger_and_holds(self):
        tr, q1, q2, r1 = basic()
        self.assertFalse(ok({"quarantined": "q1"}, tr))  # no file
        tr["sut_files"]["quarantine"] = [
            {"message_id": "5"},
            {"message_id": "6", "fragments": [{"message_id": q2["id"]}]}]
        self.assertTrue(ok({"quarantined": "q2"}, tr))   # a fragment
        self.assertFalse(ok({"quarantined": "q1"}, tr))
        want = {"ledger_status": {"label": "q1", "status": "skipped"}}
        self.assertFalse(ok(want, tr))                   # no ledger
        tr["sut_files"]["ledger"] = {"entries": {}}
        self.assertFalse(ok(want, tr))                   # no entry
        tr["sut_files"]["ledger"]["entries"][q1["id"]] = {
            "status": "replied"}
        self.assertFalse(ok(want, tr))
        tr["sut_files"]["ledger"]["entries"][q1["id"]] = {
            "status": "skipped"}
        self.assertTrue(ok(want, tr))
        for holds in (None, {}, {"h": {"item": {"item": "7"}}}):
            tr["sut_files"]["holds"] = holds
            self.assertFalse(ok({"held": "q1"}, tr), holds)
        tr["sut_files"]["holds"] = {"h": {"item": {"item": "7",
                                                   "reply_to": q1["id"]}}}
        self.assertTrue(ok({"held": "q1"}, tr))

    def test_queue_item(self):
        tr, q1, q2, r1 = basic()
        chk = {"queue_item": {"label": "q2", "follow_up": True,
                              "settings": {"speaker": "owner"}}}
        tr["sut_files"]["worker_items"] = None
        self.assertFalse(ok(chk, tr))                    # not tier 1
        tr["sut_files"]["worker_items"] = [{"message_id": q1["id"]}]
        self.assertFalse(ok(chk, tr))                    # never queued
        self.assertTrue(ok({"queue_item": {"label": "q1",
                                           "follow_up": False}}, tr))
        tr["sut_files"]["worker_items"].append(
            {"message_id": "5", "fragment_ids": [q2["id"]],
             "follow_up": True, "settings": {"speaker": "member"}})
        self.assertFalse(ok(chk, tr))
        tr["sut_files"]["worker_items"].append(
            {"message_id": "5", "fragment_ids": [q2["id"]],
             "follow_up": True, "settings": {"speaker": "owner"}})
        self.assertTrue(ok(chk, tr))                     # newest wins

    def test_overlay_lacks(self):
        tr, *_ = basic()
        tr["sut_files"]["overlay"] = None                # nothing written
        self.assertTrue(ok({"overlay_lacks": "who.replies"}, tr))
        tr["sut_files"]["overlay"] = {"values": {
            'servers."1".channels."2".replies': {"value": "off"},
            'servers."1".style.length': {"value": "brief"}}}
        self.assertFalse(ok({"overlay_lacks": "who.replies"}, tr))
        self.assertTrue(ok({"overlay_lacks": "who.dms_from"}, tr))
        tr["sut_files"]["overlay"] = ["not", "a", "mapping"]
        self.assertFalse(ok({"overlay_lacks": "who.replies"}, tr))


class Log(unittest.TestCase):
    def test_log_checks_read_the_scenario_log(self):
        tr, *_ = basic()
        tr["monitor_log"] = "Queued mention (follow-up) from ua in #c\n"
        tr["monitor_log_tail"] = "older: Refused a settings action\n"
        self.assertTrue(ok({"log_contains": r"^Queued \w+ \(follow-up\)"},
                           tr))
        self.assertFalse(ok({"log_contains": "Refused"}, tr))
        self.assertTrue(ok({"log_lacks": "Refused"}, tr))
        self.assertFalse(ok({"log_lacks": r"\(follow-up\)"}, tr))
        tr["monitor_log"] = ""                           # tail fallback
        self.assertTrue(ok({"log_contains": "Refused"}, tr))

    def test_counts_reported_by_the_runner(self):
        tr, *_ = basic()
        self.assertFalse(ok({"monitor_restarts": {"eq": 0}}, tr))
        self.assertFalse(ok({"converse_turns": {"max": 5}}, tr))
        tr.update(monitor_restarts=1, converse_turns=8)
        self.assertFalse(ok({"monitor_restarts": {"eq": 0}}, tr))
        self.assertFalse(ok({"converse_turns": {"max": 5}}, tr))
        tr.update(monitor_restarts=0, converse_turns=3)
        self.assertTrue(ok({"monitor_restarts": {"eq": 0}}, tr))
        self.assertTrue(ok({"converse_turns": {"max": 5}}, tr))


if __name__ == "__main__":
    unittest.main()
