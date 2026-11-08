"""Button/slash routing and anonymous acknowledgements outside the main loop.

The router sees the current owners on every event. A dedicated worker isolates
handler failures from later interactions and hands state changes back through
its supplied record callback; it never mutates the main loop's state directly.
"""

import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field

from muse_discord.http_runtime import DiscordCalls, RequestOptions
from muse_discord.interactions import ephemeral, interaction_user_id

APPLICATION_COMMAND_TYPE = 2
INTERACTION_CALLBACK_TIMEOUT = 2.5


@dataclass
class InteractionRouter:
    owners_source: Callable[[], set[str]]
    _handlers: dict = field(default_factory=dict, init=False, repr=False)

    def register(self, key, handler, owner_only=True):
        self._handlers[key] = (handler, owner_only)

    def dispatch(self, ix, record):
        data = ix.get("data") or {}
        if ix.get("type") in (3, 5):  # MESSAGE_COMPONENT, MODAL_SUBMIT
            key, _, rest = str(data.get("custom_id") or "").partition(":")
        elif ix.get("type") == APPLICATION_COMMAND_TYPE:
            key, rest = "/" + str(data.get("name") or ""), ""
        else:
            return None  # nothing to answer (e.g. autocomplete)
        entry = self._handlers.get(key)
        if entry is None:
            return ephemeral("That control is no longer active.")
        handler, owner_only = entry
        uid = interaction_user_id(ix)
        if owner_only and uid not in self.owners_source():
            return ephemeral("Only this bot's owners can use that.")
        return handler(ix, rest, uid, record)


@dataclass
class InteractionResponder:
    discord: DiscordCalls

    def send(self, ix, response):
        """Anonymous interaction token auth, independent of Bot-token failures."""
        path = (
            f"/interactions/{urllib.parse.quote(str(ix['id']), safe='')}/"
            f"{urllib.parse.quote(str(ix['token']), safe='')}/callback"
        )
        response = self.discord.guard(response, "an interaction response")
        self.discord.request(
            "POST",
            path,
            response,
            options=RequestOptions(timeout=INTERACTION_CALLBACK_TIMEOUT, anonymous=True),
        )


@dataclass
class InteractionWorker:
    router: InteractionRouter
    responder: InteractionResponder

    def run(self, ix_inbox, record):
        """Answer interactions as they arrive (fed by the gateway thread)."""
        while True:
            ix = ix_inbox.get()
            try:
                response = self.router.dispatch(ix, record)
            except Exception as e:
                print(f"Interaction handler error: {e}", flush=True)
                response = ephemeral("Something went wrong; try again.")
            if response is None:
                continue
            try:
                self.responder.send(ix, response)
            except Exception as e:
                print(f"Interaction response failed: {e}", flush=True)
