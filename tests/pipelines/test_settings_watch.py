"""Reload handoff keeps authorization changes on the main thread."""

import contextlib
import io
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "_lib"))
import common  # noqa: F401 (select the source tree under test)

from muse_discord.muse_settings import Watcher
from muse_discord.settings_watch import LiveSettings
from muse_discord.storage import atomic_write_text


class ReloadHandoff(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = os.path.join(temporary.name, "config.yaml")
        self.write("100")
        self.config = LiveSettings(Watcher(self.path))

    def write(self, owner, extra=""):
        atomic_write_text(self.path, f'version: 1\nowners: ["{owner}"]\n' + extra)

    def test_interaction_reloads_coalesce_without_changing_main_thread_owner_state(self):
        changes = []
        self.config.on_change = lambda before, after: changes.append(
            (threading.current_thread().name, before.owners, after.owners)
        )
        observed = []

        def worker():
            for owner in ("200", "300"):
                self.write(owner)
                observed.append((self.config.read().owners, set(self.config.owners)))

        thread = threading.Thread(target=worker, name="interactions")
        thread.start()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(observed, [(["200"], {"100"}), (["300"], {"100"})])
        self.assertEqual(changes, [])
        with contextlib.redirect_stdout(io.StringIO()):
            self.config.read()
            self.config.read()
        self.assertEqual(self.config.owners, {"300"})
        self.assertEqual(changes, [("MainThread", ["100"], ["300"])])

    def test_invalid_edit_retains_snapshot_and_reports_once_then_recovers(self):
        initial = self.config.read()
        self.write("200", "everywhere:\n  style:\n    length: impossible\n")
        log = io.StringIO()
        with contextlib.redirect_stdout(log):
            self.assertIs(self.config.read(), initial)
            self.assertIs(self.config.read(), initial)
        self.assertEqual(log.getvalue().count("Config re-read failed"), 1)
        self.assertEqual(self.config.owners, {"100"})
        self.write("300")
        with contextlib.redirect_stdout(io.StringIO()):
            self.config.read()
        self.assertEqual(self.config.owners, {"300"})

    def test_engine_changes_do_not_partially_reconfigure_a_running_monitor(self):
        self.write("100", "engine:\n  max_reply_chars: 10\n")
        with contextlib.redirect_stdout(io.StringIO()):
            latest = self.config.read()
        self.assertEqual(latest.engine.max_reply_chars, 10)
        self.assertEqual(self.config.initial.engine.max_reply_chars, 1800)


if __name__ == "__main__":
    unittest.main()
