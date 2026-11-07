"""Fake MCP app for transport tests. Never connects to an external service."""

import os
from mcp.server import MCPServer

server = MCPServer("external-app-fixture")


@server.tool()
def list_events(calendar_id: str) -> dict:
    """Return fixture data and credential-isolation flags."""
    return {
        "calendar_id": calendar_id,
        "app_credential_received": os.environ.get("APP_TOKEN") == "fixture-app-token",
        "bot_token_received": "DISCORD_BOT_TOKEN" in os.environ,
        "other_secret_received": "UNRELATED_SECRET" in os.environ,
    }


if __name__ == "__main__":
    server.run(transport="stdio")
