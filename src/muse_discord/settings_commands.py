"""Reconcile only this application's settings commands without bulk replacement."""

import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any

SETTINGS_PREFIX = "mdset"
SETTINGS_COMMAND = {
    "name": "settings",
    "type": 1,  # CHAT_INPUT
    "description": "See or change this bot's settings (owners; server admins can make it quieter)",
    # Interaction context types: 0 GUILD, 1 BOT_DM (not 2, group DMs).
    "contexts": [0, 1],
    # Installation contexts: 0 GUILD_INSTALL only (no user installs).
    "integration_types": [0],
    # Visible to every member: owners need it in any server, server
    # admins in theirs; settings_ui refuses everyone else.
    "default_member_permissions": None,
}
# The message context command (right-click a message, Apps): why the
# bot did not answer it. Discord may refuse "?" in the name; then the
# second name is registered (WHY_NAMES[1]).
WHY_NAMES = ("Why no reply?", "Why no reply")
WHY_COMMAND = {
    "name": WHY_NAMES[0],
    "type": 3,  # MESSAGE
    "contexts": [0, 1],
    "integration_types": [0],
    # Manage Server: ordinary members don't see it in the menu (owners
    # use Why no reply? on the /settings pages anyway).
    "default_member_permissions": "32",
}

SETTINGS_NAMES = (SETTINGS_COMMAND["name"], *WHY_NAMES)
COMMAND_RETRY_SECS = 60


def _command_differs(have, want):
    for k, v in want.items():
        got = have.get(k)
        if isinstance(v, list):
            got = sorted(got) if isinstance(got, list) else got
            v = sorted(v)
        if got != v:
            return True
    return bool(have.get("options"))


@dataclass
class CommandRegistrar:
    get: Callable[[str], Any]
    post: Callable[[str, dict], Any]
    patch: Callable[[str, dict], Any]
    identity_ready: Callable[[], bool]
    backing_off: Callable[[], bool]
    now: Callable[[], float] = time.time
    app_id: str | None = field(default=None, init=False)
    done: bool = field(default=False, init=False)
    next_try: float = field(default=0, init=False)
    last_error: str | None = field(default=None, init=False)

    def ensure(self, app_id):
        """Make the global /settings command match SETTINGS_COMMAND, and the
        "Why no reply?" message command WHY_COMMAND. Returns "created",
        "updated" or "unchanged" (the first that applies: /settings, then
        the message command); raises on failure."""
        base = f"/applications/{urllib.parse.quote(str(app_id), safe='')}/commands"
        have = self.get(base)
        if not isinstance(have, list):
            raise ValueError(f"unexpected command list: {str(have)[:80]}")
        r1 = self.ensure_one(base, have, SETTINGS_COMMAND, (SETTINGS_COMMAND["name"],))
        r2 = self.ensure_one(base, have, WHY_COMMAND, WHY_NAMES)
        return r1 if r1 != "unchanged" else r2

    def ensure_one(self, base, have, want, names):
        """Create or update one command (names: what it may be called; the
        one already registered is kept). A message command name Discord
        refuses (400) is retried with the next name."""
        mine = [
            c
            for c in have
            if isinstance(c, dict) and c.get("name") in names and c.get("type", 1) == want["type"]
        ]
        if mine:
            want = dict(want, name=mine[0]["name"])
            if not _command_differs(mine[0], want):
                return "unchanged"
            cid = urllib.parse.quote(str(mine[0].get("id")), safe="")
            self.patch(f"{base}/{cid}", want)
            return "updated"
        for i, name in enumerate(names):
            try:
                self.post(base, dict(want, name=name))
                return "created"
            except Exception as e:
                if getattr(e, "code", None) != HTTPStatus.BAD_REQUEST or i == len(names) - 1:
                    raise
                print(
                    f"Discord refused the command name {name!r}; trying {names[i + 1]!r}.",
                    flush=True,
                )
        return "unchanged"

    def register(self, gateway_status=None):
        """Main loop, every tick until it succeeds once (throttled to one
        try per COMMAND_RETRY_SECS). Never raises."""
        now = self.now()
        if self.done or now < self.next_try or not self.identity_ready() or self.backing_off():
            return None
        self.next_try = now + COMMAND_RETRY_SECS
        try:
            if not self.app_id:
                app = (gateway_status or {}).get("application") or {}
                self.app_id = app.get("id") if isinstance(app, dict) else None
            if not self.app_id:
                self.app_id = (self.get("/applications/@me") or {}).get("id")
            if not self.app_id:
                raise ValueError("application id unknown")
            result = self.ensure(self.app_id)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            if err != self.last_error:
                hint = (
                    " (the bot's invite may lack the applications.commands scope; "
                    "see docs/SETUP.md)"
                    if getattr(e, "code", None) == HTTPStatus.FORBIDDEN
                    else ""
                )
                print(
                    f"/settings command registration failed ({err}){hint}; "
                    f"retrying in {COMMAND_RETRY_SECS}s.",
                    flush=True,
                )
            self.last_error = err
            return None
        self.done = True
        self.last_error = None
        if result != "unchanged":
            print(
                f"/settings command {result} (global; it can take a moment to show up in Discord).",
                flush=True,
            )
        return result
