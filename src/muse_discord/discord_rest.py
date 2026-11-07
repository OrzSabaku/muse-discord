"""Synchronous callers share discord.py's async HTTP transport.

The monitor/CLI keep raw Discord dictionaries and their authorization rules.
This boundary uses the SDK's low-level HTTPClient/Route so the SDK owns rate
limit buckets, HTTP retries and multipart encoding without model conversions.
Internal SDK usage is confined here and covered by local HTTP contract tests.
"""

import asyncio
import atexit
import io
import logging
import os
import re
import secrets
import threading
import urllib.request
from concurrent.futures import TimeoutError as FutureTimeoutError
from urllib.parse import parse_qsl, unquote, urlsplit

import aiohttp
import discord
from discord.http import HTTPClient, Route

# SDK HTTP traces include interaction tokens in URLs and arbitrary response
# data. Callers log the sanitized errors below instead.
if not (2, 7, 1) <= discord.version_info[:3] < (2, 8, 0):
    raise ImportError(
        "discord.py 2.7.x (>=2.7.1) is required; install requirements.txt"
    )
logging.getLogger("discord.http").disabled = True
API = "https://discord.com/api/v10"


def _proxy_for(base):
    # discord.py does not read the proxy environment; on a host whose
    # egress goes through a proxy every call would otherwise be
    # attempted directly and fail. Loopback API overrides (tests,
    # local relays) are never proxied, and NO_PROXY is honored for
    # the API host just like in the Gateway handoff.
    host = urlsplit(base).hostname
    if host in ("127.0.0.1", "localhost", "::1"):
        return None
    if host and urllib.request.proxy_bypass_environment(host):
        return None
    return os.environ.get("HTTPS_PROXY") \
        or os.environ.get("https_proxy") or None


class DiscordError(RuntimeError):
    def __init__(self, status, discord_code=None, retry_after=None):
        self.status = self.code = status  # legacy monitor checks HTTP .code
        self.discord_code = discord_code
        self.headers = (
            {"Retry-After": str(retry_after)} if retry_after is not None else {}
        )
        super().__init__(
            f"Discord HTTP {status}"
            + (f" (code {discord_code})" if discord_code else "")
        )


async def _no_ambiguous_retry(session, context, params):
    # A failed create may already have succeeded on Discord. SDK retries are
    # safe only for reads/idempotent operations or nonce-protected messages.
    safe = (context.trace_request_ctx or {}).get("retry_safe", False)
    if params.method == "POST" and params.response.status >= 500 and not safe:
        params.response.release()
        raise DiscordError(params.response.status)


class _Session(HTTPClient):
    async def open(self, token):
        # static_login owns session setup; skip its identity probe. Scoped CLI
        # calls and anonymous callbacks must not require a /users/@me request.
        self._initializing = True
        try:
            await self.static_login(token)
        finally:
            self._initializing = False
        # Let long limits return to the monitor rather than block its main
        # loop. SDK retries short limits; the outer deadline bounds all work.
        self.max_ratelimit_timeout = 1.0

    async def request(self, route, **kwargs):
        if getattr(self, "_initializing", False):
            return {}
        return await super().request(route, **kwargs)


def route_for(method, path, base=API):
    parsed = urlsplit(path)
    if (
        not path.startswith("/")
        or path.startswith("//")
        or parsed.scheme
        or parsed.netloc
        or parsed.fragment
    ):
        raise ValueError("expected a relative Discord API path")
    pieces = parsed.path.split("/")
    if any(unquote(piece) in (".", "..") for piece in pieces):
        raise ValueError("Discord API paths cannot contain dot segments")
    template, params = [], {}
    for i, piece in enumerate(pieces):
        previous = pieces[i - 1] if i else ""
        major = {
            "channels": "channel_id",
            "guilds": "guild_id",
            "webhooks": "webhook_id",
        }.get(previous)
        if (
            piece.isdecimal()
            or major
            or previous in ("interactions", "reactions")
            and piece != "@me"
            or i > 2
            and pieces[i - 2] in ("webhooks", "interactions")
        ):
            key = major or (
                "webhook_token"
                if i > 2 and pieces[i - 2] == "webhooks"
                else f"part_{i}"
            )
            params[key] = unquote(piece)
            template.append("{" + key + "}")
        else:
            template.append(piece)
    route = Route(method, "/".join(template), **params)
    if base != API:
        u = urlsplit(base)
        if (
            u.scheme != "http"
            or u.hostname not in ("127.0.0.1", "localhost", "::1")
            or u.username
            or u.password
            or u.query
            or u.fragment
        ):
            raise ValueError("Discord API override must be a loopback HTTP URL")
        route.url = base.rstrip("/") + route.url[len(Route.BASE) :]
    return route, dict(parse_qsl(parsed.query))


