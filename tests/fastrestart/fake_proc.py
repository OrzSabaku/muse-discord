"""Stand-in for monitor.py / presence.py. Records every start in
starts.log, writes monitor.pid and the heartbeat like the real monitor.
$FAKE_MODE: ok (default) or token (prints TOKEN REJECTED, exits 3)."""
import os
import sys
import time

name = os.path.basename(sys.argv[0])
here = os.getcwd()
with open(os.path.join(here, "starts.log"), "a") as f:
    f.write(f"{name} {os.getpid()}\n")
mode = os.environ.get("FAKE_MODE", "ok")
if mode == "token":
    print("TOKEN REJECTED: Discord refused the bot token (401 from "
          "/users/@me); it was probably reset. Exiting.", flush=True)
    sys.exit(3)
if name == "presence.py":
    print("READY - fake presence online", flush=True)
else:
    with open(os.path.join(here, "monitor.pid"), "w") as f:
        f.write(str(os.getpid()))
    print("Bot user id: 999", flush=True)
hb = os.path.join(here, "monitor.heartbeat")
while True:
    if name == "monitor.py":
        with open(hb, "a"):
            pass
        os.utime(hb, None)
    time.sleep(1)
