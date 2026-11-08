"""Durable proactive state and nonce publication, including damaged-store recovery."""

import glob
import json
import os
import time
from collections.abc import Callable
from typing import Any

from muse_discord.proactive.model import VERSION


class ProactiveStore:
    def __init__(self, path, nonce_path, write_private, clock):
        self.path_source = path
        self.nonce_path = nonce_path
        self.write_private = write_private
        self.clock = clock
        self.on_load: Callable[[dict], Any] | None = None
        self._data = None
        self._path = None
        self.dirty = False
        self.flushed = 0.0
        self._nonce_written = None
        self.alert = None
        self.readonly = False

    def path(self):
        return self.path_source()

    def data(self):
        """proactive.json, read once (re-read when the path changes)."""
        p = self.path()
        if self._data is None or self._path != p:
            self._path = p
            d = None
            try:
                with open(p) as f:
                    d = json.load(f)
            except FileNotFoundError:
                pass
            except Exception as e:  # any damage (deep nesting too)
                d = self._set_aside(p, f"{type(e).__name__}: {e}"[:200])
            if d is not None and not isinstance(d, dict):
                d = self._set_aside(p, "not a JSON object")
            d = d if isinstance(d, dict) else {}
            for k, empty in (
                ("spent", {}),
                ("sent", []),
                ("people", {}),
                ("opted_out", {}),
                ("asks", {}),
            ):
                if not isinstance(d.get(k), type(empty)):
                    d[k] = empty
            if not self.slot_ok(d.get("slot")):
                d["slot"] = None
            d["v"] = VERSION
            self._data = d
            if self.on_load is not None:
                self.on_load(self._data)
        return self._data

    def _set_aside(self, p, why):
        """A store that does not parse: kept aside (never overwritten),
        the owners told once; no heartbeat while it is there."""
        bad = f"{p}.bad-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}"
        try:
            os.replace(p, bad)
        except OSError:
            bad, self.readonly = p, True
        self.alert = (
            f"proactive.json could not be read ({why}), so it was kept as "
            f"{os.path.basename(bad)}. It holds who asked not to be "
            f"messaged first: until it is fixed and put back (or deleted), "
            f"the bot reaches out to nobody."
        )
        print(f"Proactive: {self.alert}", flush=True)

    @staticmethod
    def slot_ok(s):
        return (
            isinstance(s, dict)
            and isinstance(s.get("id"), str)
            and isinstance(s.get("nonce"), str)
            and isinstance(s.get("targets"), dict)
            and isinstance(s.get("posts"), dict)
            and isinstance(s.get("expires"), (int, float))
        )

    def blocked(self):
        """A bad store is set aside (or could not be): fail closed."""
        p = self.path()
        return self.readonly or bool(glob.glob(glob.escape(p) + ".bad-*"))

    def save(self):
        if self.readonly:
            return
        try:
            self.write_private(self.path(), self.data())
            self.dirty = False
            self.flushed = self.clock()
        except OSError as e:
            print(f"Could not write proactive.json: {e}", flush=True)

    def hook_nonces(self):
        """{slot id: nonce} for the wake hook: the live, unspent slot
        only."""
        try:
            s = self.data().get("slot")
            if s and not s.get("spent") and s.get("expires", 0) > self.clock():
                return {s["id"]: s["nonce"]}
        except Exception:
            pass
        return {}

    def secret_nonces(self):
        """The slot nonce while any slot record holds it (spent too), for
        the monitor's outgoing secret filter."""
        try:
            s = self.data().get("slot")
            return {s["nonce"]} if s is not None and self.slot_ok(s) else set()
        except Exception:
            return set()

    def write_nonces(self):
        """proactive_nonce.json (mode 600, the wake hook's): written when
        it changes."""
        body = self.hook_nonces()
        if body == self._nonce_written:
            return
        try:
            self.write_private(self.nonce_path(), body)
            self._nonce_written = body
        except OSError as e:
            print(f"Could not write proactive_nonce.json: {e}", flush=True)
