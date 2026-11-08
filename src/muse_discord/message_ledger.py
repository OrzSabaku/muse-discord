"""Durable message outcomes and diagnostic reason codes shared by all paths."""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from muse_discord.message_models import LEDGER_TERMINAL, LedgerContext
from muse_discord.receiving.direct import DirectChannels
from muse_discord.storage import atomic_write, read_json

NAME_CACHE_LIMIT = 2000

_LEDGER_CODES = (
    ("settings: who.replies is off", "replies-off"),
    ("settings: none of the owners is in", "ownerless"),
    ("settings: who.bots", "bots-ignore"),
    ("settings: not allowed by who.allow", "not-allowed"),
    ("settings: on who.ignore", "not-allowed"),
    ("settings: not in who.allow", "not-allowed"),
    ("settings: who.dms_from", "dm-not-allowed"),
    ("settings: not in who.dms_from", "dm-not-allowed"),
    ("settings: shares no server", "dm-not-allowed"),
    ("daily cap reached", "cap"),
    ("bot exchange limit", "bot-exchange"),
    ("suspicious, ignored", "suspicious-ignored"),
    ("predates bot join", "predates-join"),
    ("no send permission", "no-send"),
    ("403 missing access", "no-send"),
    ("enqueue failed", "internal"),
    ("sweep enqueue failed", "internal"),
    ("recovery cap reached", "internal"),
    ("no reply (worker's call)", "worker-skipped"),
    ("send failed", "send-failed"),
    ("held for owner review (never_share)", "never-share"),
    ("never_share hold discarded", "never-share"),
    # Reminders and watches (muse_watches.REASON_*): a request refused.
    ("reminders: limit reached", "watch-cap"),
    ("reminders: not allowed", "watch-not-allowed"),
    ("reminder held", "watch-held"),
)


def ledger_code(status, reason=""):
    """A ledger entry's reason code: replied, queued, quarantined, or why
    a message was skipped (dm-off, a _LEDGER_CODES code, else other)."""
    if status in ("replied", "queued", "quarantined"):
        return status
    if status == "enqueue_failed":
        return "internal"
    reason = str(reason or "")
    if reason.startswith("settings: who.replies is off for DMs"):
        return "dm-off"
    for prefix, code in _LEDGER_CODES:
        if reason.startswith(prefix):
            return code
    return "other"


@dataclass
class MessageLedger:
    path_source: Callable[[], str]
    channels: dict
    direct: DirectChannels
    telemetry: Callable[[str, list], Any]
    now: Callable[[], float] = time.time
    names: dict[str, str] = field(default_factory=dict)

    def remember_name(self, uid, name):
        """Bound observed ledger-author names for settings screens and target labels."""
        if uid and name:
            self.names[str(uid)] = str(name)
            if len(self.names) > NAME_CACHE_LIMIT:
                for key in list(self.names)[:500]:
                    self.names.pop(key, None)

    def load(self):
        d = read_json(self.path_source())
        if d and isinstance(d.get("entries"), dict):
            return d
        return {"entries": {}}

    def save(self, d):
        items = sorted(d["entries"].items(), key=lambda kv: kv[1].get("updated", 0))
        d["entries"] = dict(items[-1000:])
        atomic_write(self.path_source(), d)

    def record(self, mid, status, reason="", context=None):
        self.record_many([mid], status, reason, context)

    def record_many(self, mids, status, reason="", context=None):
        """ledger_record for several ids in one write: a burst's trigger and
        its fragments always share one outcome. Each entry also keeps the
        author's id and a reason code (ledger_code; "Why no reply?" in
        /settings reads them), plus detail (which cap, which limit)."""
        context = context or LedgerContext()
        channel_id = context.channel_id
        author = context.author
        recoveries = context.recoveries
        author_id = context.author_id
        detail = context.detail
        mids = [m for m in mids if m]
        if not mids:
            return
        d = self.load()
        code = ledger_code(status, reason)
        self.telemetry(code, [d["entries"].get(m) for m in mids])
        # Where it happened, as the monitor knows it now (a thread's parent
        # only for a thread): /settings' Why reads it later, when the
        # channel may be gone from discovery.
        info = self.channels.get(str(channel_id)) if channel_id else None
        where = {}
        if info and info.get("guild_id"):
            where = {
                "guild_id": info["guild_id"],
                "parent_id": info.get("parent_id") if info.get("thread") else None,
            }
        elif channel_id and (channel_id in self.direct.names or channel_id in self.direct.users):
            where = {"dm": True}
        for mid in mids:
            ent = d["entries"].get(mid, {})
            ent.update(where)
            ent.update(
                {
                    "status": status,
                    "reason": reason,
                    "channel_id": channel_id or ent.get("channel_id"),
                    "author": author or ent.get("author"),
                    "author_id": str(author_id) if author_id else ent.get("author_id"),
                    "code": code,
                    "updated": self.now(),
                }
            )
            if detail is not None:
                ent["detail"] = detail
            else:
                ent.pop("detail", None)
            if recoveries is not None:
                ent["recoveries"] = recoveries
            d["entries"][mid] = ent
            if author_id and author:
                self.remember_name(author_id, author)
        self.save(d)

    def terminal(self, mid):
        return self.load()["entries"].get(mid, {}).get("status") in LEDGER_TERMINAL
