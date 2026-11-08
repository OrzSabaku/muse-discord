"""Select the installed project's Python before commands import package services."""

import os
import sys

PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(PKG_DIR, "src")

# The package's .venv counts only once bin/install.sh has checked the
# packages import in it (it then writes this marker).
VENV_PYTHON = os.path.join(PKG_DIR, ".venv", "bin", "python3")
VENV_OK = os.path.join(PKG_DIR, ".venv", ".muse-ok")


def venv_python():
    """.venv/bin/python3 when bin/install.sh set it up, else None."""
    if os.access(VENV_PYTHON, os.X_OK) and os.path.exists(VENV_OK):
        return VENV_PYTHON
    return None


# Always use the installed project's interpreter, even when the calling
# Python happens to have compatible packages. A matching prefix prevents loops.
_venv = venv_python()
if _venv and os.path.realpath(sys.prefix) != os.path.realpath(os.path.join(PKG_DIR, ".venv")):
    os.execv(_venv, [_venv, *sys.argv])


def ensure_src_on_path():
    if SRC_DIR not in sys.path:
        sys.path.insert(0, SRC_DIR)
