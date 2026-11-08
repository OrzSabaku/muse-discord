"""Count events and coordinate daily report delivery without blocking the monitor."""

import os
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Any

from muse_discord.usage_stats.contracts import SettingsMetricsSource
from muse_discord.usage_stats.limits import (
    _DAY_RE,
    _NAME_RE,
    COUNTERS,
    DAY_SECS,
    FLUSH_SECS,
    JITTER_SECS,
    KEEP_DAYS,
    LATENCY_MAX_SECS,
    MAX_CRASH_NAMES,
    MAX_LOGGED_ERRORS,
    NOTICE_AFTER_DAYS,
    NOTICE_TEXT,
    REFUSAL_CODES,
    REPORT_PATH,
    RETRY_SECS,
    SEND_NOW_FILE,
    TICK_SECS,
)
from muse_discord.usage_stats.metadata import (
    build_info,
    installed_version,
    is_release,
    os_family,
    python_minor,
)
from muse_discord.usage_stats.payload import _bump, build_payload, new_day
from muse_discord.usage_stats.policy import day_secs, day_start, enabled, endpoint, utc_day
from muse_discord.usage_stats.storage import (
    atomic_write_json,
    drain_wakes,
    load_file,
    read_install_id,
)
from muse_discord.usage_stats.transport import failure_reason, post_json


def _safe(fn):
    def wrapper(self, *a, **kw):
        try:
            return fn(self, *a, **kw)
        except Exception as e:
            self._errors += 1
            if self._errors <= MAX_LOGGED_ERRORS:
                print(f"Telemetry: {type(e).__name__} (ignored)", flush=True)
            return None

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


