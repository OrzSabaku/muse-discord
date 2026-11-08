"""Replacing a saved credential must preserve it when publication fails."""

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "_lib"))
import common  # noqa: F401 (select the source tree under test)

from muse_discord.setup.files import write_private


class PrivateCredential(unittest.TestCase):
    def test_failed_replacement_keeps_original_and_leaves_no_temporary_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "credentials"
            token = directory / "bot_token"
            write_private(str(token), "previous credential\n")
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(token.stat().st_mode), 0o600)
            with (
                patch("muse_discord.storage.os.replace", side_effect=OSError("replace failed")),
                self.assertRaises(OSError),
            ):
                write_private(str(token), "replacement credential\n")
            self.assertEqual(token.read_text(), "previous credential\n")
            self.assertEqual(list(directory.iterdir()), [token])
            self.assertEqual(stat.S_IMODE(token.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
