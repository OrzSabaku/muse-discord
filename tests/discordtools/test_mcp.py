"""Optional SDK round trips, stdio wire behavior, and media image blocks."""

import base64
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(os.environ.get("MUT_DIR", Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(ROOT / "src"))
HAVE_MCP = importlib.util.find_spec("mcp") is not None
if HAVE_MCP:
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters
    from muse_discord.discord_tools.mcp_server import create_server


@unittest.skipUnless(HAVE_MCP, "Run uv sync --group mcp to run SDK integration tests")
class MCP(unittest.IsolatedAsyncioTestCase):
    async def test_in_process_schema_and_cli_mapping(self):
        with patch(
            "muse_discord.discord_tools.mcp_server.invoke", new_callable=AsyncMock
        ) as invoke:
            invoke.return_value = {"messages": [], "untrusted": True}
            async with Client(create_server()) as client:
                tools = await client.list_tools()
                self.assertEqual(len(tools.tools), 10)
                response = await client.call_tool(
                    "list_unread",
                    {
                        "for_id": "owner-item",
                        "nonce": "nonce",
                        "channel_id": "channel",
                        "limit": 20,
                    },
                )
                self.assertFalse(response.is_error)
                invoke.assert_awaited_once_with("list-unread", "owner-item", "nonce", "channel", 20)
                invoke.reset_mock()
                error = await client.call_tool(
                    "send_message", {"channel_id": "channel", "content": "hi"}
                )
                self.assertTrue(error.is_error)
                invoke.assert_not_awaited()

    async def test_stdio_handshake_and_unauthorized_call_returns_tool_error(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = Path(directory) / "config.yaml"
            cfg.write_text(
                'version: 1\nowners: ["100000000000000001"]\ntoken_from: env:MUSE_MCP_TEST_TOKEN\n'
            )
            env = {
                **os.environ,
                "MUSE_DISCORD_CONFIG": str(cfg),
                "MUSE_MCP_TEST_TOKEN": "test-token",
            }
            params = StdioServerParameters(
                command=sys.executable, args=[str(ROOT / "bin/discord-mcp.py")], env=env
            )
            async with Client(params, read_timeout_seconds=15) as client:
                tools = await client.list_tools()
                self.assertIn("reply_message", [tool.name for tool in tools.tools])
                result = await client.call_tool(
                    "list_unread",
                    {
                        "for_id": "300000000000000001",
                        "nonce": "invalid",
                        "channel_id": "200000000000000001",
                    },
                )
                self.assertTrue(result.is_error)
                self.assertNotIn("test-token", str(result))

    async def test_media_result_has_text_and_inline_image_and_rejects_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "media/on-demand/x.png"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"PNG")
            with (
                patch("muse_discord.discord_tools.mcp_server.ROOT", root),
                patch(
                    "muse_discord.discord_tools.mcp_server.invoke", new_callable=AsyncMock
                ) as invoke,
            ):
                invoke.return_value = {"path": str(target), "bytes": 3, "content_type": "image/png"}
                async with Client(create_server()) as client:
                    args = {"for_id": "i", "nonce": "n", "channel_id": "c", "emoji": "😀"}
                    result = await client.call_tool("get_emoji", args)
                    self.assertFalse(result.is_error)
                    self.assertEqual([c.type for c in result.content], ["text", "image"])
                    self.assertEqual(base64.b64decode(result.content[1].data), b"PNG")
                    invoke.return_value["path"] = str(root / "secret")
                    error = await client.call_tool("get_emoji", args)
                    self.assertTrue(error.is_error)


if __name__ == "__main__":
    unittest.main()
