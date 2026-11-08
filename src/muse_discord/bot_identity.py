"""Bot identity discovery and recovery shared by REST and Gateway reception.

Confirm identity before slow channel discovery. The watchdog recognizes exactly
"Bot user id:", "Bot identity pending" and the fatal "TOKEN REJECTED" marker.
Transient REST failures keep the bot alive; HTTP 401 rejects the token. A READY
identity can recover reception before the next successful REST identity query.
"""

from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any


@dataclass
class BotIdentity:
    get: Callable[[str], Any]
    clean_cycle: Callable[[], None]
    reject: Callable[[str], Any]
    user_id: str | None = None
    name: str | None = None

    def adopt(self, me, how=""):
        self.user_id = me.get("id")
        self.name = me.get("username")
        print(f"Bot user id: {self.user_id}{how}", flush=True)

    def start(self):
        try:
            self.adopt(self.get("/users/@me"))
        except Exception as e:
            if getattr(e, "code", None) == HTTPStatus.UNAUTHORIZED:
                self.reject("401 from /users/@me")
            print(
                f"Bot identity pending: /users/@me failed at boot ({e}); retrying in-loop.",
                flush=True,
            )

    def retry(self, cycle_done):
        if cycle_done:
            self.clean_cycle()
            if self.user_id is None:
                try:
                    self.adopt(self.get("/users/@me"), " (recovered in-loop)")
                except Exception as e:
                    if getattr(e, "code", None) == HTTPStatus.UNAUTHORIZED:
                        self.reject("401 from /users/@me")

    def from_gateway(self, status):
        if self.user_id is None and status.get("user"):
            self.adopt(status["user"], " (from gateway READY)")
