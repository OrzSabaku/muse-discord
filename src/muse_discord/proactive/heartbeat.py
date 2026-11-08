"""Independent server/DM deadlines and fair selection between private and public targets."""

from muse_discord.proactive.model import (
    INTERVALS,
    QUIET_SPREAD_SECS,
    TARGETS_MAX,
)
from muse_discord.proactive.store import ProactiveStore
from muse_discord.proactive.targets import TargetPolicy
from muse_discord.proactive.timing import in_quiet, parse_hours, quiet_end, zone


class HeartbeatSchedule:
    def __init__(self, policy: TargetPolicy, store: ProactiveStore, rng):
        self.policy = policy
        self.store = store
        self.rng = rng

    def due_heartbeats(self, now):
        """Persist independent deadlines; a faster server cannot advance
        another server's clock. Old single-clock state is ignored.
        """
        d = self.store.data()
        clocks = d.get("heartbeats")
        if not isinstance(clocks, dict):
            clocks = d["heartbeats"] = {}
            self.store.dirty = True
        scopes = self.policy.heartbeat_scopes()
        for key in set(clocks) - scopes.keys():
            del clocks[key]
            self.store.dirty = True
        due = set()
        for key, scope in scopes.items():
            freq = self.policy.st().get("proactive.frequency", scope)
            clock = clocks.get(key)
            if (
                not isinstance(clock, dict)
                or clock.get("freq") != freq
                or not isinstance(clock.get("next"), (int, float))
            ):
                clocks[key] = {"freq": freq, "next": self.schedule(now, freq)}
                self.store.dirty = True
            elif now >= clock["next"]:
                due.add(key)
        return due

    def advance_heartbeats(self, keys, now):
        """Advance only clocks whose turn was actually considered.
        Busy slots and targets left for the other side keep their turn.
        """
        clocks = self.store.data()["heartbeats"]
        for key in keys:
            clock = clocks[key]
            clock["next"] = self.schedule(now, clock["freq"])
            self.store.dirty = True

    def schedule(self, now, freq=None):
        """The next heartbeat: a uniform gap for the frequency, moved out
        of quiet hours (to within QUIET_SPREAD_SECS after they end)."""
        freq = freq or self.policy.every("proactive.frequency")
        lo, hi = INTERVALS.get(freq, INTERVALS["rarely"])
        t = now + self.rng.uniform(lo, hi)
        hours = parse_hours(self.policy.every("proactive.quiet_hours"))
        tz = zone(self.policy.every("proactive.timezone"))
        if in_quiet(t, hours, tz):
            t = quiet_end(t, hours, tz) + self.rng.uniform(0, min(lo, QUIET_SPREAD_SECS))
        return t

    def pick_side(self, targets):
        """One slot holds private targets (owner and DMs) or public ones
        (channels), never both, so nothing private shares a run with a
        public room: alternately, else whichever side has any."""

        def shuffled(kind):  # past TARGETS_MAX, a different few each time
            ts = [t for t in targets if t["type"] == kind]
            return self.rng.sample(ts, len(ts))

        priv = [t for t in targets if t["type"] == "owner"] + shuffled("dm")
        pub = shuffled("channel")
        first, then = (pub, priv) if self.store.data().get("side") == "private" else (priv, pub)
        return (first or then)[:TARGETS_MAX]
