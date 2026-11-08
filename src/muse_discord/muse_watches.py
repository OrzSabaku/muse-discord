"""muse-discord reminders and watches.

One concept for people ("reminders and watches": one list, one cancel),
three kinds inside:

    remind      at a time, or after a delay
    watch       a Discord watch: words or phrases in the messages the
                monitor already reads (gateway and poll)
    watch-web   a web watch: an outside-world condition ("the venue
                announces the date") a checker session of the owner's
                Muse checks on the web, every so often (see "Web
                watches" below; owners only)

This module is the data model, the outbox action's shape and meaning,
the caps, the matching and the store. It makes no Discord calls:
monitor.py does the I/O (who can read which channel, the queue, the
sends) and calls in here; bin/watches.py (the owner's CLI) uses the
Store. Pydantic validates the incoming protocol.

Security properties (docs/PROTOCOL.md, Reminders and watches):
- The requester is always the author of the queue item the action
  answers, from the monitor's own record of it, never a field the
  worker writes; the place is that item's channel. A reminder can only
  come back to that person, in that place.
- Watches only ever match in channels the requester can read (the
  monitor computes it from roles and overwrites, again at match time),
  never the requester's own messages or the bot's, at most once per
  watch per DEBOUNCE_SECS, and link the message instead of quoting it.
- Matching is case-insensitive literal words and phrases (escaped,
  bounded): no user-supplied regular expressions.
- A web watch's condition is stored as data and handed to the checker
  only inside a delimited data block; a check result counts only with
  that check's one-time nonce (128 bits, compared in constant time,
  spent on use, expiring after WEB_CHECK_SECS) for a watch still
  active, and checks are budgeted per day.
"""

import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC as UTC_ZONE
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    ValidationError,
    field_validator,
    model_validator,
)

MAX_TIME_STRING_LENGTH = 40
UTC_YEAR_2100 = 4102444800
MAX_COMPILED_PATTERNS = 2000
FIRST_PRINTABLE_CHARACTER = 32

VERSION = 1
KINDS = ("remind", "watch")  # an `add`'s kind (a web watch is a
# watch with "check": "web")

# Limits (docs/PROTOCOL.md, Reminders and watches).
REMIND_MIN_SECS = 60  # at least a minute away
REMIND_MAX_SECS = 366 * 86400  # at most a year away
AFTER_MAX = 100 * REMIND_MAX_SECS  # a sane number (decide refuses
# past a year, with its line)
WATCH_DEFAULT_SECS = 7 * 86400  # a watch runs a week by default
WATCH_MAX_SECS = 30 * 86400  # and 30 days at most
WATCH_MIN_SECS = 60
# Owners have no limits. Everyone else: one silent ceiling across all
# kinds (a backstop against trolls and bad parses, not a setting), and
# each fire counts as a reply toward limits.* where it fires (monitor).
PER_PERSON_MAX = 25
TOTAL_MAX = 2000  # everyone but the owners, a bound on the file
HELD_RETRY_SECS = 600  # a fire held by a reply cap is tried this often
HELD_KEEP_SECS = 7 * 86400
LATE_SECS = 3600  # a reminder this overdue fires once, marked late
DEBOUNCE_SECS = 15 * 60  # one notification per watch per 15 minutes
FIRE_KEEP_SECS = 24 * 3600  # a fire nobody answers is dropped after this
DONE_KEEP_SECS = 48 * 3600  # schedule outcomes, so one is applied once
FIRED_KEEP = 500
NOTE_MAX = 300
WORDS_MAX = 5
WORD_MIN, WORD_MAX = 2, 60
CHANGES_MAX = 5
LIST_MAX = 25  # items listed on a queue item
TZ_MAX_USERS = 5000
# Web watches.
WEB_EVERY_MIN = 1800  # checked at most every 30 minutes
WEB_EVERY_MAX = 7 * 86400
WEB_TOTAL_PER_DAY = 500  # checks a day in all: a safety budget,
# checked when issuing (web watches are
# the owners' only)
WEB_CHECK_SECS = 15 * 60  # a check's nonce lives this long
WEB_MAX_FAILS = 5  # unknown / no report in a row: it ends
WEB_REPEAT_SECS = 6 * 3600  # "every time": quiet this long after one
WEB_BACKOFF_MAX = 86400
WHAT_MAX = SUMMARY_MAX = 300
RESULTS = ("met", "not-met", "unknown", "refused")