class Rest:
    def __init__(self, token, base=API):
        self.token, self.base = token, base
        self._http = None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name="discord-rest"
        )
        self._thread.start()

    def request(self, method, path, data=None, *, files=(), timeout=20):
        future = asyncio.run_coroutine_threadsafe(
            self._request(method, path, data, files, timeout), self._loop
        )
        try:
            return future.result(timeout=timeout + 1)
        except (FutureTimeoutError, asyncio.TimeoutError, TimeoutError):
            future.cancel()
            raise TimeoutError("Discord request timed out") from None

    async def _request(self, method, path, data, files, timeout):
        route, query = route_for(method, path, self.base)
        is_message = (
            method == "POST"
            and re.fullmatch(r"/channels/[^/]+/messages", urlsplit(path).path)
            and "message" not in (data or {})
        )
        if is_message:
            data = dict(data or {})
            if "nonce" not in data:
                data.update(nonce=secrets.token_hex(12), enforce_nonce=True)
        retry_safe = (
            method != "POST"
            or path == "/users/@me/channels"
            or bool(
                is_message and data and data.get("nonce") and data.get("enforce_nonce")
            )
        )
        http = self._http
        try:
            if http is None:
                trace = aiohttp.TraceConfig()
                trace.on_request_end.append(_no_ambiguous_retry)
                http = self._http = _Session(
                    asyncio.get_running_loop(), http_trace=trace,
                    proxy=_proxy_for(self.base),
                )
                await http.open(self.token)

            async def send():
                kwargs = {"params": query} if query else {}
                kwargs["trace_request_ctx"] = {"retry_safe": retry_safe}
                handles = [
                    discord.File(io.BytesIO(blob), filename=name)
                    for name, blob in files
                ]
                try:
                    if handles:
                        with discord.http.handle_message_parameters(
                            content=discord.utils.MISSING, files=handles
                        ) as multipart:
                            # Preserve nested forum payloads, nonces, references
                            # and Components V2 JSON exactly as the caller gave it.
                            payload = dict(data)
                            if "message" in payload:
                                payload["message"] = dict(payload["message"])
                            target = payload.get("message", payload)
                            target.setdefault(
                                "attachments",
                                [handle.to_dict(i) for i, handle in enumerate(handles)],
                            )
                            multipart.multipart[0]["value"] = discord.utils._to_json(
                                payload
                            )
                            return await http.request(
                                route, files=handles, form=multipart.multipart, **kwargs
                            )
                    return (
                        await http.request(route, json=data, **kwargs)
                        if data is not None
                        else await http.request(route, **kwargs)
                    )
                finally:
                    for handle in handles:
                        handle.close()

            return await asyncio.wait_for(send(), timeout=timeout)
        except discord.RateLimited as exc:
            raise DiscordError(429, retry_after=exc.retry_after) from None
        except discord.HTTPException as exc:
            retry = (
                exc.response.headers.get("Retry-After") if exc.status == 429 else None
            )
            raise DiscordError(exc.status, exc.code, retry) from None
        except (asyncio.TimeoutError, TimeoutError):
            # Cancellation during an SDK global-limit sleep can leave its gate
            # closed. Replace the session so later requests can recover.
            if self._http is http:
                self._http = None
            if http:
                await http.close()
            raise TimeoutError("Discord request timed out") from None
        except aiohttp.ClientError:
            raise OSError("Discord connection failed") from None

    def close(self):
        if self._loop.is_closed():
            return

        async def stop():
            if self._http:
                await self._http.close()
            pending = [
                t for t in asyncio.all_tasks() if t is not asyncio.current_task()
            ]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

        try:
            asyncio.run_coroutine_threadsafe(stop(), self._loop).result(timeout=3)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=3)
            if not self._thread.is_alive():
                self._loop.close()


_clients = {}
_lock = threading.Lock()


def transport(token, base=API):
    with _lock:
        key = (token, base)
        if key not in _clients:
            _clients[key] = Rest(token, base)
        return _clients[key]


@atexit.register
def _close_all():
    for client in tuple(_clients.values()):
        client.close()
