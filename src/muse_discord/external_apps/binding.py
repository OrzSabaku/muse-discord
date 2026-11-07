"""Monitor-authored eligibility for original, text-only owner DMs."""

from .policy import LEVELS


def card(message, fragments, *, dm, owner, from_bot, level):
    direct = all(
        not m.get("attachments")
        and not m.get("message_reference")
        and not m.get("referenced_message")
        and not m.get("message_snapshots")
        and not m.get("embeds")
        for m in [message, *fragments]
    )
    eligible = bool(dm and owner and not from_bot and direct and level in LEVELS[1:])
    return {"eligible": eligible, "level": level if level in LEVELS else "off"}
