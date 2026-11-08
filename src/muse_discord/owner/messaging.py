"""Guarded owner notifications through an injected Discord transport."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class OwnerMessenger:
    owners_source: Callable[[], set[str]]
    guard: Callable[[dict, str], dict]
    post: Callable[[str, dict], Any]

    @property
    def owners(self) -> set[str]:
        return self.owners_source()

    def notify(self, text, components=None, flags=None):
        """DM every owner (with message components, e.g. buttons, and
        message flags when given). Returns True when at least one DM landed
        (or there is nobody to tell). Never raises."""
        if not self.owners:
            print(f"Owner notice (no owners configured): {text}", flush=True)
            return True
        payload = {"content": text[:1990], "allowed_mentions": {"parse": []}}
        if components:
            payload["components"] = components
        if flags:
            payload["flags"] = flags
        delivered = False
        for uid in sorted(self.owners):
            if self.dm(uid, payload):
                delivered = True
        return delivered

    def dm(self, uid, payload):
        """Send one message payload (content, components, ...) to one
        owner's DM channel. True when it landed. Never raises."""
        try:
            payload = self.guard(payload, "an owner's DM")
            c = self.post("/users/@me/channels", {"recipient_id": uid})
            self.post(f"/channels/{c['id']}/messages", payload)
            return True
        except Exception as e:
            print(f"Owner DM to {uid} failed: {e}", flush=True)
            return False
