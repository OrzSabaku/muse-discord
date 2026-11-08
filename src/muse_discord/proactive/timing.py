"""Time conversion and quiet-hour calculations without runtime state."""

from datetime import UTC, datetime, timedelta


def iso(ts):
    if not ts:
        return None
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def snowflake_ts(mid):
    try:
        return ((int(mid) >> 22) + 1420070400000) / 1000.0
    except TypeError, ValueError:
        return 0.0


def zone(name):
    if not name or str(name).upper() == "UTC":
        return UTC
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(str(name))
    except Exception:
        return UTC


def parse_hours(text):
    """((start h, m), (end h, m)) of quiet hours, or None when off."""
    t = str(text or "").strip().lower()
    if t in ("", "off", "none"):
        return None
    try:
        a, b = t.split("-")
        sh, sm = (int(x) for x in a.strip().split(":"))
        eh, em = (int(x) for x in b.strip().split(":"))
    except ValueError:
        return None
    if (sh, sm) == (eh, em):
        return None
    return (sh, sm), (eh, em)


def in_quiet(ts, hours, tz):
    """True when ts falls in quiet hours (local time in tz)."""
    if hours is None:
        return False
    local = datetime.fromtimestamp(ts, tz)
    m = local.hour * 60 + local.minute
    s = hours[0][0] * 60 + hours[0][1]
    e = hours[1][0] * 60 + hours[1][1]
    return s <= m < e if s < e else (m >= s or m < e)


def quiet_end(ts, hours, tz):
    """When the quiet hours ts is in end (epoch seconds)."""
    local = datetime.fromtimestamp(ts, tz)
    end = local.replace(hour=hours[1][0], minute=hours[1][1], second=0, microsecond=0)
    if end <= local:
        end += timedelta(days=1)
    return end.timestamp()
