"""Monitor state and crash-safe publication of pending queue additions.

The state file is private to one monitor; the reply queue is shared with workers.
Publication saves recoverable work before acquiring the queue lock, and retains
that backlog on contention or failure. No transport or runtime initialization is
required to use these stores.
"""

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field

from muse_discord.message_ledger import MessageLedger
from muse_discord.message_models import LEDGER_TERMINAL
from muse_discord.message_queue import QueueFiles
from muse_discord.storage import atomic_write


@dataclass
class StateFile:
    path_source: Callable[[], str]
    _written: tuple[str, str] | None = field(default=None, init=False, repr=False)

    def load(self):
        path = self.path_source()
        if os.path.exists(path):
            with open(path) as f:
                state = json.load(f)
        else:
            state = {}
        state.setdefault("last_seen", {})
        state.setdefault("replied_to", [])
        state.setdefault("rate", {"day": "", "day_count": 0, "users": {}})
        state.setdefault("history", {})
        # Leftovers from the removed burst breaker.
        for key in ("breaker", "owner_triggers", "trigger_texts"):
            state.pop(key, None)
        state.pop("bot_runs", None)  # leftover from the removed bot turn cap
        state.pop("web_nonces", None)  # leftover: web watches are DM only now
        # Bursts staged by the removed name trigger: not direct address.
        state["pending"] = {
            k: v for k, v in state.get("pending", {}).items() if v.get("kind") != "name_drop"
        }
        return state

    def save(self, state):
        """Publish changed state atomically; cache only successful writes."""
        state["replied_to"] = state["replied_to"][-200:]
        path = self.path_source()
        text = json.dumps(state)
        if self._written != (path, text) or not os.path.exists(path):
            atomic_write(path, None, text=text)
            self._written = (path, text)


@dataclass
class QueuePublication:
    queue: QueueFiles
    state_file: StateFile
    ledger: MessageLedger

    def publish(self, state, additions=()):
        """Persist staged additions until the shared queue accepts them."""
        backlog = {q["message_id"]: q for q in state.get("queue_backlog", [])}
        backlog.update((q["message_id"], q) for q in additions)
        if not backlog:
            return
        entries = self.ledger.load()["entries"]
        replied = set(state.get("replied_to", []))
        state["queue_backlog"] = [
            q
            for mid, q in backlog.items()
            if mid not in replied and entries.get(mid, {}).get("status") not in LEDGER_TERMINAL
        ]
        self.state_file.save(state)  # also preserves the consumed burst and its capability
        if state["queue_backlog"]:
            self.queue.edit(add=state["queue_backlog"])
        state.pop("queue_backlog", None)
