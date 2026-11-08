#!/usr/bin/env python3
"""discord-api.py -- allowlisted Discord API tool for muse-discord.

This is the bot's owner-command path: ONLY the reply worker may call it,
and ONLY for a queue item the monitor stamped with owner_command=true
(which the monitor sets exclusively for the owners' messages, from their
verified user IDs). It is how "react to your post in #showcase" actually
does something instead of getting a chatter reply.

Every subcommand that acts in or reads a channel (read, post, thread,
react, pin, unpin, edit, delete-own) needs a leading `--for <the item's
message_id> --nonce <its owner_command_nonce>`, checked in constant
time against owner_items.json: the monitor's private file (mode 600,
read here and never printed) of the open owner-command items and their
nonces (an owner DM's is its owner_dm_nonce). The wake hook hands the
nonce only to the wake that holds that one owner item alone, so a
worker woken by someone else's message (and maybe their prompt
injection) cannot read anyone's DMs or post anywhere through this tool,
even when it knows the owner's message id. resolve-channel and
list-threads (names and ids of a server's channels and active threads)
need it too, or an open owner DM's owner_dm_nonce.

Even the owner's own run is held to the owner's own words: a
subcommand acts only in the command's own channel, or in one the
owner's message itself names, which the monitor resolved to channel
ids when it queued the command (owner_items.json: a <#id> mention, the
only way to name a thread; a #name in the command's own server; and,
for `post` only, a channel of that server named bare after "in", "to"
or "into"), and never in a DM other than the command's own. Places
that only the channel history or `read` output mention never count.
`read` output comes between markers random to each call. Text this
tool posts is refused when it carries a secret (secret_scan.py).

Every id argument must be a plain Discord id (ids are interpolated into
API paths, so "1/../../guilds/2/bans/3" must never get through), files
can only be uploaded from the package's generated/ dir, and messages
never ping roles, @everyone or @here.

An owner's DM channel is off limits unless the run holds that DM: any
subcommand on a channel the API reports as a DM with an owner (or any
DM, when the owners cannot be read from the config) needs a leading
`--for <the DM's message_id> --nonce <its owner_dm_nonce>`, checked in
constant time against owner_dm_nonces.json (the monitor's private file,
read here and never printed) and against the API (that message must be
in that channel). The wake hook hands a nonce only to a wake that holds
nothing but owner DMs, so a run handed an owner's server message cannot
act in the owner's DM either.

Subcommands are a deliberate allowlist. Moderation (ban/kick/timeout),
server settings, webhooks, roles, and anything else are NOT implemented:
if the owner asks for something outside this list, the worker says so
honestly instead of improvising.

JSON commands for unread/history, acknowledgments, split sends, replies,
and media downloads use the same owner binding and channel guard. They
are shared with bin/discord-mcp.py; see docs/TOOLS.md.

The bot token is resolved exactly like monitor.py's: token_from in the
package config.yaml (../config.yaml relative to this file, or
MUSE_DISCORD_CONFIG), read through muse_settings.py. The token is never printed.
"""

import sys
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_DIR / "src"))
from muse_discord.discord_tools.cli import create_cli  # noqa: E402

if __name__ == "__main__":
    sys.exit(create_cli(str(PACKAGE_DIR)).main(sys.argv))
