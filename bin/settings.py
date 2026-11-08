#!/usr/bin/env python3
"""muse-discord settings tool.

    bin/settings.py validate
        Check config.yaml (and config.discord.yaml, the Discord-side
        changes). Exit status 1, with every problem listed, when the bot
        would refuse to start.
    bin/settings.py show [--dms | --server ID [--channel ID]
                          [--category ID]] [--explain]
        What the bot does in one place: every setting's resolved value
        and where it comes from (built-in, config.yaml, a Discord change,
        or inherited from a wider scope). No flags = the everywhere
        scope. --explain lists every layer for each setting.
    bin/settings.py fold [--dry-run]
        Copy the Discord-side changes into config.yaml and clear them
        (server admins' changes stay where they are).
        Rewrites config.yaml (its comments are lost) after saving a
        timestamped backup next to it. Does not prompt.
Changing settings (the muse.ai path; writes config.discord.yaml, as
/settings does, and prints each change in plain words):

    bin/settings.py set KEY VALUE --place PLACE
    bin/settings.py reset KEY --place PLACE
    bin/settings.py undo --place PLACE
    bin/settings.py preset friends|community|work --place server:ID
    bin/settings.py pause --place PLACE
    bin/settings.py resume --place PLACE
    bin/settings.py explain [--place PLACE] [KEY ...]
    bin/settings.py places [--find TEXT] [--server ID]

`places` lists the places with their ids (from directory.json, which
the running monitor keeps up to date): defaults, dms, each server and
its channels. --find shows only the servers and channels whose name
contains TEXT; --server only that server.

PLACE is defaults (every server and DM without its own setting), dms,
server:<server id> or channel:<server id>:<channel id>. KEY is a
setting's key (who.replies) or its name ("When it answers"); VALUE a
value or its name (when-addressed, "Only when @mentioned", null, true,
a list of user ids separated by commas). Undo reverts the newest
change at that place with everything applied together with it (a
server's Undo includes its channels). Risky changes (judged by what
they lead to: strangers' DMs, your memory in a big server, Pause
everywhere, ...) are printed with a warning and NOT made unless --yes
is given: only add --yes after the owner explicitly said yes (exit
status 3 = needs --yes). --by NAME records who asked (default
muse.ai); it is a name, never a user id (ids are for changes made in
Discord): every change made here is sent to the owners in a silent DM
with an Undo button. --category ID names a channel's category.

--config PATH picks the file (default: $MUSE_DISCORD_CONFIG, else
config.yaml in the package directory).
"""

import os
import sys

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(PKG, "src"), os.path.join(PKG, "bin")]
import setup_common  # noqa: E402,F401 (select the installed interpreter before imports)

from muse_discord.settings_cli.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
