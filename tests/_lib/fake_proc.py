"""Stand-in for monitor.py under watchdog tests. Behavior from FAKE_MODE;
never touches the network."""

import os
import sys
import time

mode = os.environ.get("FAKE_MODE", "ok")
hb = os.path.join(os.getcwd(), "monitor.heartbeat")


def beat():
    with open(hb, "a"):
        pass
    os.utime(hb, None)


if mode == "token":
    print(
        "TOKEN REJECTED: Discord refused the bot token (401 from "
        "/users/@me); it was probably reset. Exiting.",
        flush=True,
    )
    sys.exit(3)
if mode == "pending":
    print(
        "Bot identity pending: /users/@me failed at boot (HTTP Error 429); retrying in-loop.",
        flush=True,
    )
else:
    print("Bot user id: 999", flush=True)
if mode == "gwoff":
    print(
        "GATEWAY DISABLED (code 4014): the Message Content privileged "
        "intent is not enabled. REST polling continues.",
        flush=True,
    )
beat()
while True:
    time.sleep(1)
    if mode != "hung":
        beat()
