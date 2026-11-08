"""Admission to server conversations and DMs using live settings and ownership.

Shared-server membership has its own bounded-time cache. Trigger classification
and DM audience checks share ResponsePolicy; file loading and message transport
remain outside this component.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from muse_discord import muse_settings as MS
from muse_discord.conversation import _who
from muse_discord.directory.scopes import DirectoryScopes
from muse_discord.owner.presence import OwnerPresence
from muse_discord.response_policy import ResponsePolicy


@dataclass
class SharedServers:
    get: Callable[[str], Any]
    backing_off: Callable[[], bool]
    guild_ids: Callable[[], list[str]]
    owners_absent: Callable[[str], bool]
    now: Callable[[], float] = time.time
    _cache: dict = field(default_factory=dict, init=False, repr=False)

    def clear(self):
        self._cache.clear()

    def contains(self, user_id):
        user_id = str(user_id)
        hit = self._cache.get(user_id)
        now = self.now()
        if hit and now - hit[1] < (3600 if hit[0] else 600):
            return hit[0]
        if self.backing_off():
            return bool(hit and hit[0])
        for gid in self.guild_ids():
            if self.owners_absent(gid):
                continue  # a server no owner is in shares nothing
            try:
                self.get(f"/guilds/{gid}/members/{user_id}")
            except Exception as e:
                if getattr(e, "code", None) in (403, 404):
                    continue  # not a member there
                return bool(hit and hit[0])  # transient: decide later
            self._cache[user_id] = (True, now)
            return True
        self._cache[user_id] = (False, now)
        return False


@dataclass
class AdmissionPolicy:
    response: ResponsePolicy
    presence: OwnerPresence
    directory: DirectoryScopes
    shared_servers: SharedServers

    def _dm_problem(self, uid, res):
        policy = res["who.dms_from"]
        if policy == "owners":
            return "who.dms_from is owners"
        if isinstance(policy, list) and uid not in policy:
            return "not in who.dms_from"
        if self.response.people_match(res["who.ignore"], uid):
            return "on who.ignore"
        if not self.response.people_match(res["who.allow"], uid):
            return "not in who.allow"
        if policy == "shared-servers" and not self.shared_servers.contains(uid):
            return "shares no server with the bot"
        return None

    def dm_refusal(self, author):
        """Why a DM from author is not read and answered, or None when it is:
        who.replies (off = no DMs at all), who.dms_from (owners |
        shared-servers | anyone | a list of user ids: the owners plus those
        people), who.bots, and who.allow / who.ignore at the DM scope."""
        res = self.response.settings().resolve(MS.Scope.dm())
        if res["who.replies"] == "off":
            return "who.replies is off for DMs"
        uid = str((author or {}).get("id") or "")
        bot = self.response.is_bot_author(author)
        if uid in self.response.owners and not bot:
            return None
        if bot and res["who.bots"] == "ignore":
            return "who.bots is ignore"
        return self._dm_problem(uid, res)

    def dm_sender_allowed(self, author):
        """Whether a DM from author is read and answered (see dm_refusal)."""
        return self.dm_refusal(author) is None

    def dms_enabled(self):
        return self.response.settings().get("who.replies", MS.Scope.dm()) != "off"

    def dm_listed_people(self):
        """The people who.dms_from names (a list of user ids), else []."""
        v = self.response.settings().get("who.dms_from", MS.Scope.dm())
        return [str(u) for u in v] if isinstance(v, list) else []

    def dm_poll_user_ids(self):
        if not self.dms_enabled():
            return []
        return sorted(set(self.response.owners) | set(self.dm_listed_people()))

    def dm_swept_author(self, uid):
        """Whose DMs the poll and the sweep cover (and so whose refused DMs
        are ledgered): the owners and the people who.dms_from names."""
        return str(uid) in self.response.owners or str(uid) in self.dm_listed_people()

    def _guild_problem(self, author, gid, here):
        replies = here["who.replies"]
        owner = str(author.get("id") or "") in self.response.owners
        if replies == "off":
            return "who.replies is off here"
        if not owner and self.presence.absent(gid):
            return "none of the owners is in this server"
        if self.response.is_bot_author(author) and here["who.bots"] == "ignore":
            return "who.bots: ignore"
        return None

    def trigger_verdict(self, msg, guild_id=None, state=None):
        """Return (kind, refusal) under this message's effective settings.

        Kind is mention for direct address, follow_up for conversational
        participation, or None. Refusal explains declined direct address;
        ordinary chatter gets no refusal diagnostic.
        """
        author = _who(msg)
        if author.get("id") == self.response.conversation.bot_id:
            return None, None
        gid = (
            guild_id
            or msg.get("guild_id")
            or (self.directory.channels.get(msg.get("channel_id")) or {}).get("guild_id")
        )
        here = self.response.settings().resolve(self.response.scope_for(gid, msg.get("channel_id")))
        replies = here["who.replies"]
        direct = self.response.conversation.addresses_bot(msg)
        owner = str(author.get("id") or "") in self.response.owners
        if owner:
            self.presence.note_author(gid)
        refusal = self._guild_problem(author, gid, here)
        if refusal:
            return None, refusal if direct else None
        freely = replies == "freely" and not self.response.is_bot_author(author)
        if (
            not direct
            and not freely
            and not (
                state is not None
                and replies == "conversational"
                and self.response.conversation.follow_up_open(state, msg)
            )
        ):
            return None, None
        if not self.response.who_allows(msg, gid, here):
            return None, ("not allowed by who.allow / who.ignore" if direct else None)
        return ("mention" if direct else "follow_up"), None
