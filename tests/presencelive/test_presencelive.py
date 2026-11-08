"""One discord.py connection owns presence and ingest; real SDK and watcher,
with only the WebSocket send faked. No Discord credentials or live network."""

import asyncio
import os
import queue
import sys
import time
import types
import unittest
from unittest.mock import AsyncMock

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_lib")
)
import discord
from common import scratch

from muse_discord import muse_settings as MS
from muse_discord.discord_connection import Connection


def config(status="first", kind="watching", enabled=True):
    return (
        f'version: 1\nowners: ["111"]\npresence:\n'
        f'  status: "{status}"\n  kind: {kind}\n  enabled: {str(enabled).lower()}\n'
    )


def write(path, text):
    with open(path + ".tmp", "w") as f:
        f.write(text)
    os.replace(path + ".tmp", path)


class Live(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cfg = os.path.join(scratch("muse-presence-"), "config.yaml")
        write(self.cfg, config())
        self.watcher = MS.Watcher(self.cfg)
        self.errors, self.checked = [], []
        self.client = Connection(
            queue.Queue(), {}, self.context, lambda *a: self.checked.append(a), 33281
        )
        self.client.ws = types.SimpleNamespace(open=False, change_presence=AsyncMock())

    async def asyncSetUp(self):
        await self.client._async_setup_hook()
        self.client._ready.set()

    async def asyncTearDown(self):
        await self.client.close()

    def context(self):
        _, err = self.watcher.poll()
        if err:
            self.errors.append(err)
        return types.SimpleNamespace(settings=self.watcher.current)

    async def sync(self):
        # Advance only the presence throttle, not the event loop's clock.
        self.client._presence_at -= 5
        await self.client.sync_presence()

    def sent(self):
        return [
            dict(
                status=c.kwargs["status"],
                activity=c.kwargs["activity"].to_dict() if c.kwargs["activity"] else None,
            )
            for c in self.client.ws.change_presence.call_args_list
        ]

    async def test_starts_invisible_then_uses_the_monitor_connection(self):
        self.assertEqual(self.client.status, discord.Status.invisible)
        self.assertIsNone(self.client.activity)
        ws = self.client.ws
        await self.sync()
        self.assertIs(self.client.ws, ws)
        self.assertEqual(len(self.sent()), 1)
        self.assertEqual(self.sent()[0]["status"], "online")
        self.assertEqual(self.sent()[0]["activity"]["type"], 3)
        self.assertEqual(self.sent()[0]["activity"]["name"], "first")
        self.assertEqual(self.client.activity.name, "first")
        self.assertEqual(self.client.status, discord.Status.online)
        await self.client.sync_presence()
        self.assertEqual(len(self.sent()), 1)

    async def test_file_and_discord_overlay_changes_apply_without_reconnect(self):
        await self.sync()
        write(self.cfg, config("second", "listening"))
        await self.sync()
        MS.set_value(self.cfg, "presence.status", "set in Discord", by="111")
        await self.sync()
        self.assertEqual(
            [c["activity"]["name"] for c in self.sent()],
            ["first", "second", "set in Discord"],
        )
        self.assertEqual(self.sent()[1]["activity"]["type"], 2)

    async def test_rapid_changes_collapse_to_the_latest_value(self):
        await self.sync()
        for value in ("a", "b", "c"):
            write(self.cfg, config(value))
            await self.client.sync_presence()
        self.assertEqual(len(self.sent()), 1)
        await self.sync()
        self.assertEqual([c["activity"]["name"] for c in self.sent()], ["first", "c"])

    async def test_invalid_edits_keep_the_last_good_status_and_recover(self):
        await self.sync()
        for broken in (config("bad", "bogus-kind"), "presence: [unclosed\n"):
            write(self.cfg, broken)
            await self.sync()
            self.assertEqual(len(self.sent()), 1)
        self.assertTrue(self.errors)
        write(self.cfg, config("recovered"))
        await self.sync()
        self.assertEqual(self.sent()[-1]["activity"]["name"], "recovered")

    async def test_disabled_is_invisible_and_ingest_stays_connected(self):
        write(self.cfg, config(enabled=False))
        await self.sync()
        self.assertEqual(self.client.status, discord.Status.invisible)
        self.assertFalse(self.sent())
        write(self.cfg, config())
        await self.sync()
        write(self.cfg, config(enabled=False))
        await self.sync()
        self.assertEqual(self.sent()[-1], {"status": "invisible", "activity": None})
        await self.client.on_socket_raw_receive(
            '{"op":0,"t":"MESSAGE_CREATE","d":{"id":"123","content":"hello"}}'
        )
        self.assertEqual(self.client.inbox.get_nowait()["content"], "hello")
        write(self.cfg, config("visible again"))
        await self.sync()
        self.assertEqual(self.sent()[-1]["status"], "online")

    async def test_reconnect_and_resume_use_latest_settings_and_identify_cache(self):
        await self.sync()
        self.client._ready.clear()
        write(self.cfg, config("while disconnected", "custom"))
        await self.sync()
        self.assertEqual(len(self.sent()), 1)
        await self.client.on_ready()
        self.client._ready.set()
        await self.sync()
        activity = self.sent()[-1]["activity"]
        self.assertEqual(activity["type"], 4)
        self.assertEqual(activity["state"], "while disconnected")
        self.assertEqual(self.client.activity.to_dict(), activity)
        await self.client.on_resumed()
        await self.sync()
        self.assertEqual(self.sent()[-1]["activity"], activity)

    async def test_empty_status_and_all_activity_kinds(self):
        for kind, code in [
            ("playing", 0),
            ("streaming", 1),
            ("listening", 2),
            ("watching", 3),
            ("custom", 4),
            ("competing", 5),
        ]:
            write(self.cfg, config("status", kind))
            await self.sync()
            self.assertEqual(self.sent()[-1]["activity"]["type"], code)
        write(self.cfg, config(""))
        await self.sync()
        self.assertIsNone(self.sent()[-1]["activity"])
        self.assertEqual(self.sent()[-1]["status"], "online")

    async def test_reading_settings_does_not_block_the_event_loop(self):
        original = self.client.settings_context

        def slow_context():
            time.sleep(0.15)
            return original()

        self.client.settings_context = slow_context
        task = asyncio.create_task(self.client.sync_presence())
        beats = 0
        while not task.done():
            beats += 1
            await asyncio.sleep(0.01)
        await task
        self.assertGreaterEqual(beats, 5)

    async def test_task_lifetime_and_failed_send_retry(self):
        failed = asyncio.Event()

        async def temporary_failure(**kwargs):
            failed.set()
            raise RuntimeError("temporary")

        self.client.ws.change_presence.side_effect = temporary_failure
        self.client.presence_watch.change_interval(seconds=0.02)
        await self.client.setup_hook()
        self.assertTrue(self.client.presence_watch.is_running())
        await asyncio.wait_for(failed.wait(), 1)
        self.assertEqual(self.client._presence_sent, (False, "", ""))
        self.client.ws.change_presence.side_effect = None
        for _ in range(100):
            if self.client._presence_sent[1] == "first":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.client._presence_sent[1], "first")
        task = self.client.presence_watch.get_task()
        await self.client.close()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self.assertFalse(self.client.presence_watch.is_running())

    async def test_guard_blocks_secrets_before_presence_changes(self):
        def blocked(*args):
            raise ValueError("blocked")

        self.client.settings_guard = blocked
        with self.assertRaises(ValueError):
            await self.sync()
        self.assertFalse(self.sent())
        self.assertEqual(self.client.status, discord.Status.invisible)


if __name__ == "__main__":
    unittest.main()
