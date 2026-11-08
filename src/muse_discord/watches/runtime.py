"""Coordinate watch matching, fire creation and restart recovery.

Permission checks precede store edits; queue publication follows them. Mutable
candidate matches and timers belong to this coordinator (or an explicit shared
state supplied by the application), not to an imported monitor namespace.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from muse_discord import muse_watches as W
from muse_discord.conversation import HISTORY_ATTACHED, _snowflake_ts, msg_link
from muse_discord.message_models import LEDGER_TERMINAL, LedgerContext
from muse_discord.storage import read_json as _read_json
from muse_discord.watches.audience import WatchAudience
from muse_discord.watches.fires import FirePolicy, WatchFireBuilder, due_entries
from muse_discord.watches.queue import WatchQueue
from muse_discord.watches.web import WebWatchWork

WATCH_TICK_SECS = 2.0
WATCH_UPKEEP_SECS = 15.0
WATCH_MATCH_QUEUE_LIMIT = 1000


def _place(r):
    return (r["requester"], r["channel_id"], r.get("guild_id"), bool(r.get("dm")))


def _restored_item(state, f):
    q = dict(f["item"])
    q["history"] = ((state.get("history") or {}).get(q.get("channel_id")) or [])[-HISTORY_ATTACHED:]
    return q


@dataclass
class WatchState:
    matches: list = field(default_factory=list)
    clock: dict = field(default_factory=lambda: {"next": 0.0, "upkeep": 0.0})
    web_cache: dict = field(default_factory=lambda: {"body": None, "path": None})


@dataclass
class WatchRuntime:
    store: W.Store
    audience: WatchAudience
    fires: WatchFireBuilder
    queue: WatchQueue
    web: WebWatchWork
    entries: Callable[[], dict]
    state: WatchState = field(default_factory=WatchState)

    def tick(self, state, now=None):
        """Main loop: fire due reminders and watch matches (grouped per
        person and place: several at once are one message), end expired
        watches, and (every WATCH_UPKEEP_SECS) settle or re-queue fires.

        Two phases, so no Discord request is ever made under the store's
        lock (bin/watches.py and bin/watch-report.py wait on it) or inline
        in classify_message: first, with no lock, the candidate matches
        classify_message found are checked against what their requester
        can read, and whether the bot would answer each person in each
        place that has something to fire; then, under the lock, what is due
        is fired. A fire whose place was not checked in the first phase is
        held until the next tick."""
        now = time.time() if now is None else now
        if now < self.state.clock["next"] and not self.state.matches:
            return
        self.state.clock["next"] = now + WATCH_TICK_SECS
        store = self.store
        data = store.read()
        cands = list(self.state.matches)
        del self.state.matches[:]
        matches = [(wid, m) for wid, m, ctx in cands if self.match_ok(data, wid, ctx)]
        verdicts = {}
        for key, name in self.places_due(data, matches, now).items():
            verdicts[key] = self.audience.fire_allowed(key[0], name, *key[1:])
        queued = []
        held_due = any(
            isinstance(h, dict) and h.get("next", 0) <= now
            for h in (data.get("held") or {}).values()
        )
        if matches or W.due_reminders(data, now) or W.expired_watches(data, now) or held_due:
            with store.edit() as data:
                counts = {}
                queued += self.fires.release_held(state, data, now, verdicts, counts)
                entries = due_entries(data, matches, now)
                queued += self.fires.make(
                    state,
                    data,
                    entries,
                    policy=FirePolicy(now=now, verdicts=verdicts, counts=counts),
                )
        if self.web.has_work(data, now):
            with store.edit() as data:
                queued += self.web.tick(state, data, now, verdicts, self.fires)
        self.web.write_due(store.read(), now)
        for q in queued:
            self.queue.enqueue(state, q)
        if now >= self.state.clock["upkeep"]:
            self.state.clock["upkeep"] = now + WATCH_UPKEEP_SECS
            self.upkeep(state, now)

    def places_due(self, data, matches, now):
        """{(requester, channel, guild, dm): name} of every place that has
        something to fire this tick (reminders due, matches, held fires
        due), for watch_tick's first phase."""
        out = {}
        recs = [r for _, r in W.due_reminders(data, now)]
        recs += [data["items"][w] for w, _ in matches if w in data["items"]]
        for h in (data.get("held") or {}).values():
            if isinstance(h, dict) and h.get("next", 0) <= now and h.get("es"):
                recs.append(h["es"][0][0])
        # Web watches that may fire this tick: reports waiting (read, not
        # taken: the tool writes the file atomically), and expired checks.
        ids = {
            r.get("id")
            for r in _read_json(self.web.results_path, list) or []
            if isinstance(r, dict)
        }
        for i, r in data["items"].items():
            chk = r.get("check") if isinstance(r, dict) else None
            if i in ids or (chk and chk.get("expires", 0) <= now):
                recs.append(r)
        for r in recs:
            if isinstance(r, dict) and r.get("requester"):
                out[_place(r)] = r.get("name") or r["requester"]
        return out

    def match_ok(self, data, wid, ctx):
        """A candidate match (classify_message): the watch is still there,
        its requester can read the channel now, and when the notice goes to
        a server channel, the match is in that channel (or a thread of it)
        or in one everyone in the server can read (channel_public). Fails
        closed."""
        r = data["items"].get(wid)
        if not isinstance(r, dict):
            return False
        gid, cid, parent = ctx.get("gid"), ctx.get("cid"), ctx.get("parent")
        try:
            if not self.audience.access.can_view(gid, r["requester"], cid):
                return False
            return (
                bool(r.get("dm"))
                or r.get("channel_id") in (cid, parent)
                or self.audience.access.is_public(gid, cid)
            )
        except Exception as e:
            print(f"Watch check error: {e}", flush=True)
            return False

    def remove_fires(self, drop):
        if drop:
            with self.store.edit() as d:
                for fid in drop:
                    d["fires"].pop(fid, None)

    def upkeep(self, state, now=None):
        """Fires the ledger settled (or nobody answered for a day) are
        dropped; one missing from the queue (a stop between saving it and
        queueing it) is queued again."""
        now = time.time() if now is None else now
        data = self.store.read()
        if not data["fires"]:
            return
        try:
            queued = self.queue.queued_ids()
        except OSError, ValueError:
            return
        entries = self.entries()
        drop, lost, stale = [], [], set()
        for fid, f in data["fires"].items():
            e = entries.get(fid) or {}
            if e.get("status") in LEDGER_TERMINAL:
                if str(e.get("reason") or "").startswith("held for owner"):
                    continue  # never_share: kept for Send anyway (24 h)
                drop.append(fid)
            elif now - f.get("at", now) > W.FIRE_KEEP_SECS:
                # Nobody answered it in a day: it leaves the queue too (else
                # every later answer is refused and it is woken for forever).
                drop.append(fid)
                stale.add(fid)
                print(f"Reminder {fid} was never answered; dropped.", flush=True)
                self.fires.record(
                    fid,
                    "skipped",
                    "reminder never answered",
                    context=LedgerContext(
                        channel_id=f.get("channel_id"), author_id=f.get("requester")
                    ),
                )
            elif fid not in queued and isinstance(f.get("item"), dict):
                lost.append(_restored_item(state, f))
        self.remove_fires(drop)
        self.queue.drop_stale(state, stale)
        for q in lost:
            print(f"Reminder {q['message_id']} was not in the queue; queued again.", flush=True)
            self.queue.enqueue(state, q)

    def scan(self, msg, guild_id):
        """classify_message: check a server message against the Discord
        watches (never raises)."""
        try:
            self._scan(msg, guild_id)
        except Exception as e:
            print(f"Watch check error: {e}", flush=True)

    def _scan(self, msg, guild_id):
        """The cheap part, with no Discord request: a candidate match is a
        message that is not the requester's own (nor the bot's), came after
        the watch was set, while the watch has not fired within
        DEBOUNCE_SECS, in the watch's scope, with one of its words. Whether
        the requester can read that channel, and whether the notice may go
        where they asked, watch_tick checks before firing
        (match_ok). The notice links the message; it never quotes
        it."""
        if not guild_id:
            return
        items = self.store.read()["items"]
        if items and self.audience.owners_absent(guild_id):
            return  # a server none of the owners is in: nothing from it
        text = msg.get("content") or ""
        if not items or not text:
            return
        author = msg.get("author") or {}
        aid = str(author.get("id") or "")
        if aid == self.audience.response.conversation.bot_id:
            return
        cid = str(msg.get("channel_id") or "")
        info = self.audience.access.channels.get(cid) or {}
        parent = info.get("parent_id") if info.get("thread") else None
        ts = _snowflake_ts(msg.get("id"))
        now = time.time()
        pending = {c[0] for c in self.state.matches}
        for wid, r in items.items():
            if (
                not isinstance(r, dict)
                or r.get("kind") != "watch"
                or wid in pending
                or aid == r.get("requester")
                or r.get("until", 0) <= now
                or ts + 2 < r.get("created", now)
                or now - r.get("last_fired", 0) < W.DEBOUNCE_SECS
                or not W.watch_covers(r, guild_id, cid, parent)
            ):
                continue
            hit = W.match_text(r.get("words") or [], text)
            if not hit:
                continue
            if len(self.state.matches) >= WATCH_MATCH_QUEUE_LIMIT:
                return  # a flood: the rest of this tick's matches wait
            self.state.matches.append(
                (
                    wid,
                    {
                        "words": hit,
                        "link": msg_link(guild_id, cid, msg.get("id")),
                        "channel": "#" + str(info.get("name") or cid),
                        "author": author.get("username") or "someone",
                    },
                    {"gid": guild_id, "cid": cid, "parent": parent},
                )
            )
