"""The reply format shared by the outbox writer and the sending monitor.

These checks need no token, live state or network. Request binding, nonce
verification and permission checks still belong to each caller.
"""

import re


ID_FIELDS = ("channel_id", "reply_to", "item")


def is_snowflake(value):
    # IDs enter API paths: reject booleans, leading zeroes and path fragments.
    return (
        isinstance(value, (str, int))
        and not isinstance(value, bool)
        and re.fullmatch(r"[1-9][0-9]{0,19}", str(value)) is not None
    )


def shape_problems(item):
    """Return structural errors without mutating the worker's payload."""
    if not isinstance(item, dict):
        return ["not a JSON object"]
    errors = []
    for field in ID_FIELDS:
        value = item.get(field)
        if field != "channel_id" and value in (None, ""):
            continue
        if not is_snowflake(value):
            errors.append(f"{field} is not a Discord id")
    if item.get("text") is not None and not isinstance(item["text"], str):
        errors.append("text is not a string")
    for field in ("parts", "reactions", "attachments", "remember"):
        value = item.get(field)
        if value is not None and not (
            isinstance(value, list) and all(isinstance(v, str) for v in value)
        ):
            errors.append(f"{field} is not a list of strings")
    for field, kind in (("_retry", dict), ("_hold", str)):
        if item.get(field) is not None and not isinstance(item[field], kind):
            errors.append(f"{field} was edited (leave it alone)")
    if "settings" in item or any(str(k).startswith("_nl") for k in item):
        errors.append("settings actions are no longer supported; use /settings")
    return errors


def content_problem(item, *, has_text=None):
    """Check delivery intent after shape validation.

    The monitor supplies whether any text survives its length budget;
    the CLI checks the untruncated draft. Neither treats an empty draft
    as deliberate silence.
    """
    if item.get("silent"):
        return (
            None
            if item.get("reply_to") or item.get("item")
            else "silent item names no message (reply_to or item)"
        )
    if has_text is None:
        has_text = bool(
            (item.get("text") or "").strip() or any(p.strip() for p in item.get("parts") or [])
        )
    if has_text:
        return None
    if any(e.strip() for e in item.get("reactions") or []):
        return None if item.get("reply_to") else "reactions without reply_to"
    hint = next(
        (
            f' (the reply goes in "text", not "{key}")'
            for key in ("content", "message", "reply", "body")
            if key in item
        ),
        "",
    )
    return "nothing to send: no text, parts, or reactions, and not silent: true" + hint
