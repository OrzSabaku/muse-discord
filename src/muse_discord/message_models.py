"""Message metadata shared by replies and proactive delivery; no runtime dependencies."""

from dataclasses import dataclass
from typing import Any


class SecretBlocked(Exception):
    """A send held back because it carried a secret (never retried)."""


DISCORD_MAX_CHARS = 2000  # Discord's hard per-message content limit


@dataclass
class DeliveryOptions:
    nonce: str | None = None
    mentions: dict | None = None


@dataclass
class Speaker:
    author_id: str | None = None
    from_bot: bool = False
    exchange: int = 0


@dataclass(frozen=True)
class MessageTurn:
    fragments: list[dict]
    burst_ids: list[str]
    chan_hist: list[dict]
    from_bot: bool
    bot_turns: int
    scope: Any
    here: dict
    exchange: int
    content: str


@dataclass(frozen=True)
class MessageMedia:
    media: list
    documents: list
    unviewable: list
    reply_ctx: dict | None


@dataclass(frozen=True)
class AuthorizedReply:
    fire: dict | None
    reply_to: str | int | None
    qid: str | None
    typing_keys: list[str]


@dataclass(frozen=True)
class ReplyActions:
    memory_of: str | None
    sched: tuple | None


@dataclass
class PreparedReply:
    reply_to: str | int | None
    parts: list[str]
    reactions: list[str]
    retry: dict
    gen_id: str | None
    approval: dict | None
    attachments: list
    gen_files: list
    mentions: dict | None = None


@dataclass
class LedgerContext:
    channel_id: str | None = None
    author: str | None = None
    recoveries: int | None = None
    author_id: str | None = None
    detail: dict | None = None


@dataclass
class OwnerCommand:
    channel_id: str | None = None
    content: str = ""
    guild_id: str | None = None


@dataclass
class QueueContext:
    """Origin and coalesced fragments of one incoming conversation turn."""

    dm: bool = False
    fragments: list | None = None
    guild_id: str | None = None
    follow_up: bool = False


@dataclass
class Settlement:
    status: str
    reason: str = ""
    replied: bool = False
    detail: dict | None = None


LEDGER_TERMINAL = ("replied", "quarantined", "skipped")


PROACTIVE_KIND = "proactive"


@dataclass
class MessagePlace:
    guild_id: str | None = None
    dm: bool = False
