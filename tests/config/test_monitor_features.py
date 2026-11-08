"""Optional feature failures leave safe inactive services and private diagnostics."""

import contextlib
import io
import os
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "_lib"))
import common  # noqa: F401 (select the source tree under test)

from muse_discord.monitor_features import load_proactive, load_telemetry


class OptionalFeatures(unittest.TestCase):
    def test_failed_proactive_initialization_stays_inactive_and_hides_exception_details(self):
        context = Mock(side_effect=ValueError("private configuration contents"))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            service = load_proactive(context)
        self.assertIn("ValueError", output.getvalue())
        self.assertNotIn("private configuration", output.getvalue())
        self.assertIsNone(service.tick({}))
        self.assertIsNone(service.delivery.names_slot({}))
        self.assertIsNone(service.consent.apply_button({}))

    def test_missing_telemetry_dependency_leaves_notices_and_worker_hooks_inactive(self):
        with (
            patch.dict(sys.modules, {"muse_discord.usage_stats.settings_metrics": None}),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            service = load_telemetry(lambda: ("stats", "identity", "wakes"), ".")
        self.assertEqual(service.take_notice(Mock()), "")
        self.assertEqual(service.start(), "")
        self.assertEqual(service.crashed(RuntimeError("private")), "")


if __name__ == "__main__":
    unittest.main()
