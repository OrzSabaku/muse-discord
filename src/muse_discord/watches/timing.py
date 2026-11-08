"""Strict reminder timestamps and known IANA time zones."""

from datetime import UTC as UTC_ZONE
from datetime import datetime, timedelta

from muse_discord.watches.limits import MAX_TIME_STRING_LENGTH, TZ_RE, UTC_YEAR_2100


def parse_utc(value):
    """Epoch seconds for an ISO-8601 UTC time ("...Z" or "+00:00"), else
    None. Times in another offset, or with none, are refused: the
    worker converts, the protocol only takes UTC."""
    if not isinstance(value, str) or len(value) > MAX_TIME_STRING_LENGTH:
        return None
    v = value.strip()
    if v.endswith("Z") or v.endswith("z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None or dt.utcoffset() != timedelta(0):
            return None
        ts = dt.timestamp()
    except (ValueError, OverflowError, OSError, TypeError):
        return None
    # Only times the protocol could ever take (decide checks the range).
    return ts if 0 < ts < UTC_YEAR_2100 else None  # before 2100


def iso(ts):
    """Epoch seconds as ISO-8601 UTC, to the second ("...Z")."""
    return datetime.fromtimestamp(int(ts), UTC_ZONE).strftime("%Y-%m-%dT%H:%M:%SZ")


def known_tz(name):
    """True when the IANA time zone exists here (zoneinfo), or when it
    cannot be checked (no zoneinfo / tzdata): the name is still just a
    label the worker reads back."""
    if not isinstance(name, str) or not TZ_RE.fullmatch(name):
        return False
    try:
        import zoneinfo
    except ImportError:
        return True
    try:
        zoneinfo.ZoneInfo(name)
        return True
    except Exception:
        try:
            return not zoneinfo.available_timezones()
        except Exception:
            return True
