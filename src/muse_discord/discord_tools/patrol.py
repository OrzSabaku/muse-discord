"""Saved patrol grants, independent of Gateway owner-command capabilities.

The administrator controls patrol.yaml alongside the bot configuration and
credentials. This is a local trust boundary, not isolation from a process that
can rewrite those files or access the bot token directly.
"""

import sys
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError

from .cli import CommandCLI
from .session import ToolSession

Operation = Literal["get-messages", "list-unread", "reply-message", "send-message", "mark-read"]
ChannelId = Annotated[StrictStr, Field(pattern=r"^[1-9][0-9]{16,19}$")]
MIN_PATROL_ARGV = 3  # executable, profile, command
GUILD_MESSAGE_TYPES = {0, 5, 10, 11, 12}  # text, announcement, and guild threads


class PatrolGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    channels: Annotated[
        dict[ChannelId, Annotated[list[Operation], Field(min_length=1)]], Field(min_length=1)
    ]
    # Relative paths are resolved from the package, independent of cron's cwd.
    read_state: Annotated[StrictStr, Field(min_length=1)]


class PatrolConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    profiles: Annotated[dict[StrictStr, PatrolGrant], Field(min_length=1)]


class PatrolAuthorization:
    def __init__(self, session: ToolSession, grant: PatrolGrant):
        self.session = session
        self.grant = grant
        self.binding = {"for": None, "nonce": None, "entry": None, "cmd": None}

    def authorize_command(self, lookup=False):
        """Shared dispatch hook: authorize against the saved patrol grant."""
        if self.binding["for"] is not None or self.binding["nonce"] is not None:
            raise SystemExit("Refusing: patrol cannot use owner-command bindings.")
        if not any(self.binding["cmd"] in ops for ops in self.grant.channels.values()):
            raise SystemExit("Refusing: operation is not granted to this patrol.")

    def guard_target(self, cid):
        if self.binding["cmd"] not in self.grant.channels.get(cid, []):
            raise SystemExit("Refusing: patrol operation is not granted in this channel.")
        channel = self.session.api("GET", f"/channels/{cid}")
        # Positively identify a guild message channel; malformed responses and
        # unknown types must never become permission to read or send.
        if (
            not isinstance(channel, dict)
            or channel.get("id") != cid
            or not channel.get("guild_id")
            or channel.get("type") not in GUILD_MESSAGE_TYPES
        ):
            raise SystemExit("Refusing: patrol requires a verified server message channel.")


def create_patrol_cli(package_dir: str, profile: str) -> CommandCLI:
    try:
        config = PatrolConfig.model_validate(
            yaml.safe_load((Path(package_dir) / "patrol.yaml").read_text())
        )
        grant = config.profiles[profile]
    except OSError, UnicodeError, yaml.YAMLError, ValidationError, KeyError:
        raise SystemExit(
            "Refusing: patrol configuration or profile is unavailable or invalid."
        ) from None
    session = ToolSession(package_dir)
    session.read_state_file = str(Path(package_dir) / grant.read_state)
    session.token = session.load_token()
    return CommandCLI(session, PatrolAuthorization(session, grant))


def main(package_dir: str, argv: list[str]) -> int:
    if len(argv) < MIN_PATROL_ARGV:
        print("usage: discord-patrol.py PROFILE COMMAND ... (see docs/TOOLS.md)", file=sys.stderr)
        return 2
    return create_patrol_cli(package_dir, argv[1]).main([argv[0], *argv[2:]])
