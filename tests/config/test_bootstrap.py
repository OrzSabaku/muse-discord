"""Importing the monitor is inert; explicit initialization loads its settings."""

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(os.environ.get("MUT_DIR", Path(__file__).resolve().parents[2]))


class Bootstrap(unittest.TestCase):
    def probe(self, code):
        with tempfile.TemporaryDirectory(prefix="muse-import-") as tmp:
            result = subprocess.run(
                [sys.executable, "-c", textwrap.dedent(code)],
                cwd=tmp,
                env=dict(
                    os.environ, PYTHONPATH=str(ROOT / "src"), MUSE_DISCORD_CONFIG="missing.yaml"
                ),
                capture_output=True,
                text=True,
                timeout=15,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_import_needs_no_configuration_token_network_or_threads(self):
        out = self.probe("""
            import os, socket, threading
            from unittest.mock import patch
            from muse_discord import muse_settings
            with (patch.object(muse_settings, "Watcher", side_effect=AssertionError("config read")),
                  patch.object(threading.Thread, "start",
                      side_effect=AssertionError("thread started")),
                  patch.object(socket.socket, "connect", side_effect=AssertionError("network"))):
                from muse_discord import monitor
            assert not monitor._INITIALIZED
            assert monitor.TOKEN == ""
            assert os.listdir() == []
            try:
                monitor.settings()
            except RuntimeError:
                pass
            else:
                raise AssertionError("uninitialized runtime was accepted")
        """)
        self.assertEqual(out, "")

    def test_initialization_ignores_the_callers_arguments(self):
        self.probe("""
            import os, sys
            from pathlib import Path
            from muse_discord import monitor
            Path("chosen.yaml").write_text("token_from: env:MUSE_BOOTSTRAP_TOKEN\\n")
            os.environ["MUSE_BOOTSTRAP_TOKEN"] = "configured-token"
            sys.argv = ["foreign-program", "--unrelated", "anything"]
            monitor.initialize("chosen.yaml")
            assert monitor.TOKEN == "configured-token"
        """)

    def test_explicit_initialization_uses_supplied_config_and_token(self):
        self.probe("""
            from pathlib import Path
            from muse_discord import monitor
            Path("chosen.yaml").write_text("engine:\\n  coalesce_seconds: 0\\n")
            monitor.initialize("chosen.yaml", token_override="supplied-token")
            assert monitor.TOKEN == "supplied-token"
            assert monitor._SETTINGS.engine.coalesce_seconds == 0
            assert monitor.settings().engine.coalesce_seconds == 0
            try:
                monitor.initialize("chosen.yaml", token_override="different-token")
            except RuntimeError:
                pass
            else:
                raise AssertionError("a second runtime was silently installed")
        """)


if __name__ == "__main__":
    unittest.main()
