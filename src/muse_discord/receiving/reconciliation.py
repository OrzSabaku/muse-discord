"""Reconcile recent messages against durable and in-flight work.

A pass prepares all indexes as a value before considering candidates. Recovery
never treats an unanswered follow-up, a system event, or an accounted-for burst
fragment as a fresh request. A rate limit stops the pass immediately.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

from muse_discord.admission import AdmissionPolicy
from muse_discord.conversation import _snowflake_ts, _who, channel_display
from muse_discord.message_ledger import MessageLedger
from muse_discord.message_models import LEDGER_TERMINAL, LedgerContext, QueueContext
from muse_discord.receiving.direct import DirectChannels
from muse_discord.receiving.messages import CONVERSATION_TYPES
from muse_discord.response_policy import predates_join
from muse_discord.storage import read_json


@dataclass(frozen=True)
class RecoveryWindow:
    grace_seconds: float
    max_age_seconds: float
    max_recoveries: int


@dataclass(frozen=True)
class RecoveryScan:
    state: dict
    queue: list
    now: float
    entries: dict
    q_ids: set
    outbox_targets: set
    quar_ids: set
    replied: set
    pending_ids: set
    aliases: dict


@dataclass
class MentionReconciler:
    admission: AdmissionPolicy
    ledger: MessageLedger
    get: Callable[[str], Any]
    enqueue: Callable[..., bool]
    quarantined: Callable[[], set]
    outbox_paths: tuple[str, str]
    direct: DirectChannels
    window: RecoveryWindow
    backing_off: Callable[[], bool]
    now: Callable[[], float] = time.time

    def snapshot(self, state, queue):
        entries = self.ledger.load()["entries"]
        q_ids = {q.get("message_id") for q in queue}
        outbox_targets = set()
        for path in self.outbox_paths:
            for i in read_json(path, list) or []:
                if isinstance(i, dict):
                    outbox_targets.update((i.get("reply_to"), i.get("item")))
        quar_ids = self.quarantined()
        replied = set(state.get("replied_to", []))
        pending_ids = set()
        for entry in state.get("pending", {}).values():
            pending_ids.add(entry.get("first_id"))
            for frag in entry.get("fragments", []):
                pending_ids.add(frag.get("id"))
        # Burst fragment id -> trigger id. A fragment is answered by its
        # trigger's queue item, so it is accounted for whenever its trigger
        # is (and an outbox reply under a fragment accounts for the trigger).
        aliases = state.get("reply_aliases", {})
        outbox_targets |= {aliases[t] for t in outbox_targets if t in aliases}
        return RecoveryScan(
            state,
            queue,
            self.now(),
            entries,
            q_ids,
            outbox_targets,
            quar_ids,
            replied,
            pending_ids,
            aliases,
        )

    def run(self, state, queue, channels, dms=True):
        if self.backing_off():
            return False
        scan = self.snapshot(state, queue)
        # The owners' DM channels are swept too: a DM lost in a gateway gap
        # is as lost as a mention.
        dm_channels = (
            [(dm_id, {"dm": True, "name": name}) for dm_id, name in self.direct.names.items()]
            if dms
            else []
        )
        changed = False
        for cid, info in list(channels.items()) + dm_channels:
            try:
                msgs = self.get(f"/channels/{cid}/messages?limit=20")
            except Exception as e:
                if getattr(e, "code", None) == HTTPStatus.TOO_MANY_REQUESTS:
                    print("Sweep aborted: rate limited.", flush=True)
                    return changed
                continue
            if not isinstance(msgs, list):
                continue
            for m in sorted(msgs, key=lambda x: int(x["id"])):
                changed |= self.consider(m, cid, info, scan)
        return changed

    def consider(self, m, cid, info, scan: RecoveryScan):
        dm = bool(info.get("dm"))
        mid = m["id"]
        author = _who(m)
        if (
            author.get("id") == self.admission.response.conversation.bot_id
            or m.get("type", 0) not in CONVERSATION_TYPES
        ):
            return False  # own messages and system notices are never recovered
        if dm:
            # Owner DM channels: every owner message is a trigger.
            if (
                self.admission.response.is_bot_author(author)
                or not self.admission.dm_swept_author(author.get("id"))
                or not self.admission.dm_sender_allowed(author)
            ):
                return False
        # Direct address only: follow-ups are never requeued.
        elif self.admission.trigger_verdict(m, info.get("guild_id"))[0] != "mention":
            return False
        if predates_join(scan.state, m, info.get("guild_id")):
            return False
        age = scan.now - _snowflake_ts(mid)
        if age < self.window.grace_seconds or age > self.window.max_age_seconds:
            return False
        return self.recover(m, cid, info, scan)

    def recover(self, m, cid, info, scan: RecoveryScan):
        mid = m["id"]
        author = _who(m)
        cname = "DM with " + info["name"] if info.get("dm") else channel_display(info)
        if mid in scan.replied:
            if scan.entries.get(mid, {}).get("status") != "replied":
                self.ledger.record(
                    mid,
                    "replied",
                    "sweep sync",
                    context=LedgerContext(
                        channel_id=cid, author=author.get("username"), author_id=author.get("id")
                    ),
                )
            return False
        ids = {mid, scan.aliases.get(mid, mid)}
        if any(
            ids & s
            for s in (
                scan.replied,
                scan.quar_ids,
                scan.q_ids,
                scan.outbox_targets,
                scan.pending_ids,
            )
        ):
            return False  # accounted for and in flight
        ent = scan.entries.get(mid)
        if any(scan.entries.get(i, {}).get("status") in LEDGER_TERMINAL for i in ids):
            return False
        if info.get("can_send") is False:
            self.ledger.record(
                mid,
                "skipped",
                "no send permission in channel",
                context=LedgerContext(
                    channel_id=cid, author=author.get("username"), author_id=author.get("id")
                ),
            )
            return False
        recs = (ent or {}).get("recoveries", 0)
        if recs >= self.window.max_recoveries:
            if ent and ent.get("status") != "skipped":
                self.ledger.record(
                    mid,
                    "skipped",
                    "recovery cap reached",
                    context=LedgerContext(
                        channel_id=cid, author=author.get("username"), author_id=author.get("id")
                    ),
                )
                print(
                    f"Sweep: giving up on unrecoverable mention "
                    f"{mid} from {author.get('username')} in "
                    f"{cname} (recorded in mention ledger).",
                    flush=True,
                )
            return False
        ok = self.enqueue(
            m,
            cname,
            scan.state,
            scan.queue,
            context=QueueContext(
                dm=bool(info.get("dm")), fragments=[], guild_id=info.get("guild_id")
            ),
        )
        if ok:
            scan.q_ids.add(mid)
            self.ledger.record(
                mid,
                "queued",
                f"sweep recovery #{recs + 1}",
                context=LedgerContext(
                    channel_id=cid,
                    author=author.get("username"),
                    recoveries=recs + 1,
                    author_id=author.get("id"),
                ),
            )
            print(
                f"Sweep recovered lost mention {mid} from {author.get('username')} in {cname}.",
                flush=True,
            )
        elif not self.ledger.terminal(mid):
            # (A gate inside enqueue, e.g. quarantine or the join
            # watermark, may already have recorded a terminal state.)
            self.ledger.record(
                mid,
                "enqueue_failed",
                "sweep enqueue failed",
                context=LedgerContext(
                    channel_id=cid,
                    author=author.get("username"),
                    recoveries=recs + 1,
                    author_id=author.get("id"),
                ),
            )
        return bool(ok)
