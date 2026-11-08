"""Wait for a server install and report the bot's channel access."""

import time
from http import HTTPStatus

from muse_discord.bot_credentials import read_token
from muse_discord.setup import support as C


def channel_perms(base, everyone_id, member_roles, bot_id, overwrites):
    """Discord's permission overwrite rules, for one channel."""
    if base & C.ADMINISTRATOR:
        return -1
    p = base
    ow = {o.get("id"): o for o in overwrites or []}
    if everyone_id in ow:
        p &= ~int(ow[everyone_id].get("deny") or 0)
        p |= int(ow[everyone_id].get("allow") or 0)
    deny = allow = 0
    for rid in member_roles:
        if rid in ow:
            deny |= int(ow[rid].get("deny") or 0)
            allow |= int(ow[rid].get("allow") or 0)
    p = (p & ~deny) | allow
    if bot_id in ow:
        p &= ~int(ow[bot_id].get("deny") or 0)
        p |= int(ow[bot_id].get("allow") or 0)
    return p


def describe_guild(token, bot_id, g):
    gid = g["id"]
    roles = {
        r["id"]: int(r.get("permissions") or 0)
        for r in C.call("GET", f"/guilds/{gid}/roles", token) or []
    }
    member = C.call("GET", f"/guilds/{gid}/members/{bot_id}", token) or {}
    mine = list(member.get("roles") or [])
    base = roles.get(gid, 0)
    for rid in mine:
        base |= roles.get(rid, 0)
    read, post, read_only = 0, 0, []
    for ch in C.call("GET", f"/guilds/{gid}/channels", token) or []:
        if ch.get("type") not in (0, 5, 15):  # text, announcement, forum
            continue
        p = channel_perms(base, gid, mine, bot_id, ch.get("permission_overwrites"))
        if p != -1 and not p & C.VIEW_CHANNEL:
            continue
        read += 1
        if p == -1 or p & C.SEND_MESSAGES:
            post += 1
        else:
            read_only.append("#" + str(ch.get("name")))
    return {
        "id": gid,
        "name": g.get("name"),
        "can_read": read,
        "can_post": post,
        "read_only": read_only,
    }


def wait(args, rep):
    MS = C.settings_module()
    try:
        token = read_token(C.load_settings(args.config).token_sources(), C.PKG_DIR)
    except MS.SettingsError as e:
        return rep.fail("config", f"config.yaml: {e}", 6)
    if not token:
        return rep.fail("no_token", "No saved token yet: run this script with the token first.", 2)
    deadline = time.time() + args.wait
    try:
        bot_id = C.call("GET", "/users/@me", token)["id"]
        while True:
            guilds = C.call("GET", "/users/@me/guilds", token) or []
            if guilds or time.time() >= deadline:
                break
            time.sleep(args.poll)
        if not guilds:
            return rep.fail(
                "timeout",
                "The bot hasn't joined a server yet. "
                "Ask the owner to open the invite link, pick "
                "their server and click Authorize.",
                5,
            )
        found = [describe_guild(token, bot_id, g) for g in guilds[:10]]
    except C.ApiError as e:
        code = 2 if e.status == HTTPStatus.UNAUTHORIZED else 3
        return rep.fail(
            "rejected" if e.status == HTTPStatus.UNAUTHORIZED else "unreachable",
            f"Discord: {e}",
            code,
        )
    rep.data["servers"] = found
    for g in found:
        if not g["can_read"]:
            rep.line(
                f"PROBLEM: The bot is in {g['name']} but can't see "
                f"any channels there. In Discord: Server Settings, "
                f"Roles, the bot's role: switch on View Channels and "
                f"Send Messages."
            )
            continue
        text = (
            f"OK: The bot is in {g['name']}. It can read "
            f"{g['can_read']} channels and post in {g['can_post']} of "
            f"them."
        )
        if g["read_only"]:
            text += " It can read but not post in " + ", ".join(g["read_only"][:10]) + "."
        rep.line(text)
    rep.finish()
    return 0 if any(g["can_post"] for g in found) else 4
