"""Offline harness: load monitor.py / presence.py from $MUT_DIR with every
network call stubbed and every file redirected to a scratch dir. Used by
the reliability, fastloop, updatedm and settingsplumb suites."""
import importlib.util
import os
import sys
import time

from common import FAKEWS, MUT_DIR, redirect_files, scratch

sys.path.insert(0, FAKEWS)

BOT = "999"
OWNER = "111"
G1 = "G1"
EPOCH = 1420070400000
_seq = [0]


def sf(age_secs=0.0):
    """A snowflake for a message created age_secs ago (unique, ascending
    for equal ages)."""
    _seq[0] += 1
    return str(((int((time.time() - age_secs) * 1000) - EPOCH) << 22)
               + _seq[0])


class HTTPErr(Exception):
    def __init__(self, code, headers=None):
        super().__init__(f"HTTP Error {code}")
        self.code = code
        self.headers = headers or {}

    def read(self):
        return b"{}"


class FakeAPI:
    def __init__(self, mod):
        self.mod = mod
        self.msgs = {}      # channel id -> [message dicts]
        self.routes = {}    # exact path -> response
        self.errors = {}    # path prefix -> exception (or callable)
        self.calls = []
        self.posts = []

    def _err(self, path):
        for prefix, exc in self.errors.items():
            if path.startswith(prefix):
                return exc() if callable(exc) else exc
        return None

    def _get(self, path):
        self.calls.append(path)
        err = self._err(path)
        if err is not None:
            raise err
        if path in self.routes:
            return self.routes[path]
        if path.startswith("/channels/") and "/messages" in path:
            cid = path.split("/")[2]
            q = {}
            if "?" in path:
                for kv in path.split("?", 1)[1].split("&"):
                    k, _, v = kv.partition("=")
                    q[k] = v
            limit = int(q.get("limit", 50))
            msgs = sorted(self.msgs.get(cid, []), key=lambda m: int(m["id"]))
            if "after" in q:
                msgs = [m for m in msgs if int(m["id"]) > int(q["after"])]
                msgs = msgs[:limit]
            else:
                msgs = msgs[-limit:]
            return list(reversed(msgs))
        raise HTTPErr(404)

    def get(self, path):
        return self.mod._api_call(lambda: self._get(path))

    def post(self, path, data):
        self.posts.append((path, data))
        err = self._err(path)
        if err is not None:
            raise err
        if path == "/users/@me/channels":
            return {"id": "DM1", "recipients": [{"username": "owner"}]}
        return {"id": sf()}


def write_cfg(tmp, extra=""):
    path = os.path.join(tmp, "config.yaml")
    with open(path, "w") as f:
        f.write(f"""owners:
  user_ids: ["{OWNER}"]
behavior:
  coalesce_seconds: 0.01
engine:
  rest_poll_seconds: 1
plugins:
  media_generation: {{enabled: false}}
""" + extra)
    return path


def load_monitor(tmp=None, cfg_extra=""):
    tmp = tmp or scratch("muse-run-")
    os.environ["MUSE_DISCORD_CONFIG"] = write_cfg(tmp, cfg_extra)
    sys.argv = ["x", "dummy-token"]
    _seq[0] += 1
    spec = importlib.util.spec_from_file_location(
        f"mut_monitor_{_seq[0]}", os.path.join(MUT_DIR, "monitor.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    redirect_files(mod, tmp)
    for attr, fn in [("STATE_FILE", "state.json"),
                     ("QUEUE_FILE", "reply_queue.json"),
                     ("OUTBOX_FILE", "reply_outbox.json"),
                     ("QUARANTINE_FILE", "quarantine.json"),
                     ("LEDGER_FILE", "mention_ledger.json"),
                     ("PIDFILE", "monitor.pid"),
                     ("HEARTBEAT_FILE", "monitor.heartbeat"),
                     ("MEDIA_DIR", "media"),
                     ("GENERATED_DIR", "generated"),
                     ("PLUGINS_DIR", "noplugins")]:
        setattr(mod, attr, os.path.join(tmp, fn))
    mod.api = FakeAPI(mod)
    mod.api_get = mod.api.get
    mod.api_post = mod.api.post
    mod.send_typing = lambda cid: None
    mod.collect_media = lambda m: ([], [], [])
    mod.get_reply_context = lambda m: None
    mod.BOT_ID = mod.BOT_USER_ID = BOT
    mod.BOT_NAME = "musebot"
    mod.TMP = tmp
    return mod


def load_presence(tmp=None):
    tmp = tmp or scratch("muse-run-")
    os.environ["MUSE_DISCORD_CONFIG"] = write_cfg(tmp)
    sys.argv = ["x", "dummy-token"]
    _seq[0] += 1
    spec = importlib.util.spec_from_file_location(
        f"mut_presence_{_seq[0]}", os.path.join(MUT_DIR, "presence.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def msg(cid, author_id, content="", mention=False, gid=G1, bot=False,
        age=600.0, mid=None):
    m = {"id": mid or sf(age), "channel_id": cid,
         "author": {"id": author_id, "username": f"user{author_id}",
                    "bot": bot},
         "content": content,
         "mentions": [{"id": BOT}] if mention else [],
         "timestamp": "2026-10-17T00:00:00+00:00", "type": 0}
    if gid:
        m["guild_id"] = gid
    return m


def chan(name="general", can_send=True, gid=G1):
    return {"name": name, "guild_id": gid, "guild_name": "Srv",
            "can_send": can_send}
