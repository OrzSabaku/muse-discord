"""Send one JSON report and sanitize transport failures for local diagnostics."""

import json
import urllib.error
import urllib.parse
import urllib.request

from muse_discord.usage_stats.limits import _USERINFO_RE, SCHEMA, TIMEOUT_SECS


def failure_reason(exc):
    """A short, log-safe reason for a failed post: the exception's class
    and reason, with any user:password@ in a URL masked."""
    why = getattr(exc, "reason", None) or exc
    text = f"{type(exc).__name__}: {why}"
    return _USERINFO_RE.sub("//***@", text)[:200]


def post_json(url, obj, timeout=TIMEOUT_SECS):
    """POST obj as JSON; the HTTP status (an HTTPError's too). Raises
    for anything else (no connection, a timeout)."""
    body = json.dumps(obj, separators=(",", ":")).encode()
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": f"muse-discord-telemetry/{SCHEMA}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        e.close()
        return e.code
