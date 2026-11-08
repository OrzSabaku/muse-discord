"""Explicit diagnostic follow-up: notify owners or wait for their next mention.

The CLI decides whether to invoke these operations. Importing diagnostic services
neither reads credentials nor sends messages.
"""

import time

from muse_discord.diagnostics.context import read_ledger
from muse_discord.setup import support as C

CANNOT_DM_DISCORD_CODE = 50007


ONLINE_DM = (
    "Hi! I'm online and ready. Mention me in any channel I can "
    "post in and I'll answer. You don't need to reply to this "
    "message.\n\n" + C.COMMUNITY_LINE
)


REASONS = {
    "no-send": (
        "It can't post in that channel.",
        "give the bot Send Messages there, or mention it in another channel.",
    ),
    "replies-off": (
        "Replies are off there.",
        'DM the bot "answer when I mention you", or use /settings.',
    ),
}


def online_dm(d, st, token):
    for uid in st.owners:
        try:
            ch = C.call("POST", "/users/@me/channels", token, {"recipient_id": str(uid)})
            C.call(
                "POST",
                f"/channels/{ch['id']}/messages",
                token,
                {"content": ONLINE_DM, "allowed_mentions": {"parse": []}},
            )
            d.add("ok", f'Sent owner {uid} the "I\'m online" DM.')
        except (C.ApiError, KeyError, TypeError) as e:
            code = getattr(e, "code", None)
            if code == CANNOT_DM_DISCORD_CODE:
                d.add(
                    "problem",
                    f"Couldn't DM owner {uid}: their privacy "
                    f"settings block DMs from this server's members.",
                    "ask them: in Discord, right-click the server icon, "
                    "Privacy Settings, switch on Direct Messages.",
                )
            else:
                d.add(
                    "problem",
                    f"Couldn't DM owner {uid} ({e}).",
                    "make sure the owner is in a server with the bot.",
                )


def wait_mention(d, st, secs):
    start = time.time()
    owners = {str(o) for o in st.owners}
    deadline = start + secs
    seen_status = None
    while time.time() < deadline:
        mine = [
            (e.get("updated", 0), mid, e)
            for mid, e in read_ledger(d).items()
            if str(e.get("author_id")) in owners
            and e.get("updated", 0) >= start
            and not e.get("dm")
        ]
        if mine:
            _, _mid, e = max(mine)
            status = e.get("status")
            if status == "replied":
                d.add("ok", "The bot answered the owner's mention.")
                return
            if status in ("skipped", "quarantined", "enqueue_failed"):
                what, fix = REASONS.get(
                    e.get("code"),
                    (f"It didn't answer ({e.get('reason') or status}).", "see docs/UPDATING.md."),
                )
                d.add("problem", what, fix)
                return
            seen_status = status
        time.sleep(2)
    if seen_status:
        d.add(
            "problem",
            "The bot saw the mention but hasn't answered yet.",
            "check that the wake hook is registered with the worker prompt (docs/SETUP.md).",
        )
    else:
        d.add(
            "problem",
            "No mention from the owner reached the bot.",
            "make sure they typed @ and picked the bot from the list, in a channel it can read.",
        )
