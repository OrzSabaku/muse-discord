"""muse-discord proactive: the bot starts a conversation on
its own now and then, like a person would, instead of only replying.

Off by default (proactive.enabled; the Friends preset turns it on for
its server). monitor.py loads this module (like telemetry.py) and calls
in at a few points. Separate services own storage, consent, eligibility
and heartbeat clocks:

- tick(state): the main loop. A heartbeat at jittered times
  (proactive.frequency: rarely about 1-2 a day, sometimes 4-6, often
  about hourly; moved out of quiet hours). Each heartbeat works out who
  and where the bot may reach out to right now (eligible(): owners by
  DM, people who DMed the bot first, quiet server channels where it is
  on) and, when anyone is eligible and the day's total allows, queues
  ONE item of kind "proactive" (a slot) for the worker: one wake, which
  usually ends in silence. The slot carries a nonce and the list of
  targets; it lives SLOT_SECS.
- outbox_item(state, item): an outbox item naming a slot. It may post
  only to one of that slot's targets, only with the slot's nonce, only
  while the slot is fresh and unspent, and only within the caps, in
  quiet-hours-free time, with the target still eligible. It never
  falls through to the ordinary send path.
- opt_action(state, item): "stop messaging me" (and "you can message me
  again"): an outbox item answering a person's own message carries
  "proactive": "opt-out" | "opt-in"; the person is that message's
  author from the monitor's own record, never a field the worker
  writes. An opt-out is recorded; an opt-in only asks (below).
- Asks: what a run could be steered into against someone (opting them
  back in; the time zone their quiet hours use, note_tz) is never
  taken from the outbox. The monitor DMs that person its own message
  with a Yes button (custom_id mdpro:yes:<token>), and only their own
  click records it (on_button on the interaction thread, apply_button
  in the main loop). A button lives ASK_SECS, works once, and a person
  gets at most ASKS_PER_DAY of them.
- note_item(mid, meta): every queued message (who wrote, where, when):
  whom the bot may DM first (only people who DMed it), and whether
  someone answered the last unprompted message.
- The live slot's nonce goes to proactive_nonce.json (mode 600; never
  owner_dm_nonces.json, so nothing takes it for an owner-DM nonce): the
  wake hook hands a proactive item out alone (never with anyone's
  message) and only that wake gets the nonce, as for an owner's DM.

The binding: like a reminder or watch fire, a slot is an item the
monitor starts (its own kind, an id from monitor.started_item_id), plus
the places its reply may go; reply_policy.ReplyPolicy.bind_started binds both, before the
owner-DM gate. slot = {"id", "kind": "proactive", "nonce", "at",
"expires", "targets": {channel_id: {"type", "user_id"}}, "max_posts",
"posts", "spent"}. Unlike a fire's reply, a slot's post is sent here
(through monitor.send_fragmented, pinging at most its one person) and
never written to the mention ledger. The worker cannot add a target, so nothing it read (other
people's messages, its memory) can steer an unprompted message to
anyone else, and the caps bound the rest.

proactive.json (mode 600, monitor-internal): the next heartbeat, the
live slot, spent slot ids, the unprompted messages sent (for the caps),
who has messaged the bot, opt-outs and asks. One that cannot be read
is moved aside (proactive.json.bad-<time>), the owners are told, and no
heartbeat runs while such a file is there: the opt-outs in it are never
silently lost."""

import hmac
import random
import secrets
import time
from datetime import datetime
from http import HTTPStatus

from muse_discord import muse_settings as MS
from muse_discord.message_models import DISCORD_MAX_CHARS, DeliveryOptions, SecretBlocked, Speaker
from muse_discord.proactive.consent import ConsentRequests
from muse_discord.proactive.heartbeat import HeartbeatSchedule
from muse_discord.proactive.model import (
    DAY,
    FIELD,
    FLUSH_SECS,
    FORBIDDEN,
    KIND,
    MAX_PER_SLOT,
    NONCE_FIELD,
    OPT_VALUES,
    SEND_RETRY_SECS,
    SENT_KEEP_SECS,
    SLOT_SECS,
    SPENT_KEEP,
    TEXT_KINDS,
    TICK_SECS,
    ProactiveContext,
    Verdict,
)
from muse_discord.proactive.store import ProactiveStore
from muse_discord.proactive.targets import TargetPolicy
from muse_discord.proactive.timing import iso, snowflake_ts, zone


