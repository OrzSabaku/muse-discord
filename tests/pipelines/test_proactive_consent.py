"""Consent can be restored and enforced without a running monitor."""

import contextlib
import io
import os
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "_lib"))
import common  # noqa: F401 (select the source tree under test)

from muse_discord.proactive.consent import ConsentRequests
from muse_discord.proactive.store import ProactiveStore
from muse_discord.storage import atomic_write


class ConsentRecovery(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = os.path.join(temporary.name, "proactive.json")
        self.now = 1000
        self.sender = Mock()
        self.store, self.consent = self.services()
        self.store.data()["opted_out"]["100"] = self.now
        with contextlib.redirect_stdout(io.StringIO()):
            self.consent.ask("100", "200", "in")
            self.consent.send_pending(self.now)
        self.token = next(iter(self.store.data()["asks"]))

    def services(self):
        store = ProactiveStore(
            lambda: self.path,
            lambda: self.path + ".nonce",
            lambda path, body: atomic_write(path, body, 0o600),
            lambda: self.now,
        )
        consent = ConsentRequests(store, Mock(), self.sender, lambda: False, lambda: self.now)
        return store, consent

    def test_two_simultaneous_clicks_enqueue_only_one_authorized_change(self):
        records = []
        response = self.consent.on_button({}, "yes:" + self.token, "300", records.append)
        self.assertEqual(response["type"], 4)
        self.assertEqual(records, [])
        barrier = threading.Barrier(3)

        def click():
            barrier.wait(timeout=5)
            self.consent.on_button({}, "yes:" + self.token, "100", records.append)

        threads = [threading.Thread(target=click) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(records, [{"kind": "proactive_yes", "token": self.token, "by": "100"}])
        self.assertIn("100", self.store.data()["opted_out"])
        with contextlib.redirect_stdout(io.StringIO()):
            self.consent.apply_button(records[0])
        self.assertNotIn("100", self.store.data()["opted_out"])

    def test_restarted_consent_recovers_sent_buttons_and_rechecks_recipient_and_expiry(self):
        store, consent = self.services()
        self.assertIn("100", store.data()["opted_out"])
        records = []
        consent.on_button({}, "yes:" + self.token, "100", records.append)
        self.assertEqual(len(records), 1)
        forged = dict(records[0], by="300")
        consent.apply_button(forged)
        self.assertIn("100", store.data()["opted_out"])
        self.now += 86400
        consent.apply_button(records[0])
        self.assertIn("100", store.data()["opted_out"])
        self.assertFalse(store.data()["asks"][self.token].get("done"))
        self.sender.assert_called_once()


if __name__ == "__main__":
    unittest.main()
