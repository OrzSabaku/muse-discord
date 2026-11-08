"""Launch options reach initialization before monitor workers start."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "_lib"))
import common  # noqa: F401 (select the source tree under test)

from muse_discord import monitor

ROOT = Path(os.environ.get("MUT_DIR", Path(__file__).resolve().parents[2]))


class LaunchCaptured(Exception):
    pass


class MonitorCLI(unittest.TestCase):
    def test_config_and_transient_token_reach_initialization_in_either_order(self):
        for args in (
            ["transient-token", "--config", "chosen.yaml"],
            ["--config", "chosen.yaml", "transient-token"],
        ):
            with (
                self.subTest(args=args),
                patch.object(sys, "argv", ["monitor.py", *args]),
                patch.object(monitor, "_INITIALIZED", False),
                patch.object(monitor, "initialize") as initialize,
                patch.object(monitor, "_start_monitor", side_effect=LaunchCaptured),
            ):
                with self.assertRaises(LaunchCaptured):
                    monitor.main()
                initialize.assert_called_once_with("chosen.yaml", token_override="transient-token")

    def test_help_works_without_configuration_or_runtime_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                [sys.executable, str(ROOT / "bin/monitor.py"), "--help"],
                cwd=temporary,
                env=dict(os.environ, MUSE_DISCORD_CONFIG="missing.yaml"),
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("--config", result.stdout)
            self.assertEqual(list(Path(temporary).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
