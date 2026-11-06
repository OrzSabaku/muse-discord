"""secret_scan.py -- does outgoing text carry one of the bot's secrets?

Shared by monitor.py (guard_outgoing, before every send to Discord) and
bin/discord-api.py (post, edit, thread). On the real platform every
session runs as the same OS user, so a worker that follows an injection
into its shell can read the token file and the nonce files; this keeps
what it read out of what the bot posts.

find(text, token, nonces) names what it found, or returns None:
  - the token as is, reversed, or squashed (everything outside
    [A-Za-z0-9] removed, so dots, spaces and zero-width characters do
    not hide it), and its base64 (all 3 alignments) and hex encodings;
  - a live nonce, as is, squashed or reversed;
  - anything shaped like a Discord token (three base64url segments, the
    first decoding to a snowflake), whoever's it is.
Nothing more elaborate: a determined encoder gets past any filter, and
this one must never alter text it lets through.
"""
import base64
import re

_TOKEN_SHAPE = re.compile(
    r"(?<![A-Za-z0-9_-])([A-Za-z0-9_-]{17,40})\.([A-Za-z0-9_-]{4,10})"
    r"\.([A-Za-z0-9_-]{20,})")
_NOT_ALNUM = re.compile(r"[^A-Za-z0-9]")
_FORMS = {}   # token -> (squashed, [base64 cores], hex)


def _token_forms(token):
    if token not in _FORMS:
        raw = token.encode()
        cores = []
        for i in range(3):
            full = base64.b64encode(b"\0" * i + raw).decode()
            # Only the characters every encoding of the token shares,
            # whatever the bytes before and after it.
            core = full[-(-8 * i // 6):(8 * (i + len(raw))) // 6]
            cores.append(_NOT_ALNUM.sub("", core))
        _FORMS.clear()
        _FORMS[token] = (_NOT_ALNUM.sub("", token), cores, raw.hex())
    return _FORMS[token]


def token_shaped(text):
    for m in _TOKEN_SHAPE.finditer(text):
        seg = m.group(1)
        try:
            dec = base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))
        except (ValueError, TypeError):
            continue
        if re.fullmatch(rb"[1-9][0-9]{16,19}", dec):
            return True
    return False


def find(text, token=None, nonces=()):
    """What secret text carries ("the bot token", "a live nonce",
    "something shaped like a Discord token"), or None."""
    if not isinstance(text, str) or not text:
        return None
    squashed = _NOT_ALNUM.sub("", text)
    backwards = squashed[::-1]
    if token and len(token) >= 20:
        flat, cores, hexed = _token_forms(token)
        if token in text or token in text[::-1] or flat in squashed \
                or flat in backwards or hexed in squashed.lower() \
                or any(c in squashed for c in cores):
            return "the bot token"
    for n in nonces:
        if n and (n in text or n in squashed or n in backwards):
            return "a live nonce"
    if token_shaped(text) or token_shaped(text[::-1]):
        return "something shaped like a Discord token"
    return None
