"""Block outgoing secrets and rate-limit owner notices without recursive sends."""

import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from muse_discord import secret_scan
from muse_discord.message_models import SecretBlocked


def collect_nonces(flat_maps, structured_maps, extra):
    """Every nonce the monitor holds live: the values of its flat nonce
    maps, and the "nonce" fields of owner_items and the web checks
    (never other fields, such as a check's public group tag)."""
    out = set()
    for body in flat_maps:
        if isinstance(body, dict):
            out |= {v for v in body.values() if isinstance(v, str) and v}
    with contextlib.suppress(Exception):  # a live heartbeat slot's nonce (proactive.py)
        out |= {v for v in (extra() or ()) if v}

    def walk(v):
        if isinstance(v, dict):
            for k, x in v.items():
                if k == "nonce" and isinstance(x, str) and x:
                    out.add(x)
                else:
                    walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    for body in structured_maps:
        walk(body)
    return out


def payload_strings(v, key=None):
    if isinstance(v, str):
        if key != "nonce":  # Discord's own send nonce, derived
            yield v
    elif isinstance(v, dict):
        for k, x in v.items():
            yield from payload_strings(x, k)
    elif isinstance(v, list):
        for x in v:
            yield from payload_strings(x, key)


@dataclass
class OutgoingGuard:
    token_source: Callable[[], str]
    nonces_source: Callable[[], set]
    notify: Callable[[str], Any]
    day: Callable[[], str] = field(default=lambda: time.strftime("%Y-%m-%d"))
    _alert_day: str | None = field(default=None, init=False, repr=False)
    _busy: bool = field(default=False, init=False, repr=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def find(self, text):
        return secret_scan.find(text, self.token_source(), self.nonces_source())

    def guard(self, data, where):
        """Raise SecretBlocked (and log, and tell the owners once a day) when
        anything data would send carries a secret: any of its strings, or
        all of them joined (a token split across parts, embed fields or file
        names). data is returned as is: nothing is ever altered. Every send
        helper calls it."""
        texts = list(payload_strings(data))
        for text in [*texts, "".join(texts)]:
            what = self.find(text)
            if what:
                print(
                    f"Blocked a send to {where}: it contained {what}; nothing was sent.", flush=True
                )
                self.alert()
                raise SecretBlocked(f"blocked: it contained {what}")
        return data

    def alert(self):
        """Reserve one notice per day, releasing the lock before sending it.

        Owner notices pass through the same guard, so the busy flag prevents
        recursive notices. The interaction and monitor threads share this state.
        """
        day = self.day()
        with self._lock:
            if self._alert_day == day or self._busy:
                return
            self._alert_day, self._busy = day, True
        try:
            self.notify(
                "I didn't send a reply because it looked like it "
                "held a secret (my bot token or one of my private "
                "codes). I tell you about this at most once a day."
            )
        finally:
            with self._lock:
                self._busy = False
