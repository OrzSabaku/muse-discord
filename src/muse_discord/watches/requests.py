"""Idempotent schedule actions and worker cards, bound to verified queue metadata.

Audience checks run before the store transaction. Results are remembered under
the queue id, so retrying delivery cannot create duplicate reminders or watches.
"""

from collections.abc import Callable
from dataclasses import dataclass

from muse_discord import muse_watches as W
from muse_discord.message_models import Speaker
from muse_discord.watches.audience import WatchAudience


def _outcome(rec):
    if rec.get("ok"):
        return None
    return ("refused", rec.get("line") or W.LINES["unknown"], rec.get("reason"), rec.get("detail"))


@dataclass
class WatchRequests:
    store: W.Store
    audience: WatchAudience
    queued_meta: Callable[[dict, str, dict], dict | None]
    note_timezone: Callable[[str, dict | None], None]

    def card(self, guild_id, dm, here, matches, speaker=None):
        """A queue item's `watches` field (muse_watches.card). Never raises."""
        speaker = speaker or Speaker()
        uid = speaker.author_id
        from_bot = speaker.from_bot
        try:
            owner = str(uid) in self.audience.response.owners
            return W.card(
                data=self.store.read(),
                uid=uid,
                context=W.CardContext(
                    guild_id=guild_id,
                    dm=dm,
                    owner_dm=owner and dm and (not from_bot),
                    can_set=self.audience.can_set(uid, from_bot, here, matches),
                    can_web=self.audience.can_web(uid, from_bot, dm),
                ),
            )
        except Exception as e:
            print(f"Reminders and watches: no list for an item ({e})", flush=True)
            return {"can_set": False, "can_web": False, "items": []}

    def schedule(self, state, item, index):
        """Apply a schedule action, failing closed: whatever goes wrong deciding an
        action (a bug, a value nothing expected) refuses it, nothing
        changed, and is logged; it never escapes into process_outbox, where
        it would leave the item in the processing file for every pass."""
        try:
            return self._schedule(state, item, index)
        except Exception as e:
            qid = str(item.get("item") or "")
            print(
                f"Reminders and watches for item {qid}: refused, an error "
                f"deciding it ({e.__class__.__name__}: {e})",
                flush=True,
            )
            try:
                with self.store.edit() as data:
                    if qid and qid not in data["done"]:
                        W.remember_done(data, qid, {"ok": False, "line": W.LINES["unknown"]})
            except Exception:
                pass
            return ("refused", W.LINES["unknown"], None, None)

    def _schedule(self, state, item, index):
        """Decide an outbox item's schedule action, all or nothing and once
        per queue item: None when it was done (now, or on an earlier pass of
        a retried send: the worker's reply goes out), ("refused", line,
        ledger reason or None, detail) when nothing changed (the monitor's
        line goes out instead), or ("reject", why) when the item cannot
        carry one (set aside; the queue item stays open). Who asked and
        where come from item_meta and the queue, never from the outbox."""
        qid = str(item.get("item") or "")
        store = self.store
        prev = store.read()["done"].get(qid)
        if prev is not None:
            return _outcome(prev)
        meta = self.queued_meta(state, qid, index)
        if (
            not isinstance(meta, dict)
            or qid not in index["channels"]
            or meta.get("kind") not in ("mention", "dm")
        ):
            return (
                "reject",
                "schedule: item must be an open queue item from a message (not a reminder)",
            )
        cid = str(meta.get("channel_id") or "")
        if str(item.get("channel_id")) != cid:
            return (
                "reject",
                "schedule: channel_id must be the item's own "
                "channel (reminders go back where they were "
                "asked)",
            )

        uid = str(meta.get("author_id") or "")
        changes = item["schedule"]["changes"]
        fresh = None  # decided on this pass (not read back)
        try:
            req = self.audience.request(meta, qid, changes)
            with store.edit() as data:
                prev = data["done"].get(qid)
                if prev is None:
                    try:
                        prev = {"ok": True, "done": W.decide(data, req, changes)}
                    except W.Refused as r:
                        prev = {"ok": False, "line": r.line, "reason": r.reason, "detail": r.detail}
                    W.remember_done(data, qid, prev)
                    fresh = prev
            self.note_timezone(uid, fresh)
        except W.Refused as r:
            prev = {"ok": False, "line": r.line, "reason": r.reason, "detail": r.detail}
            with store.edit() as data:
                W.remember_done(data, qid, prev)
        if prev.get("ok"):
            print(
                f"Reminders and watches for {uid} (item {qid}): "
                f"{', '.join(f'{op} {i}' for op, i in prev.get('done') or [])}",
                flush=True,
            )
        else:
            print(
                f"Reminders and watches for {uid} (item {qid}): nothing "
                f"changed ({prev.get('reason') or 'refused'})",
                flush=True,
            )
        return _outcome(prev)
