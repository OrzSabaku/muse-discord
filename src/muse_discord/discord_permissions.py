"""Permission calculations on immutable REST/Gateway snapshots, via discord.py.

No network calls or Gateway cache assumptions: discovery and member lookups
remain in the monitor. Only the SDK model construction is adapted here.
"""

from functools import lru_cache

import discord

if not (2, 7, 1) <= discord.version_info[:3] < (2, 8, 0):
    raise ImportError("discord.py 2.7.x (>=2.7.1) is required; install requirements.txt")

# A model-only state: no login, HTTP session, background task or member cache.
_STATE = discord.Client(
    intents=discord.Intents.none(), member_cache_flags=discord.MemberCacheFlags.none()
)._connection


@lru_cache(maxsize=128)
def _guild(gid, owner_id, roles):
    return discord.Guild(
        state=_STATE,
        data={
            "id": gid,
            "owner_id": owner_id or None,
            "roles": [
                {"id": rid, "name": "", "permissions": str(bits)} for rid, bits in roles
            ],
        },
    )


def _member(gid, role_values, owner_id, uid, role_ids):
    guild = _guild(str(gid), str(owner_id or ""), tuple(sorted(role_values.items())))
    member = (
        discord.Member(
            guild=guild,
            state=_STATE,
            data={
                "user": {
                    "id": str(uid),
                    "username": "",
                    "discriminator": "0",
                    "avatar": None,
                },
                "roles": [rid for rid in role_ids if str(rid) in role_values],
                "flags": 0,
            },
        )
        if uid is not None
        else guild.default_role
    )
    return guild, member


def guild_permissions(gid, roles, owner_id, uid, role_ids):
    _, member = _member(gid, roles, owner_id, uid, role_ids)
    return member.guild_permissions.value


def channel_permissions(gid, roles, owner_id, uid, role_ids, overwrites):
    guild, member = _member(gid, roles, owner_id, uid, role_ids)
    channel = discord.TextChannel(
        guild=guild,
        state=_STATE,
        data={
            "id": "1",
            "type": 0,
            "name": "",
            "position": 0,
            "permission_overwrites": overwrites or [],
        },
    )
    # The monitor uses a thread's parent snapshot and separately checks
    # Send in Threads and private-thread membership.
    return channel.permissions_for(member).value if member is not None else 0