TZ_RE = re.compile(r"(UTC|[A-Za-z_]{1,32}(/[A-Za-z0-9_+\-]{1,32}){1,2})")
PLACE_RE = re.compile(
    r"here|server|server:([1-9][0-9]{0,19})"
    r"|channel:([1-9][0-9]{0,19}):([1-9][0-9]{0,19})"
)
NOT_ON_SCHEDULE_ITEMS = ("silent", "settings", "attachments")
_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"

# The monitor's own reply when it does not do what the worker asked:
# all or nothing, so each says nothing changed.
LINES = {
    "not-allowed": "Only my owners can set reminders and watches here, so nothing changed.",
    "bot": "I don't set reminders or watches for bots.",
    "cap-person": "You have {cap} reminders and watches going; cancel one first.",
    "cap-total": "I can't set that: I'm keeping as many reminders and watches as I can right now.",
    "time": "I couldn't set that: a reminder has to be between a minute "
    "and a year away, so nothing changed.",
    "watch-time": "I couldn't set that: a watch can run for 30 days at most, so nothing changed.",
    "place": "I couldn't set that watch: {why} Nothing changed.",
    "cancel": "I couldn't find that among your reminders and watches, so nothing changed.",
    "tz": "I don't know that time zone, so nothing changed.",
    "channel": "I can only set reminders and watches where you asked, so nothing changed.",
    "unknown": "I couldn't set that, so nothing changed.",
    "web-not-allowed": "Only my owners can set web watches, and only in a "
    "DM with me, so nothing changed.",
    "web-inputs": "I can't set a web watch from a message with an "
    "attachment or a reply to someone else's, so nothing "
    "changed. Ask me again in a message of your own.",
}
# Ledger reasons: monitor.ledger_code maps the prefixes to codes.
REASON_CAP = "reminders: limit reached"  # -> watch-cap
REASON_NOT_ALLOWED = "reminders: not allowed"  # -> watch-not-allowed


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------
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
    except ValueError, OverflowError, OSError, TypeError:
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


# ---------------------------------------------------------------------------
# The outbox action's shape (bin/outbox-append.py and the monitor)
# ---------------------------------------------------------------------------
# Pydantic validates the wire format; decide() still owns authorization,
# dates relative to now, quotas, and whether the requested place is allowed.
class _Shape(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _utc(value):
    if parse_utc(value) is None:
        raise ValueError("expected an ISO-8601 UTC time before 2100")
    return value


def _word(value):
    if "\n" in value or not WORD_MIN <= len(value.strip()) <= WORD_MAX:
        raise ValueError(f"expected one line of {WORD_MIN} to {WORD_MAX} characters")
    return value


def _true(value):
    if value is not True:
        raise ValueError("must be true")
    return value


UTC = Annotated[str, AfterValidator(_utc)]
ItemId = Annotated[str, Field(pattern=r"^[rw][a-z0-9]{5}$")]
Note = Annotated[str, Field(max_length=NOTE_MAX)]


class _Add(_Shape):
    op: Literal["add"]
    note: Note | None = None


class _Reminder(_Add):
    kind: Literal["remind"]
    at: UTC | None = None
    after: Annotated[int, Field(gt=0, le=AFTER_MAX)] | None = None

    @model_validator(mode="after")
    def one_time(self):
        if ("at" in self.model_fields_set) == ("after" in self.model_fields_set):
            raise ValueError("a reminder takes exactly one of at or after")
        if self.at is None and self.after is None:
            raise ValueError("at or after cannot be null")
        return self


class _Match(_Shape):
    words: Annotated[
        list[Annotated[str, AfterValidator(_word)]], Field(min_length=1, max_length=WORDS_MAX)
    ]
    in_: Annotated[str, Field(alias="in", pattern="^(?:" + PLACE_RE.pattern + ")$")]
    every: bool = False


class _Until(_Add):
    until: UTC | None = None

    @field_validator("until", mode="before")
    @classmethod
    def no_null(cls, value):
        if value is None:
            raise ValueError("until cannot be null")
        return value


class _DiscordWatch(_Until):
    kind: Literal["watch"]
    match: _Match


class _WebWatch(_Until):
    kind: Literal["watch"]
    check: Literal["web"]
    what: Annotated[str, Field(min_length=1, max_length=WHAT_MAX)]
    every: Annotated[int, Field(ge=WEB_EVERY_MIN, le=WEB_EVERY_MAX)]
    repeat: bool = False

    @field_validator("what")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("what must not be blank")
        return value


class _CancelId(_Shape):
    op: Literal["cancel"]
    id: ItemId


class _CancelAll(_Shape):
    op: Literal["cancel"]
    all: Annotated[bool, AfterValidator(_true)]


class _Timezone(_Shape):
    op: Literal["tz"]
    tz: Annotated[str, Field(pattern="^(?:" + TZ_RE.pattern + ")$")] | None


def _action_tag(value):
    if not isinstance(value, dict):
        return None
    op = value.get("op")
    if not isinstance(op, str):
        return None
    if op == "add":
        kind = value.get("kind")
        return "web" if value.get("check") is not None else kind if isinstance(kind, str) else None
    if op == "cancel":
        return "cancel-id" if "id" in value else "cancel-all"
    return op


_Action = Annotated[
    Annotated[_Reminder, Tag("remind")]
    | Annotated[_DiscordWatch, Tag("watch")]
    | Annotated[_WebWatch, Tag("web")]
    | Annotated[_CancelId, Tag("cancel-id")]
    | Annotated[_CancelAll, Tag("cancel-all")]
    | Annotated[_Timezone, Tag("tz")],
    Discriminator(_action_tag),
]


class _Schedule(_Shape):
    v: Annotated[int, Field(ge=1, le=VERSION)]
    changes: Annotated[list[_Action], Field(min_length=1, max_length=CHANGES_MAX)]


class _ScheduledReply(_Shape):
    model_config = ConfigDict(extra="allow", strict=True)
    item: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,19}$")]
    schedule: _Schedule

    @model_validator(mode="after")
    def no_other_actions(self):
        bad = set(self.model_extra or {}) & set(NOT_ON_SCHEDULE_ITEMS)
        if bad:
            raise ValueError("a schedule item carries no " + ", ".join(sorted(bad)))
        return self


