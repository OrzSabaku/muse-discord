"""Own gateway queues, connection status and the background connection thread.

The Discord SDK is loaded only when connecting. The event consumer remains on
the application's main loop; this session only produces raw gateway events.
"""

import asyncio
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# GUILDS, guild/DM messages, message content and guild/DM typing events.
DEFAULT_INTENTS = (1 << 0) | (1 << 9) | (1 << 12) | (1 << 15) | (1 << 11) | (1 << 14)


@dataclass
class GatewaySession:
    token: str
    settings_context: Callable[[], Any]
    guard: Callable[..., Any]
    intents: int = DEFAULT_INTENTS
    inbox: queue.Queue = field(default_factory=queue.Queue, init=False)
    interactions: queue.Queue = field(default_factory=queue.Queue, init=False)
    status: dict = field(init=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)

    def __post_init__(self):
        self.status = {"wake": threading.Event(), "interactions": self.interactions}

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self.run, daemon=True, name="discord-gateway")
            self._thread.start()
            print("Gateway thread started (realtime ingest).", flush=True)
        return self.inbox, self.status

    async def connect(self):
        from muse_discord.discord_connection import Connection, run

        await run(
            self.token,
            Connection(self.inbox, self.status, self.settings_context, self.guard, self.intents),
        )

    def run(self):
        try:
            asyncio.run(self.connect())
        except Exception as exc:
            print(
                f"GATEWAY DISABLED (thread died: {exc}): "
                "realtime ingest is off; REST polling continues.",
                flush=True,
            )
            self.status["dead"] = True
        finally:
            self.status["connected"] = False
            self.status["wake"].set()
