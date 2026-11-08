"""Fragment pacing and deterministic nonces, independent of a Discord client."""

import hashlib
import json
import random
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, cast

from muse_discord.message_models import DeliveryOptions


class ReplySink(Protocol):
    guard: Callable[[dict, str], dict]

    def reply(self, channel_id, text, reply_to=None, components=None, delivery=None) -> Any: ...

    def files(self, channel_id, text, files, reply_to=None, delivery=None) -> Any: ...


def send_nonce(channel_id, reply_to, parts, index):
    """Idempotency key for one message part. A POST can fail on our side
    (timeout, dropped connection, 5xx) after Discord already created the
    message; with enforce_nonce, Discord answers a repeat of the same
    nonce from the same bot within a few minutes with the message it
    already made instead of posting it again. Derived from the reply
    itself, so retries and even a second monitor process agree on it.
    Discord caps nonces at 25 characters."""
    key = json.dumps([channel_id, reply_to, parts, index])
    return hashlib.sha256(key.encode()).hexdigest()[:25]


class SendProgress(Protocol):
    """Metadata retained on the original exception after a partial send."""

    failed_part: int
    sent_ids: list[str]


@dataclass
class PartDelivery:
    sender: ReplySink
    typing: Callable[[str], None]
    sleep: Callable[[float], None]

    def send(self, channel_id, parts, reply_to=None, attachments=(), delivery=None):
        """Send a reply as sequential messages, like a person thinking aloud.

        The first part is threaded under reply_to; follow-ups go out as plain
        messages with a typing beat between them. Attachments ride on the last
        part. Raises on the first failure; the caller retries the whole
        reply, and each part's nonce makes Discord return the parts that
        already landed instead of posting them again. mentions: what the
        parts may ping (default SEND_ALLOWED_MENTIONS); nonce_key: what the
        nonces derive from besides the text, when not reply_to (a heartbeat
        slot's id: its posts reply to nothing). Returns the sent messages'
        ids. The error raised says which part failed (failed_part) and the
        ids of the parts sent before it (sent_ids).
        """
        # Every part checked before the first goes out: a secret in part 3
        # never leaves parts 1 and 2 half-sent.
        delivery = delivery or DeliveryOptions()
        mentions = delivery.mentions
        nonce_key = delivery.nonce
        self.sender.guard({"content": list(parts)}, channel_id)  # and joined
        last_idx = len(parts) - 1
        sent = []
        for i, part in enumerate(parts):
            if i > 0:
                self.typing(channel_id)
                self.sleep(random.uniform(2.0, 4.0))
            nonce = send_nonce(channel_id, nonce_key or reply_to, parts, i)
            part_delivery = DeliveryOptions(nonce, mentions)
            try:
                if i == last_idx and attachments:
                    got = self.sender.files(
                        channel_id,
                        part,
                        attachments,
                        reply_to if i == 0 else None,
                        delivery=part_delivery,
                    )
                elif i == 0:
                    got = self.sender.reply(channel_id, part, reply_to, delivery=part_delivery)
                else:
                    got = self.sender.reply(channel_id, part, delivery=part_delivery)
            except Exception as e:
                try:
                    progress = cast(SendProgress, e)
                    progress.failed_part = i
                    progress.sent_ids = list(sent)
                except Exception:
                    pass
                raise
            if isinstance(got, dict) and got.get("id"):
                sent.append(str(got["id"]))
        return sent
