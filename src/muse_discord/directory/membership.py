"""Best-effort author role cache for message audience rules.

Uses the last known roles on REST backoff/failure. Channel permission checks use
ChannelAccess instead, whose unknown/error handling is deliberately stricter.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

MEMBER_ROLES_TTL_SECS = 600
MEMBER_CACHE_LIMIT = 2000


@dataclass
class AuthorRoles:
    get: Callable[[str], Any]
    backing_off: Callable[[], bool]
    cache: dict = field(default_factory=dict)

    def get_roles(self, guild_id, user_id):
        if not guild_id or not user_id:
            return set()
        key = (str(guild_id), str(user_id))
        hit = self.cache.get(key)
        if hit and time.time() - hit[1] < MEMBER_ROLES_TTL_SECS:
            return hit[0]
        if self.backing_off():
            return hit[0] if hit else set()
        try:
            roles = set(
                (self.get(f"/guilds/{guild_id}/members/{user_id}") or {}).get("roles") or []
            )
        except Exception:
            roles = hit[0] if hit else set()
        self.cache[key] = (roles, time.time())
        if len(self.cache) > MEMBER_CACHE_LIMIT:
            for k in sorted(self.cache, key=lambda k: self.cache[k][1])[:500]:
                del self.cache[k]
        return roles
