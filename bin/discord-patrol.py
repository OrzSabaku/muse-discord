#!/usr/bin/env python3
"""Run a saved, channel-scoped patrol without a Gateway monitor."""

import sys
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_DIR / "src"))
from muse_discord.discord_tools.patrol import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(str(PACKAGE_DIR), sys.argv))
