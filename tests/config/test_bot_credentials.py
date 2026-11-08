"""All entrypoints use ordered nonempty credentials from the target project."""

import contextlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "_lib"))
import common  # noqa: F401 (select the source tree under test)

from muse_discord.bot_credentials import read_token, token_file, token_path
from muse_discord.discord_tools.session import ToolSession


class CredentialSources(unittest.TestCase):
    def test_order_skips_empty_and_unavailable_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "empty").write_text(" \n")
            (root / "fallback").write_text(" file-value\n")
            sources = [("env", "MUSE_CREDENTIAL_EMPTY"), ("file", "missing"), ("file", "empty")]
            with patch.dict(
                os.environ,
                {"MUSE_CREDENTIAL_EMPTY": "  ", "MUSE_CREDENTIAL_VALUE": " env-value\n"},
            ):
                self.assertEqual(
                    read_token(
                        [*sources, ("env", "MUSE_CREDENTIAL_VALUE"), ("file", "fallback")], root
                    ),
                    "env-value",
                )
                self.assertEqual(read_token([*sources, ("file", "fallback")], root), "file-value")

    def test_cli_relative_path_ignores_a_different_working_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "install"
            other = Path(temporary) / "elsewhere"
            root.mkdir()
            other.mkdir()
            (root / "credential").write_text("installation-value")
            (other / "credential").write_text("wrong-installation")
            sources = [("file", "credential")]
            settings = SimpleNamespace(token_sources=lambda: sources)
            session = ToolSession(str(root))
            session.settings = Mock(return_value=settings)
            with contextlib.chdir(other):
                self.assertEqual(session.load_token(), "installation-value")
                self.assertEqual(read_token(sources, root), "installation-value")

    def test_monitor_argument_keeps_priority_over_configured_sources(self):
        from muse_discord import monitor

        settings = SimpleNamespace(token_sources=lambda: [("env", "MUSE_CREDENTIAL_VALUE")])
        with patch.dict(os.environ, {"MUSE_CREDENTIAL_VALUE": "configured"}):
            with patch.object(sys, "argv", ["monitor.py", "transient"]):
                self.assertEqual(monitor._load_token(settings), "transient")
            with patch.object(sys, "argv", ["monitor.py", "--config", "config.yaml"]):
                self.assertEqual(monitor._load_token(settings), "configured")

    def test_missing_sources_return_no_value_and_cli_reports_its_own_failure(self):
        with tempfile.TemporaryDirectory() as root:
            sources = [("file", "missing")]
            self.assertIsNone(read_token(sources, root))
            session = ToolSession(root)
            session.settings = Mock(return_value=SimpleNamespace(token_sources=lambda: sources))
            with self.assertRaisesRegex(SystemExit, "No bot token found"):
                session.load_token()
            self.assertEqual(session.token, "")

    def test_configured_output_file_and_home_expansion_share_path_resolution(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"HOME": root}):
            self.assertEqual(token_path("~/credential", "/another"), str(Path(root) / "credential"))
            self.assertEqual(
                token_file([("env", "NAME"), ("file", "credential"), ("file", "backup")], root),
                str(Path(root) / "credential"),
            )
            self.assertIsNone(token_file([("env", "NAME")], root))


if __name__ == "__main__":
    unittest.main()
