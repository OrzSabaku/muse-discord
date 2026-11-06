"""Offline stand-in for the websockets package: a live fake Discord
gateway. Each connection gets HELLO, READY after an identify (op 2),
RESUMED after a resume (op 6), and an ACK for each heartbeat. The test
can push frames (push) or end the connection (push(("close", code)) /
push("end")). Every payload the client sends is recorded in SENT as
(connection index, monotonic time, decoded payload)."""
import asyncio
import json
import time

HB_MS = 100
CONNECTS = []
SENT = []
WS = []          # live connection objects, in order
CLOSED_BY_CLIENT = []


class ConnectionClosed(Exception):
    def __init__(self, code):
        super().__init__(f"closed with code {code}")

        class _R:
            pass
        self.rcvd = _R()
        self.rcvd.code = code
        self.code = code


class _WS:
    def __init__(self, idx):
        self.idx = idx
        self.inbox = asyncio.Queue()
        self.inbox.put_nowait({"op": 10, "d": {"heartbeat_interval": HB_MS}})
        self.seq = 0

    def push(self, item):
        self.inbox.put_nowait(item)

    async def _next(self):
        item = await self.inbox.get()
        if item == "end":
            return None
        if isinstance(item, tuple) and item[0] == "close":
            raise ConnectionClosed(item[1])
        return json.dumps(item)

    async def recv(self):
        r = await self._next()
        if r is None:
            raise ConnectionClosed(1000)
        return r

    async def send(self, data):
        p = json.loads(data)
        SENT.append((self.idx, time.monotonic(), p))
        op = p.get("op")
        if op == 2:
            self.seq += 1
            self.push({"op": 0, "t": "READY", "s": self.seq, "d": {
                "session_id": "S1", "resume_gateway_url": "wss://resume.test",
                "user": {"id": "999", "username": "musebot"}}})
        elif op == 6:
            self.seq += 1
            self.push({"op": 0, "t": "RESUMED", "s": self.seq, "d": {}})
        elif op == 1:
            self.push({"op": 11})

    async def close(self, *a, **kw):
        CLOSED_BY_CLIENT.append(self.idx)
        self.push("end")

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
        CONNECTS.append(self.url)
        ws = _WS(len(CONNECTS) - 1)
        WS.append(ws)
        return ws

    async def __aexit__(self, *a):
        return False


def connect(url, **kw):
    return _Conn(url)


def reset():
    CONNECTS.clear()
    SENT.clear()
    WS.clear()
    CLOSED_BY_CLIENT.clear()
