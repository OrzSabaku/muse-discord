"""Notification limits, wire constants and refusal messages."""

import re

MAX_TIME_STRING_LENGTH = 40


UTC_YEAR_2100 = 4102444800


MAX_COMPILED_PATTERNS = 2000


FIRST_PRINTABLE_CHARACTER = 32


VERSION = 1


KINDS = ("remind", "watch")  # an `add`'s kind (a web watch is a


REMIND_MIN_SECS = 60  # at least a minute away


REMIND_MAX_SECS = 366 * 86400  # at most a year away


AFTER_MAX = 100 * REMIND_MAX_SECS  # a sane number (decide refuses


WATCH_DEFAULT_SECS = 7 * 86400  # a watch runs a week by default


WATCH_MAX_SECS = 30 * 86400  # and 30 days at most


WATCH_MIN_SECS = 60


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


WEB_EVERY_MIN = 1800  # checked at most every 30 minutes


WEB_EVERY_MAX = 7 * 86400


WEB_TOTAL_PER_DAY = 500  # checks a day in all: a safety budget,


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


REASON_CAP = "reminders: limit reached"  # -> watch-cap


REASON_NOT_ALLOWED = "reminders: not allowed"  # -> watch-not-allowed
