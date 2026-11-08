"""Who may receive an unprompted message, under current settings and history."""

from muse_discord import muse_settings as MS
from muse_discord.proactive.model import (
    ACTIVE_SECS,
    DAY,
    PEOPLE_MAX,
    REACT_CHECK_SECS,
    TEXT_KINDS,
    WEEK,
    ProactiveContext,
)
from muse_discord.proactive.store import ProactiveStore
from muse_discord.proactive.timing import in_quiet, parse_hours, snowflake_ts, zone
from muse_discord.reply_protocol import is_snowflake


class TargetPolicy:
    def __init__(self, context: ProactiveContext, store: ProactiveStore, clock):
        self.context = context
        self.store = store
        self.clock = clock

    def st(self):
        return self.context.settings()

    def every(self, key):
        return self.st().get(key, MS.Scope.everywhere())

    def tz_of(self, uid=None):
        """A person's own time zone when they confirmed it with their
        own button (note_tz), else proactive.timezone. Never the
        reminders' tz as such: a run can set that on anyone's message
        it holds."""
        if uid:
            p = self.store.data()["people"].get(str(uid)) or {}
            if isinstance(p.get("tz"), str) and p["tz"]:
                return p["tz"]
        return self.every("proactive.timezone")

    def quiet_now(self, now, uid=None):
        """Quiet hours now: in the person's own time zone (uid) when
        known, else in proactive.timezone."""
        return in_quiet(
            now, parse_hours(self.every("proactive.quiet_hours")), zone(self.tz_of(uid))
        )

    def enabled_anywhere(self):
        st = self.st()
        if st.get("proactive.enabled", MS.Scope.dm()) or st.get(
            "proactive.enabled", MS.Scope("all_servers")
        ):
            return True
        for cid, info in self.context.channels().items():
            if st.get("proactive.enabled", self.context.scope_for(info.get("guild_id"), cid)):
                return True
        return False

    def heartbeat_scopes(self):
        """Independent clocks: one per enabled server, and one for DMs.
        Channel overrides decide whether a server needs a clock.
        """
        st = self.st()
        scopes = {}
        if st.get("proactive.enabled", MS.Scope.dm()):
            scopes["dms"] = MS.Scope.dm()
        for cid, info in self.context.channels().items():
            gid = info.get("guild_id")
            if gid and st.get("proactive.enabled", self.context.scope_for(gid, cid)):
                scopes[str(gid)] = MS.Scope.server(gid)
        return scopes

    def heartbeat_key(self, target):
        if target["type"] != "channel":
            return "dms"
        info = self.context.channels().get(target["channel_id"]) or {}
        return str(info.get("guild_id"))

    def note_item(self, mid, meta):
        """A queued message (note_item_meta): who talked to the bot,
        where, when; a DM means they started a DM conversation."""
        try:
            if not isinstance(meta, dict) or meta.get("bot") or meta.get("kind") not in TEXT_KINDS:
                return
            uid = str(meta.get("author_id") or "")
            if not uid:
                return
            people = self.store.data()["people"]
            p = people.setdefault(uid, {})
            self._update_person(mid, meta, p)
            if len(people) > PEOPLE_MAX:
                for old in sorted(people, key=lambda u: people[u].get("last", 0))[
                    : len(people) - PEOPLE_MAX
                ]:
                    del people[old]
            self.store.dirty = True
        except Exception as e:
            print(f"Proactive note error: {e}", flush=True)

    def _update_person(self, mid, meta, p):
        ts = snowflake_ts(mid) or self.clock()
        cid = str(meta.get("channel_id") or "")
        if ts >= p.get("last", 0):
            p.update(last=ts, last_mid=str(mid), last_cid=cid)
        seen = p.setdefault("seen", {})  # last message per place
        if cid and ts > seen.get(cid, 0):
            seen[cid] = ts
            for old in sorted(seen, key=seen.get)[:-10]:
                del seen[old]
        if meta.get("dm") and meta.get("channel_id"):
            p["dm"] = str(meta["channel_id"])
        if meta.get("author"):
            p["name"] = str(meta["author"])[:100]

    def sent_to(self, key, value, since):
        return [
            e for e in self.store.data()["sent"] if e.get(key) == value and e.get("at", 0) >= since
        ]

    def history(self, state, cid):
        return (state.get("history") or {}).get(cid) or []

    def is_bot_msg(self, h):
        """This bot's own message."""
        return str(h.get("author_id")) in (
            str(self.context.bot_ids()[1]),
            str(self.context.bot_ids()[0]),
        )

    def human(self, h):
        """A person's message: not this bot, another bot or a webhook."""
        return not (h.get("bot") or h.get("webhook") or self.is_bot_msg(h))

    def reacted(self, e):
        """Someone reacted to that unprompted message: an answer too.
        Looked up once it matters, at most every REACT_CHECK_SECS (never
        during a REST backoff: these calls block the main loop), and
        remembered."""
        if e.get("answered"):
            return True
        now = self.clock()
        if (
            not e.get("msgs")
            or self.context.rest_backing_off()
            or now - e.get("checked", 0) < REACT_CHECK_SECS
        ):
            return False
        e["checked"] = now
        self.store.dirty = True
        try:  # its last part: one call
            context = self.context.api_get(f"/channels/{e['channel_id']}/messages/{e['msgs'][-1]}")
        except Exception:
            return False
        for r in (context or {}).get("reactions") or []:
            if int(r.get("count") or 0) - (1 if r.get("me") else 0) > 0:
                e["answered"] = True
                return True
        return False

    def person_problem(self, state, uid, cid, now):
        """Why no unprompted message may go to person uid (in their DM
        channel cid) now, or None."""
        d = self.store.data()
        if uid in d["opted_out"]:
            return "opted out"
        st = self.st()
        dm = MS.Scope.dm()
        if self.sent_total(now) >= self.total_cap():
            return "the day's total is reached"
        day = st.get("proactive.per_person_per_day", dm)
        week = st.get("proactive.per_person_per_week", dm)
        if len(self.sent_to("user_id", uid, now - DAY)) >= day:
            return "per person per day cap"
        if len(self.sent_to("user_id", uid, now - WEEK)) >= week:
            return "per person per week cap"
        return self._person_conversation_problem(state, uid, cid, now)

    def _person_conversation_problem(self, state, uid, cid, now):
        d = self.store.data()
        mine = self.sent_to("user_id", uid, 0)
        p = d["people"].get(uid) or {}
        hist = self.history(state, cid)
        if mine:
            # Answered: they wrote in that same DM since (a message of
            # theirs elsewhere is not an answer), or reacted to it.
            e = max(mine, key=lambda x: x["at"])
            answered = (
                (p.get("seen") or {}).get(cid, 0) > e["at"]
                or any(self.human(h) and snowflake_ts(h.get("id")) > e["at"] for h in hist)
                or self.reacted(e)
            )
            if not answered:
                return "no answer to the last unprompted message yet"
        newest = max([snowflake_ts(h.get("id")) for h in hist] + [p.get("last", 0)])
        if now - newest < ACTIVE_SECS:
            return "a conversation is going on"
        return None

    def channel_problem(self, state, cid, now):
        """Why no unprompted message may go to server channel cid now,
        or None (a channel the bot reads and may post in, not a thread,
        proactive on, replies not off or paused there, an owner in the
        server, quiet for ACTIVE_SECS, the bot not the last to speak,
        within the caps)."""
        context = self.context
        info = context.channels().get(cid)
        if not info or info.get("thread") or info.get("can_send") is not True:
            return "not a channel the bot can post in"
        gid = info.get("guild_id")
        if context.owners_absent(gid) or context.owner_check_pending(state, gid):
            return "none of the owners is in that server"
        here = self.st().resolve(context.scope_for(gid, cid))
        if not here["proactive.enabled"]:
            return "proactive is off there"
        if here["who.replies"] == "off":
            return "replies are off (or paused) there"
        problem = self._channel_cap_problem(cid, now, here)
        if problem:
            return problem
        return self._channel_conversation_problem(state, cid, now)

    def _channel_cap_problem(self, cid, now, here):
        if self.sent_total(now) >= self.total_cap():
            return "the day's total is reached"
        cap = here["proactive.per_channel_per_day"]
        if len(self.sent_to("channel_id", cid, now - DAY)) >= cap:
            return "per channel per day cap"
        return None

    def _channel_conversation_problem(self, state, cid, now):
        hist = self.history(state, cid)
        humans = [snowflake_ts(h.get("id")) for h in hist if self.human(h)]
        if not humans:
            return "no one is known to talk there"
        last_h = max(humans)
        own = [snowflake_ts(h.get("id")) for h in hist if self.is_bot_msg(h)]
        if own and max(own) > last_h:
            return "the bot spoke last there"
        if now - last_h < ACTIVE_SECS:
            return "a conversation is going on"
        mine = self.sent_to("channel_id", cid, 0)
        if mine:
            # Answered: a person wrote there since (other bots, feeds and
            # webhooks are not people), or someone reacted to it.
            e = max(mine, key=lambda x: x["at"])
            if last_h <= e["at"] and not self.reacted(e):
                return "no answer to the last unprompted message yet"
        return None

    def total_cap(self):
        """Explicit bot-wide daily ceiling, independent of frequency."""
        return self.every("proactive.per_day")

    def sent_total(self, now):
        return len([e for e in self.store.data()["sent"] if e.get("at", 0) >= now - DAY])

    def owner_dm_channel(self, uid):
        """An owner's DM channel: known, else opened (the same call
        notify_owners makes); None when Discord cannot say."""
        p = self.store.data()["people"].get(uid) or {}
        if p.get("dm") or p.get("owner_dm"):
            return p.get("dm") or p["owner_dm"]
        for cid, u in self.context.dm_users().items():
            if u == uid:
                return str(cid)
        if self.context.rest_backing_off():
            return None
        try:
            c = self.context.api_post("/users/@me/channels", {"recipient_id": uid})
        except Exception as e:
            print(f"Proactive: no DM channel for owner {uid} ({e}).", flush=True)
            return None
        cid = str((c or {}).get("id") or "")
        if not is_snowflake(cid):
            return None
            # Its own key: "dm" means they DMed the bot first, and an owner who
            # never did must not become a "dm" target once no longer an owner.
        self.store.data()["people"].setdefault(uid, {})["owner_dm"] = cid
        self.store.dirty = True
        return cid

    def target_problem(self, state, t, now):
        """Why target t (a slot's binding entry) is not eligible now."""
        if self.quiet_now(now, t.get("user_id")):
            return "quiet hours"
        if t["type"] == "channel":
            return self.channel_problem(state, t["channel_id"], now)
        return self._dm_target_problem(state, t, now)

    def _dm_target_problem(self, state, t, now):
        st = self.st()
        dm_here = st.resolve(MS.Scope.dm())
        if not dm_here["proactive.enabled"]:
            return "proactive is off for DMs"
        if dm_here["who.replies"] == "off":
            return "DMs are off (or paused)"
        uid = t["user_id"]
        owner = uid in self.context.owners()
        if t["type"] == "owner" and not owner:
            return "no longer an owner"
        if t["type"] == "dm":
            problem = self._dm_identity_problem(t, uid, owner)
            if problem:
                return problem
        return self.person_problem(state, uid, t["channel_id"], now)

    def _dm_identity_problem(self, t, uid, owner):
        if owner:
            return "an owner now (owner check-ins instead)"
        p = self.store.data()["people"].get(uid) or {}
        if p.get("dm") != t["channel_id"]:
            return "never DMed the bot there"
        if self.context.dm_refusal({"id": uid, "username": p.get("name") or uid}) is not None:
            return "the DM settings no longer answer them"
        return None

    def eligible(self, state, now):
        """[target] the bot may reach out to now (the slot's list)."""
        context, d = self.context, self.store.data()
        cands = []
        dm_here = self.st().resolve(MS.Scope.dm())
        if dm_here["proactive.enabled"] and dm_here["who.replies"] != "off":
            for uid in sorted(context.owners()):
                if uid in d["opted_out"]:
                    continue
                cid = self.owner_dm_channel(uid)
                if cid:
                    cands.append({"type": "owner", "user_id": uid, "channel_id": cid})
            for uid, p in sorted(d["people"].items()):
                if p.get("dm") and uid not in context.owners() and uid not in d["opted_out"]:
                    cands.append({"type": "dm", "user_id": uid, "channel_id": p["dm"]})
        for cid in sorted(context.channels()):
            cands.append({"type": "channel", "user_id": None, "channel_id": str(cid)})
        return self._eligible_targets(state, cands, now)

    def _eligible_targets(self, state, cands, now):
        out = []
        for t in cands:
            try:
                why = self.target_problem(state, t, now)
            except Exception as e:
                why = f"error ({e})"
            if why is None:
                out.append(t)
        return out
