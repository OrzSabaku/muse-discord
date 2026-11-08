"""Daily boundaries, consent switches and explicitly configured report endpoints."""

import os
import time
from datetime import UTC, datetime

from muse_discord.usage_stats.limits import _DAY_RE, ENV_SWITCH, ENV_URL, SETTING, TELEMETRY_URL


def utc_day(now=None):
    return datetime.fromtimestamp(time.time() if now is None else now, UTC).strftime("%Y-%m-%d")


def day_start(day):
    """The epoch second a UTC day ("YYYY-MM-DD") starts."""
    return datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC).timestamp()


def day_secs(day):
    """day_start for a valid "YYYY-MM-DD", else None."""
    try:
        return day_start(day) if _DAY_RE.match(str(day or "")) else None
    except ValueError:
        return None


def env_off(environ=None):
    v = (os.environ if environ is None else environ).get(ENV_SWITCH, "")
    return v.strip().lower() in ("off", "0", "false", "no", "disabled")


def endpoint(environ=None):
    """The report URL. MUSE_DISCORD_TELEMETRY_URL, when set at all,
    replaces TELEMETRY_URL; set to "" it means no endpoint (the tests
    rely on this, so they can never reach the real Worker)."""
    env = os.environ if environ is None else environ
    url = (env.get(ENV_URL, TELEMETRY_URL or "")).strip()
    return url.rstrip("/") if url.startswith(("https://", "http://")) else ""


def enabled(st, environ=None):
    """On unless the env var or the setting turns it off. st: a
    muse_settings.Settings (None: the setting's default, on)."""
    if env_off(environ):
        return False
    if st is None:
        return True
    try:
        return bool(st.top(SETTING))
    except Exception:
        return True
