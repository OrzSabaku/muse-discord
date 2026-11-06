#!/usr/bin/env python3
"""Start Discord presence from the source package."""
from pathlib import Path
import runpy
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
if __name__ == '__main__':
    runpy.run_module('muse_discord.presence', run_name='__main__')
