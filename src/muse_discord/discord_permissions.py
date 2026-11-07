"""Permission calculations on immutable REST/Gateway snapshots, via discord.py.

No network calls or Gateway cache assumptions: discovery and member lookups
remain in the monitor. Only the SDK model construction is adapted here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from functools import lru_cache
from typing import TYPE_CHECKING, cast

import discord

if TYPE_CHECKING:
    from discord.types.guild import Guild as GuildPayload
    from discord.types.member import MemberWithUser
    from discord.types.channel import TextChannel as TextChannelPayload, PermissionOverwrite

if not (2, 7, 1) <= discord.version_info[:3] < (2, 8, 0):
    raise ImportError("discord.py 2.7.x (>=2.7.1) is required; run uv sync")

# A model-only state: no login, HTTP session, background task or member cache.
_STATE = discord.Client(
    intents=discord.Intents.none(), member_cache_flags=discord.MemberCacheFlags.none()
)._connection


@lru_cache(maxsize=128)
def _guild(gid: str, owner_id: str, roles: tuple[tuple[str, int], ...]) -> discord.Guild:
    return discord.Guild(
        state=_STATE,
        # These SDK constructors consume only the permission snapshot fields.
        # The isolated casts document that this is not a full Gateway payload.
        data=cast(
            "GuildPayload",
            {
                "id": gid,
                "owner_id": owner_id or None,
                "roles": [{"id": rid, "name": "", "permissions": str(bits)} for rid, bits in roles],
            },
        ),
    )


def _member(
    guild: discord.Guild, role_values: Mapping[str, int], uid: str, role_ids: Iterable[str]
) -> discord.Member:
    return discord.Member(
        guild=guild,
        state=_STATE,
        data=cast(
            "MemberWithUser",
            {
                "user": {"id": str(uid), "username": "", "discriminator": "0", "avatar": None},
                "roles": [rid for rid in role_ids if str(rid) in role_values],
                "flags": 0,
            },
        ),
    )


def guild_permissions(
    gid: str, roles: Mapping[str, int], owner_id: str | None, uid: str, role_ids: Iterable[str]
) -> int:
    guild = _guild(str(gid), str(owner_id or ""), tuple(sorted(roles.items())))
    return _member(guild, roles, uid, role_ids).guild_permissions.value


def channel_permissions(
    gid: str,
    roles: Mapping[str, int],
    owner_id: str | None,
    uid: str | None,
    role_ids: Iterable[str],
    overwrites: Sequence[PermissionOverwrite] | None,
) -> int:
    guild = _guild(str(gid), str(owner_id or ""), tuple(sorted(roles.items())))
    member = _member(guild, roles, uid, role_ids) if uid is not None else guild.default_role
    channel = discord.TextChannel(
        guild=guild,
        state=_STATE,
        data=cast(
            "TextChannelPayload",
            {
                "id": "1",
                "type": 0,
                "name": "",
                "position": 0,
                "permission_overwrites": overwrites or [],
            },
        ),
    )
    # The monitor uses a thread's parent snapshot and separately checks
    # Send in Threads and private-thread membership.
    return channel.permissions_for(member).value if member is not None else 0