def _validation_problems(model, value):
    try:
        model.model_validate(value)
    except ValidationError as exc:
        out = []
        for error in exc.errors(include_url=False, include_input=False):
            loc, message = error["loc"], error["msg"]
            if error["type"].startswith("union_tag"):
                message = "expected an add (remind/watch/web), cancel, or tz action"
            if error["type"] == "extra_forbidden":
                loc = loc[:-1]  # unknown field names can contain secrets too
            out.append(f"{'.'.join(map(str, loc)) or 'schedule'}: {message}")
        return out
    return []


def shape_problems(item):
    """Validate only schedule items; ordinary reply validation is the monitor's."""
    return (
        _validation_problems(_ScheduledReply, item)
        if isinstance(item, dict) and "schedule" in item
        else []
    )


# ---------------------------------------------------------------------------
# The store: watches.json, durable, monitor-internal (and bin/watches.py)
# ---------------------------------------------------------------------------
def empty():
    return {
        "v": VERSION,
        "items": {},
        "fires": {},
        "done": {},
        "tz": {},
        "fired": {},
        "web": {},
        "held": {},
    }


def _clean(data):
    out = empty()
    if isinstance(data, dict):
        for k in out:
            if isinstance(data.get(k), dict):
                out[k] = data[k]
    return out


class Store:
    """watches.json. Reads are cached on the file's mtime and size (the
    monitor peeks at it every tick); every change is a read-modify-write
    under an flock on watches.lock, written atomically, so the owner's
    CLI and the monitor never lose each other's edits."""

    def __init__(self, path):
        self.path = path
        self.lock_path = os.path.join(os.path.dirname(path) or ".", "watches.lock")
        self._sig = None
        self._data = None

    def _stat(self):
        try:
            st = os.stat(self.path)
            return (st.st_mtime_ns, st.st_size, st.st_ino)
        except OSError:
            return None

    def _load(self):
        try:
            with open(self.path) as f:
                return _clean(json.load(f))
        except FileNotFoundError:
            return empty()
        except (OSError, ValueError) as e:
            # A damaged file is kept aside, never silently emptied.
            bad = f"{self.path}.bad-{int(time.time())}"
            with contextlib.suppress(OSError):
                os.replace(self.path, bad)
            print(f"watches.json unreadable ({e}); moved to {os.path.basename(bad)}", flush=True)
            return empty()

    def read(self):
        """The current data (do not modify it: use edit())."""
        sig = self._stat()
        if self._data is None or sig != self._sig:
            self._data = self._load()
            self._sig = sig
        return self._data

    @contextlib.contextmanager
    def edit(self):
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            data = self._load()
            before = json.dumps(data, sort_keys=True)
            yield data
            if json.dumps(data, sort_keys=True) != before:
                self._write(data)
            self._data = data
            self._sig = self._stat()
        finally:
            os.close(fd)

    def _write(self, data):
        d = os.path.dirname(self.path) or "."
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".watches.json.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                os.fchmod(f.fileno(), 0o600)
                json.dump(data, f)
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise


