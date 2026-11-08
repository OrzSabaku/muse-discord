"""Parse monitor launch options without loading configuration or credentials."""

import argparse
from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True)
class MonitorOptions:
    config: str | None
    token: str | None = field(repr=False)


def parse_options(argv: Sequence[str] | None = None) -> MonitorOptions:
    parser = argparse.ArgumentParser(prog="monitor.py", description="Run the Discord monitor.")
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="configuration file (otherwise use the environment or project default)",
    )
    parser.add_argument(
        "token", nargs="?", help="optional transient token override; otherwise use token_from"
    )
    args = parser.parse_args(argv)
    return MonitorOptions(args.config, args.token)
