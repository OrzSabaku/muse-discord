"""Actual stdio app invocation and MCP facade mapping, using the optional SDK."""

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import yaml

ROOT = Path(os.environ.get("MUT_DIR", Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(ROOT / "src"))
from muse_discord.external_apps.broker import Broker  # noqa: E402 (local import path bootstrap)
from muse_discord.external_apps.policy import Refused  # noqa: E402 (local import path bootstrap)

HAVE_MCP = importlib.util.find_spec("mcp") is not None
if HAVE_MCP:
    from mcp import Client
    from muse_discord.discord_tools.mcp_server import create_server

OWNER = "100000000000000001"
ITEM = "300000000000000001"
CHANNEL = "200000000000000001"
NONCE = "a" * 32


@unittest.skipUnless(HAVE_MCP, "Optional MCP SDK")
class AppTransport(unittest.TestCase):
    def test_broker_invokes_real_stdio_app_without_unrelated_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.yaml").write_text(
                yaml.safe_dump(
                    {
                        "version": 1,
                        "owners": [OWNER],
                        "everywhere": {"abilities": {"external_apps": "read-only"}},
                    }
                )
            )
            policy = {
                "version": 1,
                "apps": {
                    "calendar": {
                        "level": "read-only",
                        "command": [
                            sys.executable,
                            str(ROOT / "tests/externalapps/fixtures/app_server.py"),
                        ],
                        "env": {"APP_TOKEN": "FIXTURE_APP_TOKEN"},
                        "tools": {
                            "list_events": {
                                "effect": "read",
                                "constraints": {"calendar_id": ["personal"]},
                            }
                        },
                    }
                },
            }
            (root / "external-apps.yaml").write_text(yaml.safe_dump(policy))
            (root / "owner_dm_nonces.json").write_text(json.dumps({ITEM: NONCE}))
            (root / "reply_queue.json").write_text(
                json.dumps(
                    [
                        {
                            "message_id": ITEM,
                            "kind": "dm",
                            "author_id": OWNER,
                            "channel_id": CHANNEL,
                            "from_bot": False,
                            "external_apps": {"eligible": True, "level": "read-only"},
                        }
                    ]
                )
            )
            with patch.dict(
                os.environ,
                {
                    "FIXTURE_APP_TOKEN": "fixture-app-token",
                    "DISCORD_BOT_TOKEN": "fixture-bot-token",
                    "UNRELATED_SECRET": "fixture-other-secret",
                },
            ):
                broker = Broker(root)
                plan = broker.plan(
                    ITEM, NONCE, "calendar", "list_events", {"calendar_id": "personal"}
                )
                result = broker.execute(ITEM, NONCE, plan["request_id"])
            self.assertTrue(result["untrusted"])
            self.assertEqual(result["channel_id"], CHANNEL)
            data = json.loads(result["result"]["content"][0]["text"])
            self.assertEqual(data["calendar_id"], "personal")
            self.assertTrue(data["app_credential_received"])
            self.assertFalse(data["bot_token_received"])
            self.assertFalse(data["other_secret_received"])
            with self.assertRaises(Refused):
                broker.execute(ITEM, NONCE, plan["request_id"])


@unittest.skipUnless(HAVE_MCP, "Optional MCP SDK")
class Facade(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_plan_and_execute_keep_dm_binding_and_signed_receipt(self):
        with patch(
            "muse_discord.discord_tools.mcp_server.invoke_external", new_callable=AsyncMock
        ) as invoke:
            invoke.return_value = {"request_id": "r", "decision": "approval-required"}
            async with Client(create_server()) as client:
                args = {
                    "for_id": ITEM,
                    "nonce": NONCE,
                    "app": "calendar",
                    "tool": "draft",
                    "arguments": {"calendar_id": "personal"},
                }
                result = await client.call_tool("external_app_plan", args)
                self.assertFalse(result.is_error)
                invoke.assert_awaited_once_with(
                    "plan",
                    ITEM,
                    NONCE,
                    "calendar",
                    "draft",
                    "--arguments",
                    "-",
                    body=args["arguments"],
                )
                invoke.reset_mock()
                receipt = {"payload": {"request_id": "r"}, "signature": "signed"}
                result = await client.call_tool(
                    "external_app_execute",
                    {"for_id": ITEM, "nonce": NONCE, "request_id": "r", "receipt": receipt},
                )
                self.assertFalse(result.is_error)
                invoke.assert_awaited_once_with(
                    "execute", ITEM, NONCE, "r", "--receipt", "-", body=receipt
                )


if __name__ == "__main__":
    unittest.main()