def new_id(data, kind):
    prefix = "r" if kind == "remind" else "w"
    while True:
        i = prefix + "".join(secrets.choice(_ALPHABET) for _ in range(5))
        if i not in data["items"]:
            return i


def active(data, now=None):
    """The live items (not past their expiry)."""
    now = time.time() if now is None else now
    return {
        i: r
        for i, r in data["items"].items()
        if isinstance(r, dict) and (r.get("kind") == "remind" or r.get("until", 0) > now)
    }


# ---------------------------------------------------------------------------
# Deciding a schedule action (all or nothing)
# ---------------------------------------------------------------------------
class Refused(Exception):
    def __init__(self, line, reason=None, detail=None):
        super().__init__(line)
        self.line = line
        self.reason = reason  # a ledger reason, or None (replied)
        self.detail = detail


@dataclass
class Request:
    """Monitor-authored identity and permissions for one schedule request."""

    requester: str
    name: str
    owner: bool
    guild_id: str | None
    channel_id: str
    dm: bool
    message_id: str
    message_time: float
    can_set: bool
    where: str
    resolve: Callable[[str], tuple[dict[str, Any], str]]
    can_web: bool = False

    def __post_init__(self):
        self.requester = str(self.requester)
        self.channel_id = str(self.channel_id)
        self.message_id = str(self.message_id)
        self.owner = bool(self.owner)
        self.dm = bool(self.dm)

    def new_record(self, c, now):
        kind = "watch-web" if c.get("check") == "web" else c["kind"]
        rec = {
            "kind": kind,
            "requester": self.requester,
            "name": self.name,
            "guild_id": None if self.dm else self.guild_id,
            "channel_id": self.channel_id,
            "dm": self.dm,
            "message_id": self.message_id,
            "created": now,
            "where": self.where,
            "note": (c.get("note") or "").strip()[:NOTE_MAX],
        }
        if kind == "remind":
            fields = self.reminder_fields(c, now)
        elif kind == "watch-web":
            fields = self.web_fields(c, now)
        else:
            fields = self.watch_fields(c, now)
        rec.update(fields)
        return rec

    def reminder_fields(self, c, now):
        due = parse_utc(c["at"]) if "at" in c else self.message_time + c["after"]
        if due is None:
            raise Refused(LINES["time"])
        delay = due - self.message_time
        if delay < REMIND_MIN_SECS or delay > REMIND_MAX_SECS or due - now > REMIND_MAX_SECS:
            raise Refused(LINES["time"])
        return {"due": due}

    def web_fields(self, c, now):
        until = parse_utc(c["until"]) if "until" in c else now + WATCH_DEFAULT_SECS
        if until is None or until - now < WATCH_MIN_SECS or until - now > WATCH_MAX_SECS:
            raise Refused(LINES["watch-time"])
        return {
            "until": until,
            "what": " ".join(c["what"].split()),
            "every": c["every"],
            "repeat": bool(c.get("repeat")),
            "owner": self.owner,
            "next_check": now,
            "check": None,
            "fails": 0,
            "checks": 0,
        }

    def watch_fields(self, c, now):
        m = c["match"]
        until = parse_utc(c["until"]) if "until" in c else now + WATCH_DEFAULT_SECS
        if until is None or until - now < WATCH_MIN_SECS or until - now > WATCH_MAX_SECS:
            raise Refused(LINES["watch-time"])
        scope, scope_name = self.resolve(m["in"])
        return {
            "until": until,
            "scope": scope,
            "scope_name": scope_name,
            "words": [w.strip() for w in m["words"]],
            "every": bool(m.get("every")),
            "last_fired": 0,
            "fired": 0,
        }

    def cancel_ids(self, items, live, change):
        # Web watches can only be changed from the requesting owner's DM.
        def permitted(record):
            return self.can_web or record.get("kind") != "watch-web"

        if change.get("all"):
            return [
                i
                for i, record in items.items()
                if record.get("requester") == self.requester and i in live and permitted(record)
            ]
        record = items.get(change["id"])
        if (
            record is None
            or change["id"] not in live
            or not permitted(record)
            or not (self.owner or record.get("requester") == self.requester)
        ):
            raise Refused(LINES["cancel"])
        return [change["id"]]

    def set_timezone(self, tz, change):
        if change["tz"] is None:
            tz.pop(self.requester, None)
        elif not known_tz(change["tz"]):
            raise Refused(LINES["tz"])
        else:
            tz[self.requester] = change["tz"]


