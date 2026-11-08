"""Discord request policy above the SDK: backoff, read deadlines and liveness.

The SDK owns HTTP sessions, multipart encoding and short retries. This layer
records long 429 windows and terminal authentication failures; anonymous
interaction callbacks use request() so their token failure cannot reject the
bot's credential. Network and watchdog activity happen only when invoked.
"""

import json
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus

from muse_discord.monitor_health import MonitorHealth


@dataclass
class RequestOptions:
    files: tuple | list = ()
    timeout: float = 20
    anonymous: bool = False


@dataclass
class RestBackoff:
    initial_seconds: float
    max_seconds: float
    now: Callable[[], float] = time.time
    jitter: Callable[[float, float], float] = random.uniform
    until: float = 0
    step: int = 0
    logged_until: float = 0

    def active(self):
        return self.now() < self.until

    def note(self, exc):
        now = self.now()
        delay = None
        hdrs = getattr(exc, "headers", None)
        if hdrs is not None:
            try:
                delay = float(hdrs.get("Retry-After"))
            except TypeError, ValueError:
                delay = None
        if delay is None:
            try:
                delay = float(json.loads(exc.read()).get("retry_after"))
            except Exception:
                delay = None
        if delay is None:
            delay = min(self.max_seconds, self.initial_seconds * (2**self.step)) + self.jitter(0, 5)
            self.step = min(self.step + 1, 5)
        delay = max(1.0, min(float(delay), 3600.0))
        self.until = max(self.until, now + delay)
        if now >= self.logged_until:
            print(f"RATE LIMITED: backing off REST for {delay:.0f}s.", flush=True)
            self.logged_until = self.until

    def clean_cycle(self):
        """Reset the exponential step after a poll cycle that stayed clean."""
        if not self.active():
            self.step = 0


@dataclass
class DiscordCalls:
    token_source: Callable[[], str]
    backoff: RestBackoff
    health: MonitorHealth
    guard: Callable[[dict, str], dict]
    endpoint: str = "https://discord.com/api/v10"
    read_timeout: float = 8

    def request(self, method, path, data=None, options=None):
        options = options or RequestOptions()
        files = options.files
        timeout = options.timeout
        anonymous = options.anonymous
        from muse_discord.discord_rest import transport

        return transport(None if anonymous else self.token_source(), self.endpoint).request(
            method, path, data, files=files, timeout=timeout
        )

    def call(self, fn):
        try:
            return fn()
        except Exception as e:
            if getattr(e, "code", None) == HTTPStatus.TOO_MANY_REQUESTS:
                self.backoff.note(e)
            elif getattr(e, "code", None) == HTTPStatus.UNAUTHORIZED:
                self.health.rejected = "401 from the REST API"
            raise
        finally:
            # A call that returned (or failed) is progress: long but live
            # stretches (discovery, the sweep, an outbox backlog) never look
            # stuck to the watchdog; a call that hangs does.
            self.health.progress()

    def get(self, path):
        return self.call(
            lambda: self.request("GET", path, options=RequestOptions(timeout=self.read_timeout))
        )

    def send(self, method, path, data):
        data = self.guard(data, path)
        return self.call(lambda: self.request(method, path, data))

    def post(self, path, data):
        return self.send("POST", path, data)

    def patch(self, path, data):
        return self.send("PATCH", path, data)

    def upload(self, path, payload, blobs):
        # MessageSender has checked the filenames and file contents before
        # this transport step; encoding those bytes belongs to the SDK.
        return self.call(
            lambda: self.request(
                "POST", path, payload, options=RequestOptions(files=blobs, timeout=60)
            )
        )

    def put(self, path):
        return self.call(lambda: self.request("PUT", path))
