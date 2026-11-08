"""Pure reply normalization and mass-mention neutralization."""

MAX_PARTS = 4


def sanitize_reply(text, max_chars):
    """Apply output guardrails to a drafted reply."""
    text = (text or "").strip()[:max_chars]
    # Neutralize mass mentions with a zero-width space
    text = text.replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")
    return text


def normalize_parts(item, max_chars):
    """Return a reply as a list of message parts for fragmented delivery.

    The worker drafts long replies as "parts" (2-4 short messages, like a
    person thinking aloud) instead of one "text" blob. Short replies stay a
    single-element list. Enforces the part cap and max_reply_chars as the
    budget of all parts together (the later ones are cut first).
    """
    raw = item.get("parts")
    clean = [p for p in raw if isinstance(p, str) and p.strip()] if isinstance(raw, list) else []
    if clean:
        parts = [sanitize_reply(p, max_chars) for p in clean[:MAX_PARTS]]
        if len(clean) > MAX_PARTS:
            # Fold overflow into the last part rather than dropping thoughts.
            rest = " ".join(sanitize_reply(p, max_chars) for p in clean[MAX_PARTS:])
            parts[-1] = sanitize_reply((parts[-1] + " " + rest).strip(), max_chars)
    else:
        parts = [sanitize_reply(item.get("text", ""), max_chars)]
    out, left = [], max_chars
    for p in parts:
        p = p[:left].rstrip()
        if p:
            out.append(p)
            left -= len(p)
    return out
