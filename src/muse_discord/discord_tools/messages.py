"""Message presentation and splitting, with no network or settings imports."""

import re
from datetime import UTC, datetime

BMP_MAX_CODEPOINT = 0xFFFF
MAX_FENCE_LANGUAGE_LENGTH = 80


def readable_content(text, mentions):
    users = {
        str(u["id"]): u.get("global_name") or u.get("username", "?")
        for u in mentions
        if u.get("id")
    }
    text = re.sub(r"<@!?(\d+)>", lambda m: "@" + users[m[1]] if m[1] in users else m[0], text)
    text = re.sub(r"<a?:([^:>\s]+):\d+>", r":\1:", text)

    def timestamp(match):
        try:
            return datetime.fromtimestamp(int(match[1]), UTC).isoformat()
        except ValueError, OverflowError, OSError:
            return match[0]

    return re.sub(r"<t:(-?\d+)(?::[tTdDfFR])?>", timestamp, text)


def format_message(message):
    author = message.get("author") or {}
    text = message.get("content") or ""
    result = {
        "id": message["id"],
        "author_id": author.get("id"),
        "author_name": author.get("global_name") or author.get("username", "?"),
        "author_is_bot": bool(author.get("bot")),
        "content": text,
        "content_readable": readable_content(text, message.get("mentions") or []),
        "timestamp": message.get("timestamp"),
        "attachments": [
            {k: a[k] for k in ("id", "filename", "content_type", "size") if k in a}
            for a in message.get("attachments") or []
        ],
    }
    ref = message.get("message_reference") or {}
    if ref.get("message_id"):
        result["reply_to_message_id"] = ref["message_id"]
        original = message.get("referenced_message")
        result["reply_to"] = (
            {
                "author_id": (original.get("author") or {}).get("id"),
                "content": (original.get("content") or "")[:200],
            }
            if original
            else {"deleted": True}
        )
    return result


def split_message(text, limit=2000, max_chunks=10):
    """Split at line/word boundaries; close and reopen fenced code blocks.

    Discord counts UTF-16 code units, so astral characters use two units.
    Reject oversized output before sending even its first part.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Message content must not be empty.")
    if len(text.encode("utf-16-le")) // 2 <= limit:
        return [text]
    chunks, fence, prefix = [], None, ""
    remaining = text
    while remaining:
        # Reserve a newline and closing fence even when this slice opens one.
        budget = limit - len(prefix.encode("utf-16-le")) // 2 - 4
        end = _split_boundary(remaining, budget)
        part, remaining = remaining[:end], remaining[end:]
        for line in part.splitlines():
            if line.startswith("```"):
                fence = None if fence is not None else line[3:].strip()
        chunk = prefix + part
        if fence is not None:
            chunk += "\n```"
            # Limit a user-supplied language tag so reopening always fits.
            if len(fence) > MAX_FENCE_LANGUAGE_LENGTH:
                raise ValueError("Code fence language tag is too long.")
            prefix = "```" + fence + "\n"
        else:
            prefix = ""
        chunks.append(chunk)
        if len(chunks) > max_chunks:
            raise ValueError(f"Message requires more than {max_chunks} parts.")
    return chunks


def _split_boundary(remaining, budget):
    used, end = 0, 0
    for i, char in enumerate(remaining):
        width = 2 if ord(char) > BMP_MAX_CODEPOINT else 1
        if used + width > budget:
            break
        used += width
        end = i + 1
    if not end:
        raise ValueError("Code fence leaves no room for content.")
    if end < len(remaining):
        boundary = remaining.rfind("\n", 0, end + 1)
        if boundary < end // 2:
            boundary = remaining.rfind(" ", 0, end + 1)
        if boundary >= end // 2:
            end = boundary + 1
    return end
