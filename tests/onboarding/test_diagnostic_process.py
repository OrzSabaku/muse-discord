"""Health checks recognize monitor processes in this installation only."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "_lib"))
import common  # noqa: F401 (select the source tree under test)

from muse_discord.diagnostics.context import Doctor


@unittest.skipUnless(Path("/proc/self/cmdline").exists(), "Linux process inspection required")
class MonitorDetection(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "bin").mkdir()
        (self.root / "bin/monitor.py").write_text(
            "import time\nprint('ready', flush=True)\ntime.sleep(60)\n"
        )
        self.process = subprocess.Popen(
            ["python3", "bin/monitor.py"],
            executable=sys.executable,
            cwd=self.root,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self.stop)
        self.assertEqual(self.process.stdout.readline(), "ready\n")

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
        self.process.wait(timeout=5)
        self.process.stdout.close()

    def test_reads_the_pidfile_written_by_the_source_package_monitor(self):
        pidfile = self.root / "src/muse_discord/monitor.pid"
        pidfile.parent.mkdir(parents=True)
        pidfile.write_text(str(self.process.pid))
        self.assertEqual(Doctor(str(self.root), True).monitor_pid(), self.process.pid)

    @unittest.skipUnless(shutil.which("pgrep"), "pgrep required for fallback discovery")
    def test_finds_the_watchdog_bin_entrypoint_without_a_pidfile(self):
        self.assertEqual(Doctor(str(self.root), True).monitor_pid(), self.process.pid)

    def test_a_pidfile_cannot_point_to_another_installation(self):
        other = self.root / "another-install"
        other.mkdir()
        (other / "monitor.pid").write_text(str(self.process.pid))
        self.assertIsNone(Doctor(str(other), True).monitor_pid())


if __name__ == "__main__":
    unittest.main()
