"""Lifetime resources work without booting a monitor or connecting to Discord."""

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "_lib"))
import common  # noqa: F401 (select the source tree under test)

from muse_discord.monitor_process import MonitorLock


class MonitorProcess(unittest.TestCase):
    def test_second_instance_cannot_acquire_and_failed_attempt_closes_its_descriptor(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "monitor.pid")
            first, second = MonitorLock(lambda: path), MonitorLock(lambda: path)
            self.addCleanup(first.close)
            self.addCleanup(second.close)
            first.acquire()
            first.acquire()  # repeated startup cannot replace the held descriptor
            descriptors = len(os.listdir("/proc/self/fd"))
            with (
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaises(SystemExit) as stopped,
            ):
                second.acquire()
            self.assertEqual(stopped.exception.code, 0)
            self.assertEqual(len(os.listdir("/proc/self/fd")), descriptors)
            first.close()
            second.acquire()
            self.assertEqual(Path(path).read_text(), str(os.getpid()))

    def test_live_legacy_monitor_without_flock_is_rejected_without_overwriting_its_pid(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import os,sys; print(os.getpid(),flush=True); sys.stdin.read()",
                    "monitor.py",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
            ) as peer,
        ):
            try:
                pid = peer.stdout.readline().strip()
                self.assertEqual(pid, str(peer.pid))
                path = os.path.join(directory, "monitor.pid")
                Path(path).write_text(pid)
                lock = MonitorLock(lambda: path)
                self.addCleanup(lock.close)
                with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                    lock.acquire()
                self.assertEqual(Path(path).read_text(), pid)
            finally:
                peer.terminate()
                peer.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
