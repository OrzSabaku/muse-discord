"""discord.py boundary: native routing, queue forwarding, fatal auth/intents."""

import asyncio
import json
import os
import queue
import sys
import threading
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "settingsui"),
)
import harness as H
from test_native import interaction

from muse_discord import discord_connection as DC


class Connection(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ctx = H.FakeCtx()
        self.inbox, self.ix = queue.Queue(), queue.Queue()
        self.status = {"wake": threading.Event(), "interactions": self.ix}
        self.client = DC.Connection(
            self.inbox, self.status, lambda: self.ctx, lambda *a: None, 33281
        )

    async def asyncSetUp(self):
        await self.client._async_setup_hook()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_proxy_environment_reaches_the_discord_client(self):
        # discord.py never reads the proxy environment itself; on a
        # host whose egress goes through a proxy the Gateway connect
        # is attempted directly and dies, so Connection must hand the
        # environment's proxy over.
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://proxy.example:3128"}, clear=True):
            client = DC.Connection(
                self.inbox,
                self.status,
                lambda: self.ctx,
                lambda *a: None,
                33281,
            )
            try:
                self.assertEqual(client.http.proxy, "http://proxy.example:3128")
            finally:
                await client.close()

    async def test_no_proxy_environment_means_no_proxy(self):
        with patch.dict(os.environ, {}, clear=True):
            client = DC.Connection(
                self.inbox,
                self.status,
                lambda: self.ctx,
                lambda *a: None,
                33281,
            )
            try:
                self.assertIsNone(client.http.proxy)
            finally:
                await client.close()

    async def test_no_proxy_bypass_for_discord_hosts_disables_proxy(self):
        with patch.dict(
            os.environ,
            {
                "HTTPS_PROXY": "http://proxy.example:3128",
                "NO_PROXY": ".discord.com,.discord.gg",
            },
            clear=True,
        ):
            client = DC.Connection(
                self.inbox,
                self.status,
                lambda: self.ctx,
                lambda *a: None,
                33281,
            )
            try:
                self.assertIsNone(client.http.proxy)
            finally:
                await client.close()

    async def event(self, event, data):
        await self.client.on_socket_raw_receive(json.dumps({"op": 0, "t": event, "d": data}))

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
        self.assertEqual(self.inbox.get_nowait(), {"_typing": {"channel_id": "2", "user_id": "3"}})
        await self.event("GUILD_DELETE", {"id": "6", "unavailable": True})
        self.assertTrue(self.inbox.empty())
        await self.event("GUILD_DELETE", {"id": "6"})
        self.assertEqual(self.inbox.get_nowait()["_guild_event"]["op"], "delete")
        await self.event("GUILD_CREATE", {"id": "6", "member_count": 12})
        self.assertEqual(self.inbox.get_nowait()["_guild_event"]["member_count"], 12)
        await self.event("CHANNEL_UPDATE", {"id": "2", "permission_overwrites": []})
        self.assertEqual(self.inbox.get_nowait()["_perm_event"]["t"], "CHANNEL_UPDATE")

    async def test_settings_are_not_sent_to_legacy_worker_but_other_buttons_are(self):
        for data in [
            {"name": "settings"},
            {"name": "Why no reply?"},
            {"custom_id": "mdset:open:server:123"},
            {"custom_id": "mset:abcdefghijkl:save"},
        ]:
            await self.event("INTERACTION_CREATE", {"data": data})
        self.assertTrue(self.ix.empty())
        raw = {"data": {"custom_id": "mdhold:1:yes"}}
        await self.event("INTERACTION_CREATE", raw)
        self.assertEqual(self.ix.get_nowait(), raw)

    async def test_native_command_ack_precedes_context_loading(self):
        ix = interaction(self.ctx, data={"name": "settings"})
        ix.response.is_done = lambda: ix.response.defer.await_count > 0

        def context():
            self.assertTrue(ix.response.defer.await_count)
            return self.ctx

        self.client.settings_context = context
        await self.client.on_interaction(ix)
        ix.response.defer.assert_awaited_once()
        self.assertIn("view", ix.edit_original_response.call_args.kwargs)

    async def test_join_notice_and_old_settings_buttons(self):
        ix = interaction(self.ctx, data={"custom_id": f"mdset:open:server:{H.G2}"})
        ix.response.is_done = lambda: ix.response.defer.await_count > 0
        await self.client.on_interaction(ix)
        self.assertIn(
            "Study Group",
            str(ix.edit_original_response.call_args.kwargs["view"].to_components()),
        )
        old = interaction(self.ctx, data={"custom_id": "mdset:3:pg:x:h"})
        await self.client.on_interaction(old)
        self.assertIn("古い形式", old.edit_original_response.call_args.kwargs["content"])

    async def test_native_dispatch_restores_user_bound_action_from_real_components(
        self,
    ):
        ix = interaction(self.ctx)
        await DC.settings_ui.open_settings(ix, self.ctx)
        view = ix.edit_original_response.call_args.kwargs["view"]
        await self.client.setup_hook()
        action = next(i for i in view.walk_children() if isinstance(i, DC.settings_ui.Action))
        match = action.template.fullmatch(action.custom_id)
        restored = await DC.settings_ui.Action.from_custom_id(ix, action.item, match)
        self.assertEqual(restored.token, action.token)
        self.assertEqual(restored.action, action.action)

    async def test_auth_and_intents_failures_allow_rest_fallback(self):
        failures = [
            DC.discord.LoginFailure("invalid"),
            DC.discord.PrivilegedIntentsRequired(None),
        ]
        for failure in failures:
            status = {"wake": threading.Event()}
            with patch.object(DC.Connection, "start", new=AsyncMock(side_effect=failure)):
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

    async def test_native_callbacks_do_not_wait_for_main_worker(self):
        blocked = threading.Event()

        async def blocked_worker():
            await asyncio.to_thread(blocked.wait, 2)

        task = asyncio.create_task(blocked_worker())
        ix = interaction(self.ctx, data={"name": "settings"})
        ix.response.is_done = lambda: ix.response.defer.await_count > 0
        try:
            await asyncio.wait_for(self.client.on_interaction(ix), timeout=1)
            self.assertIn("view", ix.edit_original_response.call_args.kwargs)
        finally:
            blocked.set()
            await task

    async def test_real_discord_dispatch_routes_serialized_select_and_acknowledges(
        self,
    ):
        from unittest.mock import MagicMock

        from discord.webhook.async_ import async_context

        await self.client._async_setup_hook()
        await self.client.setup_hook()
        screen = DC.settings_ui.Screen(H.OWNER, f"server:{H.G1}", key="style.length")
        self.ctx.session_put(H.OWNER, screen.token, screen)
        view = DC.settings_ui.Panel(screen, self.ctx, "owner", interaction_gid=H.G1)
        self.client._connection.user = DC.discord.ClientUser(
            state=self.client._connection,
            data={
                "id": H.BOT,
                "username": "muse",
                "discriminator": "0",
                "avatar": None,
            },
        )
        actor = {
            "id": H.OWNER,
            "username": "owner",
            "discriminator": "0",
            "avatar": None,
        }
        raw = {
            "id": "800000000000000001",
            "application_id": H.BOT,
            "attachment_size_limit": 10000000,
            "channel": {
                "id": H.MUSECHAT,
                "type": 0,
                "name": "muse-chat",
                "position": 0,
                "permission_overwrites": [],
            },
            "token": "offline",
            "type": 3,
            "version": 1,
            "guild_id": H.G1,
            "channel_id": H.MUSECHAT,
            "member": {
                "user": actor,
                "permissions": "0",
                "flags": 0,
                "roles": [],
                "joined_at": "2026-01-01T00:00:00+00:00",
            },
            "data": {
                "component_type": 3,
                "custom_id": f"mset:{screen.token}:set",
                "values": ['"brief"'],
            },
            "message": {
                "id": "800000000000000002",
                "channel_id": H.MUSECHAT,
                "author": actor,
                "content": "",
                "flags": 32768,
                "type": 0,
                "timestamp": "2026-01-01T00:00:00+00:00",
                "components": view.to_components(),
            },
        }
        adapter = MagicMock()
        adapter.create_interaction_response = AsyncMock(
            return_value={"interaction": {"id": raw["id"]}}
        )
        finished = asyncio.Event()

        async def rendered(*args):
            finished.set()

        token = async_context.set(adapter)
        try:
            with patch.object(DC.settings_ui, "redraw", side_effect=rendered):
                self.client._connection.parse_interaction_create(raw)
                await asyncio.wait_for(finished.wait(), 2)
            adapter.create_interaction_response.assert_awaited_once()
            self.assertEqual(screen.draft, {"style.length": "brief"})
            self.assertEqual(self.ctx.value("style.length", H.MS.Place.server(H.G1)), "normal")
        finally:
            async_context.reset(token)

    async def test_expired_native_modal_gets_a_response_after_restart(self):
        ix = interaction(self.ctx, data={"custom_id": "mset:abcdefghijkl:submit"})
        ix.type = DC.discord.InteractionType.modal_submit
        await self.client.on_interaction(ix)
        ix.response.send_message.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
