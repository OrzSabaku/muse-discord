"""Apply opt-out replies and request personal confirmation for opt-in or timezone changes.

The original queued message identifies the person. Worker output cannot choose
someone else or restore consent; only that person's own button can do so.
"""

from collections.abc import Callable
from dataclasses import dataclass

from muse_discord import muse_settings as MS
from muse_discord.proactive.consent import ConsentRequests
from muse_discord.proactive.model import FIELD, OPT_VALUES, TEXT_KINDS, ProactiveContext
from muse_discord.proactive.store import ProactiveStore
from muse_discord.proactive.targets import TargetPolicy


@dataclass
class ConsentActions:
    context: ProactiveContext
    store: ProactiveStore
    policy: TargetPolicy
    consent: ConsentRequests
    clock: Callable[[], float]

    def _opt_reply_problem(self, item, qid, index, meta):
        rt = str(item.get("reply_to") or "")
        if (
            item.get("silent")
            or item.get("standalone")
            or not self.context.normalize_parts(item)
            or (rt != qid and index["frags"].get(rt) != qid)
        ):
            return (
                f"{FIELD}: goes on a reply with text, under that message (reply_to), so they see it"
            )
        if item.get(FIELD) == "opt-in" and not (meta.get("dm") and meta.get("kind") == "dm"):
            return f"{FIELD}: opt-in only in the person's own DM"
        return None

    def _opt_message_meta(self, state, item, index, qid):
        if qid not in index["channels"]:
            return None, f"{FIELD}: that message is no longer waiting for a reply"
        meta = self.context.queued_item_meta(state, qid, index)
        if not isinstance(meta, dict) or meta.get("bot") or meta.get("kind") not in TEXT_KINDS:
            return (
                None,
                f"{FIELD}: goes on the reply to the person's own message (item: its message_id)",
            )
        if str(item.get("channel_id")) != str(meta.get("channel_id")) or index["channels"].get(
            qid
        ) != str(meta.get("channel_id")):
            return None, f"{FIELD}: channel_id must be that message's channel"
        return meta, None

    def opt_action(self, state, item, index=None):
        """None when the outbox item carries no opt-out / opt-in, or it was
        recorded; else why it is refused. The person is the author of
        the message the item answers (item_meta), never anything the
        worker wrote. That message must be a person's own (a mention or
        a DM, not a bot's), still in the queue (index: queue_index()),
        and the item a visible reply under it (text, threaded under it
        or a burst fragment of it): the person always sees what was
        recorded. Opt-in only in the person's own DM."""
        if FIELD not in item:
            return None
        v = item.get(FIELD)
        if v not in OPT_VALUES:
            return f'{FIELD}: expected "opt-out" or "opt-in"'
        qid = str(item.get("item") or "")
        index = index or {"frags": {}, "channels": {}}
        meta, problem = self._opt_message_meta(state, item, index, qid)
        if problem is not None:
            return problem
        assert meta is not None
        problem = self._opt_reply_problem(item, qid, index, meta)
        if problem is not None:
            return problem
        uid = str(meta.get("author_id") or "")
        if not uid:
            return f"{FIELD}: whose message it is is not known"
        d = self.store.data()
        if v == "opt-out":
            d["opted_out"][uid] = self.clock()
            self.consent.close(uid, "in")
            self.store.save()
            print(f"Proactive: {uid} opted out (item {qid}).", flush=True)
        elif uid in d["opted_out"]:
            # Never recorded from the outbox (one run may hold several
            # people's messages): the person's own button records it.
            self.consent.ask(uid, str(meta["channel_id"]), "in")
        return None

    def note_schedule(self, uid, fresh):
        """A reminder's time zone change requests confirmation for proactive quiet hours."""
        for op, value in (fresh or {}).get("done") or []:
            if op == "tz":
                try:
                    self.note_tz(uid, value)
                except Exception as exc:
                    print(f"Proactive: time zone note error: {exc}", flush=True)

    def note_tz(self, uid, tz):
        """A schedule action set person uid's time zone (the reminders'):
        their quiet hours move only when they confirm it themselves, and
        only someone the bot may DM first is asked."""
        uid = str(uid)
        d = self.store.data()
        p = d["people"].get(uid) or {}
        if tz == p.get("tz") or uid in d["opted_out"]:
            return
        dm_here = self.policy.st().resolve(MS.Scope.dm())
        if not dm_here["proactive.enabled"] or dm_here["who.replies"] == "off":
            return
        if uid in self.context.owners():
            cid = self.policy.owner_dm_channel(uid)
        elif (
            p.get("dm")
            and self.context.dm_refusal({"id": uid, "username": p.get("name") or uid}) is None
        ):
            cid = p["dm"]
        else:
            return
        if cid:
            self.consent.ask(uid, cid, "tz", tz)
