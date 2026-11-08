"""Classify a pasted credential and consume a private token drop-off file.

These checks need no Discord connection or bot configuration. Credentials are
returned to the caller and never logged; consuming a valid private file deletes it.
"""

import base64
import os
import re
import stat

TOKEN_RE = re.compile(
    r"[A-Za-z0-9_-]{17,40}\.[A-Za-z0-9_-]{4,10}"
    r"\.[A-Za-z0-9_-]{25,}"
)


AGAIN = (
    "In the Developer Portal, open your app, click **Bot** in the "
    "left menu, then **Reset Token**, then **Copy**, and paste it "
    "here."
)


WRONG_PASTE = {
    "empty": "Nothing came through.",
    "id": "That's the Application ID (just a number), not the token.",
    "public_key": "That's the Public Key from the General Information page, not the token.",
    "client_secret": "That's the Client Secret from the OAuth2 page, not the bot's token.",
    "url": "That's a link, not the token.",
    "user_token": "That looks like a token for a personal Discord account, "
    "not a bot. Don't share that one with anyone.",
    "unknown": "That doesn't look like a bot token.",
    "rejected": "Discord didn't accept that token. Either it was reset "
    "after you copied it, or it's your own account's token "
    "rather than the bot's (never share that one).",
}


WRONG_PASTE_PATTERNS = (
    (r"(?is)(?:https?://|discord(app)?\.com/|www\.).*", "url"),
    (r"\d{15,21}", "id"),
    (r"[0-9a-fA-F]{64}", "public_key"),
    (r"[A-Za-z0-9_-]{32}", "client_secret"),
)


def clean_paste(raw):
    s = (raw or "").strip().strip("`'\"").strip()
    if s.lower().startswith("bot "):
        s = s[4:].strip()
    return s


def classify(s):
    """ "token" for something shaped like a bot token, else the kind of
    wrong paste (a WRONG_PASTE key)."""
    if not s:
        return "empty"
    for pattern, kind in WRONG_PASTE_PATTERNS:
        if re.fullmatch(pattern, s):
            return kind
    if s.startswith("mfa."):
        return "user_token"
    if TOKEN_RE.fullmatch(s):
        head = s.split(".", 1)[0]
        try:
            uid = base64.urlsafe_b64decode(head + "=" * (-len(head) % 4))
        except (ValueError, TypeError):
            return "unknown"
        if re.fullmatch(rb"\d{15,21}", uid):
            return "token"
    return "unknown"


def read_token_file(path):
    """The token from a file the Muse wrote, then delete it. Only a file
    the user owns, in a place only they can read (the file mode 600, or
    its directory mode 700), never a symlink. Returns (token, problem)."""
    path = os.path.expanduser(path)
    try:
        st = os.lstat(path)
        dst = os.stat(os.path.dirname(os.path.abspath(path)))
    except OSError as e:
        return None, f"Can't read {path}: {e.strerror}."
    uid = os.getuid()
    if not stat.S_ISREG(st.st_mode) or st.st_uid != uid:
        return None, f"{path} isn't a plain file of this account."
    private_file = not st.st_mode & 0o077
    private_dir = dst.st_uid == uid and not dst.st_mode & 0o077
    if not (private_file or private_dir):
        return None, (
            f"{path} is readable by other accounts; write the "
            f"token into ~/.config/muse-discord/incoming/ "
            f"(bash bin/install.sh makes it) instead."
        )
    with open(path) as f:
        token = f.read()
    os.unlink(path)
    return token, None
