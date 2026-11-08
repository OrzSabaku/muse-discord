"""Keep the legacy approval interaction worker responsive during a slow
main-loop tick. Native settings callbacks run on discord.py's event loop.
"""

import contextlib
import io
import json
import os
import signal
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "_lib"))
import monitor_harness as H  # noqa: E402
from common import scratch  # noqa: E402

tmp = scratch("muse-plumb-main-")
mod = H.load_monitor(tmp)
mod.api.routes["/users/@me"] = {"id": H.BOT, "username": "musebot"}
mod.BOT_ID = mod.BOT_USER_ID = None
mod.discover_channels = lambda previous=None: (
    {"C1": H.chan()},
    {H.G1: {"name": "Srv", "system_channel_id": None, "joined_at": None}},
)
with open(mod.STATE_FILE, "w") as f:
    json.dump({"guild_joined": {H.G1: 0}, "known_guilds": {H.G1: "Srv"}}, f)


in_outbox = threading.Event()
acked = threading.Event()


class Stop(BaseException):
    pass


def slow_outbox(state):
    in_outbox.set()
    acked.wait(3)  # only acks that come while the main loop is blocked
    raise Stop()


mod._OUTBOX.process = slow_outbox
res = {"acks": [], "handled_on": []}


def handle(ix, ctx):
    res["handled_on"].append(threading.current_thread().name)
    return {
        "type": 4 if ix["type"] == 2 else 7,
        "data": {
            "content": f"page for {ix['id']}",
            "flags": 64,
            "seen_guilds": [g["id"] for g in ctx.guilds()],
        },
    }


for key in ("/test-settings", "test-settings"):
    mod._INTERACTIONS.register(
        key, lambda ix, rest, uid, record: handle(ix, mod.settings_context()), owner_only=False
    )


def cb(i, resp):
    res["acks"].append({"id": i["id"], "after": time.time() - res["put_at"][i["id"]], "resp": resp})
    if len(res["acks"]) == 3:
        acked.set()


mod._INTERACTION_RESPONDER.send = cb
res["put_at"] = {}


def fake_gw():
    status = mod._GATEWAY.status
    in_outbox.wait(5)
    for n, (t, data) in enumerate(
        [
            (2, {"name": "test-settings", "type": 1}),
            (3, {"custom_id": "test-settings:p:e", "component_type": 2}),
            (5, {"custom_id": "test-settings:m:x", "components": []}),
        ]
    ):
        iid = f"90{n}"
        res["put_at"][iid] = time.time()
        status["interactions"].put(
            {"id": iid, "token": "tok", "type": t, "data": data, "user": {"id": H.OWNER}}
        )
        time.sleep(0.05)


mod._GATEWAY.run = fake_gw


def on_alarm(*_):
    raise Stop()


signal.signal(signal.SIGALRM, on_alarm)
signal.setitimer(signal.ITIMER_REAL, 10)
out = io.StringIO()
with contextlib.redirect_stdout(out), contextlib.suppress(Stop):
    mod.main()
res["out"] = out.getvalue()
print(json.dumps(res))
