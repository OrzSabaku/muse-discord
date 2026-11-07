"""Setup tools must use the project interpreter, even from another Python."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
import venv

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "_lib"))
from common import MUT_DIR, scratch


class ProjectPython(unittest.TestCase):
    def test_setup_tool_switches_to_project_python_without_reexec_loop(self):
        package = Path(scratch("muse-python-"))
        tools = package / "bin"
        tools.mkdir()
        shutil.copy(Path(MUT_DIR) / "bin/setup_common.py", tools)
        # A dependency-free venv is sufficient: interpreter selection must
        # happen before probing whether the invoking Python has packages.
        target = package / ".venv"
        venv.EnvBuilder(with_pip=False).create(target)
        (target / ".muse-ok").touch()
        probe = tools / "probe.py"
        probe.write_text("import setup_common\nimport sys\nprint(sys.prefix)\n")
        result = subprocess.run([sys.executable, str(probe)],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()).resolve(), target.resolve())


if __name__ == "__main__":
    unittest.main()
