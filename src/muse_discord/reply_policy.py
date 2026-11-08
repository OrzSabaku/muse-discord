"""Worker reply validation and binding, independent of monitor startup.

Watch fires and proactive slots must be checked before ordinary queue replies.
A failure to inspect a possible slot is a refusal, never permission to send.
"""

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from muse_discord import muse_watches as W
from muse_discord import reply_protocol
from muse_discord.generated_replies import GeneratedReplies
from muse_discord.message_models import PROACTIVE_KIND
from muse_discord.message_queue import QueueFiles
from muse_discord.messaging.formatting import normalize_parts
from muse_discord.outbox_store import redact_nonces
from muse_discord.owner.authorization import OwnerAuthorization
from muse_discord.owner.review import ReplyReview
from muse_discord.reply_binding import ReplyBinding
from muse_discord.watches.delivery import WatchDelivery


@dataclass
class ReplyPolicy:
    binding: ReplyBinding
    owners: OwnerAuthorization
    review: ReplyReview
    generated: GeneratedReplies
    watches: WatchDelivery
    proactive_outbox: Callable[[dict, dict], Any]
    queue: QueueFiles
    rejected_path: str
    max_reply_chars: int

    def problem(self, item):
        """Why an outbox item cannot be processed, or None. Ids given as
        numbers are normalized to strings in place. Checked first, so a
        wrong type never raises out of process_outbox mid-pass."""
        problems = reply_protocol.shape_problems(item)
        if problems:
            return "; ".join(problems)
        for field in reply_protocol.ID_FIELDS:
            if item.get(field) in (None, ""):
                item.pop(field, None)
            else:
                item[field] = str(item[field])
        if "schedule" in item:
            # Reminders and watches (muse_watches.py): shape only here.
            problems = W.shape_problems(item)
            if problems:
                return "schedule: " + "; ".join(problems)
        if item.get("silent"):
            return reply_protocol.content_problem(item)
        return reply_protocol.content_problem(
            item, has_text=bool(normalize_parts(item, self.max_reply_chars))
        )

    def refuse(self, rejects, item, why, note=""):
        """Log a refused outbox item and set it aside (nonces blanked)."""
        where = item.get("channel_name") if isinstance(item, dict) else None
        print(
            f"Refused outbox item for {where or '?'}: {why}{note} (set aside "
            f"in {os.path.basename(self.rejected_path)})",
            flush=True,
        )
        rejects.append((redact_nonces(item), why))

    def bind_started(self, state, item, settled, remaining, rejects):
        """The one binding step for outbox items that answer an item the
        monitor started, run before OwnerAuthorization.gate (docs/PROTOCOL.md, "Fires: the
        monitor starts a queue item"): a reminder or watch fire (its
        requester, its place) or a heartbeat slot (proactive.py: its
        targets, nonce and caps; sent there, never by the path below).
        Returns (fire, handled): handled means the item was settled, set
        aside or kept for a retry here; fire is a bound fire for the send
        path below, else None."""
        fire = self.watches.bind(item)
        if isinstance(fire, dict) and fire.get("gone"):
            print(
                f"Outbox item for reminder {fire['gone']}: it was settled already; nothing sent.",
                flush=True,
            )
            # (Its ledger entry already says how it ended.)
            settled.append({"item": fire["gone"]})
            return None, True
        if isinstance(fire, str):
            self.refuse(rejects, item, fire)
            return None, True
        if fire is not None:
            return fire, False
        try:
            pro = self.proactive_outbox(state, item)
        except Exception as e:  # never blocks the replies
            print(f"Proactive outbox error: {e}", flush=True)
            pro = None
            if self.could_name_slot(item):
                # Never left to the ordinary path: set aside.
                self.refuse(rejects, item, "proactive: its slot could not be checked")
                return None, True
        if pro is None:
            return None, False
        if pro.reject:
            self.refuse(rejects, item, pro.reject)
        if pro.retry:
            remaining.append(item)
        if pro.spent:
            settled.append({"item": pro.spent})
        return None, True

    def could_name_slot(self, item):
        """When proactive.py cannot say: an item shaped like a slot's answer
        (its nonce field and no reply_nonce) or naming a queued slot."""
        if item.get("owner_dm_nonce") and not item.get("reply_nonce"):
            return True
        names = {str(item[f]) for f in ("item", "reply_to") if item.get(f)}
        try:
            return any(
                isinstance(q, dict)
                and q.get("kind") == PROACTIVE_KIND
                and str(q.get("message_id")) in names
                for q in self.queue.load()
            )
        except Exception:
            return True
