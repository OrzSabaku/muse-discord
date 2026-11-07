"""discord.py Gateway: queue forwarding and fatal authentication/intents."""

import json
import os
import queue
import sys
import threading
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "settingsui"
    ),
)
import harness as H
from muse_discord import discord_connection as DC


class Connection(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ctx = H.FakeCtx()
        self.inbox, self.ix = queue.Queue(), queue.Queue()
        self.status = {"wake": threading.Event(), "interactions": self.ix}
        self.client = DC.Connection(
            self.inbox, self.status, lambda: self.ctx, lambda *a: None, 33281
        )

    async def asyncTearDown(self):
        await self.client.close()

    async def event(self, event, data):
        await self.client.on_socket_raw_receive(
            json.dumps({"op": 0, "t": event, "d": data})
        )

    async def test_raw_dispatch_keeps_message_typing_guild_and_permission_data(self):
        msg = {
            "id": "1",
            "channel_id": "2",
            "content": "hello",
            "attachments": [{"id": "3"}],
        }
        await self.event("READY", {"user": {"id": "4"}, "application": {"id": "5"}})
        await self.event("MESSAGE_CREATE", msg)
        self.assertEqual(self.inbox.get_nowait(), msg)
        self.assertTrue(self.status["wake"].is_set())
        self.assertEqual(self.status["application"]["id"], "5")
        await self.event("TYPING_START", {"channel_id": "2", "user_id": "3"})
        self.assertEqual(
            self.inbox.get_nowait(), {"_typing": {"channel_id": "2", "user_id": "3"}}
        )
        await self.event("GUILD_DELETE", {"id": "6", "unavailable": True})
        self.assertTrue(self.inbox.empty())
        await self.event("GUILD_DELETE", {"id": "6"})
        self.assertEqual(self.inbox.get_nowait()["_guild_event"]["op"], "delete")
        await self.event("GUILD_CREATE", {"id": "6", "member_count": 12})
        self.assertEqual(self.inbox.get_nowait()["_guild_event"]["member_count"], 12)
        await self.event("CHANNEL_UPDATE", {"id": "2", "permission_overwrites": []})
        self.assertEqual(self.inbox.get_nowait()["_perm_event"]["t"], "CHANNEL_UPDATE")





    async def test_auth_and_intents_failures_allow_rest_fallback(self):
        failures = [
            DC.discord.LoginFailure("invalid"),
            DC.discord.PrivilegedIntentsRequired(None),
        ]
        for failure in failures:
            status = {"wake": threading.Event()}
            with patch.object(
                DC.Connection, "start", new=AsyncMock(side_effect=failure)
            ):
                await DC.run(
                    "dummy",
                    self.inbox,
                    status,
                    lambda: self.ctx,
                    lambda *a: None,
                    33281,
                )
            self.assertFalse(status["connected"])
            self.assertTrue(status["wake"].is_set())
            self.assertTrue(status.get("token_rejected") or status.get("dead"))





if __name__ == "__main__":
    unittest.main()
