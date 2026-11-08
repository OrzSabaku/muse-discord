"""Telemetry core has no configuration dependency and keeps its private identity stable."""

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "_lib"))
import common  # noqa: F401 (select the source tree under test)

from muse_discord import telemetry as T


class Components(unittest.TestCase):
    def test_core_import_needs_no_configuration_or_discord_library(self):
        for dependency in ("pydantic", "yaml", "discord"):
            self.assertNotIn(dependency, sys.modules)

    def test_installation_identity_survives_subsequent_reads_and_stays_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "telemetry_id"
            identity = T.reset_install_id(path)
            self.assertEqual(T.read_install_id(path), identity)
            self.assertEqual(T.read_install_id(path, create=False), identity)
            self.assertEqual(path.read_text(), identity + "\n")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_failed_identity_replacement_keeps_original_and_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "telemetry_id"
            identity = T.reset_install_id(path)
            with (
                patch("muse_discord.storage.os.replace", side_effect=OSError("write refused")),
                self.assertRaises(OSError),
            ):
                T.reset_install_id(path)
            self.assertEqual(T.read_install_id(path), identity)
            self.assertEqual(os.listdir(directory), ["telemetry_id"])


if __name__ == "__main__":
    unittest.main()
