"""Coalesce incoming messages, preserving gateway order and durable retry state.

Pending bursts remain in the caller's persisted state. Gateway activity is owned
by one tracker for the monitor lifetime; REST-only input waits the full window.
The component has no Discord client, configuration loader or monitor dependency.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from muse_discord.conversation import _who
from muse_discord.message_models import LedgerContext, MessagePlace, QueueContext
from muse_discord.message_queue import keep_last

BURST_QUIET_SECS = 1.5
DM_BURST_QUIET_SECS = 0.5
BOT_BURST_QUIET_SECS = 5.0
ACTIVITY_CACHE_LIMIT = 1000
PENDING_MESSAGE_ATTEMPTS = 3


@dataclass
class GatewayActivity:
    entries: dict = field(default_factory=dict)
    sequence: int = 0

    def clear(self):
        self.entries.clear()
        self.sequence = 0

    def note(self, channel_id, user_id, msg_id=None, *, now):
        """Record a gateway MESSAGE_CREATE (msg_id set) or TYPING_START from
        user_id in channel_id, in drain order."""
        if not channel_id or not user_id:
            return
        self.sequence += 1
        key = f"{channel_id}:{user_id}"
        act = dict(self.entries.get(key) or {})
        if msg_id:
            if act.get("msg_id") and int(act["msg_id"]) >= int(msg_id):
                return  # an older message arriving late changes nothing
            act.update(msg_id=msg_id, msg_at=now, msg_seq=self.sequence)
        else:
            act["typing_seq"] = self.sequence
        self.entries[key] = act
        if len(self.entries) > ACTIVITY_CACHE_LIMIT:
            cutoff = now - 600
            for k in [k for k, a in self.entries.items() if a.get("msg_at", 0) < cutoff]:
                del self.entries[k]


def alias_fragments(state, trigger_id, fragments):
    """Remember which trigger each burst fragment belongs to. The worker
    replies under the LAST fragment while the ledger and the sweep track
    the trigger, so a reply (or a silent pass) under any fragment settles
    the whole burst, and the sweep never requeues a fragment alone."""
    if not fragments:
        return
    aliases = state.setdefault("reply_aliases", {})
    for f in fragments:
        aliases[f["id"]] = trigger_id
    keep_last(aliases, 500)


@dataclass(frozen=True)
class CoalescePolicy:
    window: float
    bot_window: float
    is_bot_author: Callable[[dict | None], bool]


@dataclass
class BurstEffects:
    ingested: Callable[[dict, str], Any]
    settled: Callable[[str], bool]
    record_many: Callable[..., Any]


@dataclass
class PendingBursts:
    state: dict
    activity: GatewayActivity
    policy: CoalescePolicy
    enqueue: Callable[..., bool]
    effects: BurstEffects
    now: Callable[[], float] = time.time

    def hold(self, channel_id, msg):
        """If (channel, author) already has a burst being coalesced, append this
        message to it as a fragment and return True (caller must not classify it
        independently). The trigger message itself is never a fragment."""
        author_id = _who(msg).get("id")
        key = f"{channel_id}:{author_id}"
        entry = self.state.get("pending", {}).get(key)
        if entry and msg["id"] != entry["first_id"]:
            entry["fragments"].append(msg)
            print(
                f"Holding fragment from {_who(msg).get('username')} in "
                f"{entry['channel_name']} for one coalesced reply.",
                flush=True,
            )
            return True
        return False

    def stage(self, msg, cname, kind, place=None):
        """Stage a trigger (mention / DM message) in the
        pending buffer instead of queueing it immediately. Fragments arriving
        within the configured window fold into the same burst; flush() turns
        each matured burst into a single queue item. A second trigger from the
        same author while a burst is open becomes a fragment of it."""
        place = place or MessagePlace()
        guild_id = place.guild_id
        dm = place.dm
        author_id = _who(msg).get("id")
        key = f"{msg['channel_id']}:{author_id}"
        pending = self.state.setdefault("pending", {})
        if key in pending:
            entry = pending[key]
            if msg["id"] == entry["first_id"] or any(
                f["id"] == msg["id"] for f in entry["fragments"]
            ):
                return  # already in this burst (staged twice): nothing to add
            if kind == "mention" and entry.get("kind") == "follow_up":
                entry["kind"] = "mention"  # direct address beats a follow-up
            entry["fragments"].append(msg)
            print(
                f"Additional trigger from {_who(msg).get('username')} "
                f"folded into pending burst in {cname}.",
                flush=True,
            )
            return
        pending[key] = {
            "first_id": msg["id"],
            "channel_id": msg["channel_id"],
            "channel_name": cname,
            "guild_id": guild_id,
            "author_id": author_id,
            "kind": kind,  # "mention" | "follow_up" | "dm"
            "dm": dm,
            "first_seen": self.now(),
            "window": self.policy.bot_window
            if self.policy.is_bot_author(msg.get("author"))
            else self.policy.window,
            "trigger": msg,
            "fragments": [],
        }
        self.effects.ingested(self.state, msg["id"])
        print(
            f"Staged {kind} from {_who(msg).get('username')} in {cname} for coalescing.", flush=True
        )

    def quiet(self, entry, now):
        """True once a pending burst's author has stopped (see above)."""
        act = self.activity.entries.get(f"{entry['channel_id']}:{entry['author_id']}")
        if not act or not act.get("msg_id"):
            return False
        newest = max([entry["trigger"]] + entry["fragments"], key=lambda m: int(m["id"]))
        if act["msg_id"] != newest["id"]:
            return False  # the gateway did not see the newest message
        if act.get("typing_seq", 0) > act["msg_seq"]:
            return False  # typing again since their newest message
        quiet = (
            BOT_BURST_QUIET_SECS
            if self.policy.is_bot_author(entry["trigger"].get("author"))
            else BURST_QUIET_SECS
        )
        if entry.get("dm") and quiet == BURST_QUIET_SECS:
            quiet = DM_BURST_QUIET_SECS  # a human's DM
        return now - act["msg_at"] >= quiet

    def wait(self, now):
        """How long the main loop may sleep before a staged burst could
        flush: until its window ends or its author's quiet gap (as in
        quiet) passes, at most 0.5s (typing events do not wake the
        loop). None when no burst is staged."""
        due = []
        for entry in self.state.get("pending", {}).values():
            if entry.get("next_try", 0) > now:
                continue  # enqueue back-off: its retry is minutes away
            due.append(entry.get("first_seen", now) + entry.get("window", self.policy.window))
            act = (
                self.activity.entries.get(f"{entry.get('channel_id')}:{entry.get('author_id')}")
                or {}
            )
            if act.get("msg_at"):
                due.append(
                    act["msg_at"] + (DM_BURST_QUIET_SECS if entry.get("dm") else BURST_QUIET_SECS)
                )
        if not due:
            return None
        ahead = [d - now for d in due if d > now]
        return max(0.02, min([*ahead, 0.5]))

    def flush(self, queue, force=False):
        """Queue matured bursts; a failed one never holds back other authors."""
        now = self.now()
        pending = self.state.get("pending", {})
        flushed = False
        for key, entry in list(pending.items()):
            if not force and entry.get("next_try", 0) > now:
                continue
            early = self.quiet(entry, now)
            if not (force or early) and now - entry["first_seen"] < entry.get(
                "window", self.policy.window
            ):
                continue
            ok = self._enqueue(entry, queue)
            why = " (author stopped typing)" if early and not force else ""
            print(
                f"Flushed {entry['kind']} burst ({1 + len(entry['fragments'])} message(s)) "
                f"from {_who(entry['trigger']).get('username')} in "
                f"{entry['channel_name']}: queued={ok}{why}",
                flush=True,
            )
            trigger_id = entry["trigger"]["id"]
            if ok:
                del pending[key]
                flushed = True
            elif any(q["message_id"] == trigger_id for q in queue):
                # The sweep may have queued the trigger; its fragments must
                # still settle with that same request.
                alias_fragments(self.state, trigger_id, entry["fragments"])
                del pending[key]
            elif self.effects.settled(trigger_id) or self._retry_exhausted(entry, now):
                del pending[key]
        return flushed

    def _enqueue(self, entry, queue):
        n_queue = len(queue)
        try:
            return self.enqueue(
                entry["trigger"],
                entry["channel_name"],
                self.state,
                queue,
                context=QueueContext(
                    dm=entry["kind"] == "dm",
                    fragments=entry["fragments"],
                    guild_id=entry["guild_id"],
                    follow_up=entry["kind"] == "follow_up",
                ),
            )
        except Exception as exc:
            # An enqueue can fail after appending. Roll back only its additions;
            # otherwise the duplicate check would mistake the failed attempt
            # for published work and discard the pending burst.
            del queue[n_queue:]
            print(f"Enqueue error: {type(exc).__name__}: {exc}", flush=True)
            return False

    def _retry_exhausted(self, entry, now):
        entry["tries"] = entry.get("tries", 0) + 1
        if entry["tries"] < PENDING_MESSAGE_ATTEMPTS:
            entry["next_try"] = now + 300
            print(
                f"Enqueue failed for burst in {entry['channel_name']}; "
                f"retry {entry['tries']}/3 in 5 min.",
                flush=True,
            )
            return False
        trigger = entry["trigger"]
        who = _who(trigger).get("username")
        self.effects.record_many(
            [trigger["id"]] + [f["id"] for f in entry["fragments"]],
            "skipped",
            f"enqueue failed {entry['tries']}x",
            context=LedgerContext(
                channel_id=entry["channel_id"],
                author=who,
                author_id=_who(trigger).get("id"),
            ),
        )
        print(
            f"Dropping burst from {who} in {entry['channel_name']} after 3 failed "
            f"enqueues (recorded in mention ledger).",
            flush=True,
        )
        return True