def _check_schedule_caps(req, live, kind):
    if not req.owner:
        mine = sum(1 for r in live.values() if r.get("requester") == req.requester)
        if mine >= PER_PERSON_MAX:
            raise Refused(
                LINES["cap-person"].format(cap=PER_PERSON_MAX),
                REASON_CAP,
                {"kind": kind, "cap": PER_PERSON_MAX, "per": "person"},
            )
        if len(live) >= TOTAL_MAX:
            raise Refused(
                LINES["cap-total"], REASON_CAP, {"kind": kind, "cap": TOTAL_MAX, "per": "total"}
            )


def decide(data, req, changes, now=None):
    """Apply changes (a valid schedule.changes list) for req to data,
    all or nothing. Returns a list of what was done ([(op, id)]); raises
    Refused, leaving data untouched."""
    now = time.time() if now is None else now
    adds = [c for c in changes if c.get("op") == "add"]
    if not req.can_set and adds:
        raise Refused(LINES["not-allowed"], REASON_NOT_ALLOWED)
    if not (req.can_web and req.owner) and any(c.get("check") == "web" for c in adds):
        raise Refused(LINES["web-not-allowed"], REASON_NOT_ALLOWED, {"kind": "watch-web"})
    items = dict(data["items"])
    live = active(data, now)
    tz = dict(data["tz"])
    done = []
    for c in changes:
        op = c["op"]
        if op == "tz":
            req.set_timezone(tz, c)
            done.append(("tz", c["tz"]))
            continue
        if op == "cancel":
            ids = req.cancel_ids(items, live, c)
            for i in ids:
                items.pop(i, None)
                live.pop(i, None)
                done.append(("cancel", i))
            continue
        rec = req.new_record(c, now)
        kind = rec["kind"]
        _check_schedule_caps(req, live, kind)
        rid = new_id({"items": items}, kind)
        items[rid] = rec
        live[rid] = rec
        done.append(("add", rid))
    data["items"] = items
    if len(tz) > TZ_MAX_USERS:
        for k in list(tz)[: len(tz) - TZ_MAX_USERS]:
            del tz[k]
    data["tz"] = tz
    return done


def remember_done(data, qid, outcome, now=None):
    """Record a schedule outcome for queue item qid (applied once, ever:
    a retried send or a resend after a restart reads it back)."""
    now = time.time() if now is None else now
    d = data["done"]
    d[str(qid)] = dict(outcome, at=now)
    for k in [k for k, v in d.items() if now - (v or {}).get("at", 0) > DONE_KEEP_SECS]:
        del d[k]
    for k in list(d)[:-500]:
        del d[k]


# ---------------------------------------------------------------------------
# What a queue item carries: the requester's own list
# ---------------------------------------------------------------------------
def _entry(i, r, show_for=False):
    """One listed item. show_for: someone else's, listed in an owner's
    DM: who and when, but not its note or words, which that person's
    request chose (text from others never reaches an owner-DM wake)."""
    e = {"id": i, "kind": r.get("kind"), "set_in": r.get("where")}
    if not show_for:
        e["note"] = r.get("note") or ""
    if r.get("kind") == "remind":
        e["due"] = iso(r.get("due", 0))
    elif r.get("kind") == "watch-web":
        e.update(
            {
                "web": True,
                "every_secs": r.get("every"),
                "repeat": bool(r.get("repeat")),
                "until": iso(r.get("until", 0)),
            }
        )
        if not show_for:
            e["what"] = r.get("what")
    else:
        e.update(
            {
                "in": r.get("scope_name"),
                "every": bool(r.get("every")),
                "until": iso(r.get("until", 0)),
            }
        )
        if not show_for:
            e["words"] = r.get("words")
    if show_for:
        e["for"] = r.get("name") or r.get("requester")
        e["for_id"] = r.get("requester")
    return e


@dataclass
class CardContext:
    guild_id: str | None = None
    dm: bool = False
    owner_dm: bool = False
    can_set: bool = True
    can_web: bool = False


