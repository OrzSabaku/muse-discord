"""Discord interaction response values; no runtime or network dependencies."""


def ephemeral(text):
    """A callback response only the clicking user sees."""
    return {"type": 4, "data": {"content": text, "flags": 64, "allowed_mentions": {"parse": []}}}


def closed_card(ix, line):
    """The clicked message, buttons removed, with line added: what was
    chosen."""
    original = (ix.get("message") or {}).get("content") or ""
    return {
        "type": 7,
        "data": {
            "content": original[: 1990 - len(line)] + "\n\n" + line,
            "components": [],
            "allowed_mentions": {"parse": []},
        },
    }


def interaction_user_id(ix):
    user = (ix.get("member") or {}).get("user") or ix.get("user") or {}
    return str(user.get("id") or "")
