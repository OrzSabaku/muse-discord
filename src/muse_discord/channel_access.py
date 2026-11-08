"""Channel audiences from permission snapshots and bounded member lookups.

Discovery and Gateway events supply the guild/overwrite registries. REST access
is injected; uncertain permissions fail closed when deciding who can view a
watch match. This service has no monitor or settings dependency.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any

PERM_VIEW_CHANNEL = 1 << 10
WATCH_MEMBER_TTL_SECS = 120
WATCH_MEMBER_FAIL_SECS = 60
MEMBER_CACHE_LIMIT = 2000


@dataclass
class ChannelAccess:
    channels: dict
    get: Callable[[str], Any]
    backing_off: Callable[[], bool]
    guilds: dict = field(default_factory=dict)
    overwrites: dict = field(default_factory=dict)
    members: dict = field(default_factory=dict)

    def note_guild(self, gid, gobj):
        """Discovery: a guild's role permissions and owner."""
        roles = (gobj or {}).get("roles")
        if not roles:
            return
        perms = {}
        for r in roles:
            try:
                perms[str(r.get("id"))] = int(r.get("permissions") or 0)
            except TypeError, ValueError:
                perms[str(r.get("id"))] = 0
        self.guilds[gid] = {"roles": perms, "owner_id": str(gobj.get("owner_id") or "")}

    def member_roles(self, gid, uid):
        """uid's role ids in guild gid, or None when they are not a member
        (404). Raises when it cannot tell (a failure is remembered for
        WATCH_MEMBER_FAIL_SECS, so a flood of checks makes no requests)."""
        key = (str(gid), str(uid))
        hit = self.members.get(key)
        now = time.time()
        if hit and hit[0] == "error" and now - hit[1] < WATCH_MEMBER_FAIL_SECS:
            raise RuntimeError("member lookup failed recently")
        if hit and hit[0] != "error" and now - hit[1] < WATCH_MEMBER_TTL_SECS:
            return hit[0]
        if self.backing_off():
            raise RuntimeError("REST is backing off")
        try:
            m = self.get(f"/guilds/{gid}/members/{uid}")
            if not isinstance(m, dict) or not isinstance(m.get("roles"), list):
                raise RuntimeError("member lookup returned no role information")
            roles = {str(r) for r in m["roles"]}
        except Exception as e:
            if getattr(e, "code", None) != HTTPStatus.NOT_FOUND:
                self.members[key] = ("error", now)
                raise
            roles = None
        self.members[key] = (roles, time.time())
        if len(self.members) > MEMBER_CACHE_LIMIT:
            for k in sorted(self.members, key=lambda k: self.members[k][1])[:500]:
                del self.members[k]
        return roles

    def channel(self, gid, cid):
        """(the channel whose overwrites apply: a thread's parent, private
        thread?), or None when discovery does not know it."""
        info = self.channels.get(cid) or {}
        if info.get("guild_id") != gid:
            return None
        chan = info.get("parent_id") if info.get("thread") else cid
        if chan not in self.overwrites:
            return None
        return chan, bool(info.get("private"))

    def permission_event(self, ev):
        """Gateway CHANNEL_* / GUILD_ROLE_* events keep the permission data
        watches use current between discoveries."""
        t, d = ev.get("t"), ev.get("d") or {}
        try:
            if t.startswith("CHANNEL_"):
                if d.get("id") and d.get("guild_id"):
                    self.overwrites[str(d["id"])] = d.get("permission_overwrites") or []
                return
            gid = d.get("id") if t == "GUILD_UPDATE" else d.get("guild_id")
            g = self.guilds.get(str(gid))
            if g is None:
                return
            if t == "GUILD_UPDATE":
                if "owner_id" in d:
                    g["owner_id"] = str(d["owner_id"] or "")
            elif t == "GUILD_ROLE_DELETE":
                g["roles"].pop(str(d.get("role_id")), None)
            else:
                role = d.get("role") or {}
                g["roles"][str(role.get("id"))] = int(role.get("permissions") or 0)
        except (TypeError, ValueError, AttributeError) as e:
            print(f"Permission event {t} not applied: {e}", flush=True)

    def permissions(self, gid, uid, cid):
        """uid's permissions in channel cid of guild gid, from the guild's
        roles and the channel's (a thread: its parent's) overwrites as
        discovery last saw them, and uid's roles: 0 when uid is not a member,
        and in a private thread (members only, so never matched); None when
        it cannot be told."""
        g = self.guilds.get(gid)
        ch = self.channel(gid, cid)
        if not g or ch is None:
            return None
        chan, private = ch
        try:
            roles = self.member_roles(gid, uid)
        except Exception:
            return None
        if roles is None or private:
            return 0
        try:
            from muse_discord.discord_permissions import PermissionSnapshot

            return PermissionSnapshot(gid, g["roles"], g["owner_id"]).channel_permissions(
                str(uid), roles, self.overwrites[chan]
            )
        except KeyError, TypeError, ValueError:
            return None

    def can_view(self, gid, uid, cid):
        """uid can read channel cid now (fails closed)."""
        p = self.permissions(gid, uid, cid)
        return bool(p and p & PERM_VIEW_CHANNEL)

    def is_public(self, gid, cid):
        """Everyone in the server can read cid: @everyone may view it, and
        no role or member overwrite there denies View Channel (permissions
        only add up, so then nobody is shut out, and any channel's audience
        is a subset of its). Fails closed."""
        g = self.guilds.get(gid)
        ch = self.channel(gid, cid)
        if not g or ch is None or ch[1]:
            return False
        ows = self.overwrites[ch[0]]
        for ow in ows or []:
            try:
                denies = int(ow.get("deny") or 0) & PERM_VIEW_CHANNEL
            except TypeError, ValueError:
                return False
            if denies and ow.get("id") != gid:
                return False
        try:
            from muse_discord.discord_permissions import PermissionSnapshot

            p = PermissionSnapshot(gid, g["roles"], g["owner_id"]).channel_permissions(
                None, (), ows
            )
        except KeyError, TypeError, ValueError:
            return False
        return bool(p & PERM_VIEW_CHANNEL)