class Proactive:
    def __init__(self, context: ProactiveContext, clock=time.time, rng=None):
        self.context = context
        self.clock = clock
        self._next_tick = 0.0
        self.store = ProactiveStore(
            context.path, context.nonce_path, context.write_private, lambda: self.clock()
        )
        self.consent = ConsentRequests(
            self.store,
            context.settings,
            context.send_reply,
            context.rest_backing_off,
            lambda: self.clock(),
        )
        self.policy = TargetPolicy(context, self.store, lambda: self.clock())
        self.heartbeat = HeartbeatSchedule(self.policy, self.store, rng or random.Random())

    def describe(self, state, t, now):
        """What the worker gets about one target: who or where, when they
        last talked and how that ended, the last unprompted message, and
        the settings card of the place. No message text."""
        context = self.context
        cid, uid = t["channel_id"], t.get("user_id")
        hist = self.policy.history(state, cid)
        out = {"channel_id": cid, "type": t["type"]}
        mine = self.policy.sent_to("channel_id", cid, 0)
        out["last_unprompted"] = iso(max([e["at"] for e in mine] or [0]))
        if t["type"] == "channel":
            info = context.channels().get(cid) or {}
            gid = info.get("guild_id")
            scope = context.scope_for(gid, cid)
            card = context.settings_card(
                scope,
                self.policy.st().resolve(scope),
                cid,
                speaker=Speaker(author_id=None, from_bot=False),
            )
            card.pop("speaker", None)
            humans = [h for h in hist if not self.policy.is_bot_msg(h)]
            out.update(
                where=context.channel_display(info),
                last_message_at=iso(snowflake_ts(hist[-1]["id"])) if hist else None,
                recent_messages=len([h for h in humans if now - snowflake_ts(h.get("id")) < DAY]),
                recent_people=len(
                    {h.get("author_id") for h in humans if now - snowflake_ts(h.get("id")) < DAY}
                ),
            )
        else:
            p = self.store.data()["people"].setdefault(uid, {})
            name = (
                context.user_names().get(uid)
                or p.get("name")
                or context.dm_channels().get(cid)
                or uid
            )
            if name != uid and p.get("name") != name:
                p["name"] = name
                self.store.dirty = True
            scope = MS.Scope.dm()
            card = context.settings_card(
                scope,
                self.policy.st().resolve(scope),
                cid,
                speaker=Speaker(author_id=uid, from_bot=False),
            )
            card.pop("places", None)
            last_them = max(
                [p.get("last", 0)]
                + [snowflake_ts(h.get("id")) for h in hist if str(h.get("author_id")) == uid]
            )
            last_you = max(
                [0] + [snowflake_ts(h.get("id")) for h in hist if self.policy.is_bot_msg(h)]
            )
            outcome = None
            if p.get("last_mid"):
                e = context.ledger()["entries"].get(p["last_mid"]) or {}
                outcome = e.get("code")
            tz = self.policy.tz_of(uid)
            out.update(
                user_id=uid,
                name=name,
                owner=t["type"] == "owner",
                local_time=datetime.fromtimestamp(now, zone(tz)).strftime("%A %H:%M"),
                timezone=tz,
                where=f"DM with {name}",
                last_from_them=iso(last_them),
                last_from_you=iso(last_you),
                ended_with=None
                if not (last_them or last_you)
                else ("you" if last_you > last_them else "them"),
                their_last_message=outcome,
            )
        out["settings"] = card
        return out

    def make_slot(self, state, targets, now):
        d = self.store.data()
        sid = self.context.started_item_id(now)  # the fires' id scheme
        slot = {
            "id": sid,
            "kind": KIND,
            "nonce": secrets.token_hex(16),
            "at": now,
            "expires": now + SLOT_SECS,
            "max_posts": MAX_PER_SLOT,
            "spent": False,
            "posts": {},
            "targets": {
                t["channel_id"]: {"type": t["type"], "user_id": t.get("user_id")} for t in targets
            },
        }
        tzname = self.policy.every("proactive.timezone")
        local = datetime.fromtimestamp(now, zone(tzname))
        item = {
            "message_id": sid,
            "kind": KIND,
            "channel_id": None,
            "channel_name": "(no one asked: your call)",
            "guild_id": None,
            "author": None,
            "author_id": None,
            "content": "",
            "timestamp": iso(now),
            "queued_at": now,
            "media": [],
            "documents": [],
            "unviewable": [],
            "history": [],
            "may_generate": False,
            "owner_command": False,
            "reply_to": None,
            "burst": False,
            "burst_fragments": [],
            "from_bot": False,
            "bot_turns": 0,
            "participants": [],
            "follow_up": False,
            "settings": None,
            FIELD: {
                "expires_at": iso(now + SLOT_SECS),
                "max_messages": MAX_PER_SLOT,
                "local_time": local.strftime("%A %H:%M"),
                "timezone": tzname,
                "targets": [self.describe(state, t, now) for t in targets],
            },
        }
        slot["pending_item"] = item
        d["slot"] = slot
        d["side"] = "public" if targets[0]["type"] == "channel" else "private"
        self.store.save()  # the slot (and its nonce) first,
        self.store.write_nonces()  # then the hook's nonce file,
        self.publish_slot()  # then the queue (retried by upkeep if busy)
        print(
            f"Heartbeat: {len(targets)} place(s) or people it may reach out to; queued slot {sid}.",
            flush=True,
        )
        return item

    def publish_slot(self):
        """Retry a saved slot item without changing its nonce or heartbeat deadline."""
        slot = self.store.data().get("slot") or {}
        item = slot.get("pending_item")
        if item is None:
            return
        self.context.queue_edit(add=[item])
        slot.pop("pending_item", None)
        self.store.save()

    def end_slot(self, state, why):
        d = self.store.data()
        s = d.get("slot")
        if not s:
            return
        d["spent"][s["id"]] = self.clock()
        for old in sorted(d["spent"], key=d["spent"].get)[:-SPENT_KEEP]:
            del d["spent"][old]
        d["slot"] = None
        sid = s["id"]
        try:
            self.context.queue_edit(drop=lambda q: str(q.get("message_id")) == sid)
        except Exception as e:
            print(f"Proactive: could not drop slot {sid} ({e}).", flush=True)
        self.store.save()
        self.store.write_nonces()
        print(f"Slot {sid} ended ({why}).", flush=True)

    def tick(self, state, now=None):
        """Main loop: slot upkeep, then the heartbeat when it is due."""
        now = self.clock() if now is None else now
        self.consent.send_pending(now)  # each pass: right after the reply it follows
        if now < self._next_tick:
            return
        self._next_tick = now + TICK_SECS
        d = self.store.data()
        self._tick_upkeep(state, now)
        due = self.heartbeat.due_heartbeats(now)
        if due:
            if d.get("slot"):
                print("Heartbeat deferred: the last one is still open.", flush=True)
            elif self.store.blocked():
                print(
                    "Heartbeat skipped: a bad proactive.json is set aside "
                    "(docs/PROTOCOL.md, proactive.json).",
                    flush=True,
                )
            elif self.policy.sent_total(now) < self.policy.total_cap():
                candidates = [
                    t
                    for t in self.policy.eligible(state, now)
                    if self.policy.heartbeat_key(t) in due
                ]
                targets = self.heartbeat.pick_side(candidates)
                available = {self.policy.heartbeat_key(t) for t in candidates}
                selected = {self.policy.heartbeat_key(t) for t in targets}
                self.heartbeat.advance_heartbeats((due - available) | selected, now)
                if targets:
                    self.make_slot(state, targets, now)
                    return
                print("Heartbeat: nobody and nowhere to reach out to now; no wake.", flush=True)
        if self.store.dirty and now - self.store.flushed >= FLUSH_SECS:
            self.store.save()

    def _tick_upkeep(self, state, now):
        d = self.store.data()
        s = d.get("slot")
        if s and not self.store.slot_ok(s):
            print("Proactive: a damaged slot record was dropped.", flush=True)
            d["slot"] = s = None
        if s:
            sending = [
                p
                for p in s["posts"].values()
                if p.get("status") == "sending" and now - p.get("first", now) < SEND_RETRY_SECS + 60
            ]
            if s.get("spent") and not sending:
                self.end_slot(state, "used")
            elif not s.get("spent") and now > s.get("expires", 0):
                self.end_slot(state, "expired unanswered")
        self.store.write_nonces()  # (after a restart, say; only when it changed)
        self.publish_slot()
        self.drop_orphans()
        if self.store.alert and self.context.notify_owners(self.store.alert):
            self.store.alert = None
        cut = now - SENT_KEEP_SECS
        if any(e.get("at", 0) < cut for e in d["sent"]):
            d["sent"] = [e for e in d["sent"] if e.get("at", 0) >= cut]
            self.store.dirty = True
        if not d.get("seeded"):
            # People who DMed the bot before this module kept count.
            for mid, meta in list((state.get("item_meta") or {}).items()):
                self.policy.note_item(mid, meta)
            d["seeded"] = True
            self.store.dirty = True

    def drop_orphans(self):
        """A proactive queue item that is not the live slot (its record
        was lost, or it ended) leaves the queue: it would never get a
        nonce, and the hook would hold it back for good."""
        s = self.store.data().get("slot") or {}
        live = s.get("id") if not s.get("spent") else None
        try:
            if any(
                isinstance(q, dict) and q.get("kind") == KIND and str(q.get("message_id")) != live
                for q in self.context.load_queue()
            ):
                self.context.queue_edit(
                    drop=lambda q: q.get("kind") == KIND and str(q.get("message_id")) != live
                )
                print("Proactive: dropped a slot item with no live slot.", flush=True)
        except (OSError, ValueError) as e:
            print(f"Proactive: queue unreadable ({e}).", flush=True)

    def names_slot(self, item):
        """The slot id an outbox item names (item or reply_to; a live or
        spent slot), else None: such an item is bound here, by the slot's
        nonce, never by a reply_nonce (monitor.bound_elsewhere)."""
        if not isinstance(item, dict):
            return None
        d = self.store.data()
        s = d.get("slot") if self.store.slot_ok(d.get("slot")) else {}
        known = set(d["spent"]) | ({s["id"]} if s else set())
        names = [str(item[f]) for f in ("item", "reply_to") if item.get(f)]
        return next((n for n in names if n in known), None)

    def outbox_item(self, state, item):
        """None when the outbox item names no slot (it goes the usual way);
        else a Verdict. Posts only to the slot's targets, with its nonce,
        while it is fresh and unspent, within the caps. A damaged record
        never raises: an item naming a slot is then set aside."""
        d = self.store.data()
        s = d.get("slot") if self.store.slot_ok(d.get("slot")) else {}
        sid = self.names_slot(item)
        if sid is None:
            return None
        try:
            return self._decide(state, item, d, s, sid)
        except Exception as e:
            print(f"Proactive: slot {sid}: {type(e).__name__}: {e}", flush=True)
            return Verdict(f"slot {sid}: could not be checked", spent=sid)

    def _slot_binding_problem(self, item, s, sid):
        if str(item.get("item") or "") != sid:
            return Verdict(f"slot {sid}: item must be the slot's message_id")
        bad = [k for k in FORBIDDEN if k in item]
        if bad:
            return Verdict(f"slot {sid}: carries no {', '.join(bad)}")
        if not s or s["id"] != sid:
            return Verdict(f"slot {sid}: already used or expired")
        got = item.get(NONCE_FIELD)
        if not isinstance(got, str) or not hmac.compare_digest(
            s["nonce"].encode(), got.encode("utf-8", "replace")
        ):
            # A forgery from another wake: the slot is untouched.
            return Verdict(f"slot {sid}: bad nonce")
        return None

    def _slot_status_problem(self, s, now):
        sid = s["id"]
        if s.get("spent"):
            return Verdict(f"slot {sid}: already used", spent=sid)
        if self.store.blocked():
            s["spent"] = True
            return Verdict(f"slot {sid}: proactive.json is set aside", spent=sid)
        if now > s.get("expires", 0):
            return Verdict(f"slot {sid}: expired", spent=sid)
        return None

    def _decide(self, state, item, d, s, sid):
        now = self.clock()
        problem = self._slot_binding_problem(item, s, sid)
        if problem is not None:
            return problem
        cid = str(item.get("channel_id") or "")
        post = s["posts"].get(cid)
        if post and post.get("status") == "sending":
            return self._send(state, s, cid, item, now)  # a retry
        problem = self._slot_status_problem(s, now)
        if problem is not None:
            return problem
        if item.get("silent"):
            s["spent"] = True
            self.store.save()
            print(f"Slot {sid}: the worker chose not to reach out.", flush=True)
            return Verdict(spent=sid)
        return self._accept_post(state, item, s, now)

    def _accept_post(self, state, item, s, now):
        sid = s["id"]
        cid = str(item.get("channel_id") or "")
        d = self.store.data()
        t = s["targets"].get(cid)
        if t is None:
            s["spent"] = True
            self.store.save()
            return Verdict(f"slot {sid}: {cid} is not one of its targets", spent=sid)
        t = dict(t, channel_id=cid)
        why = self.policy.target_problem(state, t, now)
        if why is None and len(s["posts"]) >= s.get("max_posts", MAX_PER_SLOT):
            why = "the slot's messages are used"
            # Clamped to Discord's limit after the rewrites, as replies are.
        parts = [x[:DISCORD_MAX_CHARS] for x in self.context.normalize_parts(item)]
        if why is None and not parts:
            why = "nothing to send"
        if (
            why is None
            and t["type"] != "owner"
            and self.context.never_share_hit(state, {"channel_id": cid}, None, parts)
        ):
            why = "it contains a never_share phrase (dropped)"
        if why is not None:
            s["spent"] = True
            self.store.save()
            return Verdict(f"slot {sid}: not sent to {cid}: {why}", spent=sid)
        s["posts"][cid] = {"status": "sending", "first": now, "n": 0, "next": 0, "parts": parts}
        if len(s["posts"]) >= s.get("max_posts", MAX_PER_SLOT):
            s["spent"] = True
        entry = {
            "at": now,
            "slot": sid,
            "channel_id": cid,
            "user_id": t.get("user_id"),
            "type": t["type"],
        }
        d["sent"].append(entry)
        self.store.save()
        return self._send(state, s, cid, item, now)

    def _send(self, state, s, cid, item, now):
        """Send (or retry) the accepted post for cid: the parts fixed at
        acceptance (the outbox copy is worker-writable), pinging at most
        that one person."""
        sid, post = s["id"], s["posts"][cid]
        if post.get("next", 0) > now:
            return Verdict(retry=True, spent=sid)
        t = s["targets"][cid]
        mentions = {"parse": []}
        if t.get("user_id"):
            mentions["users"] = [t["user_id"]]
        parts = post["parts"]
        try:
            # The monitor's one send path (plain messages: no reply_to),
            # each part's Discord nonce seeded with the slot id.
            msgs = self.context.send_fragmented(
                cid, parts, None, (), delivery=DeliveryOptions(mentions=mentions, nonce=sid)
            )
        except Exception as e:
            return self._send_failure(s, cid, now, e)
        post["status"] = "sent"
        post.pop("parts", None)
        for x in self.store.data()["sent"]:
            if x.get("slot") == sid and x.get("channel_id") == cid:
                x["msgs"] = msgs[-4:]  # for reactions (an answer too)
        self.store.save()
        print(
            f"Slot {sid}: reached out in {cid} ({t['type']}, "
            f"{len(parts)} part(s)): {parts[0][:60]}",
            flush=True,
        )
        return Verdict(spent=sid)

    def _send_failure(self, s, cid, now, e):
        sid, post = s["id"], s["posts"][cid]
        code = getattr(e, "code", None)
        post["n"] = post.get("n", 0) + 1
        # A secret in it (the monitor's send guard): never retried,
        # the slot is used up, nothing counts toward the caps.
        blocked = isinstance(e, SecretBlocked)
        # A part may have landed: any part after the first (on this
        # or an earlier attempt), or the first one when the error
        # does not say it was refused (a timeout can land).
        part = getattr(e, "failed_part", None)
        if part != 0 or getattr(e, "sent_ids", None):
            post["landed"] = True
        refused = (
            isinstance(code, int)
            and HTTPStatus.BAD_REQUEST <= code < HTTPStatus.INTERNAL_SERVER_ERROR
        )
        maybe = not blocked and (post.get("landed") or not refused)
        if code == HTTPStatus.FORBIDDEN or blocked or now - post["first"] >= SEND_RETRY_SECS:
            post["status"] = "failed"
            if maybe or code == HTTPStatus.FORBIDDEN:
                # It counts, as a reply that may have landed does; a
                # 403 (blocked, or no longer allowed there) too, so
                # nobody is tried again before they write.
                for x in self.store.data()["sent"]:
                    if x.get("slot") == sid and x.get("channel_id") == cid:
                        x["msgs"] = list(getattr(e, "sent_ids", None) or [])[-4:]
            else:
                self.store.data()["sent"] = [
                    x
                    for x in self.store.data()["sent"]
                    if not (x.get("slot") == sid and x.get("channel_id") == cid)
                ]
            self.store.save()
            print(
                f"Slot {sid}: could not post to {cid} ({e}); "
                f"dropped{' (part of it may have landed)' if maybe else ''}.",
                flush=True,
            )
            return Verdict(spent=sid)
        post["next"] = now + min(30, 2 ** post["n"])
        self.store.save()
        print(
            f"Slot {sid}: post to {cid} failed (attempt {post['n']}; retrying, same nonce): {e}",
            flush=True,
        )
        return Verdict(retry=True, spent=sid)

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
