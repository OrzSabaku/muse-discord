"""Offline harness for the settings suites (settings, settingsplumb,
monitorfixes): import muse_settings and monitor.py from $MUT_DIR with a
temp config, every Discord call stubbed and every file redirected to a
scratch dir."""
import contextlib
import importlib.util
import io
import os
import re
import sys
import time

from common import MUT_DIR as MUT, redirect_files, scratch

sys.path.insert(0, MUT)
import muse_settings as MS  # noqa: E402,F401 (re-exported)

BOT = "999000000000000001"
OWNER = "111000000000000001"
U1 = "222000000000000001"
U2 = "222000000000000002"
OTHER_BOT = "333000000000000001"
G1, G2 = "500000000000000001", "600000000000000001"
CH, CH2, CAT, THREAD = ("701000000000000001", "702000000000000001",
                        "800000000000000001", "703000000000000001")
CH_G2 = "704000000000000001"
ROLE = "900000000000000001"
EPOCH = 1420070400000
_seq = [0]


def sf(age=0.0):
    _seq[0] += 1
    return str(((int((time.time() - age) * 1000) - EPOCH) << 22) + _seq[0])


def tmpdir(prefix):
    return scratch("muse-set-" + prefix)


def write(path, text):
    with open(path, "w") as f:
        f.write(text)
    return path


def bump(path, secs=2):
    """Make a rewrite visible to an mtime check even within one tick."""
    t = time.time() + secs + _seq[0] * 0.001
    _seq[0] += 1
    os.utime(path, (t, t))


class HTTPErr(Exception):
    def __init__(self, code):
        super().__init__(f"HTTP Error {code}")
        self.code = code
        self.headers = {}

    def read(self):
        return b"{}"


class FakeAPI:
    def __init__(self):
        self.posts = []
        self.gets = []
        self.members = {}   # (guild, user) -> roles; absent = 404
        self.history = {}   # channel -> [messages]

    def get(self, path):
        self.gets.append(path)
        m = re.fullmatch(r"/guilds/(\d+)/members/(\d+)", path)
        if m:
            if (m[1], m[2]) in self.members:
                return {"roles": self.members[(m[1], m[2])]}
            raise HTTPErr(404)
        m = re.match(r"/channels/(\d+)/messages", path)
        if m:
            return list(reversed(self.history.get(m[1], [])))
        raise HTTPErr(404)

    def post(self, path, data):
        self.posts.append((path, data))
        if path == "/users/@me/channels":
            return {"id": "4" + data["recipient_id"][1:],
                    "recipients": [{"username": "owner"}]}
        return {"id": sf()}

    def channel_posts(self, cid):
        return [d for p, d in self.posts if p == f"/channels/{cid}/messages"]

    def dm_posts(self):
        return [d for p, d in self.posts
                if p.startswith("/channels/4") and p.endswith("/messages")]


def load_monitor(cfg_text, tmp=None, channels=True):
    tmp = tmp or tmpdir("mon_")
    cfg = write(os.path.join(tmp, "config.yaml"), cfg_text)
    os.environ["MUSE_DISCORD_CONFIG"] = cfg
    sys.argv = ["x", "dummy-token"]
    _seq[0] += 1
    spec = importlib.util.spec_from_file_location(
        f"settings_mut_monitor_{_seq[0]}", os.path.join(MUT, "monitor.py"))
    mod = importlib.util.module_from_spec(spec)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        spec.loader.exec_module(mod)
    mod.import_out = out.getvalue()
    redirect_files(mod, tmp)
    for attr, fn in [("STATE_FILE", "state.json"),
                     ("QUEUE_FILE", "reply_queue.json"),
                     ("OUTBOX_FILE", "reply_outbox.json"),
                     ("OUTBOX_PROCESSING_FILE",
                      "reply_outbox.processing.json"),
                     ("OUTBOX_REJECTED_FILE", "reply_outbox.rejected.json"),
                     ("QUARANTINE_FILE", "quarantine.json"),
                     ("LEDGER_FILE", "mention_ledger.json"),
                     ("LOCK_FILE", "reply.lock"),
                     ("HEARTBEAT_FILE", "monitor.heartbeat"),
                     ("MEDIA_DIR", "media"), ("GENERATED_DIR", "generated")]:
        setattr(mod, attr, os.path.join(tmp, fn))
    api = FakeAPI()
    mod.api = api
    mod.real_api_post = mod.api_post   # for tests of the helper itself
    mod.api_get = api.get
    mod.api_post = api.post
    mod.send_typing = lambda cid: None
    mod.collect_media = lambda m: ([], [], [])
    mod.BOT_ID = mod.BOT_USER_ID = BOT
    mod.BOT_NAME = "muse"
    mod.cfg_path = cfg
    mod.tmp = tmp
    if channels:
        mod.CHANNELS = {
            CH: {"name": "general", "guild_id": G1, "guild_name": "Lounge",
                 "can_send": True},
            CH2: {"name": "random", "guild_id": G1, "guild_name": "Lounge",
                  "can_send": True},
            THREAD: {"name": "general > plans", "guild_id": G1,
                     "guild_name": "Lounge", "can_send": True,
                     "thread": True, "parent_id": CH},
            CH_G2: {"name": "chat", "guild_id": G2, "guild_name": "Other",
                    "can_send": True},
        }
        mod.CHANNEL_CATEGORY.update({CH: CAT})
        mod.GUILDS = {G1: {"name": "Lounge"}, G2: {"name": "Other"}}
    return mod


def state(mod):
    with contextlib.redirect_stdout(io.StringIO()):
        st = mod.load_state()
    st["guild_joined"] = {G1: 0, G2: 0}
    return st


def author(uid, bot=False, name=None):
    return {"id": uid, "username": name or f"u{uid[-2:]}", "bot": bot}


def gmsg(uid, content="hi", mention=False, channel=CH, guild=G1, bot=False,
         roles=None, mid=None, reply_to_author=None, mentions=(), age=0.0):
    m = {"id": mid or sf(age), "channel_id": channel, "content": content,
         "author": author(uid, bot), "type": 0,
         "timestamp": "2026-10-18T00:00:00+00:00",
         "mentions": ([{"id": BOT}] if mention else [])
         + [{"id": x} for x in mentions]}
    if guild:
        m["guild_id"] = guild
    if roles is not None:
        m["member"] = {"roles": list(roles)}
    if reply_to_author:
        m["message_reference"] = {"message_id": sf(60)}
        m["referenced_message"] = {"id": "1", "author": {
            "id": reply_to_author}, "content": "earlier"}
    return m


def dmsg(uid, content="hi", bot=False, channel="400000000000000077"):
    return {"id": sf(), "channel_id": channel, "content": content,
            "author": author(uid, bot), "type": 0,
            "timestamp": "2026-10-18T00:00:00+00:00", "mentions": []}


@contextlib.contextmanager
def quiet():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield buf
