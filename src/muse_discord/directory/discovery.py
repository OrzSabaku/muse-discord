"""Incremental channel/thread discovery and initial ingestion watermarks.

REST access, identity and directory registries are explicit inputs. A transient
failure retains known channels; new threads start at their own creation id so
messages written before discovery are still ingested.
"""

import time
from collections.abc import Callable, Container
from dataclasses import dataclass
from typing import Any

from muse_discord.channel_access import ChannelAccess
from muse_discord.conversation import PRIVATE_THREAD_TYPE, _snowflake_ts, now_snowflake
from muse_discord.directory.state import DirectoryState
from muse_discord.reply_protocol import is_snowflake as _is_snowflake

READABLE_CHANNEL_TYPES = (0, 5)
THREAD_PARENT_TYPES = (15, 16)
PERM_SEND_MESSAGES = 1 << 11
PERM_SEND_IN_THREADS = 1 << 38
NEW_THREAD_SLACK_SECS = 60


def _no_access(e):
    """True when a read probe failed because the bot cannot read the
    channel (403 Missing Access, 404 Unknown Channel). Anything else (429,
    timeout, 5xx) is transient and says nothing about access."""
    return getattr(e, "code", None) in (403, 404)


def _thread_info(t, gid, gname, names_by_id, perms_by_id):
    can_send = None
    if perms_by_id is not None:
        parent_perms = perms_by_id.get(t.get("parent_id"))
        if parent_perms is not None:
            can_send = bool(parent_perms & (PERM_SEND_IN_THREADS | PERM_SEND_MESSAGES))
    parent = names_by_id.get(t.get("parent_id"), "thread")
    return {
        "name": f"{parent} > {t.get('name', 'thread')}",
        "guild_id": gid,
        "guild_name": gname,
        "can_send": can_send,
        "thread": True,
        "parent_id": t.get("parent_id"),
        "private": t.get("type") == PRIVATE_THREAD_TYPE,
    }


@dataclass
class ThreadSnapshot:
    names: dict
    known_ids: Container[str]
    permissions: dict | None = None
    previous: dict | None = None


@dataclass
class ChannelProbe:
    discovery: Discovery
    gid: str
    gname: str
    base_perms: int | None
    member_roles: list | set[str]
    previous: dict
    found: dict

    def scan(self, channels):
        permissions = {}
        for ch in channels:
            ctype = ch.get("type")
            if ctype not in READABLE_CHANNEL_TYPES + THREAD_PARENT_TYPES:
                continue
            cid = ch["id"]
            perms = None
            if self.base_perms is not None:
                perms = self.discovery.channel_perms(
                    self.gid, self.base_perms, ch.get("permission_overwrites"), self.member_roles
                )
                permissions[cid] = perms
            if ctype not in READABLE_CHANNEL_TYPES:
                continue  # forum/media: only their threads are read
            if self.discovery.backing_off():
                if cid in self.previous:
                    self.found[cid] = self.previous[cid]
                continue  # probing on would only extend the rate limit
            try:
                self.discovery.get(f"/channels/{cid}/messages?limit=1")
            except Exception as e:
                if not _no_access(e) and cid in self.previous:
                    self.found[cid] = self.previous[cid]  # transient: keep it
                continue  # no read access; skip quietly
            finally:
                yield
            can_send = None
            if perms is not None:
                can_send = bool(perms & PERM_SEND_MESSAGES)
            self.found[cid] = {
                "name": ch["name"],
                "guild_id": self.gid,
                "guild_name": self.gname,
                "can_send": can_send,
            }
        return permissions


