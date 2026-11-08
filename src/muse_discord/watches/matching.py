"""Literal word matching, watch coverage and notification grouping."""

import re

from muse_discord.watches.limits import MAX_COMPILED_PATTERNS

_PATTERNS = {}


def matcher(words):
    """A compiled case-insensitive matcher for a watch's words: escaped
    literals between non-word boundaries (no user regex; linear time)."""
    key = tuple(words)
    pat = _PATTERNS.get(key)
    if pat is None:
        alts = "|".join(re.escape(w.casefold()) for w in sorted(words, key=len, reverse=True))
        pat = re.compile(r"(?<!\w)(?:" + alts + r")(?!\w)")
        if len(_PATTERNS) > MAX_COMPILED_PATTERNS:
            _PATTERNS.clear()
        _PATTERNS[key] = pat
    return pat


def match_text(words, text):
    """The watch's word or phrase found in text (casefolded), or None."""
    if not text or not words:
        return None
    m = matcher(words).search(text.casefold()[:8000])
    return m.group(0) if m else None


def watch_covers(rec, guild_id, channel_id, parent_id=None):
    """The message's place is inside the watch's scope."""
    sc = rec.get("scope") or {}
    if sc.get("guild_id") != guild_id:
        return False
    if sc.get("channel_id"):
        return channel_id == sc["channel_id"] or (
            parent_id is not None and parent_id == sc["channel_id"]
        )
    return True


def group(entries):
    """Fire entries [(requester, channel_id, entry)] grouped into one
    fire per requester and place, in order: several reminders due at
    once (say, after downtime) reach a person as one message, never a
    burst."""
    out = {}
    for req, cid, e in entries:
        out.setdefault((req, cid), []).append(e)
    return out
