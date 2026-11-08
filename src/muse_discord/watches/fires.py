"""Build grouped notification fires and retain deferred entries without loss.

The caller checks audiences before entering a store transaction. This builder
uses those verdicts and local counters only; it never performs REST lookups.
"""

from collections.abc import Callable
from dataclasses import dataclass

from muse_discord import muse_watches as W
from muse_discord.conversation import HISTORY_ATTACHED, channel_display, msg_link
from muse_discord.message_models import LedgerContext, Speaker
from muse_discord.watches.audience import WatchAudience

WATCH_FIRES_MAX = 500
WATCH_HELD_MAX = 500
WATCH_FIRES_PER_SERVER_TICK = 5
WATCH_REFUSED_GRACE = 24 * 3600
WATCH_TICK_SECS = 2.0


@dataclass
class PendingFire:
    fid: str
    uid: str
    cid: str
    entries: list
    kind: str
    now: float
    next_at: float
    held: dict | None = None
    why: str | None = None


@dataclass
class FirePolicy:
    now: float
    verdicts: dict
    counts: dict
    kind: str = "reminder"


@dataclass
class FireGroup:
    uid: str
    cid: str
    entries: list
    fid: str | None = None
    held: dict | None = None


def hold(data, pending):
    """Keep a fire for later (data["held"]). Into one already held for
    the same person, place and kind it folds: the same watch again
    counts as `more` on its entry ("3 more mentions"), never a second
    notice, and nothing is dropped (distinct entries are bounded by the
    person's own reminders and watches). Returns the held record's id."""
    fid = pending.fid
    uid = pending.uid
    cid = pending.cid
    es = pending.entries
    kind = pending.kind
    now = pending.now
    next_at = pending.next_at
    held = pending.held
    why = pending.why
    held_all = data.setdefault("held", {})
    for hid, h in held_all.items():
        if (
            hid != fid
            and isinstance(h, dict)
            and h.get("uid") == uid
            and h.get("cid") == cid
            and h.get("kind") == kind
        ):
            have = {e.get("id"): e for _, e in h["es"]}
            for r, e in es:
                old = have.get(e.get("id"))
                if old is not None and e.get("kind") != "remind":
                    old["more"] = old.get("more", 0) + 1 + e.get("more", 0)
                    if e.get("matched"):
                        old["matched"] = e["matched"]
                else:
                    h["es"].append([r, e])
            return hid
    rec = dict(held or {})
    rec.update(
        {
            "at": (held or {}).get("at", now),
            "next": next_at,
            "kind": kind,
            "uid": uid,
            "cid": cid,
            "es": [[r, e] for r, e in es],
            "tries": (held or {}).get("tries", 0) + 1,
        }
    )
    rec.pop("why", None)
    if why:
        rec["why"] = why
    held_all[fid] = rec
    if len(held_all) > WATCH_HELD_MAX:
        for old in sorted(held_all, key=lambda k: held_all[k].get("at", 0))[
            : len(held_all) - WATCH_HELD_MAX
        ]:
            print(f"Reminder {old} dropped: too many held.", flush=True)
            del held_all[old]
    return fid


def due_entries(data, matches, now):
    entries = []
    for i, r in sorted(W.due_reminders(data, now), key=lambda ir: ir[1].get("due", 0)):
        del data["items"][i]
        entries.append((r["requester"], r["channel_id"], (r, W.fire_entry(i, r, now))))
    for wid, matched in matches:
        r = data["items"].get(wid)
        if not r or r.get("until", 0) <= now or now - r.get("last_fired", 0) < W.DEBOUNCE_SECS:
            continue
        r["last_fired"] = now
        r["fired"] = r.get("fired", 0) + 1
        entries.append(
            (r["requester"], r["channel_id"], (dict(r), W.fire_entry(wid, r, now, matched)))
        )
        if not r.get("every"):
            del data["items"][wid]
    for wid in W.expired_watches(data, now):
        del data["items"][wid]
        print(f"Watch {wid} ended (its time ran out).", flush=True)
    return entries


