#!/usr/bin/env python3
"""Optional MCP stdio server; run ``uv sync --group mcp`` first."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main():
    try:
        from muse_discord.discord_tools.mcp_server import create_server
    except ImportError:
        print('MCP SDK missing: use Python 3.11+ and run "uv sync --group mcp".', file=sys.stderr)
        return 1
    create_server().run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
