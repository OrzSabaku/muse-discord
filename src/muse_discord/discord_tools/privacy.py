"""Refuse outgoing tool text containing credentials or published live capabilities.

The structured files are scanned recursively for nonce fields. No secret value
is included in a rejection, including values split across output parts or files.
"""

import json
import os

from muse_discord import secret_scan as SCAN
from muse_discord.outgoing_guard import collect_nonces

FLAT_NONCE_FILES = ("item_nonces.json", "proactive_nonce.json")
RECORD_NONCE_FILES = ("web_watches_due.json",)


def live_nonces(owner_dm_file, owner_items_file):
    def load(path):
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    here = os.path.dirname(owner_items_file)
    flat = [owner_dm_file] + [os.path.join(here, n) for n in FLAT_NONCE_FILES]
    records = [owner_items_file] + [os.path.join(here, n) for n in RECORD_NONCE_FILES]
    return collect_nonces(
        (load(path) for path in flat), (load(path) for path in records), lambda: ()
    )


def scrub(text, token, owner_dm_file, owner_items_file):
    """Text to post, as is: refused when it carries a secret
    (secret_scan.find: the bot token in any of its simple disguises, a
    live nonce, or anything shaped like a Discord token; the value is
    never printed). The same filter as monitor.py's guard_outgoing."""
    what = SCAN.find(text, token, live_nonces(owner_dm_file, owner_items_file))
    if what:
        raise SystemExit(f"Refusing: the text contains {what}; nothing was sent.")
    return text
