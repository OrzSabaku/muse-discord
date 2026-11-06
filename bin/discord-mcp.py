#!/usr/bin/env python3
"""Optional MCP stdio server; install requirements-mcp.txt first."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    try:
        from discord_tools.mcp_server import create_server
    except ImportError:
        print('MCP SDK missing: use Python 3.11+ and install requirements-mcp.txt.',
              file=sys.stderr)
        return 1
    create_server().run(transport='stdio')
    return 0


if __name__ == '__main__':
    sys.exit(main())
