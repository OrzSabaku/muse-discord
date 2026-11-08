"""Shared setup REST operations, Discord facts and owner-config token lookup.

Importing this module performs no network access, token reads or interpreter
selection. Commands arrange their Python runtime before importing setup services.
"""

import os
import sys
import urllib.parse
from typing import Any

from muse_discord.paths import project_root

PKG_DIR = str(project_root(__file__))

API = "https://discord.com/api/v10"
# Tests point the scripts at a local fake with this variable. Only a
# loopback http URL is accepted, so it can never send the token to
# another host.
API_ENV = "MUSE_DISCORD_API_BASE"
TIMEOUT = 20

# What the bot asks for in a server: View Channel, Send Messages, Send
# Messages in Threads, Read Message History, Attach Files, Embed Links,
# Add Reactions, Pin Messages. No administrator, no moderation.
PERMISSIONS = (
    (1 << 10) | (1 << 11) | (1 << 38) | (1 << 16) | (1 << 15) | (1 << 14) | (1 << 6) | (1 << 51)
)
VIEW_CHANNEL = 1 << 10
SEND_MESSAGES = 1 << 11
ADMINISTRATOR = 1 << 3

# Application flags (docs: Application, "Application Flags"). Only the
# *_LIMITED intent flags can be changed through the API.
MESSAGE_CONTENT = 1 << 18
MESSAGE_CONTENT_LIMITED = 1 << 19
EDITABLE_FLAGS = (1 << 13) | (1 << 15) | MESSAGE_CONTENT_LIMITED

COMMUNITY_LINE = (
    "Want to meet other Muse owners and their Muses? Join "
    "the Muse Agent server: https://discord.gg/2wwcHbq47N"
)


class ApiError(Exception):
    """A Discord REST error: status is the HTTP status (0 when Discord
    could not be reached), code Discord's JSON error code, if any."""

    def __init__(self, status, code=None, message=""):
        super().__init__(
            f"HTTP {status}: {message}" if status else f"cannot reach Discord: {message}"
        )
        self.status = status
        self.code = code
        self.message = message


def api_base():
    base = os.environ.get(API_ENV)
    if not base:
        return API
    u = urllib.parse.urlparse(base)
    if u.scheme != "http" or u.hostname not in ("127.0.0.1", "localhost", "::1"):
        sys.exit(f"{API_ENV} must be a loopback http URL; ignoring it is not safe, so stopping.")
    return base.rstrip("/")


def call(method: str, path: str, token: str, body: Any = None) -> Any:
    """One REST call as the bot. Returns the parsed JSON (None for an
    empty body). Raises ApiError. Never puts the token in an error."""
    from muse_discord.discord_rest import DiscordError, transport

    try:
        value = transport(token, api_base()).request(method, path, body, timeout=TIMEOUT)
        return value if value != "" else None
    except DiscordError as exc:
        raise ApiError(exc.status, exc.discord_code, str(exc)) from None
    except (OSError, TimeoutError) as exc:
        raise ApiError(0, None, str(exc)) from None


def invite_url(app_id):
    """The link that adds the bot to a server (integration_type=0: a
    server install, never a user install)."""
    q = urllib.parse.urlencode(
        {
            "client_id": app_id,
            "scope": "bot",
            "permissions": str(PERMISSIONS),
            "integration_type": "0",
        }
    )
    return f"https://discord.com/oauth2/authorize?{q}"


def settings_module():
    from muse_discord import muse_settings

    return muse_settings


def load_settings(config_path):
    """config.yaml through muse_settings, as the bot loads it. Raises
    muse_settings.SettingsError."""
    return settings_module().load(config_path)
