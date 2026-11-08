"""Monitor-authored eligibility for original, text-only owner DMs."""

from dataclasses import dataclass

from .policy import LEVELS


@dataclass
class DMOrigin:
    dm: bool
    owner: bool
    from_bot: bool


def card(message, fragments, *, origin, level):
    dm = origin.dm
    owner = origin.owner
    from_bot = origin.from_bot
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