@dataclass
class Recorder:
    """The monitor's counters. paths() gives (telemetry.json, the install
    id file, the wakes file), read on every use so tests can move them.
    Every method is safe to call from any thread and never raises."""

    paths: Callable[[], tuple[str, str, str]]
    metrics: SettingsMetricsSource | None = None
    pkg_dir: str | None = None
    clock: Callable[[], float] = time.time
    post: Callable[[str, dict[str, Any]], int] = post_json
    environ: Mapping[str, str] | None = None

    def __post_init__(self):
        self.lock = threading.Lock()
        self.data: dict[str, Any] | None = None  # telemetry.json, loaded on first use
        self.version = None
        self.ahead, self.modified = 0, False
        self._errors = 0
        self._last_tick = 0.0
        self._last_flush = 0.0
        self._flushed_day = None
        self._hooked = False
        self._sending = None  # the send thread
        self._result: tuple[str, list[str], list[str], str | None, bool] | None = (
            None  # (day attempted, [sent days], [dropped])
        )

    # -- state -------------------------------------------------------------
    def _load(self):
        if self.data is not None:
            return self.data
        d = load_file(self.paths()[0])
        today = utc_day(self.clock())
        rec = d.get("today")
        if not isinstance(rec, dict) or not _DAY_RE.match(str(rec.get("day") or "")):
            rec = new_day(today)
        base = new_day(rec["day"])
        base.update(rec)
        d["today"] = base
        pend = d.get("pending")
        d["pending"] = (
            [p for p in pend if isinstance(p, dict) and _DAY_RE.match(str(p.get("day") or ""))]
            if isinstance(pend, list)
            else []
        )
        self.data = d
        self._roll(today)
        return d

    def _today(self):
        d = self._load()
        today = utc_day(self.clock())
        if d["today"]["day"] != today:
            self._roll(today)
        return d["today"]

    def _roll(self, today):
        """Close the stored day when it is not today: queued for sending
        unless telemetry was off at any point that day."""
        d = self.data
        if d is None:
            raise RuntimeError("telemetry must be loaded before rolling the day")
        rec = d["today"]
        if rec["day"] >= today:
            return
        if not rec.get("off"):
            d["pending"] = [p for p in d["pending"] if p["day"] != rec["day"]] + [rec]
        d["pending"] = sorted(d["pending"], key=lambda p: p["day"])
        d["pending"] = [
            p for p in d["pending"] if day_start(p["day"]) >= day_start(today) - KEEP_DAYS * 86400
        ][-KEEP_DAYS:]
        d["today"] = new_day(today)

    def flush(self):
        """Write telemetry.json now (atomically)."""
        with self.lock:
            self._flush_locked()

    def _flush_locked(self):
        if self.data is None:
            return
        atomic_write_json(self.paths()[0], self.data)
        self._last_flush = self.clock()

    # -- counting (any thread) ------------------------------------------------
    @_safe
    def start(self):
        """At monitor start: one restart, the version (found on a
        thread: git can be slow), and thread crashes counted."""
        with self.lock:
            _bump(self._today(), "restarts")
        threading.Thread(target=self._find_version, daemon=True, name="telemetry-version").start()
        if self._hooked:
            return
        self._hooked = True
        prev = threading.excepthook

        def hook(args):
            if args.exc_type is not None and not issubclass(
                args.exc_type, (SystemExit, KeyboardInterrupt)
            ):
                self.crashed(args.exc_type, flush=False)
            prev(args)

        threading.excepthook = hook

    def _find_version(self):
        try:
            self.version = installed_version(self.pkg_dir or ".")
            self.ahead, self.modified = build_info(self.pkg_dir or ".", self.version)
        except Exception:
            self.version = "unknown"

    @_safe
    def count(self, name, n=1):
        if name not in COUNTERS:
            return
        with self.lock:
            _bump(self._today(), name, n)

    @_safe
    def ledger(self, code, before=None):
        """A mention-ledger outcome (monitor.ledger_code) for one message
        or burst: replied, queued, quarantined, or why it was skipped.
        before: the burst's ledger entries before this write; when every
        one already had this outcome, it is not counted again."""
        if before and all(isinstance(e, dict) and e.get("code") == code for e in before):
            return
        with self.lock:
            rec = self._today()
            if code == "replied":
                _bump(rec, "replies_sent")
            elif code in ("queued", "quarantined"):
                _bump(rec, code)
            else:
                _bump(rec["refusals"], code if code in REFUSAL_CODES else "other")

    @_safe
    def latency(self, secs):
        """Seconds from a message to the reply that answered it."""
        s = min(LATENCY_MAX_SECS, max(0, round(float(secs))))
        with self.lock:
            _bump(self._today()["latency"], str(s))

    @_safe
    def crashed(self, exc, flush=True):
        """An exception that stopped the monitor or one of its threads:
        its class name only (never its message)."""
        if exc is None or isinstance(exc, (SystemExit, KeyboardInterrupt)):
            return
        cls = exc if isinstance(exc, type) else type(exc)
        if issubclass(cls, (SystemExit, KeyboardInterrupt)):
            return
        name = cls.__name__ if _NAME_RE.match(cls.__name__) else "Other"
        with self.lock:
            crashes = self._today()["crashes"]
            if name not in crashes and len(crashes) >= MAX_CRASH_NAMES - 1:
                name = "Other"
            _bump(crashes, name)
            if flush:
                self._flush_locked()

    # -- the main loop ----------------------------------------------------------
    @_safe
    def tick(self, settings=None, guilds=None, member_counts=None, force=False):
        """Main loop, every iteration (cheap: does its work at most every
        TICK_SECS). settings: a callable giving the current Settings;
        guilds: the servers the bot is in; member_counts: {id: count}."""
        now = self.clock()
        if not force and now - self._last_tick < TICK_SECS:
            return
        self._last_tick = now
        st = settings() if callable(settings) else settings
        on = enabled(st, self.environ)
        wakes = drain_wakes(self.paths()[2])
        with self.lock:
            d = self._load()
            rec = self._today()
            if wakes:
                _bump(rec, "wakes", wakes)
            if not on:
                rec["off"] = True
                d["pending"] = []  # days of an install that is off now
                d.pop("notice", None)
                d.pop("failing", None)
            if st is not None and self.metrics is not None:
                d["history_mark"] = self.metrics.collect(rec, st, d.get("history_mark"))
            counts = [v for v in (member_counts or {}).values() if isinstance(v, int) and v > 0]
            rec["gauges"] = {
                "servers": len(guilds or ()),
                "members": sum(counts),
                "features": self.metrics.features(st) if self.metrics is not None else {},
                "version": self.version or rec.get("gauges", {}).get("version"),
                "ahead": self.ahead,
                "modified": self.modified,
                "python": python_minor(),
                "os": os_family(),
            }
            self._take_result(d, now)
            forced = self._sending is None and self._take_send_now()
            if (
                on
                and self.released()
                and endpoint(self.environ)
                and d["pending"]
                and self._sending is None
                and (forced or self._send_due(d, now))
            ):
                self._start_send(d, now)
            if force or now - self._last_flush >= FLUSH_SECS or self._flushed_day != rec["day"]:
                self._flushed_day = rec["day"]
                self._flush_locked()

    def released(self):
        """Only a release (a vX.Y.Z tag) sends: a dev
        checkout, an unreleased main or an unknown version never does."""
        return is_release(self.version)

    def _send_due(self, d, now):
        """One attempt per UTC day, after a random time within the first
        JITTER_SECS of it (drawn once per day)."""
        today = utc_day(now)
        if d.get("last_attempt") == today:
            after = d.get("retry_after")  # set when today's send failed
            return isinstance(after, (int, float)) and now >= after
        sched = d.get("send_after")
        if not isinstance(sched, (int, float)) or utc_day(sched) != today:
            sched = day_start(today) + random.uniform(0, JITTER_SECS)
            d["send_after"] = sched
        return now >= sched

    def _start_send(self, d, now):
        tries = d.get("tries")
        d["tries"] = (
            max(0, tries) + 1
            if isinstance(tries, int) and d.get("last_attempt") == utc_day(now)
            else 1
        )
        d["last_attempt"] = utc_day(now)
        d.pop("retry_after", None)
        install_id = read_install_id(self.paths()[1])
        payloads = [build_payload(p, install_id) for p in d["pending"]]
        url = endpoint(self.environ) + REPORT_PATH
        attempt = utc_day(now)

        def run():
            sent, dropped, error, reached = [], [], None, False
            try:
                for p in payloads:
                    try:
                        status = self.post(url, p)
                    except Exception as e:
                        error = failure_reason(e)  # offline / timeout
                        break
                    reached = True
                    if HTTPStatus.OK <= status < HTTPStatus.MULTIPLE_CHOICES:
                        sent.append(p["day"])
                    elif (
                        HTTPStatus.BAD_REQUEST <= status < HTTPStatus.INTERNAL_SERVER_ERROR
                        and status != HTTPStatus.TOO_MANY_REQUESTS
                    ):
                        dropped.append(p["day"])  # refused: never again
                    else:
                        error = f"HTTP {status}"
                        break
            finally:
                with self.lock:
                    self._result = (attempt, sent, dropped, error, reached)

        t = threading.Thread(target=run, daemon=True, name="telemetry-send")
        self._sending = t
        t.start()

    def _take_result(self, d, now):
        if self._result is None:
            return
        attempt, sent, dropped, error, reached = self._result
        self._result = None
        self._sending = None
        gone = set(sent) | set(dropped)
        d["pending"] = [p for p in d["pending"] if p["day"] not in gone]
        d["last_send"] = {
            "day": attempt,
            "sent": len(sent),
            "refused": len(dropped),
            "ok": not d["pending"],
        }
        if reached:
            d.pop("failing", None)
            d.pop("notice", None)
        if d["pending"] and error:
            d["last_send"]["error"] = error
            tries = d.get("tries") if isinstance(d.get("tries"), int) else 1
            d["retry_after"] = now + RETRY_SECS * 2 ** min(max(tries, 1) - 1, 5)
            at = datetime.fromtimestamp(d["retry_after"], UTC)
            print(
                f"Telemetry: send failed ({error}); trying again after {at:%Y-%m-%d %H:%M} UTC.",
                flush=True,
            )
        if d["pending"] and error and not reached:
            # Unreachable on consecutive days: {since, last} (days).
            f = d.get("failing") if isinstance(d.get("failing"), dict) else {}
            since, last = day_secs(f.get("since")), day_secs(f.get("last"))
            day = day_start(attempt)
            if since is None or last is None or day - last > DAY_SECS or last < since:
                since = day
            d["failing"] = {"since": utc_day(since), "last": attempt}
            if day - since >= (NOTICE_AFTER_DAYS - 1) * 86400 and d.get("notice") is None:
                d["notice"] = "due"
        self._flush_locked()

    @_safe
    def take_notice(self, settings=None):
        """The owner DM about an unreachable endpoint, once (else None):
        the monitor sends it. settings: as for tick."""
        st = settings() if callable(settings) else settings
        with self.lock:
            d = self._load()
            if d.get("notice") != "due" or not enabled(st, self.environ):
                return None
            d["notice"] = "sent"
            self._flush_locked()
        host = urllib.parse.urlsplit(endpoint(self.environ)).hostname
        return NOTICE_TEXT.format(host=host or "the stats server", days=NOTICE_AFTER_DAYS)

    def _take_send_now(self):
        """True (once) when `bin/telemetry.py send` asked for a send."""
        try:
            os.unlink(os.path.join(os.path.dirname(self.paths()[0]), SEND_NOW_FILE))
            return True
        except OSError:
            return False
