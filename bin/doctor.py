#!/usr/bin/env python3
"""doctor.py -- is the bot healthy? One command, plain answers.

    python3 bin/doctor.py                    # check, then DM the owners
                                             # "I'm online"
    python3 bin/doctor.py --no-dm            # check only
    python3 bin/doctor.py --wait-mention 600 # wait for an owner's
                                             # @mention to be answered

Checks: the config and token; a worker prompt too old to reply; the
monitor process; that Discord accepted the token and the realtime
connection is up (Message Content intent on); the /settings command;
that the three hooks ran recently (each leaves a .seen-<hook> stamp in
the package dir), and a note when the optional web watch hook does
not; the last reply sent.
Each problem is printed with its fix, in plain words. Then, unless
--no-dm, it DMs each owner that the bot is online (the only thing it
ever changes), with the invite to the Muse Agent server.

--wait-mention SECONDS watches the mention ledger for an owner's next
@mention (made after this command starts) and reports how it ended:
answered, or why not (for instance, the bot cannot post in that
channel). It sends no DM.

Exit status: 0 when nothing is wrong (notes allowed), 1 when something
is, 2 for a bad config (nothing else checked). --json prints one JSON
object. Never prints the token.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import setup_common

setup_common.ensure_src_on_path()
from muse_discord.diagnostics.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
