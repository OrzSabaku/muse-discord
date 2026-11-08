"""Protocol boundaries, not Pydantic internals: reject spoofed metadata and
invalid mixtures before the monitor mutates watches or consumes credentials."""

import copy
import os
import sys
import unittest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_lib")
)
import common  # noqa: F401

from muse_discord import muse_watches as W


def item(change):
    return {"item": "123456789012345678", "text": "OK", "schedule": {"v": 1, "changes": [change]}}


class WatchSchema(unittest.TestCase):
    def test_all_protocol_actions_and_reports(self):
        changes = [
            {"op": "add", "kind": "remind", "at": "2027-01-01T00:00:00Z"},
            {"op": "add", "kind": "remind", "after": 60},
            {
                "op": "add",
                "kind": "watch",
                "match": {"words": ["hello"], "in": "server:123", "every": True},
            },
            {
                "op": "add",
                "kind": "watch",
                "check": "web",
                "what": "a release",
                "every": W.WEB_EVERY_MIN,
            },
            {"op": "cancel", "id": "wabcde"},
            {"op": "cancel", "all": True},
            {"op": "tz", "tz": "Europe/Berlin"},
            {"op": "tz", "tz": None},
        ]
        for change in changes:
            value = item(change)
            before = copy.deepcopy(value)
            self.assertEqual(W.shape_problems(value), [], change)
            self.assertEqual(value, before)
        report = {
            "id": "wabcde",
            "nonce": "a" * 32,
            "result": "met",
            "summary": "yes\nnow",
            "at": 123.5,
        }
        self.assertEqual(W.report_problems(report), [])

    def test_extra_author_place_and_action_mixtures_are_rejected(self):
        valid = {"op": "add", "kind": "remind", "after": 60}
        for field in ("requester", "channel_id", "guild_id", "owner", "match", "until", "check"):
            self.assertTrue(W.shape_problems(item(dict(valid, **{field: "forged"}))), field)
        for extra in (
            {"at": "2027-01-01T00:00:00Z"},
            {"after": None},
            {"after": True},
            {"after": "60"},
        ):
            self.assertTrue(W.shape_problems(item(dict(valid, **extra))))
        for field in W.NOT_ON_SCHEDULE_ITEMS:
            value = item(valid)
            value[field] = None
            self.assertTrue(W.shape_problems(value))
        self.assertTrue(W.shape_problems(item({"op": "cancel", "all": 1})))
        self.assertTrue(W.shape_problems(item({"op": "cancel", "all": False})))
        self.assertTrue(W.shape_problems(item({"op": "cancel", "all": True, "id": "wabcde"})))

    def test_time_formats_words_and_strict_version(self):
        for at in (
            "2027-01-01T00:00:00",
            "2027-01-01T00:00:00+01:00",
            "2101-01-01T00:00:00Z",
            None,
        ):
            self.assertTrue(W.shape_problems(item({"op": "add", "kind": "remind", "at": at})))
        watch = {"op": "add", "kind": "watch", "match": {"words": ["hello"], "in": "here"}}
        for words in ([], ["x"], ["a\nb"], [True], ["hello"] * (W.WORDS_MAX + 1)):
            bad = copy.deepcopy(watch)
            bad["match"]["words"] = words
            self.assertTrue(W.shape_problems(item(bad)))
        for version in (True, 1.0, "1", 2):
            bad = item({"op": "tz", "tz": None})
            bad["schedule"]["v"] = version
            self.assertTrue(W.shape_problems(bad))
        bad["schedule"] = {"v": 1, "changes": []}
        self.assertTrue(W.shape_problems(bad))

    def test_invalid_discriminators_and_errors_do_not_echo_input(self):
        secret = "secret-token-should-not-be-printed"
        for action in (
            {"op": secret},
            {"op": []},
            {"op": "add", "kind": {}},
            {"op": "tz", "tz": None, secret: 1},
        ):
            errors = W.shape_problems(item(action))
            self.assertTrue(errors)
            self.assertNotIn(secret, "\n".join(errors))

    def test_reports_reject_spoofed_fields_and_control_characters(self):
        report = {"id": "wabcde", "nonce": "a" * 32, "result": "met", "summary": "yes"}
        for extra in (
            {"nonce": "x"},
            {"id": "another"},
            {"result": True},
            {"summary": "\tcontrol"},
            {"summary": "x" * (W.SUMMARY_MAX + 1)},
            {"owner": True},
        ):
            self.assertTrue(W.report_problems(dict(report, **extra)))

    def test_optional_deadlines_allow_omission_but_not_null(self):
        changes = [
            {"op": "add", "kind": "watch", "match": {"words": ["hello"], "in": "here"}},
            {
                "op": "add",
                "kind": "watch",
                "check": "web",
                "what": "a release",
                "every": W.WEB_EVERY_MIN,
            },
        ]
        for change in changes:
            with self.subTest(change=change):
                self.assertEqual(W.shape_problems(item(change)), [])
                self.assertTrue(W.shape_problems(item(dict(change, until=None))))
                self.assertEqual(
                    W.shape_problems(item(dict(change, until="2027-01-01T00:00:00Z"))), []
                )

    def test_report_timestamp_allows_omission_but_not_null(self):
        report = {"id": "wabcde", "nonce": "a" * 32, "result": "met", "summary": "yes"}
        self.assertEqual(W.report_problems(report), [])
        self.assertEqual(W.report_problems(dict(report, at=0)), [])
        self.assertTrue(W.report_problems(dict(report, at=None)))


if __name__ == "__main__":
    unittest.main()