@dataclass
class Discovery:
    get: Callable[[str], Any]
    backing_off: Callable[[], bool]
    identity: Callable[[], str | None]
    allowed_guilds: set[str]
    state: DirectoryState
    access: ChannelAccess

    @property
    def bot_id(self) -> str | None:
        return self.identity()

    def guilds(self):
        """Return [(guild_id, guild_name)] for every guild the bot is in
        (restricted to bot.guild_ids when that config list is non-empty), or
        None when the list could not be fetched: a failure is not "in no
        guilds", and must not unmonitor everything."""
        try:
            out = [(g["id"], g.get("name") or g["id"]) for g in self.get("/users/@me/guilds")]
        except Exception as e:
            print(f"Guild discovery failed: {e}", flush=True)
            return None
        if self.allowed_guilds:
            out = [g for g in out if g[0] in self.allowed_guilds]
        return out

    def guild_base_perms(self, gid, gobj):
        """(base_permissions, member_role_ids, joined_at) for the bot in one
        guild. base_permissions is None (with no roles) when it cannot be
        determined (callers then treat send permission as unknown and rely on
        the send-time 403 safety net); joined_at is the bot's ISO join
        timestamp from the member object, or None when the fetch failed."""
        if not self.bot_id:
            return None, [], None
        try:
            member = self.get(f"/guilds/{gid}/members/{self.bot_id}")
        except Exception:
            return None, [], None
        joined_at = member.get("joined_at")
        roles = gobj.get("roles") or []
        if not roles:
            return None, [], joined_at
        member_roles = set(map(str, member.get("roles") or []))
        role_values = {}
        for role in roles:
            try:
                role_values[str(role["id"])] = int(role.get("permissions") or 0)
            except KeyError, TypeError, ValueError:
                continue
        member_roles.intersection_update(role_values)
        try:
            from muse_discord.discord_permissions import PermissionSnapshot

            perms = PermissionSnapshot(gid, role_values, gobj.get("owner_id")).guild_permissions(
                self.bot_id, member_roles
            )
        except KeyError, TypeError, ValueError:
            return None, [], joined_at
        return perms, member_roles, joined_at

    def channel_perms(self, gid, base_perms, overwrites, member_role_ids, member_id=None):
        from muse_discord.discord_permissions import PermissionSnapshot

        roles = dict.fromkeys(map(str, member_role_ids), 0)
        roles[str(gid)] = base_perms
        try:
            return PermissionSnapshot(gid, roles, None).channel_permissions(
                member_id or self.bot_id, member_role_ids, overwrites
            )
        except KeyError, TypeError, ValueError:
            return None

    def channels(self, previous=None):
        """Return ({channel_id: info}, {guild_id: guild_info}).

        Covers every guild text channel the bot can actually read right now,
        across every guild it belongs to. Read access is probed per channel
        (private channels answer 403 and are skipped quietly). Each channel
        info carries can_send: the bot's effective Send Messages permission
        (for threads, Send Messages in Threads on the parent channel),
        computed from guild roles + channel overwrites; None when the
        computation was unavailable.

        previous is what a refresh monitored so far (None at startup). Only
        a 403/404 means "no access": when the guild list, a guild's channel
        list, or one channel's probe fails transiently (or REST is backing
        off from a 429), the previous entries are kept as they were instead
        of unmonitoring those channels (no poll, no sweep) until the next
        refresh."""
        gen = self.channels_iter(previous)
        while True:
            try:
                next(gen)
            except StopIteration as e:
                return e.value

    def channels_iter(self, previous=None):
        """discover_channels as a generator that yields after each request,
        so the main loop can spread a refresh over many ticks."""
        previous = previous or {}
        found = {}
        guilds_out = {}
        guild_list = self.guilds()
        yield
        if guild_list is None:
            return dict(previous), dict(self.state.guilds)
        for gid, gname in guild_list:
            try:
                # with_counts: approximate_member_count, for /settings.
                gobj = self.get(f"/guilds/{gid}?with_counts=true")
            except Exception as e:
                print(f"Guild fetch failed for {gname}: {e}", flush=True)
                gobj = {}
            yield
            self.access.note_guild(gid, gobj)  # reminders and watches: role perms
            base_perms, member_roles, joined_at = self.guild_base_perms(gid, gobj)
            yield
            guilds_out[gid] = {
                "name": gname,
                "joined_at": joined_at,
            }
            count = gobj.get("approximate_member_count")
            if isinstance(count, int):
                self.state.member_counts[gid] = count
            try:
                channels = self.get(f"/guilds/{gid}/channels")
            except Exception as e:
                print(f"Channel list failed for {gname}: {e}", flush=True)
                found.update({c: i for c, i in previous.items() if i.get("guild_id") == gid})
                continue  # (its _GUILD_CHANNELS entry stays as it was)
            finally:
                yield
            # Every channel of the guild, categories included, for the
            # /settings channel and category pickers (threads are not
            # listed: they follow their parent).
            self.state.guild_channels[gid] = [
                {
                    "id": ch["id"],
                    "name": ch.get("name") or ch["id"],
                    "type": ch.get("type"),
                    "parent_id": ch.get("parent_id"),
                    "position": ch.get("position"),
                }
                for ch in channels
                if isinstance(ch, dict) and ch.get("id")
            ]
            names_by_id = {ch["id"]: ch.get("name", "?") for ch in channels}
            for ch in channels:
                if ch.get("parent_id"):
                    self.state.categories[ch["id"]] = ch["parent_id"]
                # Who else can read it (watches: the requester's view).
                self.access.overwrites[ch["id"]] = ch.get("permission_overwrites") or []
            probe = ChannelProbe(self, gid, gname, base_perms, member_roles, previous, found)
            perms_by_id = yield from probe.scan(channels)
            # Threads (forum posts, text-channel threads) never appear in the
            # guild channel list, so they need their own discovery or the bot
            # is blind inside every post.
            found.update(
                (
                    yield from self.threads_iter(
                        gid,
                        gname,
                        snapshot=ThreadSnapshot(
                            names=names_by_id,
                            known_ids=found,
                            permissions=perms_by_id,
                            previous=previous,
                        ),
                    )
                )
            )
        return found, guilds_out

    def threads_iter(self, gid, gname, snapshot):
        """{thread_id: info} for the active threads in one guild that the bot
        can read and whose ids aren't in known_ids yet, as a generator that
        yields after each request (see discover_channels_iter).

        Forum posts and threads never appear in /guilds/{id}/channels, so
        they come from the active-threads endpoint. The display name is
        parent-qualified ("help > <post title>"). Archived threads are
        skipped: a reply un-archives a thread, and the next refresh picks it
        up. A thread's send permission comes from its parent (Send Messages
        in Threads, or Send Messages; the send-time 403 net catches the
        rest): perms_by_id maps parent id -> permissions, and None (the
        thread-only refresh) leaves can_send unknown. previous: as in
        discover_channels, kept on transient failures."""
        names_by_id = snapshot.names
        known_ids = snapshot.known_ids
        perms_by_id = snapshot.permissions
        previous = snapshot.previous
        out = {}
        previous = previous or {}
        listed_at = time.time()
        try:
            resp = self.get(f"/guilds/{gid}/threads/active")
        except Exception as e:
            print(f"Thread list failed for {gname}: {e}", flush=True)
            return {
                t: i
                for t, i in previous.items()
                if i.get("guild_id") == gid and i.get("thread") and t not in known_ids
            }
        finally:
            yield
        since = self.state.threads_listed.get(gid, self.state.started_at) - NEW_THREAD_SLACK_SECS
        self.state.threads_listed[gid] = listed_at
        for t in resp.get("threads", []):
            tid = t["id"]
            if tid in known_ids or tid in out:
                continue
            if self.backing_off():
                if tid in previous:
                    out[tid] = previous[tid]
                continue
            try:
                self.get(f"/channels/{tid}/messages?limit=1")
            except Exception as e:
                if not _no_access(e) and tid in previous:
                    out[tid] = previous[tid]  # transient: keep it
                continue  # no read access; skip quietly
            finally:
                yield
            out[tid] = _thread_info(t, gid, gname, names_by_id, perms_by_id)
            if tid not in previous and _snowflake_ts(tid) >= since:
                self.state.new_threads.add(tid)
        return out

    def seed(self, cid, cname, state):
        """Mark a newly discovered channel's current messages as seen so its
        history is not treated as fresh mentions. Only a channel with no
        last_seen is seeded: one dropped and re-added by discovery, or an
        archived thread a new message brought back, keeps its watermark so
        the poll still reads what arrived in between. When the channel is
        empty or the read fails, the watermark is "now": the history stays
        seen and the next message is still new. A brand-new thread (see
        DirectoryState.new_threads) is seeded just below its own id instead, so the poll
        reads every message in it (a forum post's first message has the
        thread's id)."""
        brand_new = cid in self.state.new_threads
        self.state.new_threads.discard(cid)
        if cid in state["last_seen"]:
            return
        if brand_new and _is_snowflake(cid):
            state["last_seen"][cid] = str(int(cid) - 1)
            print(f"Tracking new thread #{cname} from its start.", flush=True)
            return
        try:
            msgs = self.get(f"/channels/{cid}/messages?limit=5")
        except Exception as e:
            print(f"Init error #{cname}: {e}", flush=True)
            msgs = None
        if msgs:
            newest = max(msgs, key=lambda m: int(m["id"]))
            state["last_seen"][cid] = newest["id"]
            print(f"Tracking new channel #{cname} from message {newest['id']}.", flush=True)
        else:
            state["last_seen"][cid] = now_snowflake()
            print(f"Tracking new channel #{cname} from now.", flush=True)
