"""Offline stand-in for the websockets package: scripted connections.

SCRIPTS: list of connections, each a list of steps:
  dict            -> delivered as one JSON frame (recv / async for)
  ("close", code) -> raises ConnectionClosed(code) at that point
  "end"           -> normal end of iteration
When SCRIPTS runs out, connect() raises Exhausted (a BaseException, so it
escapes the code's `except Exception` reconnect loops).
"""
import asyncio
import json

SCRIPTS = []
CONNECTS = []   # urls connected to, in order
SENT = []       # (connection index, decoded payload)


class Exhausted(BaseException):
    pass


class _Close:
    def __init__(self, code):
        self.code = code


class ConnectionClosed(Exception):
    def __init__(self, code):
        super().__init__(f"closed with code {code}")
        self.rcvd = _Close(code)
        self.code = code


class _WS:
    def __init__(self, idx, steps):
        self.idx = idx
        self.steps = list(steps)

    async def _next(self):
        await asyncio.sleep(0)
        if not self.steps:
            return None
        step = self.steps.pop(0)
        if step == "end":
            return None
        if isinstance(step, tuple) and step[0] == "close":
            raise ConnectionClosed(step[1])
        return json.dumps(step)

    async def recv(self):
        r = await self._next()
        if r is None:
            raise ConnectionClosed(1000)
        return r

    async def send(self, data):
        SENT.append((self.idx, json.loads(data)))

    def __aiter__(self):
        return self

    async def __anext__(self):
        r = await self._next()
        if r is None:
            raise StopAsyncIteration
        return r


class _Conn:
    def __init__(self, url):
        self.url = url

    async def __aenter__(self):
        if not SCRIPTS:
            raise Exhausted()
        CONNECTS.append(self.url)
        return _WS(len(CONNECTS) - 1, SCRIPTS.pop(0))

    async def __aexit__(self, *a):
        return False


def connect(url, **kw):
    return _Conn(url)


def reset(scripts):
    SCRIPTS[:] = scripts
    CONNECTS.clear()
    SENT.clear()


# Backoff sleeps become instant; long ones (heartbeat intervals) stay real
# so the heartbeat task idles until it is cancelled.
_real_sleep = asyncio.sleep


async def _fast_sleep(delay, *a, **kw):
    if delay >= 1000:
        return await _real_sleep(delay, *a, **kw)
    return await _real_sleep(0)


asyncio.sleep = _fast_sleep
