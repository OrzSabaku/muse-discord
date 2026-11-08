"""Conservative incoming-text screening and durable quarantine of whole bursts."""

import contextlib
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from muse_discord.message_ledger import MessageLedger
from muse_discord.message_models import LedgerContext
from muse_discord.storage import atomic_write, read_json

# Prompt-injection / exfiltration patterns. A match means the message is
# quarantined for the owner's review instead of queued for auto-reply.
# Deliberately conservative: false positives just skip one auto-reply.
INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|prior|above|earlier)\s+instructions",
    r"disregard\s+(all\s+)?(previous|prior|above)?\s*instructions",
    r"forget\s+(all\s+)?(previous|prior|above)?\s*instructions",
    r"\byour\s+system\s*prompt\b",
    r"(reveal|share|send|show|tell|give|leak|paste|print|post|drop|output).{0,40}\bsystem\s*prompt\b",
    r"\byou\s+are\s+now\b",
    r"\breveal\b.{0,50}\b(instructions|prompt|secrets?)\b",
    r"\bjailbreak\b",
    r"\bdeveloper\s+mode\b",
    r"pretend\s+(you'?re|you\s+are|to\s+be)\b",
    r"act\s+as\s+(a\s+)?(different|new|another)\b",
    r"output\s+(your|the)\s+(initial|original|system)\s+(prompt|instructions)",
    r"(read|cat|open|print)\s+~?/",
    r"MEMORY\.md",
    r"\.env\b",
    # Credential terms need solicitation context: the bare term
    # "api key" shows up constantly in legitimate dev talk, so it only
    # matches with a request verb aimed at the key, "your ... api key",
    # or an assignment.
    r"(reveal|share|send|show|tell|give|leak|paste|print|post|drop|output).{0,40}\bapi[\s_-]?key\b",
    r"\byour\b.{0,25}\bapi[\s_-]?key\b",
    r"\bapi[\s_-]?key\b\s*(is|=|:)",
]


def is_suspicious(content):
    """The injection pattern content matches, else False."""
    lowered = content.lower()
    for pat in INJECTION_PATTERNS:
        if re.search(pat, lowered):
            return pat
    return False


def current_timestamp():
    return datetime.now(UTC).isoformat()


@dataclass
class MessageQuarantine:
    path: str
    ledger: MessageLedger
    timestamp: Callable[[], str] = current_timestamp

    def hold(self, msg, channel_name, matched_pattern, fragments=()):
        """Hold a message (and its burst fragments: a split injection must
        not have its second half swept back in alone) for the owner."""
        entry = {
            "message_id": msg["id"],
            "channel_id": msg.get("channel_id"),
            "channel_name": channel_name,
            "author": msg.get("author", {}).get("username"),
            "author_id": msg.get("author", {}).get("id"),
            "content": msg.get("content", ""),
            "matched_pattern": matched_pattern,
            "timestamp": msg.get("timestamp"),
            "quarantined_at": self.timestamp(),
        }
        if fragments:
            entry["fragments"] = [
                {"message_id": f["id"], "content": f.get("content", "")} for f in fragments
            ]
        q = read_json(self.path, list)
        if q is None:  # kept for the owner, never silently emptied
            with contextlib.suppress(OSError):
                os.replace(self.path, self.path + ".bad")
            print("quarantine.json was unreadable; moved to quarantine.json.bad.", flush=True)
            q = []
        q.append(entry)
        atomic_write(self.path, q[-100:])  # keep last 100
        self.ledger.record_many(
            [msg["id"]] + [f["id"] for f in fragments],
            "quarantined",
            matched_pattern,
            context=LedgerContext(
                channel_id=msg.get("channel_id"),
                author=entry["author"],
                author_id=(msg.get("author") or {}).get("id"),
            ),
        )
        print(
            f"Quarantined suspicious message from {entry['author']} in #{channel_name} "
            f"(matched: {matched_pattern}): {entry['content'][:80]}",
            flush=True,
        )

    def ids(self):
        ids = set()
        for e in read_json(self.path, list) or []:
            if isinstance(e, dict):
                ids.add(e.get("message_id"))
                ids.update(
                    fr.get("message_id") for fr in e.get("fragments") or [] if isinstance(fr, dict)
                )
        return ids
