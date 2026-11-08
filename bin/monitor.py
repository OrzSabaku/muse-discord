#!/usr/bin/env python3
"""Start the Discord monitor from the source package."""

import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
if __name__ == "__main__":
    runpy.run_module("muse_discord.monitor", run_name="__main__")
