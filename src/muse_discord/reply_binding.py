"""Bind ordinary replies to their live queue item, channel and issued capability.

Held/proactive replies have their own completed binders. This layer grants no
capability and sends no reply; diagnostics for obsolete worker instructions are
kept separate from the authorization decision.
"""

import contextlib
import hmac
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from muse_discord.message_queue import answered_id, trigger_of
from muse_discord.owner.review import released_ok as hold_released_ok
from muse_discord.storage import PrivateFile, atomic_write

REPLY_NONCE_FIELD = "reply_nonce"
MISSING_REPLY_NONCE = "no reply_nonce (an outdated worker prompt?)"
STALE_PROMPT_HITS = 3
STALE_PROMPT_WINDOW = 3600
STALE_PROMPT_DM = (
    "My instructions for writing replies are out of date, "
    "so I can't send replies. Ask your Muse to update "
    "muse-discord; that refreshes them."
)


@dataclass
class StalePrompt:
    path: str
    notify: Callable[[str], Any]
    now: Callable[[], float] = time.time
    day: Callable[[], str] = field(default=lambda: time.strftime("%Y-%m-%d"))

    def note(self, state, refusal):
        """After own_channel_problem: count refusals for a missing
        reply_nonce (see STALE_PROMPT_HITS); a passing item clears them."""
        now = self.now()
        if refusal is None:
            if state.pop("stale_prompt_hits", None) is not None or os.path.exists(self.path):
                with contextlib.suppress(OSError):
                    os.remove(self.path)
            return
        if MISSING_REPLY_NONCE not in refusal:
            return
        hits = [
            t
            for t in state.get("stale_prompt_hits") or []
            if isinstance(t, (int, float)) and now - t < STALE_PROMPT_WINDOW
        ]
        hits.append(now)
        state["stale_prompt_hits"] = hits[-STALE_PROMPT_HITS:]
        if len(hits) < STALE_PROMPT_HITS:
            return
        if not os.path.exists(self.path):
            print(
                "WORKER PROMPT OUT OF DATE: outbox items carry no "
                "reply_nonce; re-render the worker prompt (docs/UPDATING.md).",
                flush=True,
            )
            with contextlib.suppress(OSError):
                atomic_write(self.path, {"since": now, "refused": len(hits)})
        day = self.day()
        if state.get("stale_prompt_alert") != day:
            state["stale_prompt_alert"] = day
            self.notify(STALE_PROMPT_DM)


@dataclass
class ReplyBinding:
    replies: PrivateFile
    names_slot: Callable[[dict], Any]
    stale_prompt: StalePrompt

    def bound_elsewhere(self, state, item):
        """True for an outbox item whose own binder has checked it in full,
        so own_channel_problem leaves it alone: a held reply an owner chose
        to send (hold_released_ok), or an item for a heartbeat slot
        (proactive.py, through bind_started: the slot's own nonce, its
        targets and caps bind it; it has no reply_nonce). The one place to
        add another such binder. A reminder or watch firing is not one:
        WatchDelivery.bind checks its place, and its reply_nonce still binds it to
        the run that was handed it."""
        if hold_released_ok(state, item):
            return True
        try:
            return bool(self.names_slot(item))
        except Exception as e:  # then the reply_nonce check binds it
            print(f"Proactive: could not check for a slot ({e}).", flush=True)
            return False

    def nonce_problem(self, state, item, t):
        want = ((state.get("reply_nonces") or {}).get(t) or {}).get("nonce")
        if not want:
            return f"{t}: no live reply_nonce yet (its next wake gets one)"
        got = item.get(REPLY_NONCE_FIELD)
        if got is None:
            if t not in (self.replies.cache.get("body") or {}):
                # Never written for the hook: no wake could have had it.
                return f"{t}: its reply_nonce was never written (see the log)"
            return (
                f"{t}: {MISSING_REPLY_NONCE} (copy the item's "
                f"{REPLY_NONCE_FIELD} from the wake payload)"
            )
        if not isinstance(got, str) or not hmac.compare_digest(
            str(want).encode(), got.encode("utf-8", "replace")
        ):
            return (
                f"{t}: bad reply_nonce (missing or wrong: copy the item's "
                f"{REPLY_NONCE_FIELD} from the wake payload)"
            )
        return None

    def problem(self, state, item, index):
        """None when this outbox item answers an open queue item, in that
        item's own channel (a thread, for a message in a thread), and
        carries that item's reply_nonce (handed only to the wake holding the
        item's lease); else why it is refused. So a run can neither post
        anywhere else under a made-up item, nor answer, silence or schedule
        on an item it was not handed (its id may be in the history it read).
        An owner's command to post elsewhere goes through bin/discord-api.py
        with the item's owner_command_nonce. A held reply an owner chose to
        send was checked when it was first drafted (bound_elsewhere)."""
        if self.bound_elsewhere(state, item):
            return None
        qid = answered_id(state, item)
        if not qid:
            return "answers no queued message (set item to its message_id)"
        t = trigger_of(state, qid, index["frags"])
        cid = index["channels"].get(t)
        if not cid:
            return f"{t} is not an open queue item (answered already, or never queued)"
        if str(item.get("channel_id")) != cid:
            return f"channel_id is not the channel of {t}"
        # It replies (and reacts: reactions go on reply_to) under its own
        # message or one of its own burst fragments, never under another
        # open item in the channel.
        rt = item.get("reply_to")
        if rt and trigger_of(state, rt, index["frags"]) != t:
            return f"reply_to is not {t} or one of its fragments"
        return self.nonce_problem(state, item, t)