@dataclass
class WatchFireBuilder:
    audience: WatchAudience
    record: Callable[..., None]
    settings_card: Callable[..., dict]

    def item(self, state, fid, group, now, kind="reminder"):
        """The queue item of a fire (kind "reminder", or "watch-met" for a
        web watch): what is due or matched in `reminder`, with the place's
        recent history and settings card, so the worker writes it as a
        normal reply."""
        uid, cid = group.uid, group.cid
        first = group.entries[0][0]
        name = first.get("name") or uid
        gid, dm = first.get("guild_id"), bool(first.get("dm"))
        entries = [dict(e, asked=msg_link(gid, cid, r["message_id"])) for r, e in group.entries]
        hist = (state.get("history") or {}).get(cid) or []
        scope = self.audience.response.scope_for(gid, cid, dm)
        card = self.settings_card(
            scope,
            self.audience.response.settings().resolve(scope),
            cid,
            speaker=Speaker(author_id=uid, from_bot=False),
        )
        card.pop("places", None)
        cname = (
            f"DM with {name}"
            if dm
            else channel_display(self.audience.access.channels.get(cid) or {"name": cid})
        )
        return {
            "message_id": fid,
            "channel_id": cid,
            "channel_name": cname,
            "guild_id": gid,
            "kind": kind,
            "author": name,
            "author_id": uid,
            "content": "",
            "timestamp": W.iso(now),
            "media": [],
            "documents": [],
            "unviewable": [],
            "history": hist[-HISTORY_ATTACHED:],
            "may_generate": False,
            "owner_command": False,
            "burst": False,
            "burst_fragments": [],
            "reply_to": None,
            "from_bot": False,
            "bot_turns": 0,
            "participants": self.audience.response.conversation.conversation_participants(
                hist, {"author": {"id": uid, "username": name}}, None
            ),
            "queued_at": now,
            "follow_up": False,
            "settings": card,
            "reminder": {"items": entries, "late": any(e.get("late") for e in entries)},
        }

    def make(self, state, data, entries, policy):
        """Fires for entries [(requester, place, (record, fire entry))], one
        per person and place, recorded in data["fires"] (under the store's
        lock); returns their queue items (queued by the caller after the
        store is written, so a fire is never queued without its record)."""
        now = policy.now
        verdicts = policy.verdicts
        counts = policy.counts
        kind = policy.kind
        out = []
        for (uid, cid), es in W.group(entries).items():
            q = self.fire_group(
                state,
                data,
                policy=FirePolicy(now=now, kind=kind, verdicts=verdicts, counts=counts),
                group=FireGroup(uid=uid, cid=cid, entries=es),
            )
            if q:
                out.append(q)
        return out

    def release_held(self, state, data, now, verdicts, counts):
        """Held fires (see fire_group) whose time came: tried again.
        One held past its grace (a day when the place refused it, a week for
        a reply cap) is dropped and ledgered."""
        out = []
        held = data.setdefault("held", {})
        for fid, h in sorted(held.items(), key=lambda kv: kv[1].get("at", 0)):
            if not isinstance(h, dict) or h.get("next", 0) > now:
                continue
            del held[fid]
            grace = WATCH_REFUSED_GRACE if h.get("why") else W.HELD_KEEP_SECS
            if now - h.get("at", now) > grace:
                why = h.get("why") or "a reply cap"
                print(f"Reminder {fid} could not go out for too long ({why}); dropped.", flush=True)
                self.record(
                    fid,
                    "skipped",
                    f"reminder dropped: {why}",
                    context=LedgerContext(channel_id=h.get("cid"), author_id=h.get("uid")),
                )
                continue
            es = [(r, dict(e, late=True)) for r, e in h.get("es") or []]
            q = self.fire_group(
                state,
                data,
                policy=FirePolicy(
                    now=now, kind=h.get("kind") or "reminder", verdicts=verdicts, counts=counts
                ),
                group=FireGroup(uid=h["uid"], cid=h["cid"], entries=es, fid=fid, held=h),
            )
            if q:
                out.append(q)
        return out

    def fire_group(self, state, data, policy, group):
        """One fire for es [(record, fire entry)] of one person in one
        place, or None when it is held for later instead:
        - the place was not checked this tick (verdicts): the next tick;
        - the bot would not answer them there now (who.replies, the
          ownerless-server gate, who.allow / who.ignore, DMs, or they can no
          longer read it): retried with a growing gap for a day, ledgered
          ("reminder held: <why>"), then dropped;
        - too many fires this tick in that server, or waiting overall:
          spread over the next ticks;
        - for anyone but the owners, a fire counts as a reply toward the
          limits (limits.per_day, limits.per_person_per_day) where it fires;
          over a cap it is held, ledgered ("reminder held: daily cap
          reached", Why no reply? explains it), and tried every
          HELD_RETRY_SECS. Never lost while held."""
        uid = group.uid
        cid = group.cid
        es = group.entries
        fid = group.fid
        held = group.held
        now = policy.now
        kind = policy.kind
        verdicts = policy.verdicts
        counts = policy.counts
        r0 = es[0][0]
        gid, dm = r0.get("guild_id"), bool(r0.get("dm"))
        name = r0.get("name") or uid
        fid = fid or W.new_fire_id(now)
        key = (uid, cid, gid, dm)
        if key not in verdicts:
            hold(
                data,
                pending=PendingFire(
                    fid=fid,
                    uid=uid,
                    cid=cid,
                    entries=es,
                    kind=kind,
                    now=now,
                    next_at=now,
                    held=held,
                    why=(held or {}).get("why"),
                ),
            )
            return None
        why = verdicts[key]
        if why:
            tries = (held or {}).get("tries", 0)
            hid = hold(
                data,
                pending=PendingFire(
                    fid=fid,
                    uid=uid,
                    cid=cid,
                    entries=es,
                    kind=kind,
                    now=now,
                    next_at=now + min(3600, 30 * 2**tries),
                    held=held,
                    why=why,
                ),
            )
            if held is None and hid == fid:
                print(f"Reminder for {name} in {cid} held ({why}).", flush=True)
                self.record(
                    fid,
                    "skipped",
                    f"reminder held: {why}",
                    context=LedgerContext(
                        channel_id=cid, author=name, author_id=uid, detail={"why": why}
                    ),
                )
            return None
        if (
            counts.get(gid, 0) >= WATCH_FIRES_PER_SERVER_TICK
            or len(data["fires"]) >= WATCH_FIRES_MAX
        ):
            hold(
                data,
                pending=PendingFire(
                    fid=fid,
                    uid=uid,
                    cid=cid,
                    entries=es,
                    kind=kind,
                    now=now,
                    next_at=now + WATCH_TICK_SECS,
                    held=held,
                ),
            )
            return None
        scope = self.audience.response.scope_for(gid, cid, dm)
        capped = self.audience.response.rate_block(state, uid, cid, scope)
        if capped:
            hid = hold(
                data,
                pending=PendingFire(
                    fid=fid,
                    uid=uid,
                    cid=cid,
                    entries=es,
                    kind=kind,
                    now=now,
                    next_at=now + W.HELD_RETRY_SECS,
                    held=held,
                ),
            )
            if held is None and hid == fid:
                print(
                    f"Reminder for {name} in {cid} held: a reply cap is reached ({capped['key']}).",
                    flush=True,
                )
                self.record(
                    fid,
                    "skipped",
                    "reminder held: daily cap reached",
                    context=LedgerContext(
                        channel_id=cid, author=name, author_id=uid, detail=capped
                    ),
                )
            return None
        self.audience.response.rate_record(state, uid, scope)
        counts[gid] = counts.get(gid, 0) + 1
        if held is not None:
            self.record(
                fid, "queued", context=LedgerContext(channel_id=cid, author=name, author_id=uid)
            )  # no longer held
        q = self.item(state, fid, group, now, kind)
        data["fires"][fid] = {
            "id": fid,
            "at": now,
            "requester": uid,
            "channel_id": cid,
            "guild_id": gid,
            "dm": dm,
            "originals": sorted({r["message_id"] for r, _ in es}, key=int),
            # (Its history is the place's, re-read if it is queued again.)
            "item": dict(q, history=[]),
        }
        W.note_fired(data, fid, now)
        return q
