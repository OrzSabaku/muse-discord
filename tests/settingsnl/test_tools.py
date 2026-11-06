"""bin/settings.py places, offline: the place ids a worker may name in
a settings request, listed from directory.json. (bin/outbox-append.py
is tested in tests/locking/test_outbox_append.py.)

Usage: [MUT_DIR=<tree>] python3 tests/settingsnl/test_tools.py"""
import json
import os
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "_lib"))
from common import MUT_DIR, scratch  # noqa: E402

G1, CH, CH2, CAT = ("500000000000000001", "701000000000000001",
                    "702000000000000001", "800000000000000001")
G2, CH_G2 = "600000000000000001", "704000000000000001"
DIRECTORY = {
    "updated": 0,
    "guilds": [{"id": G1, "name": "Work", "member_count": 120},
               {"id": G2, "name": "Friends", "member_count": None}],
    "channels": {
        G1: [{"id": CAT, "name": "Staff", "type": 4, "parent_id": None},
             {"id": CH, "name": "general", "type": 0, "parent_id": CAT},
             {"id": CH2, "name": "random", "type": 0, "parent_id": None},
             {"id": "705000000000000001", "name": "Lobby", "type": 2,
              "parent_id": None}],
        G2: [{"id": CH_G2, "name": "general", "type": 0,
              "parent_id": None}]},
}


class Places(unittest.TestCase):
    def setUp(self):
        self.d = scratch("muse-nl-places-")
        self.cfg = os.path.join(self.d, "config.yaml")
        with open(self.cfg, "w") as f:
            f.write("version: 1\nowners: [\"111000000000000001\"]\n"
                    "token_from: env:MUSE_TEST_TOKEN\n")

    def directory(self, age=0):
        with open(os.path.join(self.d, "directory.json"), "w") as f:
            json.dump(dict(DIRECTORY, updated=int(time.time() - age)), f)

    def cli(self, *args):
        r = subprocess.run([sys.executable,
                            os.path.join(MUT_DIR, "bin", "settings.py"),
                            "--config", self.cfg] + list(args),
                           capture_output=True, text=True, timeout=60)
        return r.returncode, r.stdout, r.stderr

    def test_places_lists_every_place_id(self):
        """The worker picks place ids from `bin/settings.py places`:
        every server and text channel in directory.json, no category or
        voice channel."""
        self.directory()
        rc, out, err = self.cli("places")
        self.assertEqual(rc, 0, err)
        ids = [ln.split()[0] for ln in out.splitlines() if ln.strip()]
        self.assertEqual(ids, [
            "defaults", "dms", f"server:{G1}", f"channel:{G1}:{CH}",
            f"channel:{G1}:{CH2}", f"server:{G2}", f"channel:{G2}:{CH_G2}"])


if __name__ == "__main__":
    unittest.main()