def card(data, uid, now=None, context=None):
    """The `watches` field of a queue item: the author's reminders and
    watches that may be shown where they asked (all of them in a DM,
    those set in this server in a server, with a count of the rest),
    their time zone if they told it, and whether they may set new ones
    (can_set; can_web for web watches).
    In an owner's own DM: everyone's, marked with whose."""
    context = context or CardContext()
    guild_id = context.guild_id
    dm = context.dm
    owner_dm = context.owner_dm
    can_set = context.can_set
    can_web = context.can_web
    now = time.time() if now is None else now
    uid = str(uid)
    live = sorted(
        active(data, now).items(), key=lambda kv: kv[1].get("due") or kv[1].get("until", 0)
    )
    if owner_dm:
        shown = [_entry(i, r, show_for=r.get("requester") != uid) for i, r in live]
        hidden = 0
    else:
        mine = [(i, r) for i, r in live if r.get("requester") == uid]
        ok = [(i, r) for i, r in mine if dm or (not r.get("dm") and r.get("guild_id") == guild_id)]
        shown = [_entry(i, r) for i, r in ok]
        hidden = len(mine) - len(ok)
    out = {
        "can_set": bool(can_set),
        "can_web": bool(can_set and can_web),
        "items": shown[:LIST_MAX],
    }
    if hidden:
        out["elsewhere"] = hidden  # theirs, set in other places
    if len(shown) > LIST_MAX:
        out["more"] = len(shown) - LIST_MAX
    if data["tz"].get(uid):
        out["tz"] = data["tz"][uid]
    return out


# ---------------------------------------------------------------------------
# Due reminders, matches, fires
# ---------------------------------------------------------------------------
def due_reminders(data, now=None):
    now = time.time() if now is None else now
    return [
        (i, r)
        for i, r in data["items"].items()
        if isinstance(r, dict) and r.get("kind") == "remind" and r.get("due", 0) <= now
    ]


def expired_watches(data, now=None):
    now = time.time() if now is None else now
    return [
        i
        for i, r in data["items"].items()
        if isinstance(r, dict) and r.get("kind") != "remind" and r.get("until", 0) <= now
    ]


def next_due(data):
    """When the next reminder or expiry is due (epoch), or None."""
    ts = [
        r.get("due") if r.get("kind") == "remind" else r.get("until")
        for r in data["items"].values()
        if isinstance(r, dict)
    ]
    ts = [t for t in ts if isinstance(t, (int, float))]
    return min(ts) if ts else None


_PATTERNS = {}


def matcher(words):
    """A compiled case-insensitive matcher for a watch's words: escaped
    literals between non-word boundaries (no user regex; linear time)."""
    key = tuple(words)
    pat = _PATTERNS.get(key)
    if pat is None:
        alts = "|".join(re.escape(w.casefold()) for w in sorted(words, key=len, reverse=True))
        pat = re.compile(r"(?<!\w)(?:" + alts + r")(?!\w)")
        if len(_PATTERNS) > MAX_COMPILED_PATTERNS:
            _PATTERNS.clear()
        _PATTERNS[key] = pat
    return pat


def match_text(words, text):
    """The watch's word or phrase found in text (casefolded), or None."""
    if not text or not words:
        return None
    m = matcher(words).search(text.casefold()[:8000])
    return m.group(0) if m else None


def watch_covers(rec, guild_id, channel_id, parent_id=None):
    """The message's place is inside the watch's scope."""
    sc = rec.get("scope") or {}
    if sc.get("guild_id") != guild_id:
        return False
    if sc.get("channel_id"):
        return channel_id == sc["channel_id"] or (
            parent_id is not None and parent_id == sc["channel_id"]
        )
    return True


def group(entries):
    """Fire entries [(requester, channel_id, entry)] grouped into one
    fire per requester and place, in order: several reminders due at
    once (say, after downtime) reach a person as one message, never a
    burst."""
    out = {}
    for req, cid, e in entries:
        out.setdefault((req, cid), []).append(e)
    return out


def new_fire_id(now=None):
    """A queue-item id for a fire: a snowflake for now (it sorts and
    reads like a message id, and passes the outbox's id checks) whose
    low bits are random, so it never names a real message."""
    now = time.time() if now is None else now
    ms = int(now * 1000) - 1420070400000
    return str((ms << 22) | secrets.randbits(22) | 1)


