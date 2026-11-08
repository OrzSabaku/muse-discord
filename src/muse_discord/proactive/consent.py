"""Personal consent requests: durable retries, recipient binding and one-use buttons."""

import secrets
import threading
from http import HTTPStatus

from muse_discord import muse_settings as MS
from muse_discord.message_models import DeliveryOptions
from muse_discord.proactive.model import ASK_KEY, ASK_SECS, ASK_SEND_SECS, ASKS_MAX, ASKS_PER_DAY
from muse_discord.proactive.store import ProactiveStore


class ConsentRequests:
    def __init__(self, store: ProactiveStore, settings, send_reply, backing_off, clock):
        self.store = store
        self.settings = settings
        self.send_reply = send_reply
        self.backing_off = backing_off
        self.clock = clock
        self._lock = threading.Lock()
        self._live = {}
        store.on_load = self.sync

    def sync(self, data=None):
        """The open asks, for on_button (the interaction thread)."""
        d, now = self.store.data() if data is None else data, self.clock()
        live = {
            t: (a.get("user_id"), a.get("at", 0))
            for t, a in (d.get("asks") or {}).items()
            if isinstance(a, dict)
            and a.get("sent")
            and not a.get("done")
            and now - a.get("at", 0) < ASK_SECS
        }
        with self._lock:
            self._live = live

    def close(self, uid, what):
        for a in self.store.data()["asks"].values():
            if a.get("user_id") == uid and a.get("what") == what:
                a["done"] = True
        self.sync()

    def ask(self, uid, cid, what, tz=None):
        """DM person uid (in their DM channel cid) the monitor's own
        question with a Yes button: what "in" (message them first
        again) or "tz" (use tz for their quiet hours). Sent by
        send_asks; at most ASKS_PER_DAY a person, one open per kind."""
        d, now = self.store.data(), self.clock()
        asks = d["asks"]
        for t in [
            t
            for t, a in asks.items()
            if not isinstance(a, dict) or now - a.get("at", 0) >= ASK_SECS
        ]:
            del asks[t]
        mine = [a for a in asks.values() if a.get("user_id") == uid]
        if any(a.get("what") == what and not a.get("done") and a.get("tz") == tz for a in mine):
            return  # that very question is open already
        if len(mine) >= ASKS_PER_DAY:
            print(
                f"Proactive: no new button for {uid} ({what}): {ASKS_PER_DAY} in a day already.",
                flush=True,
            )
            return
        self.close(uid, what)  # a newer question replaces it
        asks[secrets.token_hex(8)] = {
            "user_id": uid,
            "channel_id": cid,
            "what": what,
            "tz": tz,
            "at": now,
            "sent": False,
            "n": 0,
            "next": 0,
        }
        for old in sorted(asks, key=lambda t: asks[t]["at"])[:-ASKS_MAX]:
            del asks[old]
        self.store.save()
        print(f"Proactive: asking {uid} with a button ({what}).", flush=True)

    def ask_text(self, a):
        if a["what"] == "in":
            q = "Okay if I message you first again now and then?"
        else:
            default = self.settings().get("proactive.timezone", MS.Scope.everywhere())
            tz = a.get("tz") or f"the default time zone ({default})"
            q = f"Should I use {tz} for the night hours when I don't message you first?"
        return (
            f"{q} Tap **Yes** if so; otherwise nothing changes. "
            f"(Only you can tap it, for 24 hours.)"
        )

    def send_pending(self, now):
        """Send the asks not sent yet (each pass of the main loop);
        retried for ASK_SEND_SECS, then dropped."""
        d = self.store.data()
        owed = [
            (t, a)
            for t, a in d["asks"].items()
            if isinstance(a, dict)
            and not a.get("sent")
            and not a.get("done")
            and a.get("next", 0) <= now
        ]
        if not owed or self.backing_off():
            return
        for tok, a in owed:
            try:
                self.send_reply(
                    a["channel_id"],
                    self.ask_text(a),
                    None,
                    components=[
                        {
                            "type": 1,
                            "components": [
                                {
                                    "type": 2,
                                    "style": 1,
                                    "label": "Yes",
                                    "custom_id": f"{ASK_KEY}:yes:{tok}",
                                }
                            ],
                        }
                    ],
                    delivery=DeliveryOptions(nonce=tok, mentions={"parse": []}),
                )
                a["sent"], a["at"] = True, now
            except Exception as e:
                a["n"] = a.get("n", 0) + 1
                a["next"] = now + min(30, 2 ** a["n"])
                if (
                    getattr(e, "code", None) == HTTPStatus.FORBIDDEN
                    or now - a["at"] >= ASK_SEND_SECS
                ):
                    a["done"] = True
                print(f"Proactive: could not send the button to {a['user_id']} ({e}).", flush=True)
            self.store.dirty = True
        self.sync()
        self.store.save()

    def on_button(self, ix, rest, uid, record):
        """mdpro:yes:<token>, on the interaction thread: only the person
        asked, once, while it is open. record() hands it to the main
        loop (apply_button)."""
        verb, _, tok = str(rest).partition(":")
        with self._lock:
            rec = self._live.get(tok)
            if verb != "yes" or rec is None or self.clock() - rec[1] >= ASK_SECS:
                return None
            if rec[0] != str(uid):
                return {
                    "type": 4,
                    "data": {
                        "content": "That button is not for you.",
                        "flags": 64,
                        "allowed_mentions": {"parse": []},
                    },
                }
            del self._live[tok]
        record({"kind": "proactive_yes", "token": tok, "by": str(uid)})
        original = (ix.get("message") or {}).get("content") or ""
        line = "**Yes**: done."
        return {
            "type": 7,
            "data": {
                "content": original[: 1990 - len(line)] + "\n\n" + line,
                "components": [],
                "allowed_mentions": {"parse": []},
            },
        }

    def apply_button(self, rec):
        """Main loop: the person's own Yes (on_button). Checked again
        here: the ask open, theirs."""
        d = self.store.data()
        tok = str(rec.get("token") or "")
        a = d["asks"].get(tok)
        if (
            not isinstance(a, dict)
            or a.get("done")
            or not a.get("sent")
            or a.get("user_id") != str(rec.get("by"))
            or self.clock() - a.get("at", 0) >= ASK_SECS
        ):
            return
        a["done"] = True
        uid = a["user_id"]
        if a["what"] == "in":
            d["opted_out"].pop(uid, None)
            print(f"Proactive: {uid} opted back in (their button).", flush=True)
        else:
            p = d["people"].setdefault(uid, {})
            if a.get("tz"):
                p["tz"] = a["tz"]
            else:
                p.pop("tz", None)
            print(f"Proactive: {uid} confirmed their time zone.", flush=True)
        self.sync()
        self.store.save()
