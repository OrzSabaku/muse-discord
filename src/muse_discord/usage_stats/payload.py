"""Construct anonymous reports from explicit field and enum allowlists."""

import math

from muse_discord.usage_stats.limits import (
    _NAME_RE,
    _SEMVER_RE,
    COUNTERS,
    FEATURES,
    OS_FAMILIES,
    PRESETS,
    REFUSAL_CODES,
    SCHEMA,
    SOURCES,
)
from muse_discord.usage_stats.metadata import os_family, python_minor


def members_bucket(total):
    """Total members rounded down to a power of 2 (0 stays 0)."""
    try:
        n = int(total)
    except (TypeError, ValueError):
        return 0
    if n <= 0:
        return 0
    return 1 << math.floor(math.log2(n))


def percentile(hist, q):
    """The q-th percentile (0-1) of a {seconds: count} histogram, in
    whole seconds; None when it is empty."""
    pairs = sorted((int(k), int(v)) for k, v in (hist or {}).items() if int(v) > 0)
    total = sum(v for _k, v in pairs)
    if not total:
        return None
    rank = max(1, math.ceil(q * total))
    seen = 0
    for k, v in pairs:
        seen += v
        if seen >= rank:
            return k
    return pairs[-1][0]


def new_day(day):
    return {
        "day": day,
        **{c: 0 for c in COUNTERS},
        "refusals": {},
        "latency": {},
        "settings_changes": {},
        "presets": {},
        "crashes": {},
        "gauges": {},
    }


def _bump(d, key, n=1):
    d[key] = int(d.get(key, 0)) + n


def build_payload(rec, install_id):
    """The report for one closed day: every field, and nothing else (the
    Worker refuses anything it does not know)."""
    g = rec.get("gauges") or {}
    refusals = {
        k: int(v)
        for k, v in (rec.get("refusals") or {}).items()
        if k in REFUSAL_CODES and int(v) > 0
    }
    crashes = {
        k: int(v)
        for k, v in (rec.get("crashes") or {}).items()
        if _NAME_RE.match(str(k)) and int(v) > 0
    }
    lat = rec.get("latency") or {}
    version = str(g.get("version") or "unknown")
    release = bool(_SEMVER_RE.match(version))
    if not release and version != "unreleased":
        version = "unknown"
    os_ = g.get("os") or os_family()
    feats = {
        k: v
        for k, v in (g.get("features") or {}).items()
        if k in FEATURES and (v in FEATURES[k] or v == "other")
    }
    return {
        "schema": SCHEMA,
        "install_id": install_id,
        "day": rec["day"],
        "version": version,
        # Past the release, or edited: lets reports from main or a
        # customized copy be told apart from the release itself.
        "ahead": max(0, min(int(g.get("ahead") or 0), 100_000)) if release else 0,
        "modified": release and bool(g.get("modified")),
        "python": str(g.get("python") or python_minor())[:8],
        "os": os_ if os_ in OS_FAMILIES else "other",
        "servers": max(0, int(g.get("servers") or 0)),
        "members_bucket": members_bucket(g.get("members") or 0),
        "messages_seen": int(rec.get("messages_seen", 0)),
        "queued": int(rec.get("queued", 0)),
        "replies_sent": int(rec.get("replies_sent", 0)),
        "quarantined": int(rec.get("quarantined", 0)),
        "refusals": refusals,
        "wakes": int(rec.get("wakes", 0)),
        "latency_p50": percentile(lat, 0.5),
        "latency_p90": percentile(lat, 0.9),
        "settings_changes": {
            k: int(v)
            for k, v in (rec.get("settings_changes") or {}).items()
            if k in SOURCES and int(v) > 0
        },
        "presets_applied": {
            k: int(v) for k, v in (rec.get("presets") or {}).items() if k in PRESETS and int(v) > 0
        },
        "media_sent": int(rec.get("media_sent", 0)),
        "restarts": int(rec.get("restarts", 0)),
        "crashes": crashes,
        "features": feats,
    }
