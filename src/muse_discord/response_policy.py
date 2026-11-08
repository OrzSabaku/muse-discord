"""Response budgets and audience membership for current configuration.

Mutable counters live in the state supplied by the caller. Roles and scope
lookups are explicit capabilities; this policy does no file or network I/O of
its own. Admission and media-delivery accounting use the same implementation.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from muse_discord import muse_settings as MS
from muse_discord.conversation import Conversation, _snowflake_ts


def _iso_ts(iso):
    try:
        return datetime.fromisoformat(str(iso)).timestamp()
    except TypeError, ValueError:
        return None


def note_guild_joined(state, gid, joined_at):
    ts = _iso_ts(joined_at) if joined_at else None
    if ts is not None:
        state.setdefault("guild_joined", {})[gid] = ts


def join_watermark(state, gid):
    """The bot's join time in a guild. When Discord has not told us yet,
    fall back to the moment this monitor first saw the guild: later than
    the real join, so it can only err toward silence."""
    joined = state.get("guild_joined", {}).get(gid)
    if joined is not None:
        return joined
    first_seen = state.setdefault("guild_first_seen", {})
    if gid not in first_seen:
        first_seen[gid] = time.time()
        print(
            f"Join time unknown for guild {gid}; ignoring messages "
            f"older than now until Discord reports it.",
            flush=True,
        )
    return first_seen[gid]


def predates_join(state, msg, gid):
    if not gid:
        return False  # DMs have no join
    return _snowflake_ts(msg.get("id")) < join_watermark(state, gid)


def _media_counts(state):
    """Today's media.per_day counts in state ("media_sent": day,
    sent_today = the everywhere total, counts by server / dms), reset
    each UTC day."""
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    g = state.get("media_sent")
    if not isinstance(g, dict):
        g = state["media_sent"] = {}
    if g.get("day") != today:
        g.clear()
        g.update(day=today, sent_today=0, counts={})
    return g


@dataclass
class ResponsePolicy:
    settings_source: Callable[[], MS.Settings]
    owners_source: Callable[[], set[str]]
    conversation: Conversation
    resolve_scope: Callable[..., MS.Scope]
    channel_scope: Callable[..., MS.Scope]
    roles: Callable[..., set]
    count: Callable[[str], object]

    def settings(self) -> MS.Settings:
        return self.settings_source()

    @property
    def owners(self) -> set[str]:
        return self.owners_source()

    def scope_for(self, guild_id=None, channel_id=None, dm=False) -> MS.Scope:
        return self.resolve_scope(guild_id, channel_id, dm)

    def member_roles(self, guild_id, user_id):
        return self.roles(guild_id, user_id)

    def is_bot_author(self, author):
        return self.conversation.is_bot_author(author)

    def own_replies_in_run(self, history, msg):
        return self.conversation.own_replies_in_run(history, msg)

    @staticmethod
    def predates_join(state, msg, gid):
        return predates_join(state, msg, gid)

    def rate_block(self, state, user_id, channel_id=None, scope=None):
        """Enforce limits.per_day and limits.per_person_per_day (none = off):
        None when allowed, else the cap that stops it, {"key", "counter"
        ("everywhere", "server:<id>", "dms": where the cap is set), "cap"}.
        Each cap counts what happens in the scope WHERE IT IS SET (everywhere
        = one total across DMs and every server, a server = that server, dms
        = all DMs), and every cap set along the message's scope applies.
        Owners are never capped or counted: these limits bound abuse and
        token spend from strangers, not the owner's own use of the bot.
        Counted at queue time, one per burst."""
        if user_id in self.owners:
            return None
        if scope is None:
            scope = self.channel_scope(channel_id)
        st = self.settings()
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        rate = state["rate"]

        if rate["day"] != today:
            rate["day"] = today
            rate["day_count"] = 0
            rate["users"] = {}
            rate["scopes"] = {}
        scopes = rate.setdefault("scopes", {})

        for cid, cap in st.cap_layers("limits.per_day", scope):
            n = (
                rate["day_count"]
                if cid == "everywhere"
                else (scopes.get(cid) or {}).get("count", 0)
            )
            if n >= cap:
                where = "" if cid == "everywhere" else f" for {cid}"
                print(f"Rate limit: daily cap ({cap}){where} reached, skipping.", flush=True)
                return {"key": "limits.per_day", "counter": cid, "cap": cap}

        day_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        for cid, cap in st.cap_layers("limits.per_person_per_day", scope):
            users = (
                rate["users"]
                if cid == "everywhere"
                else scopes.setdefault(cid, {}).setdefault("users", {})
            )
            user_hits = [t for t in users.get(user_id, []) if t >= day_start]
            users[user_id] = user_hits
            if len(user_hits) >= cap:
                print(
                    f"Rate limit: user {user_id} hit per-user daily cap"
                    f"{'' if cid == 'everywhere' else ' for ' + cid}, "
                    f"skipping.",
                    flush=True,
                )
                return {"key": "limits.per_person_per_day", "counter": cid, "cap": cap}

        return None

    def rate_record(self, state, user_id, scope=None):
        """Count one queued item against every counter of its scope (see
        rate_block). Owners are not counted."""
        if user_id in self.owners:
            return
        rate = state["rate"]
        now = time.time()
        rate["day_count"] += 1
        rate["users"].setdefault(user_id, []).append(now)
        for cid in MS.Settings.counter_ids(scope or MS.Scope.everywhere())[1:]:
            c = rate.setdefault("scopes", {}).setdefault(cid, {})
            c["count"] = c.get("count", 0) + 1
            c.setdefault("users", {}).setdefault(user_id, []).append(now)

    def media_permission(self, state, scope, here, author_matches):
        """may_generate for a message in scope (here: its resolved
        settings): media.enabled is on there, the author is in media.who
        there (an empty list counts as the owners), and no media.per_day
        cap set along the scope is reached (each counts in its own scope,
        see note_media_sent)."""
        if not here["media.enabled"]:
            return False
        if not author_matches(here["media.who"] or "owners"):
            return False
        counts = _media_counts(state)
        for counter, cap in self.settings().cap_layers("media.per_day", scope):
            sent = (
                counts.get("sent_today", 0)
                if counter == "everywhere"
                else (counts.get("counts") or {}).get(counter, 0)
            )
            if sent >= cap:
                print(
                    f"Media generation: daily cap ({cap}) reached; not granting may_generate",
                    flush=True,
                )
                return False
        return True

    def note_media_sent(self, state, scope):
        """Count one reply with generated files sent in scope, for every
        media.per_day counter it adds to (everywhere, its server, or DMs)."""
        self.count("media_sent")
        counts = _media_counts(state)
        counts["sent_today"] = counts.get("sent_today", 0) + 1
        for counter in MS.Settings.counter_ids(scope):
            if counter != "everywhere":
                c = counts.setdefault("counts", {})
                c[counter] = c.get(counter, 0) + 1

    def people_match(self, value, user_id, roles=lambda: set()):
        """True when user_id is in a people value: "everyone", "owners", or
        a list of user ids and "role:<id>" entries (roles() is only called
        when the list names a role)."""
        user_id = str(user_id or "")
        if value == "everyone":
            return True
        if value == "owners":
            return user_id in self.owners
        if not isinstance(value, list) or not value:
            return False
        if user_id in value:
            return True
        want = {v[5:] for v in value if v.startswith("role:")}
        return bool(want and want & set(roles()))
