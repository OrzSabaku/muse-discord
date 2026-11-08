"""Parse health-check arguments and select checks or owner follow-up."""

import argparse
import json
import os

from muse_discord.diagnostics.checks import check
from muse_discord.diagnostics.context import Doctor
from muse_discord.diagnostics.followup import online_dm, wait_mention
from muse_discord.setup import support as C


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check the bot's health.")
    ap.add_argument(
        "--no-dm", action="store_true", help="don't send the owners the \"I'm online\" DM"
    )
    ap.add_argument(
        "--wait-mention",
        type=float,
        metavar="SECONDS",
        help="wait for an owner's @mention to be answered",
    )
    ap.add_argument("--quick", action="store_true", help="don't wait for the hooks to show up")
    ap.add_argument("--dir", default=C.PKG_DIR, help=argparse.SUPPRESS)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    d = Doctor(os.path.abspath(args.dir), args.json)
    cfg = os.environ.get("MUSE_DISCORD_CONFIG") or d.path("config.yaml")
    MS = C.settings_module()
    try:
        st = MS.load(cfg)
    except MS.SettingsError as e:
        d.add(
            "problem",
            f"config.yaml is not valid: {e}",
            "python3 bin/settings.py validate names each problem.",
        )
        code = 2
    else:
        if args.wait_mention is not None:
            wait_mention(d, st, args.wait_mention)
        else:
            token = check(d, st, args)
            if token and not args.no_dm and not d.failed:
                online_dm(d, st, token)
        code = 1 if d.failed else 0
    if args.json:
        print(json.dumps({"ok": code == 0, "results": d.results}, indent=1))
    elif code == 0:
        print("All good.")
    return code
