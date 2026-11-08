"""Owner membership checks and conservative server admission state.

Only the member query performs I/O. Restoring the mirror and testing admission
use supplied state/settings; failed lookups keep a newly joined server gated.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from muse_discord.muse_settings import Settings

OWNER_RECHECK_SECS = 3600
OWNER_CHECKS_PER_PASS = 5
OWNER_NOTICE_WAIT_FAILS = 3


def _owner_entry(e):
    if isinstance(e, dict):
        return {
            "in": bool(e.get("in")),
            "checked": bool(e.get("checked")),
            "next": float(e.get("next") or 0),
            "fails": int(e.get("fails") or 0),
        }
    return None


@dataclass
class OwnerPresence:
    owners_source: Callable[[], set[str]]
    settings_source: Callable[[], Settings]
    get: Callable[[str], Any]
    backing_off: Callable[[], bool]
    membership: dict = field(default_factory=dict)
    spoke: dict = field(default_factory=dict)

    @property
    def owners(self) -> set[str]:
        return self.owners_source()

    def restore(self, state):
        """Rebuild OWNER_IN_GUILD from state (at boot, and after a check)."""
        seen = state.setdefault("owner_in_guild", {})
        for gid in list(seen):
            e = _owner_entry(seen[gid])
            if e is None:
                del seen[gid]
            else:
                seen[gid] = e
        self.membership.clear()
        self.membership.update({g: (e["in"] if e["checked"] else None) for g, e in seen.items()})

    def member(self, gid):
        """Whether any owner is in the server; None when Discord could not
        say (checked again later)."""
        for uid in sorted(self.owners):
            try:
                self.get(f"/guilds/{gid}/members/{uid}")
                return True
            except Exception as e:
                if getattr(e, "code", None) in (403, 404):
                    continue  # not a member
                return None
        return False

    def joined(self, state, gid):
        """A join: the server is ownerless until a check says otherwise."""
        state.setdefault("owner_in_guild", {}).setdefault(
            gid, {"in": False, "checked": False, "next": 0, "fails": 0}
        )
        self.membership.setdefault(gid, None)

    def check(self, state, guilds, joined=(), every=False):
        """Check the servers due for an owner check (just joined, or still
        ownerless), oldest due first, a few per pass (every: all of them);
        then refresh the mirror."""
        self.restore(state)
        seen = state["owner_in_guild"]
        now = time.time()
        for gid in joined:
            self.joined(state, gid)
        for gid, e in seen.items():
            if self.spoke.get(gid):
                e.update({"in": True, "checked": True})
        self.spoke.clear()
        if self.owners and not self.backing_off():
            due = sorted(
                (e["next"], gid)
                for gid, e in seen.items()
                if not e["in"] and e["next"] <= now and gid in guilds
            )
            for _next, gid in due[: None if every else OWNER_CHECKS_PER_PASS]:
                if self.backing_off():
                    break  # the rest stay gated, and are checked later
                e = seen[gid]
                res = self.member(gid)
                if res is None:
                    e["fails"] += 1
                    e["next"] = now + min(60 * 2 ** e["fails"], OWNER_RECHECK_SECS)
                    continue
                e.update({"in": res, "checked": True, "fails": 0, "next": now + OWNER_RECHECK_SECS})
                if not res:
                    print(
                        f"No owner is in server {gid}: answering nobody "
                        f"there until an owner chooses When it answers "
                        f"for it.",
                        flush=True,
                    )
        self.restore(state)

    def absent(self, gid):
        """True when no owner is known to be in this server (checked and
        absent, or not checked yet since the bot joined) and no owner chose
        When it answers for it: then it answers nobody there."""
        if not gid or not self.owners or gid not in self.membership or self.membership[gid] is True:
            return False
        return not self.settings_source().owner_sets_server(gid, "who.replies")

    def pending(self, state, gid):
        """True while a just-joined server's owner check has not answered
        and has not failed often enough to give up waiting for it."""
        e = (state.get("owner_in_guild") or {}).get(gid)
        return (
            isinstance(e, dict)
            and not e.get("checked")
            and int(e.get("fails") or 0) < OWNER_NOTICE_WAIT_FAILS
        )