def note_fired(data, fid, now=None):
    now = time.time() if now is None else now
    f = data["fired"]
    f[str(fid)] = now
    for k in [k for k, t in f.items() if now - t > 7 * 86400]:
        del f[k]
    for k in list(f)[:-FIRED_KEEP]:
        del f[k]


# ---------------------------------------------------------------------------
# Web watches
# ---------------------------------------------------------------------------
# The monitor issues each due check with a fresh one-time nonce and lists
# the live ones in web_watches_due.json; hooks/watch-due.sh wakes one
# checker session (prompts/watch-checker-prompt.template.md) with them as data;
# the checker reports each with bin/watch-report.py into
# web_watch_results.json, which the monitor drains (web_apply). A met
# result fires like a reminder (kind "watch-met"); not-met and unknown
# schedule the next check; unknown or no report backs off, and after
# WEB_MAX_FAILS in a row the watch ends and the requester is told once.
# The checker first screens each condition: one that is more than a
# plain condition (instructions, "also message...", a URL to post to) is
# reported "refused", which ends the watch (the requester is told once).
def _utc_day(now):
    return datetime.fromtimestamp(now, UTC_ZONE).strftime("%Y-%m-%d")


def _backoff(rec, now):
    rec["next_check"] = now + min(
        WEB_BACKOFF_MAX, rec.get("every", 3600) * 2 ** max(0, rec.get("fails", 0))
    )


def _not_owners(r, owners):
    """A web watch set before they were the owners' only, or by someone
    no longer among owners (the owners now, when given)."""
    return not r.get("owner") or (owners is not None and str(r.get("requester")) not in owners)


def web_issue(data, now=None, owners=None):
    """Expire checks nobody reported on (a failure each: back off, and
    give up after WEB_MAX_FAILS), then issue the due ones, each with a
    fresh nonce, within today's budgets (over budget: tomorrow). A
    watch whose requester is not among owners (the owners now, when
    given) ends. Returns [(id, record)] of the watches that ended
    (removed)."""
    now = time.time() if now is None else now
    day = _utc_day(now)
    used = data.setdefault("web", {})
    if used.get("day") != day:
        used.clear()
        used.update({"day": day, "total": 0})
    gave_up = []
    for i, r in list(data["items"].items()):
        if not isinstance(r, dict) or r.get("kind") != "watch-web" or r.get("until", 0) <= now:
            continue
        if _not_owners(r, owners):
            # It ends, its check withdrawn (the requester is told, as for
            # one that gave up).
            gave_up.append((i, data["items"].pop(i)))
            continue
        chk = r.get("check")
        if chk and chk.get("expires", 0) <= now:
            r["check"] = None
            r["fails"] = r.get("fails", 0) + 1
            if r["fails"] >= WEB_MAX_FAILS:
                gave_up.append((i, data["items"].pop(i)))
                continue
            _backoff(r, now)
            continue
        if chk or r.get("next_check", 0) > now:
            continue
        if used["total"] >= WEB_TOTAL_PER_DAY:
            tomorrow = (
                datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC_ZONE).timestamp() + 86400
            )
            r["next_check"] = tomorrow + secrets.randbelow(600)
            continue
        used["total"] += 1
        r["check"] = {
            "nonce": secrets.token_hex(16),
            "issued": now,
            "expires": now + WEB_CHECK_SECS,
        }
        r["checks"] = r.get("checks", 0) + 1
    return gave_up


def web_due(data, now=None):
    """What web_watches_due.json lists: every live check, as data for the
    checker (the condition, the id, the nonce, when it expires), the
    hook's lease key, and `group`, an opaque tag per requester: the hook
    never hands two requesters' checks (and nonces) to one checker run,
    so one person's page cannot report on another's watch."""
    now = time.time() if now is None else now
    out = []
    for i, r in sorted(data["items"].items()):
        chk = r.get("check") if isinstance(r, dict) else None
        if r.get("kind") == "watch-web" and chk and chk.get("expires", 0) > now:
            out.append(
                {
                    "id": i,
                    "key": f"{i}:{int(chk['issued'])}",
                    "group": hashlib.sha256(str(r.get("requester")).encode()).hexdigest()[:16],
                    "nonce": chk["nonce"],
                    "what": r.get("what"),
                    "expires": iso(chk["expires"]),
                }
            )
    return out


