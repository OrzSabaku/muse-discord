"""discord.py owns the Gateway and native settings interactions.

The monitor's existing queue consumes raw Discord message dictionaries, so
forward decoded dispatch events without changing ingestion/dedup semantics.
Legacy approval/update buttons still use the monitor's interaction worker.
"""

import asyncio
import json
import os
import time
import urllib.request

import discord
from discord.ext import tasks

from muse_discord import muse_settings as ms
from muse_discord import settings_ui


class Connection(discord.Client):
    def __init__(self, inbox, status, context_factory, guard, intents):
        # discord.py does not read the proxy environment; hosts whose
        # egress goes through a proxy must hand it over, or the Gateway
        # (and REST) connection is attempted directly and fails.
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        # Honor NO_PROXY/no_proxy so Discord endpoints can stay direct when
        # operators intentionally bypass the proxy for them.
        if proxy and any(
            urllib.request.proxy_bypass_environment(host)
            for host in ("discord.com", "gateway.discord.gg")
        ):
            proxy = None
        super().__init__(
            proxy=proxy,
            intents=discord.Intents(intents),
            status=discord.Status.invisible,
            enable_debug_events=True,
            max_messages=None,
            member_cache_flags=discord.MemberCacheFlags.none(),
            chunk_guilds_at_startup=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.inbox, self.gateway_status = inbox, status
        self.settings_context, self.settings_guard = context_factory, guard
        self.settings_log = lambda message: print(message, flush=True)
        self._presence_sent = (False, "", "")
        self._presence_at = float("-inf")
        self._presence_error = None

    async def setup_hook(self):
        self.add_dynamic_items(settings_ui.Action)
        self.presence_watch.start()

    async def close(self):
        self.presence_watch.cancel()
        await super().close()

    @tasks.loop(seconds=2)
    async def presence_watch(self):
        try:
            await self.sync_presence()
            self._presence_error = None
        except Exception as exc:
            message = f"Presence update failed: {type(exc).__name__}: {exc}"
            if message != self._presence_error:
                self.settings_log(message)
                self._presence_error = message

    @presence_watch.before_loop
    async def before_presence_watch(self):
        await self.wait_until_ready()

    async def sync_presence(self):
        # SettingsCtx uses the monitor's watcher: invalid edits retain the last
        # valid settings. Disk work must not block Gateway heartbeats/callbacks.
        if not self.is_ready():
            return
        st = await asyncio.to_thread(lambda: self.settings_context().settings)
        enabled = st.presence_enabled
        name = str(st.presence_status or "").strip() if enabled else ""
        kind = st.presence_kind if name else ""
        want = (enabled, name, kind)
        now = time.monotonic()
        if want == self._presence_sent or now - self._presence_at < 5:
            return
        activity = None
        if name:
            activity = (
                discord.CustomActivity(name=name)
                if kind == "custom"
                else discord.Activity(type=discord.ActivityType[kind], name=name)
            )
        await asyncio.to_thread(
            self.settings_guard, activity.to_dict() if activity else {}, "presence"
        )
        status = discord.Status.online if enabled else discord.Status.invisible
        await self.change_presence(status=status, activity=activity)
        # The public setters also prepare the next IDENTIFY after a reconnect.
        self.status, self.activity = status, activity
        self._presence_sent, self._presence_at = want, time.monotonic()
        self.settings_log("Status updated on the monitor connection.")

    async def on_ready(self):
        self._presence_sent = None

    async def on_resumed(self):
        self._presence_sent = None

    async def on_connect(self):
        self.gateway_status["connected"] = True

    async def on_disconnect(self):
        self.gateway_status["connected"] = False

    async def on_socket_raw_receive(self, raw):
        packet = json.loads(raw)
        if packet.get("op") != 0:
            return
        event, data = packet.get("t"), packet.get("d") or {}
        if event == "READY":
            self.gateway_status.update(user=data.get("user"), application=data.get("application"))
            print("Gateway READY: realtime ingest live (discord.py).", flush=True)
        elif event == "RESUMED":
            print("Gateway session resumed.", flush=True)
        elif event == "MESSAGE_CREATE":
            self.inbox.put(data)
            self.gateway_status["last_event"] = time.time()
            if self.gateway_status.get("wake"):
                self.gateway_status["wake"].set()
        elif event in ("GUILD_CREATE", "GUILD_DELETE"):
            if event == "GUILD_DELETE" and data.get("unavailable"):
                return
            self.inbox.put(
                {
                    "_guild_event": {
                        "op": "create" if event == "GUILD_CREATE" else "delete",
                        "id": data.get("id"),
                        "joined_at": data.get("joined_at"),
                        "member_count": data.get("member_count"),
                    }
                }
            )
        elif event in (
            "CHANNEL_CREATE",
            "CHANNEL_UPDATE",
            "GUILD_ROLE_CREATE",
            "GUILD_ROLE_UPDATE",
            "GUILD_ROLE_DELETE",
            "GUILD_UPDATE",
        ):
            self.inbox.put({"_perm_event": {"t": event, "d": data}})
        elif event == "TYPING_START":
            self.inbox.put(
                {
                    "_typing": {
                        "channel_id": data.get("channel_id"),
                        "user_id": data.get("user_id"),
                    }
                }
            )
        elif event == "INTERACTION_CREATE" and not self.native_settings(data):
            inbox = self.gateway_status.get("interactions")
            if inbox is not None:
                inbox.put(data)

    @staticmethod
    def native_settings(raw):
        data = raw.get("data") or {}
        return data.get("name") in ("settings", "Why no reply?", "Why no reply") or str(
            data.get("custom_id", "")
        ).startswith((settings_ui.PREFIX, "mdset:"))

    async def on_interaction(self, interaction):
        data = interaction.data or {}
        name, custom = data.get("name"), data.get("custom_id", "")
        try:
            if name in (
                "settings",
                "Why no reply?",
                "Why no reply",
            ) or custom.startswith("mdset:"):
                # Acknowledge before even loading config from disk.
                await interaction.response.defer(ephemeral=True, thinking=True)
                ctx = await asyncio.to_thread(self.settings_context)
                if name in ("Why no reply?", "Why no reply"):
                    await settings_ui.why_message(interaction, ctx)
                elif custom.startswith("mdset:") and not custom.startswith("mdset:open:server:"):
                    await interaction.edit_original_response(
                        content="この設定画面は古い形式です。/settingsを開き直してください。"
                    )
                else:
                    place = None
                    if custom.startswith("mdset:open:server:"):
                        if str(interaction.user.id) not in set(map(str, ctx.settings.owners)):
                            raise ms.SettingsError("この操作はBotの所有者のみ利用できます。")
                        place = ms.parse_place("server:" + custom.rsplit(":", 1)[1], ctx)
                    await settings_ui.open_settings(interaction, ctx, place)
            elif (
                custom.startswith(settings_ui.PREFIX)
                and interaction.type == discord.InteractionType.modal_submit
            ):
                # Modal objects are deliberately not restored after a restart.
                # The native modal callback acknowledges first when still alive.
                await asyncio.sleep(0)
                if not interaction.response.is_done():
                    await settings_ui.error(
                        interaction,
                        "入力画面の有効期限が切れました。/settingsを開き直してください。",
                    )
            # mset:* buttons/selects are dispatched by discord.py's DynamicItem.
        except ms.SettingsError as exc:
            await settings_ui.error(interaction, str(exc))
        except Exception as exc:
            self.settings_log(f"settings command failed: {type(exc).__name__}: {exc}")
            await settings_ui.error(
                interaction,
                "設定画面を開けませんでした。monitor.logを確認してください。",
            )


async def run(token, inbox, status, context_factory, guard, intents):
    client = Connection(inbox, status, context_factory, guard, intents)
    try:
        async with client:
            await client.start(token)
    except discord.LoginFailure:
        status["token_rejected"] = True
    except discord.ConnectionClosed as exc:
        if exc.code == 4004:
            status["token_rejected"] = True
        else:
            print(
                f"GATEWAY DISABLED (code {exc.code}): REST polling continues.",
                flush=True,
            )
            status["dead"] = True
    except discord.PrivilegedIntentsRequired:
        print(
            "GATEWAY DISABLED (code 4014): enable Message Content intent; REST polling continues.",
            flush=True,
        )
        status["dead"] = True
    finally:
        status["connected"] = False
        if status.get("wake"):
            status["wake"].set()
