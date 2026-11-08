"""Anonymous report field allowlists, size limits and delivery defaults."""

import re

MAX_DISCORD_ID_LENGTH = 25


MIN_PRESET_CHANGES = 2


MIN_SERVER_HISTORY_KEY_PARTS = 4


MAX_LOGGED_ERRORS = 3


DAY_SECS = 86400


SCHEMA = 1


TELEMETRY_URL = "https://muse-telemetry.clono.workers.dev"


REPORT_PATH = "/v1/report"


ENV_SWITCH = "MUSE_DISCORD_TELEMETRY"  # "off" turns it off


ENV_URL = "MUSE_DISCORD_TELEMETRY_URL"  # overrides TELEMETRY_URL


SETTING = "telemetry.usage_stats"


KEEP_DAYS = 7  # unsent closed days kept at most


FLUSH_SECS = 300  # counters reach telemetry.json this often


TICK_SECS = 30  # tick() does its work at most this often


JITTER_SECS = 3 * 3600  # the daily send waits up to this into the day


TIMEOUT_SECS = 10  # per request


RETRY_SECS = 3600  # a failed send retries after this, doubling


NOTICE_AFTER_DAYS = 3  # unreachable this many days: tell the owners


SEND_NOW_FILE = "telemetry.send_now"  # next to telemetry.json


LATENCY_MAX_SECS = 3600  # longer latencies count as this


MAX_CRASH_NAMES = 20


MAX_WAKES_BYTES = 1 << 20  # a wakes file past this is just dropped


REFUSAL_CODES = (
    "replies-off",
    "dm-off",
    "bots-ignore",
    "not-allowed",
    "dm-not-allowed",
    "cap",
    "bot-exchange",
    "suspicious-ignored",
    "predates-join",
    "no-send",
    "internal",
    "worker-skipped",
    "send-failed",
    "never-share",
    "other",
)


SOURCES = ("settings", "dm", "cli")


PRESETS = ("friends", "community", "work")


COUNTERS = (
    "messages_seen",
    "queued",
    "replies_sent",
    "quarantined",
    "wakes",
    "media_sent",
    "restarts",
)


OS_FAMILIES = ("linux", "darwin", "windows", "freebsd", "openbsd")


FEATURES = {
    # name: allowed values (anything else is reported as "other")
    "server_replies": ("off", "when-addressed", "conversational"),
    "dm_replies": ("off", "when-addressed", "conversational"),
    "dms_from": ("owners", "shared-servers", "anyone", "list", "nobody"),
    "memory": ("everything", "public-only", "nothing"),
    "suspicious": ("hold-for-review", "ignore", "answer"),
    "bots": ("ignore", "when-addressed"),
    "length": ("brief", "normal", "detailed"),
    "daily_cap": ("on", "off"),
    "per_person_cap": ("on", "off"),
    "presence": ("on", "off"),
    "update_notices": ("on", "off"),
    "media": ("on", "off"),
}


_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}"
    r"-[0-9a-f]{12}$"
)


_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


_SEMVER_RE = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


_USERINFO_RE = re.compile(r"//[^/@\s]+@|[^\s/'\"@]+:[^\s'\"]*@")


NOTICE_TEXT = (
    "My anonymous usage stats haven't reached "
    "{host} for {days} days. Most likely Muse "
    "asked you to allow it and the request wasn't approved. If you're "
    'happy to share them, pick the "always" option next time Muse asks. '
    'To stop them instead, tell me "turn off usage stats".'
)