class _Report(_Shape):
    id: ItemId
    nonce: Annotated[str, Field(pattern=r"^[0-9a-f]{32,64}$")]
    result: Literal["met", "not-met", "unknown", "refused"]
    summary: Annotated[str, Field(max_length=SUMMARY_MAX)]
    at: Annotated[float, Field(ge=0)] | None = None  # local report-tool timestamp

    @field_validator("at", mode="before")
    @classmethod
    def no_null_at(cls, value):
        if value is None:
            raise ValueError("at cannot be null")
        return value

    @field_validator("summary")
    @classmethod
    def plain_text(cls, value):
        if any(ord(ch) < FIRST_PRINTABLE_CHARACTER and ch != "\n" for ch in value):
            raise ValueError("summary must be plain text")
        return value


def report_problems(rep):
    return _validation_problems(_Report, rep)


def _web_report_problem(r, chk, now, owners, nonce):
    if not r or r.get("kind") != "watch-web" or r.get("until", 0) <= now:
        return "no such active web watch"
    if _not_owners(r, owners):
        return "its requester is not an owner"
    if not chk or chk.get("expires", 0) <= now:
        return "no live check (expired or already reported)"
    if not hmac.compare_digest(str(chk.get("nonce")).encode(), nonce.encode()):
        return "wrong nonce"
    return None


def _apply_web_result(data, i, r, result, now):
    if result == "met":
        r["fails"] = 0
        r["last_met"] = now
        if not r.get("repeat"):
            del data["items"][i]
        else:
            r["next_check"] = now + max(r.get("every", 0), WEB_REPEAT_SECS)
        return "met", i, dict(r)
    if result == "refused":
        return "refused", i, data["items"].pop(i)
    if result == "not-met":
        r["fails"] = 0
        r["next_check"] = now + r.get("every", 3600)
        return "not-met", i, r
    r["fails"] = r.get("fails", 0) + 1
    if r["fails"] >= WEB_MAX_FAILS:
        return "gave-up", i, data["items"].pop(i)
    _backoff(r, now)
    return "unknown", i, r


def web_apply(data, rep, now=None, owners=None):
    """Apply one check report: (result, id, record) for a report naming
    a live check of an active web watch with that check's nonce (spent
    now), else (None, id, why). met: the watch ends unless it repeats, then
    stays quiet WEB_REPEAT_SECS; not-met: the next check in `every`;
    unknown: a failure (back off; ends after WEB_MAX_FAILS, result
    "gave-up"); refused (the checker judged the condition to be more
    than a condition: instructions, a request to act): the watch ends."""
    now = time.time() if now is None else now
    if report_problems(rep):
        return None, None, "malformed"
    i = rep["id"]
    r = data["items"].get(i)
    chk = (r or {}).get("check") if isinstance(r, dict) else None
    reason = _web_report_problem(r, chk, now, owners, rep["nonce"])
    if reason is not None:
        return None, i, reason
    r["check"] = None
    return _apply_web_result(data, i, r, rep["result"], now)


def web_work(data, now=None, owners=None):
    """True when web_issue has something to do (a check to issue or to
    expire, or a watch to end), so the monitor takes the lock only
    then."""
    now = time.time() if now is None else now
    for r in data["items"].values():
        if isinstance(r, dict) and r.get("kind") == "watch-web" and r.get("until", 0) > now:
            if _not_owners(r, owners):
                return True
            chk = r.get("check")
            if (chk and chk.get("expires", 0) <= now) or (
                not chk and r.get("next_check", 0) <= now
            ):
                return True
    return False


def fire_entry(i, r, now, matched=None):
    """One entry of a fire's `reminder.items`."""
    e = {
        "id": i,
        "kind": r.get("kind"),
        "note": r.get("note") or "",
        "set_at": iso(r.get("created", now)),
    }
    if r.get("kind") == "remind":
        e["due"] = iso(r.get("due", now))
        e["late"] = now - r.get("due", now) > LATE_SECS
    elif r.get("kind") == "watch-web":
        res = (matched or {}).get("result", "met")
        e.update(
            {
                "what": r.get("what"),
                "result": res,
                "ends": iso(r.get("until", now)) if r.get("repeat") and res == "met" else "now",
            }
        )
        if matched and matched.get("summary"):
            e["summary"] = matched["summary"]
    else:
        e.update({"words": r.get("words"), "in": r.get("scope_name")})
        if matched:
            e["matched"] = matched
        e["ends"] = "now" if not r.get("every") else iso(r.get("until", now))
    return e
