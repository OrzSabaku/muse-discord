"""discord.py owns Gateway connection, heartbeats and reconnection.

The monitor's existing queue consumes raw Discord message dictionaries, so
forward decoded dispatch events without changing ingestion/dedup semantics.
Interactions still use the monitor's existing interaction worker.
"""

import json
import time

import discord



class Connection(discord.Client):
    def __init__(self, inbox, status, context_factory, guard, intents):
        super().__init__(
            intents=discord.Intents(intents),
            enable_debug_events=True,
            max_messages=None,
            member_cache_flags=discord.MemberCacheFlags.none(),
            chunk_guilds_at_startup=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.inbox, self.gateway_status = inbox, status
        self.settings_context, self.settings_guard = context_factory, guard
        self.settings_log = lambda message: print(message, flush=True)

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
            self.gateway_status.update(
                user=data.get("user"), application=data.get("application")
            )
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
        elif event == "INTERACTION_CREATE":
            inbox = self.gateway_status.get("interactions")
            if inbox is not None:
                inbox.put(data)



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
