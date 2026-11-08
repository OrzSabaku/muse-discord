"""Proactive capabilities, limits and application service contract."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from muse_discord import muse_settings as MS

VERSION = 1

KIND = "proactive"

FIELD = "proactive"  # the slot's details on a queue item;

NONCE_FIELD = "owner_dm_nonce"  # the wake hook's one capability field

OPT_VALUES = ("opt-out", "opt-in")

SLOT_SECS = 15 * 60  # a slot is spent or gone after this

MAX_PER_SLOT = 1  # unprompted messages per heartbeat

ACTIVE_SECS = 30 * 60  # never into a conversation this fresh

TICK_SECS = 30

INTERVALS = {
    "rarely": (12 * 3600, 24 * 3600),
    "sometimes": (4 * 3600, 6 * 3600),
    "often": (45 * 60, 75 * 60),
}

QUIET_SPREAD_SECS = 2 * 3600  # after quiet hours, a heartbeat lands

TARGETS_MAX = 25

DAY, WEEK = 86400, 7 * 86400

SENT_KEEP_SECS = 8 * DAY

SPENT_KEEP = 500

PEOPLE_MAX = 3000

FLUSH_SECS = 60

REACT_CHECK_SECS = 6 * 3600  # reactions looked up at most this often

SEND_RETRY_SECS = 90  # as the monitor's sends (nonce window)

TEXT_KINDS = ("mention", "dm")  # queued messages that count as talking

FORBIDDEN = (
    "reply_to",
    "reactions",
    "attachments",
    "settings",
    "schedule",
    FIELD,
)  # never on a slot's outbox item

ASK_KEY = "mdpro"  # an ask's button: mdpro:yes:<token>

ASK_SECS = DAY  # a button works this long, once

ASKS_PER_DAY = 3  # buttons one person gets in a day

ASKS_MAX = 300  # ask records kept in all

ASK_SEND_SECS = 10 * 60  # an ask not sent by then is dropped


class Verdict:
    """outbox_item's answer: reject (why it is set aside, or None),
    retry (keep it in the outbox for a later pass), spent (the slot id
    whose queue item can go: the slot is used up)."""

    def __init__(self, reject=None, retry=False, spent=None):
        self.reject, self.retry, self.spent = reject, retry, spent


@dataclass(frozen=True)
class ProactiveContext:
    """The live state and operations used by proactive messaging, supplied at boot."""

    path: Callable[[], str]
    nonce_path: Callable[[], str]
    bot_id: Callable[[], str | None]
    owners: Callable[[], set[str]]
    channels: Callable[[], dict]
    dm_channels: Callable[[], dict]
    dm_users: Callable[[], dict]
    user_names: Callable[[], dict]
    write_private: Callable[..., Any]
    ledger: Callable[[], dict]
    api_get: Callable[[str], Any]
    api_post: Callable[[str, dict], Any]
    channel_display: Callable[[dict], str]
    dm_refusal: Callable[[dict], str | None]
    load_queue: Callable[[], list]
    never_share_hit: Callable[..., Any]
    normalize_parts: Callable[[dict], list[str]]
    notify_owners: Callable[..., Any]
    owner_check_pending: Callable[..., Any]
    owners_absent: Callable[..., Any]
    queue_edit: Callable[..., Any]
    queued_item_meta: Callable[..., Any]
    rest_backing_off: Callable[[], bool]
    scope_for: Callable[..., Any]
    send_fragmented: Callable[..., Any]
    send_reply: Callable[..., Any]
    settings: Callable[[], MS.Settings]
    settings_card: Callable[..., Any]
    started_item_id: Callable[..., Any]
