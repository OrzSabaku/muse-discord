"""Hold confidential replies and apply an owner's one-time review choice.

Button handlers expect owner authentication from the interaction dispatcher.
They report choices to the main loop; only apply_choice mutates durable state.
"""

import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from muse_discord import muse_settings as MS
from muse_discord.interactions import closed_card, ephemeral
from muse_discord.message_models import Settlement
from muse_discord.message_queue import answered_id as _answered_id
from muse_discord.owner.messaging import OwnerMessenger

HOLD_TTL_SECS = 24 * 3600


def released_ok(state, item):
    """A held reply an owner chose to send (Send anyway): its hold id,
    item and channel are the ones the monitor recorded for it."""
    hid = item.get("_hold")
    rec = (state.get("hold_released") or {}).get(hid) if isinstance(hid, str) else None
    return (
        isinstance(rec, dict)
        and rec.get("channel_id") == str(item.get("channel_id"))
        and rec.get("qid") is not None
        and str(rec["qid"]) == str(_answered_id(state, item))
    )


def buttons(hid):
    return [
        {
            "type": 1,
            "components": [
                {"type": 2, "style": 4, "label": "Send anyway", "custom_id": f"mdhold:send:{hid}"},
                {"type": 2, "style": 2, "label": "Discard", "custom_id": f"mdhold:discard:{hid}"},
            ],
        }
    ]


@dataclass
class ReplyReview:
    settings_source: Callable[[], MS.Settings]
    messenger: OwnerMessenger
    dm_users: dict
    settle: Callable[..., None]
    live: dict = field(default_factory=dict)

    def hit(self, state, item, qid, parts):
        """True when this reply must be held (see above)."""
        phrases = self.settings_source().get("privacy.never_share", MS.Scope.everywhere())
        if not phrases or not parts:
            return False
        if released_ok(state, item):
            return False  # an owner chose Send anyway
        meta = state.get("item_meta", {}).get(qid) if qid else None
        if meta:
            owner_dm = bool(meta.get("dm")) and str(meta.get("author_id")) in self.messenger.owners
        else:
            owner_dm = self.dm_users.get(item.get("channel_id")) in self.messenger.owners
        if owner_dm:
            return False
        text = "\n".join(parts)
        return any(
            re.search(r"(?<!\w)" + re.escape(str(p)) + r"(?!\w)", text, re.IGNORECASE)
            for p in phrases
            if str(p).strip()
        )

    def hold(self, state, item, qid, parts):
        """Hold a reply that hit never_share and DM the owners about it."""
        hid = secrets.token_hex(6)
        where = item.get("channel_name") or item.get("channel_id")
        to = item.get("author") or "someone"
        state.setdefault("holds", {})[hid] = {
            "item": {k: v for k, v in item.items() if k != "_retry"},
            "at": time.time(),
            "qid": qid,
            "where": where,
            "to": to,
        }
        self.live[hid] = time.time()
        self.settle(
            state,
            item,
            outcome=Settlement(status="skipped", reason="held for owner review (never_share)"),
        )
        head = (
            f"**I held back a reply** to {to} in {where}: it contains one "
            f"of your never-share phrases, so I didn't post it.\n"
        )
        tail = (
            "\n**Send anyway** posts it as written; **Discard** drops "
            "it. If you don't choose within a day, I drop it."
        )
        body = "> " + "\n".join(parts).replace("\n", "\n> ")
        room = 1990 - len(head) - len(tail)
        if len(body) > room:
            body = body[: room - 1] + "\u2026"
        payload = {
            "content": head + body + tail,
            "allowed_mentions": {"parse": []},
            "components": buttons(hid),
        }
        delivered = [
            uid for uid in sorted(self.messenger.owners) if self.messenger.dm(uid, payload)
        ]
        print(
            f"Held a reply to {to} in {where}: it contains a never_share "
            f"phrase; {'owners DMed' if delivered else 'no owner reachable'}"
            f" (hold {hid}).",
            flush=True,
        )

    def expire(self, state):
        now = time.time()
        holds = state.get("holds") or {}
        for hid, hold in list(holds.items()):
            if now - hold.get("at", 0) >= HOLD_TTL_SECS:
                del holds[hid]
                self.live.pop(hid, None)
                self.settle(
                    state,
                    hold["item"],
                    outcome=Settlement(status="skipped", reason="never_share hold expired (24 h)"),
                )
                print(
                    f"Held reply in {hold.get('where')} expired undecided; discarded.", flush=True
                )
        released = state.get("hold_released") or {}
        for hid in [
            h
            for h, t in released.items()
            if now - (t.get("at", 0) if isinstance(t, dict) else t) > 2 * 86400
        ]:
            del released[hid]

    def on_button(self, ix, rest, uid, record):
        action, _, hid = rest.partition(":")
        at = self.live.get(hid)
        if action not in ("send", "discard") or at is None or time.time() - at >= HOLD_TTL_SECS:
            return ephemeral(
                "That reply is no longer waiting: it was sent or dropped, or a day went by."
            )
        self.live.pop(hid, None)  # one decision per hold
        record(
            {
                "kind": "hold_choice",
                "hold": hid,
                "action": action,
                "by": uid,
                "at": int(time.time()),
            }
        )
        return closed_card(
            ix,
            "**Send anyway** chosen: posting it now."
            if action == "send"
            else "**Discarded**: it will not be posted.",
        )

    def apply_choice(self, state, rec):
        hid = rec.get("hold")
        hold = state.get("holds", {}).pop(hid, None)
        self.live.pop(hid, None)
        if not hold:
            return  # expired meanwhile
        where = hold.get("where") or "?"
        if rec.get("action") == "send":
            # Sent through the outbox like any reply (retries, the
            # attachment pipeline, settling); the hold id lets this one
            # item past the never_share check.
            state.setdefault("hold_released", {})[hid] = {
                "at": time.time(),
                "qid": hold.get("qid"),
                "channel_id": str((hold.get("item") or {}).get("channel_id")),
            }
            state.setdefault("released", []).append(dict(hold["item"], _hold=hid))
            print(
                f"Owner {rec.get('by')} chose Send anyway for the held reply in {where}.",
                flush=True,
            )
        else:
            self.settle(
                state,
                hold["item"],
                outcome=Settlement(
                    status="skipped", reason="never_share hold discarded by an owner"
                ),
            )
            print(f"Owner {rec.get('by')} discarded the held reply in {where}.", flush=True)
