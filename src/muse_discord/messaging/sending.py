"""Guarded Discord text, attachment and reaction delivery.

JSON posts, multipart uploads and PUT requests are injected transport operations.
File names and bytes are checked before any upload reaches that transport.
"""

import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from muse_discord.message_models import DeliveryOptions

SEND_ALLOWED_MENTIONS = {"parse": ["users"], "replied_user": True}


def reply_reference(message_id):
    """The message_reference for a reply. fail_if_not_exists: false makes
    Discord post it as a plain message when the message replied to was
    deleted meanwhile, instead of answering 400 on every retry."""
    return {"message_id": message_id, "fail_if_not_exists": False}


@dataclass
class MessageSender:
    guard: Callable[[dict, str], dict]
    post: Callable[[str, dict], Any]
    upload: Callable[[str, dict, list], Any]
    put: Callable[[str], Any]

    def reply(self, channel_id, text, reply_to=None, components=None, delivery=None):
        delivery = delivery or DeliveryOptions()
        nonce = delivery.nonce
        mentions = delivery.mentions
        text = self.guard({"content": text}, channel_id)["content"]
        data = {"content": text, "allowed_mentions": mentions or SEND_ALLOWED_MENTIONS}
        if components:
            data["components"] = components
        if reply_to:
            data["message_reference"] = reply_reference(reply_to)
        if nonce:
            data["nonce"] = nonce
            data["enforce_nonce"] = True
        return self.post(f"/channels/{channel_id}/messages", data)

    def files(self, channel_id, text, files, reply_to=None, delivery=None):
        """Send a message with file attachments via multipart/form-data.
        files: list of (local_path, filename)."""
        delivery = delivery or DeliveryOptions()
        nonce = delivery.nonce
        mentions = delivery.mentions
        self.guard({"content": text, "files": [name for _, name in files]}, channel_id)
        blobs = []
        for path, name in files:
            with open(path, "rb") as f:
                blobs.append((name, f.read()))
        self.guard(
            {
                "content": text,
                "data": [blob.decode("latin-1") for _, blob in blobs],
                "files": [name for name, _ in blobs],
            },
            channel_id,
        )
        payload = {"content": text, "allowed_mentions": mentions or SEND_ALLOWED_MENTIONS}
        if reply_to:
            payload["message_reference"] = reply_reference(reply_to)
        if nonce:
            payload.update(nonce=nonce, enforce_nonce=True)
        return self.upload(f"/channels/{channel_id}/messages", payload, blobs)

    def reaction(self, channel_id, message_id, emoji):
        """Add a Unicode emoji reaction to a message (PUT .../@me). Used for
        reactions drafted by the worker via the outbox 'reactions' list."""
        encoded = urllib.parse.quote(emoji, safe="")  # "/" stays in the segment
        try:
            self.guard({"content": emoji}, channel_id)
            self.put(f"/channels/{channel_id}/messages/{message_id}/reactions/{encoded}/@me")
            return True
        except Exception as e:
            print(f"Reaction {emoji!r} failed on {message_id}: {e}", flush=True)
            return False
