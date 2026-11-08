"""Parse settings CLI arguments and dispatch to display or mutation commands."""

import argparse
import sys

from muse_discord import muse_settings as MS
from muse_discord.settings import values as settings_values
from muse_discord.settings_cli.editing import (
    NeedsYes,
    cmd_fold,
    cmd_pause,
    cmd_preset,
    cmd_reset,
    cmd_resume,
    cmd_set,
    cmd_undo,
)
from muse_discord.settings_cli.presentation import (
    cmd_explain,
    cmd_places,
    cmd_show,
    cmd_validate,
)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="settings.py", description="muse-discord settings tool")
    ap.add_argument("--config", help="config.yaml to use")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate", help="check config.yaml")
    sh = sub.add_parser("show", help="resolved settings for one scope")
    g = sh.add_mutually_exclusive_group()
    g.add_argument("--dms", action="store_true")
    g.add_argument("--server")
    sh.add_argument("--channel", help="a channel id (with --server)")
    sh.add_argument("--category", help="that channel's category id")
    sh.add_argument("--explain", action="store_true")
    fo = sub.add_parser("fold", help="copy Discord changes into config.yaml")
    fo.add_argument("--dry-run", action="store_true")

    def changer(name, help_, *pos):
        p = sub.add_parser(name, help=help_)
        for a in pos:
            p.add_argument(*a[0], **a[1])
        p.add_argument(
            "--place",
            required=name != "explain",
            help="defaults, dms, server:ID or channel:ID:ID",
        )
        p.add_argument("--category", help="the channel's category id")
        if name != "explain":
            p.add_argument("--by", default="muse.ai", help="who asked (default: muse.ai)")
            p.add_argument(
                "--yes",
                action="store_true",
                help="make a risky change (only after the owner said yes)",
            )
        return p

    changer("set", "change a setting at a place", (["key"], {}), (["value"], {}))
    changer("reset", "make a place follow its wider place again", (["key"], {}))
    changer("undo", "undo the newest change at a place")
    changer(
        "preset",
        "apply Friends, Community or Work to a server",
        (["name"], {"choices": ["friends", "community", "work"], "type": str.lower}),
    )
    changer("pause", "stop answering at a place")
    changer("resume", "undo a pause at a place")
    changer("explain", "what the bot does at a place, in plain words", (["keys"], {"nargs": "*"}))
    pl = sub.add_parser("places", help="the places (servers, channels) with their ids")
    pl.add_argument("--find", help="only names containing this text")
    pl.add_argument("--server", help="only this server id")
    args = ap.parse_args(argv)
    by = getattr(args, "by", None)
    if by is not None and (not str(by).strip() or settings_values._id_str(str(by)) is not None):
        print(
            f"--by {by!r}: give a name (like muse.ai), not a user id; "
            f"user ids are for changes made in Discord.",
            file=sys.stderr,
        )
        return 2
    try:
        return {
            "validate": cmd_validate,
            "show": cmd_show,
            "fold": cmd_fold,
            "set": cmd_set,
            "reset": cmd_reset,
            "undo": cmd_undo,
            "preset": cmd_preset,
            "pause": cmd_pause,
            "resume": cmd_resume,
            "explain": cmd_explain,
            "places": cmd_places,
        }[args.cmd](args)
    except NeedsYes:
        print(
            "Not changed: this is risky. Tell the owner the warning; run "
            "it again with --yes only after they say yes."
        )
        return 3
    except MS.SettingsError as e:
        print(str(e), file=sys.stderr)
        return 1
