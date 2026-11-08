#!/usr/bin/env python3
"""connect-discord.py -- connect a new Discord app to this install.

docs/SETUP.md. The Muse writes the token the owner pasted in chat to
a file in ~/.config/muse-discord/incoming/ (made mode 700 by
bin/install.sh) with its file-writing tool, never a shell command, and
names that file:

    python3 bin/connect-discord.py --name "Pip" \
        --token-file ~/.config/muse-discord/incoming/token

The file is read, then deleted. A token typed into a shell command (an
argument, or a heredoc in `bash -c '...'`) shows in the process list, so
never do that. --saved reuses the token already saved (to re-check after
a portal step); with neither, the token is read from stdin.

It then:

- checks the paste is a bot token, and if not, says what was pasted
  instead (the Application ID, the Public Key, the Client Secret, a
  link) and what to copy;
- asks Discord who the bot is (GET /users/@me) and which app it
  belongs to (GET /applications/@me): the application id and the owner
  (the app's owner, or its team's owner) come from there, so nobody
  types an id;
- turns on the Message Content intent (PATCH /applications/@me flags,
  GATEWAY_MESSAGE_CONTENT_LIMITED), then reads the app back to check;
- saves the token to the token file (mode 600, written atomically),
  writes config.yaml with the owner filled in and checks it the way the
  bot will (an existing valid config.yaml is kept, or backed up before
  the owner is added; an invalid one stops everything before anything
  is written), and, while persona.md is still the shipped placeholder,
  writes the default persona there: the bot is the Muse itself, by
  name;
- prints the invite link.

    python3 bin/connect-discord.py --wait [SECONDS]

waits (default 600 s) until the bot is in a server, then says which
server, and how many channels it can read and post in. It uses the
saved token.

Exit status: 0 done; 2 the paste is not a bot token or Discord refused
it (nothing written); 3 Discord could not be reached (nothing written);
4 connected and saved, but the owner has a step to do in the Developer
Portal (printed); 5 --wait timed out; 6 config.yaml is invalid (nothing
written: fix it first) or could not be written. --json prints one JSON
object instead of sentences ("problem" is the kind, "owner_steps" the
portal steps). The token is never printed, logged, or written anywhere but the
token file.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import setup_common

setup_common.ensure_src_on_path()
from muse_discord.setup.connection import connect  # noqa: E402
from muse_discord.setup.invite import wait  # noqa: E402
from muse_discord.setup.report import Report  # noqa: E402
from muse_discord.setup.support import PKG_DIR as PKG  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Connect a Discord app to muse-discord (token on "
        "stdin), or --wait for the bot to join a server."
    )
    ap.add_argument("--name", help="the bot's name (your Muse's name)")
    ap.add_argument(
        "--token-file",
        metavar="PATH",
        help="read the pasted token from this file (in "
        "~/.config/muse-discord/incoming/), then delete it",
    )
    ap.add_argument(
        "--saved",
        action="store_true",
        help="use the token already saved (re-check after a portal step)",
    )
    ap.add_argument(
        "--wait",
        nargs="?",
        type=float,
        const=600.0,
        metavar="SECONDS",
        help="wait for the bot to be in a server (default 600 s), then report what it can see",
    )
    ap.add_argument("--poll", type=float, default=5.0, help=argparse.SUPPRESS)
    ap.add_argument(
        "--config",
        default=os.environ.get("MUSE_DISCORD_CONFIG") or os.path.join(PKG, "config.yaml"),
    )
    ap.add_argument("--json", action="store_true", help="one JSON object instead of sentences")
    args = ap.parse_args(argv)
    rep = Report(args.json)
    if args.wait is not None:
        return wait(args, rep)
    if not (args.token_file or args.saved) and sys.stdin.isatty():
        print("Use --token-file PATH (see docs/SETUP.md).", file=sys.stderr)
        return 2
    return connect(args, rep)


if __name__ == "__main__":
    sys.exit(main())
