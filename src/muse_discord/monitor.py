"""
muse-discord monitor: the engine of the muse-discord connector.

Watches Discord for messages that address the bot (mentions, replies to
the bot, and DMs per policy), coalesces bursts, and appends work items to
reply_queue.json. A separate worker (an agent session; see docs/PROTOCOL.md)
drafts replies into reply_outbox.json, and this process sends them.
Gateway websocket ingest provides realtime delivery; a periodic REST poll
repairs gaps. A reconciliation sweep plus a mention ledger make silent
message loss structurally difficult: every mention reaches a recorded
terminal state (replied, quarantined, or skipped with a reason).

This process is deterministic (no LLM) and never drafts text itself.

Security model:
- Discord message content is UNTRUSTED input.
  1. Injection-pattern pre-filter: suspicious messages are quarantined
     for the owner's review instead of being queued for auto-reply
     (privacy.suspicious).
  2. Rate limits: daily reply caps, per scope and per person, bound the
     token budget an attacker could burn by spamming mentions
     (limits.*). Who is answered at all is enforced too (who.*).
  3. Output validation: worker-drafted replies are length-capped and
     mass mentions (@everyone/@here) are neutralized before sending;
     sends only ever ping users (allowed_mentions), never roles.
  4. Send dedup: never send two replies to the same message (protects
     against overlapping worker runs).
  5. The bot token is read from where token_from points (a file or an
     env var) at startup and is never written to any state file. The
     worker never holds the token; it only reads and writes the
     queue/outbox files.
  6. never_share: a reply containing one of the owner's listed phrases
     is held for the owners instead of posted (see hold_reply).

Configuration: config.yaml next to this file (or --config PATH, or the
MUSE_DISCORD_CONFIG env var), plus config.discord.yaml (changes made
from Discord), both read through muse_settings.py. See
config.example.yaml for the schema.
"""

import contextlib
import fcntl
import hmac
import json
import os
import queue as pyqueue
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus

try:
    import yaml  # noqa: F401 (muse_settings needs it)
    from pydantic import TypeAdapter  # noqa: F401 (Pydantic v2 required)
except ImportError:
    print("PyYAML and Pydantic v2 are required: run uv sync", file=sys.stderr)
    sys.exit(1)

from muse_discord import generated_media, media_ingest, reply_protocol, storage
from muse_discord import muse_settings as MS
from muse_discord import muse_watches as W
from muse_discord import secret_scan as SCAN
from muse_discord.admission import AdmissionPolicy, SharedServers
from muse_discord.bursts import (
    BurstEffects,
    CoalescePolicy,
    GatewayActivity,
    PendingBursts,
    alias_fragments,
)
from muse_discord.channel_access import ChannelAccess
from muse_discord.conversation import (
    PRIVATE_THREAD_TYPE,
    Conversation,
    channel_display,
    is_forwarded,
    now_snowflake,
)
from muse_discord.conversation import THREAD_TYPES as _THREAD_TYPES
from muse_discord.conversation import _snowflake_ts as _snowflake_ts
from muse_discord.conversation import _who as _who
from muse_discord.directory.discovery import (
    Discovery,
    DiscoveryState,
    ThreadSnapshot,
)
from muse_discord.directory.lifecycle import GuildLifecycle
from muse_discord.directory.membership import AuthorRoles
from muse_discord.directory.scopes import DirectoryScopes
from muse_discord.directory.snapshot import DirectorySnapshot
from muse_discord.ingest import MessageEnqueuer
from muse_discord.interactions import ephemeral, interaction_user_id
from muse_discord.message_models import (
    LEDGER_TERMINAL,
    LedgerContext,
    MessagePlace,
    QueueContext,
    SecretBlocked,
    Speaker,
)
from muse_discord.message_models import (
    PROACTIVE_KIND as PRO_KIND,
)
from muse_discord.message_queue import QueueFiles, owner_dm_metadata, trigger_of
from muse_discord.message_queue import answered_id as _answered_id
from muse_discord.message_queue import keep_last as _keep_last
from muse_discord.messaging import formatting as reply_formatting
from muse_discord.messaging.parts import PartDelivery
from muse_discord.messaging.sending import MessageSender
from muse_discord.messaging.typing import TypingIndicators, TypingState
from muse_discord.outbox_store import OutboxFiles, OutboxProcessor
from muse_discord.outbox_store import redact_nonces as redact_nonces
from muse_discord.owner.admin_digest import AdminDigests
from muse_discord.owner.authorization import OwnerAuthorization
from muse_discord.owner.capabilities import CapabilityFiles, CapabilityIssuer
from muse_discord.owner.join_notices import JoinNotices
from muse_discord.owner.memory import OwnerMemory
from muse_discord.owner.messaging import OwnerMessenger
from muse_discord.owner.presence import OwnerPresence
from muse_discord.owner.review import ReplyReview
from muse_discord.owner.review import released_ok as hold_released_ok
from muse_discord.owner.targets import CommandTargets
from muse_discord.owner.updates import UpdateNotices, version_installed
from muse_discord.paths import project_root
from muse_discord.reply_delivery import OutboxDelivery
from muse_discord.response_policy import (
    ResponsePolicy,
    predates_join,
)
from muse_discord.response_policy import _iso_ts as _iso_ts
from muse_discord.rest_scheduler import (
    DiscoveryRefresh,
    RestClock,
    RestContext,
    RestOperations,
    RestSchedule,
    RestScheduler,
)
from muse_discord.rest_scheduler import run_phase as _run_phase
from muse_discord.storage import PrivateFile, SharedLockTimeout, atomic_write
from muse_discord.storage import publish_private as _publish
from muse_discord.storage import read_json as _read_json
from muse_discord.watches.audience import WatchAudience
from muse_discord.watches.delivery import WatchDelivery
from muse_discord.watches.fires import WatchFireBuilder
from muse_discord.watches.queue import WatchQueue
from muse_discord.watches.requests import WatchRequests
from muse_discord.watches.runtime import WatchRuntime
from muse_discord.watches.web import WebWatchWork

CONFIG_PATH_ARGUMENT = 2
APPLICATION_COMMAND_TYPE = 2
HEARTBEAT_INTERVAL_SECS = 30
MEMBER_CACHE_LIMIT = 2000
GENERATED_SEND_HISTORY_LIMIT = 50
GENERATED_PATH_PAIR_LENGTH = 2
EMPTY_OUTBOX_MAX_BYTES = 4  # JSON [] with optional whitespace/newline


BASE_DIR = str(project_root(__file__))
STATE_FILE = os.path.join(BASE_DIR, "state.json")
QUEUE_FILE = os.path.join(BASE_DIR, "reply_queue.json")
OUTBOX_FILE = os.path.join(BASE_DIR, "reply_outbox.json")
QUARANTINE_FILE = os.path.join(BASE_DIR, "quarantine.json")
LEDGER_FILE = os.path.join(BASE_DIR, "mention_ledger.json")
# Touched by the main loop; the watchdog restarts a monitor whose
# heartbeat goes stale (a live process whose loop is stuck).
HEARTBEAT_FILE = os.path.join(BASE_DIR, "monitor.heartbeat")
# The monitor and the worker both edit the queue and the outbox. Every
# read-modify-write of either file happens under an exclusive flock on
# this file (see shared_lock and docs/PROTOCOL.md), so neither side can write
# back a stale copy over the other's changes.
LOCK_FILE = os.path.join(BASE_DIR, "reply.lock")
# Outbox items the monitor has taken and is sending. If it dies mid-send
# they are merged back on the next pass (part nonces keep resends safe).
OUTBOX_PROCESSING_FILE = os.path.join(BASE_DIR, "reply_outbox.processing.json")
# Malformed outbox items, set aside for the owner's review (last 100).
OUTBOX_REJECTED_FILE = os.path.join(BASE_DIR, "reply_outbox.rejected.json")
MEDIA_DIR = os.path.join(BASE_DIR, "media")
GENERATED_DIR = os.path.join(BASE_DIR, "generated")


def _config_path():
    if len(sys.argv) > CONFIG_PATH_ARGUMENT and sys.argv[1] == "--config":
        return sys.argv[2]
    return os.environ.get("MUSE_DISCORD_CONFIG") or os.path.join(BASE_DIR, "config.yaml")


_INITIALIZED = False
_SETTINGS: MS.Watcher
_S0: MS.Settings
CFG: dict
OWNER_USER_IDS: set[str]
GUILD_ALLOWLIST: set[str]
TREAT_AS_HUMAN: set[str]
_ENGINE: MS.EngineSettings
COALESCE_SECS: float
BOT_COALESCE_SECS: float
MAX_REPLY_CHARS: int
POLL_INTERVAL: int
SWEEP_EVERY_CYCLES: int
SWEEP_GRACE_SECS: float
SWEEP_MAX_AGE_SECS: float
SWEEP_MAX_RECOVERIES: int
BACKOFF_INITIAL_SECS: float
BACKOFF_MAX_SECS: float
CHANNEL_REFRESH_CYCLES: int
TOKEN = ""


# ---------------------------------------------------------------------------
# Settings and services are created by initialize(), never by importing this
# module. Watcher reloads config and the Discord overlay during normal operation.
# ---------------------------------------------------------------------------

# Anonymous daily usage counts (telemetry.py). Off with
# telemetry.usage_stats: off or MUSE_DISCORD_TELEMETRY=off.
TELEMETRY_FILE = os.path.join(BASE_DIR, "telemetry.json")
TELEMETRY_ID_FILE = os.path.join(BASE_DIR, "telemetry_id")
TELEMETRY_WAKES_FILE = os.path.join(BASE_DIR, "telemetry.wakes")


class _NoTelemetry:
    def __getattr__(self, name):
        return lambda *a, **kw: ""


def _import_telemetry():
    """A telemetry.Recorder; a stand-in that does nothing when
    telemetry.py cannot be loaded (it never stops the bot)."""
    try:
        from muse_discord.telemetry import Recorder

        return Recorder(
            lambda: (TELEMETRY_FILE, TELEMETRY_ID_FILE, TELEMETRY_WAKES_FILE),
            ms=MS,
            pkg_dir=BASE_DIR,
        )
    except Exception as e:
        print(f"Telemetry not loaded ({type(e).__name__}); usage counts are off.", flush=True)
        return _NoTelemetry()


# Reaching out on its own, unprompted (proactive.py; docs/PROTOCOL.md,
# "Unprompted messages"). Off by default; when proactive.py cannot be
# loaded the bot only replies.
PROACTIVE_FILE = os.path.join(BASE_DIR, "proactive.json")
# The live heartbeat slot's nonce, for the wake hook only (mode 600).
PROACTIVE_NONCE_FILE = os.path.join(BASE_DIR, "proactive_nonce.json")


TM = _NoTelemetry()


class _NoProactive:
    def __getattr__(self, name):
        return lambda *a, **kw: None


def _proactive_context():
    from muse_discord.proactive import ProactiveContext

    return ProactiveContext(
        path=lambda: PROACTIVE_FILE,
        nonce_path=lambda: PROACTIVE_NONCE_FILE,
        bot_ids=lambda: (BOT_ID, BOT_USER_ID),
        owners=lambda: OWNER_USER_IDS,
        channels=lambda: CHANNELS,
        dm_channels=lambda: DM_CHANNELS,
        dm_users=lambda: DM_CHANNEL_USER,
        user_names=lambda: _USER_NAMES,
        write_private=lambda path, body: atomic_write(path, body, 0o600),
        ledger=lambda *args, **kwargs: _ledger_load(*args, **kwargs),
        api_get=lambda *args, **kwargs: api_get(*args, **kwargs),
        api_post=lambda *args, **kwargs: api_post(*args, **kwargs),
        channel_display=lambda *args, **kwargs: channel_display(*args, **kwargs),
        dm_refusal=lambda *args, **kwargs: dm_refusal(*args, **kwargs),
        load_queue=lambda *args, **kwargs: load_queue(*args, **kwargs),
        never_share_hit=lambda *args, **kwargs: never_share_hit(*args, **kwargs),
        normalize_parts=lambda *args, **kwargs: normalize_parts(*args, **kwargs),
        notify_owners=lambda *args, **kwargs: notify_owners(*args, **kwargs),
        owner_check_pending=lambda state, gid: _owner_presence().pending(state, gid),
        owners_absent=lambda *args, **kwargs: owners_absent(*args, **kwargs),
        queue_edit=lambda *args, **kwargs: queue_edit(*args, **kwargs),
        queued_item_meta=lambda *args, **kwargs: queued_item_meta(*args, **kwargs),
        rest_backing_off=lambda *args, **kwargs: rest_backing_off(*args, **kwargs),
        scope_for=lambda *args, **kwargs: scope_for(*args, **kwargs),
        send_fragmented=lambda *args, **kwargs: send_fragmented(*args, **kwargs),
        send_reply=lambda *args, **kwargs: send_reply(*args, **kwargs),
        settings=lambda *args, **kwargs: settings(*args, **kwargs),
        settings_card=lambda *args, **kwargs: settings_card(*args, **kwargs),
        started_item_id=lambda *args, **kwargs: started_item_id(*args, **kwargs),
    )


def _import_proactive():
    try:
        from muse_discord.proactive import Proactive

        return Proactive(_proactive_context())
    except Exception as e:
        print(f"proactive.py not loaded ({type(e).__name__}); the bot only replies.", flush=True)
        return _NoProactive()


PRO = _NoProactive()
# The raw file (test-only keys such as testing: live here).
# The interaction thread (/settings) reads settings and writes the
# overlay while the main loop reads them: the reload is serialized by
# _SETTINGS_LOCK, and every read-modify-write of config.discord.yaml in
# this process (a /settings change, an undo, the conflict rule retiring
# entries) holds _OVERLAY_LOCK. A reload noticed off the main thread
# leaves its follow-up work (owner list) to the main
# loop's next settings() call.
_SETTINGS_LOCK = threading.RLock()
_OVERLAY_LOCK = threading.RLock()
_SETTINGS_PENDING = []  # settings before reloads the main loop owes hooks


def settings():
    """The current settings, reloaded when config.yaml or
    config.discord.yaml changed since the last call (a stat per call; an
    invalid edit keeps the previous settings and is logged once).
    Thread-safe."""
    if not _INITIALIZED:
        raise RuntimeError("call initialize() before using the monitor")
    with _SETTINGS_LOCK:
        prev = _SETTINGS.current
        changed, err = _SETTINGS.poll()
        cur = _SETTINGS.current
        if changed:
            _SETTINGS_PENDING.append(prev)
        pending = None
        if _SETTINGS_PENDING and threading.current_thread() is threading.main_thread():
            pending = _SETTINGS_PENDING[0]
            del _SETTINGS_PENDING[:]
    if err:
        print(f"Config re-read failed; keeping the previous settings:\n{err}", flush=True)
    if pending is not None:
        _on_settings_changed(pending, cur)
    return cur


def _on_settings_changed(prev, cur):
    global OWNER_USER_IDS
    print("Settings reloaded (config.yaml or config.discord.yaml changed).", flush=True)
    if sorted(cur.owners) != sorted(prev.owners):
        OWNER_USER_IDS = set(cur.owners)
        print(f"Owners are now: {', '.join(sorted(OWNER_USER_IDS)) or 'none'}", flush=True)


# --- Read once at import: identity and engine internals ---
RESPOND_TO = {"mention", "reply_to_bot"}
# The conversational follow-up window (who.replies: conversational).
# Test-only (the e2e harness's driver bots stand in for people): these
# author ids count as human everywhere the monitor checks author.bot
# (see is_bot_author). Absent or empty = no effect.


def _conversation():
    return Conversation(BOT_ID, BOT_NAME, TREAT_AS_HUMAN, frozenset(RESPOND_TO))


def is_bot_author(author):
    return _conversation().is_bot_author(author)


# Operational defaults and normalization are owned by EngineSettings.


def _load_token(initial_settings):
    # argv (manual/transient use) wins; then token_from in order: an
    # env:NAME source when that variable is set, a file when readable.
    # The watchdog never passes a token.
    if len(sys.argv) > 1 and sys.argv[1] != "--config":
        return sys.argv[1]
    for kind, where in initial_settings.token_sources():
        if kind == "env":
            if where and os.environ.get(where):
                return os.environ[where].strip()
            continue
        try:
            with open(os.path.expanduser(where)) as f:
                return f.read().strip()
        except OSError:
            continue
    return None


API = "https://discord.com/api/v10"

# The bot's own identity, fetched at startup. Used to recognize mentions,
# skip the bot's own messages, and compute its channel permissions.
# BOT_USER_ID is the same id (proactive.py reads it).
BOT_ID = None
BOT_NAME = None
BOT_USER_ID = None

# Generated media: when the media settings grant permission on a queue
# item (media_permission), the worker may write generated files into
# GENERATED_DIR/<the item's message_id>/; the send path attaches a reply's
# files from its own item's directory only (validated in
# generated_media.validate_paths), so one item's image can never ride on another
# item's reply. Generation approvals are single-use, keyed by the
# triggering message id and bound to its channel, because the worker's
# outbox item does not carry the permission flag.

# Conversation history: rolling window of recent messages kept per
# channel/DM so the reply worker drafts with context. Stored in state.json,
# attached to queue items (minus the triggering message itself).

# Channels and guilds are discovered live at startup and refreshed
# periodically: every channel/thread the bot can read, across every
# guild it belongs to (restricted to bot.guild_ids when configured).
# CHANNELS: {channel_id: {"name", "guild_id", "guild_name", "can_send"}}
#   can_send is the bot's effective Send Messages permission for the
#   channel (Send Messages in Threads for threads), or None when it
#   could not be computed; callers then attempt the send and rely on
#   the 403 safety net in process_outbox.
# GUILDS: {guild_id: {"name", "joined_at"}}
CHANNELS = {}
GUILDS = {}


# Channel types the bot can meaningfully read: 0 = text, 5 = announcement.
# Forum (15) and media (16) channels hold no messages themselves, only
# threads (posts), whose send permission comes from them.

# Re-check for brand-new threads this often (in poll cycles). Threads are
# where forum posts live; a new post should be picked up within a few
# minutes, not at the next full channel refresh.
THREAD_REFRESH_CYCLES = 5


def _discovery_state():
    return DiscoveryState(
        guilds=GUILDS,
        member_counts=_MEMBER_COUNTS,
        guild_channels=_GUILD_CHANNELS,
        categories=CHANNEL_CATEGORY,
        threads_listed=_THREADS_LISTED,
        new_threads=_NEW_THREADS,
        started_at=MONITOR_STARTED,
    )


def _discovery():
    return Discovery(
        api_get,
        rest_backing_off,
        lambda: BOT_ID,
        GUILD_ALLOWLIST,
        _discovery_state(),
        _channel_access(),
    )


def get_guilds():
    return _discovery().guilds()


# Discord permission bits used by the talk-scope computation.


def discover_channels(previous=None):
    return _discovery().channels(previous)


def discover_channels_iter(previous=None):
    return _discovery().channels_iter(previous)


# Brand-new threads. A thread created since the previous successful
# thread listing of its guild (or since the monitor started) is seeded at
# its own id, not at its newest message: a mention posted in it before
# the gateway event was drained would otherwise sit at or below the
# watermark, be skipped by the drain as "poll already handled", and wait
# for the sweep. Its whole (short) history is read by the poll instead;
# the gateway/poll dedup (gw_seen, the watermark) keeps that to one
# reply. Older threads seen for the first time keep the usual seeding:
# their history is history.
MONITOR_STARTED = time.time()
_THREADS_LISTED = {}  # guild id -> when its threads were last listed
_NEW_THREADS = set()  # thread ids to seed at their start


def discover_threads_iter(gid, gname, snapshot):
    return _discovery().threads_iter(gid, gname, snapshot)


def seed_channel_seen(cid, cname, state):
    return _discovery().seed(cid, cname, state)


# Prompt-injection / exfiltration patterns. A match means the message is
# quarantined for the owner's review instead of queued for auto-reply.
# Deliberately conservative: false positives just skip one auto-reply.
INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|prior|above|earlier)\s+instructions",
    r"disregard\s+(all\s+)?(previous|prior|above)?\s*instructions",
    r"forget\s+(all\s+)?(previous|prior|above)?\s*instructions",
    r"\byour\s+system\s*prompt\b",
    r"(reveal|share|send|show|tell|give|leak|paste|print|post|drop|output).{0,40}\bsystem\s*prompt\b",
    r"\byou\s+are\s+now\b",
    r"\breveal\b.{0,50}\b(instructions|prompt|secrets?)\b",
    r"\bjailbreak\b",
    r"\bdeveloper\s+mode\b",
    r"pretend\s+(you'?re|you\s+are|to\s+be)\b",
    r"act\s+as\s+(a\s+)?(different|new|another)\b",
    r"output\s+(your|the)\s+(initial|original|system)\s+(prompt|instructions)",
    r"(read|cat|open|print)\s+~?/",
    r"MEMORY\.md",
    r"\.env\b",
    # Credential terms need solicitation context: the bare term
    # "api key" shows up constantly in legitimate dev talk, so it only
    # matches with a request verb aimed at the key, "your ... api key",
    # or an assignment.
    r"(reveal|share|send|show|tell|give|leak|paste|print|post|drop|output).{0,40}\bapi[\s_-]?key\b",
    r"\byour\b.{0,25}\bapi[\s_-]?key\b",
    r"\bapi[\s_-]?key\b\s*(is|=|:)",
]


@dataclass
class RequestOptions:
    files: tuple | list = ()
    timeout: float = 20
    anonymous: bool = False


def discord_request(method, path, data=None, options=None):
    options = options or RequestOptions()
    files = options.files
    timeout = options.timeout
    anonymous = options.anonymous
    from muse_discord.discord_rest import transport

    return transport(None if anonymous else TOKEN, API).request(
        method, path, data, files=files, timeout=timeout
    )


# ---------------------------------------------------------------------------
# REST rate limits. Discord 429s are per IP (shared egress), so any 429
# records a backoff window (Retry-After honored, else exponential), and
# the main loop holds all REST-initiated work (polls, sweeps, discovery)
# until it passes. The gateway path and outbox sends keep running.
# ---------------------------------------------------------------------------
_rl_until = 0.0
_rl_backoff_step = 0
_rl_storm_logged_until = 0.0


def rest_backing_off():
    return time.time() < _rl_until


def _note_rate_limit(exc):
    global _rl_until, _rl_backoff_step, _rl_storm_logged_until
    now = time.time()
    delay = None
    hdrs = getattr(exc, "headers", None)
    if hdrs is not None:
        try:
            delay = float(hdrs.get("Retry-After"))
        except TypeError, ValueError:
            delay = None
    if delay is None:
        try:
            delay = float(json.loads(exc.read()).get("retry_after"))
        except Exception:
            delay = None
    if delay is None:
        delay = min(
            BACKOFF_MAX_SECS, BACKOFF_INITIAL_SECS * (2**_rl_backoff_step)
        ) + random.uniform(0, 5)
        _rl_backoff_step = min(_rl_backoff_step + 1, 5)
    delay = max(1.0, min(float(delay), 3600.0))
    _rl_until = max(_rl_until, now + delay)
    if now >= _rl_storm_logged_until:
        print(f"RATE LIMITED: backing off REST for {delay:.0f}s.", flush=True)
        _rl_storm_logged_until = _rl_until


def _rl_note_clean_cycle():
    """Reset the exponential step after a poll cycle that stayed clean."""
    global _rl_backoff_step
    if not rest_backing_off():
        _rl_backoff_step = 0


def _api_call(fn):
    global _token_rejected
    try:
        return fn()
    except Exception as e:
        if getattr(e, "code", None) == HTTPStatus.TOO_MANY_REQUESTS:
            _note_rate_limit(e)
        elif getattr(e, "code", None) == HTTPStatus.UNAUTHORIZED:
            _token_rejected = "401 from the REST API"
        raise
    finally:
        # A call that returned (or failed) is progress: long but live
        # stretches (discovery, the sweep, an outbox backlog) never look
        # stuck to the watchdog; a call that hangs does.
        touch_heartbeat()


# ---------------------------------------------------------------------------
# Token rejection and liveness, for the watchdog. A reset token makes
# every REST call answer 401 and the gateway close with 4004; neither
# heals by retrying, and a monitor that keeps running deaf hides the
# problem from the watchdog (it only restarts dead processes). So the
# monitor prints the "TOKEN REJECTED" marker and exits; the watchdog
# greps for it and raises the token alert. The main loop also touches
# HEARTBEAT_FILE so the watchdog can tell a stuck loop from a live one.
# ---------------------------------------------------------------------------
EXIT_TOKEN_REJECTED = 3
_token_rejected = None  # set (to where) by a 401 on any REST call
_heartbeat_at = 0.0


def token_rejected_exit(where):
    print(
        f"TOKEN REJECTED: Discord refused the bot token ({where}); it "
        f"was probably reset in the developer portal. Exiting. Store a "
        f"fresh token and the watchdog starts the monitor again.",
        flush=True,
    )
    sys.exit(EXIT_TOKEN_REJECTED)


def touch_heartbeat():
    """Mark the monitor as making progress (throttled to one touch per
    30s). Called from the main loop and after every REST call."""
    global _heartbeat_at
    now = time.time()
    if now - _heartbeat_at < HEARTBEAT_INTERVAL_SECS:
        return
    _heartbeat_at = now
    try:
        with open(HEARTBEAT_FILE, "a"):
            pass
        os.utime(HEARTBEAT_FILE, None)
    except OSError:
        pass


# Every GET is a read on the main loop (poll, sweep, discovery, reply
# context), so it gets a shorter timeout than sends: a request a proxy
# leaves hanging must not stall the gateway drain, the burst flushes,
# and the outbox for 20s. Sends (api_post, uploads) keep their 20s+.
READ_TIMEOUT_SECS = 8


def api_get(path):
    return _api_call(
        lambda: discord_request("GET", path, options=RequestOptions(timeout=READ_TIMEOUT_SECS))
    )


# ---------------------------------------------------------------------------
# Secrets never leave in anything the bot sends. On the real platform
# every session (workers included) runs as the same OS user, so a worker
# that follows an injection into its shell can read the token file and
# every nonce file; the nonces bind what it writes to the outbox, but
# only this filter keeps a secret it read from being posted. Before any
# send to Discord (replies, fires, owner DMs and notices, edits,
# interaction responses, uploads and their files), the payload is
# checked: the bot token
# itself, any nonce the monitor holds live, or anything shaped like a
# Discord token (three base64url segments, the first decoding to a
# snowflake) drops the send (SecretBlocked), logged without the value,
# and the owners are DMed about it once a day. Nothing is ever altered.
# ---------------------------------------------------------------------------


_SECRET_ALERT = {"day": None, "busy": False}


def _live_nonces():
    """Every nonce the monitor holds live: the values of its flat nonce
    maps, and the "nonce" fields of owner_items and the web checks
    (never other fields, such as a check's public group tag)."""
    out = set()
    for cache in (_OWNER_DM_NONCES_WRITTEN, _ITEM_NONCES_WRITTEN):
        body = cache.get("body")
        if isinstance(body, dict):
            out |= {v for v in body.values() if isinstance(v, str) and v}
    with contextlib.suppress(Exception):  # a live heartbeat slot's nonce (proactive.py)
        out |= {v for v in (PRO.secret_nonces() or ()) if v}

    def walk(v):
        if isinstance(v, dict):
            for k, x in v.items():
                if k == "nonce" and isinstance(x, str) and x:
                    out.add(x)
                else:
                    walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    for cache in (_OWNER_ITEMS_WRITTEN, _WEB_DUE_WRITTEN):
        walk(cache.get("body"))
    return out


def secret_in(text):
    """What secret text carries, or None (secret_scan.find)."""
    return SCAN.find(text, TOKEN, _live_nonces())


def _payload_strings(v, key=None):
    if isinstance(v, str):
        if key != "nonce":  # Discord's own send nonce, derived
            yield v
    elif isinstance(v, dict):
        for k, x in v.items():
            yield from _payload_strings(x, k)
    elif isinstance(v, list):
        for x in v:
            yield from _payload_strings(x, key)


def guard_outgoing(data, where):
    """Raise SecretBlocked (and log, and tell the owners once a day) when
    anything data would send carries a secret: any of its strings, or
    all of them joined (a token split across parts, embed fields or file
    names). data is returned as is: nothing is ever altered. Every send
    helper calls it."""
    texts = list(_payload_strings(data))
    for text in [*texts, "".join(texts)]:
        what = secret_in(text)
        if what:
            print(f"Blocked a send to {where}: it contained {what}; nothing was sent.", flush=True)
            _secret_alert(where)
            raise SecretBlocked(f"blocked: it contained {what}")
    return data


def _secret_alert(where):
    """DM the owners about a blocked send, once a day (never raises)."""
    day = time.strftime("%Y-%m-%d")
    if _SECRET_ALERT["day"] == day or _SECRET_ALERT["busy"]:
        return
    _SECRET_ALERT.update(day=day, busy=True)
    try:
        notify_owners(
            "I didn't send a reply because it looked like it "
            "held a secret (my bot token or one of my private "
            "codes). I tell you about this at most once a day."
        )
    finally:
        _SECRET_ALERT["busy"] = False


def _api_send(method, path, data):
    data = guard_outgoing(data, path)
    return _api_call(lambda: discord_request(method, path, data))


def api_post(path, data):
    return _api_send("POST", path, data)


def api_patch(path, data):
    return _api_send("PATCH", path, data)


# Bound contention without bypassing mutual exclusion. The main loop retries
# deferred work after the holder releases the lock.
LOCK_WAIT_SECS = 20


def shared_lock():
    """Acquire the configured queue/outbox lock; never hold it across network calls."""
    return storage.shared_lock(LOCK_FILE, LOCK_WAIT_SECS)


def _queue_files():
    return QueueFiles(QUEUE_FILE, LOCK_FILE, LOCK_WAIT_SECS)


def _outbox_files():
    return OutboxFiles(OUTBOX_FILE, OUTBOX_PROCESSING_FILE, OUTBOX_REJECTED_FILE, _queue_files())


def _publish_reply_capabilities(state):
    reply_nonces_write(state)
    owner_dm_write_nonces(state)


def _deliver_outbox_item(batch, item):
    OutboxDelivery(
        batch, item, _MonitorReplyPolicy(), _MonitorReplyEffects(), _OUTBOX_CURRENT
    ).process()


def _outbox_processor():
    return OutboxProcessor(
        _outbox_files(),
        queue_index,
        save_state,
        _publish_reply_capabilities,
        _deliver_outbox_item,
        _OUTBOX_CURRENT,
        _OUTBOX_POISON,
    )


def load_queue():
    return _queue_files().load()


def queue_edit(add=(), drop=None):
    return _queue_files().edit(add, drop)


def publish_queue_additions(state, additions=()):
    """Persist staged additions until the shared queue accepts them."""
    backlog = {q["message_id"]: q for q in state.get("queue_backlog", [])}
    backlog.update((q["message_id"], q) for q in additions)
    if not backlog:
        return
    entries = _ledger_load()["entries"]
    replied = set(state.get("replied_to", []))
    state["queue_backlog"] = [
        q
        for mid, q in backlog.items()
        if mid not in replied and entries.get(mid, {}).get("status") not in LEDGER_TERMINAL
    ]
    save_state(state)  # also preserves the consumed burst and its capability
    if state["queue_backlog"]:
        queue_edit(add=state["queue_backlog"])
    state.pop("queue_backlog", None)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            state = json.load(f)
    else:
        state = {}
    state.setdefault("last_seen", {})
    state.setdefault("replied_to", [])
    state.setdefault("rate", {"day": "", "day_count": 0, "users": {}})
    state.setdefault("history", {})
    # Leftovers from the removed burst breaker.
    for key in ("breaker", "owner_triggers", "trigger_texts"):
        state.pop(key, None)
    state.pop("bot_runs", None)  # leftover from the removed bot turn cap
    state.pop("web_nonces", None)  # leftover: web watches are DM only now
    # Bursts staged by the removed name trigger: not direct address.
    state["pending"] = {
        k: v for k, v in state.get("pending", {}).items() if v.get("kind") != "name_drop"
    }
    # The ownerless-server gate holds from the first message on, even
    # when the boot discovery fails.
    mirror_owner_presence(state)
    return state


_STATE_WRITTEN: dict[str, str | None] = {"text": None}


def save_state(state):
    """Write state.json, unless it holds exactly this already (the main
    loop saves several times a tick, mostly with nothing changed)."""
    state["replied_to"] = state["replied_to"][-200:]
    text = json.dumps(state)
    if text != _STATE_WRITTEN["text"] or not os.path.exists(STATE_FILE):
        atomic_write(STATE_FILE, None, text=text)
        _STATE_WRITTEN["text"] = text


def is_suspicious(content):
    """The injection pattern content matches, else False."""
    lowered = content.lower()
    for pat in INJECTION_PATTERNS:
        if re.search(pat, lowered):
            return pat
    return False


def quarantine(msg, channel_name, matched_pattern, fragments=()):
    """Hold a message (and its burst fragments: a split injection must
    not have its second half swept back in alone) for the owner."""
    entry = {
        "message_id": msg["id"],
        "channel_id": msg.get("channel_id"),
        "channel_name": channel_name,
        "author": msg.get("author", {}).get("username"),
        "author_id": msg.get("author", {}).get("id"),
        "content": msg.get("content", ""),
        "matched_pattern": matched_pattern,
        "timestamp": msg.get("timestamp"),
        "quarantined_at": datetime.now(UTC).isoformat(),
    }
    if fragments:
        entry["fragments"] = [
            {"message_id": f["id"], "content": f.get("content", "")} for f in fragments
        ]
    q = _read_json(QUARANTINE_FILE, list)
    if q is None:  # kept for the owner, never silently emptied
        with contextlib.suppress(OSError):
            os.replace(QUARANTINE_FILE, QUARANTINE_FILE + ".bad")
        print("quarantine.json was unreadable; moved to quarantine.json.bad.", flush=True)
        q = []
    q.append(entry)
    atomic_write(QUARANTINE_FILE, q[-100:])  # keep last 100
    ledger_record_many(
        [msg["id"]] + [f["id"] for f in fragments],
        "quarantined",
        matched_pattern,
        context=LedgerContext(
            channel_id=msg.get("channel_id"),
            author=entry["author"],
            author_id=(msg.get("author") or {}).get("id"),
        ),
    )
    print(
        f"Quarantined suspicious message from {entry['author']} in #{channel_name} "
        f"(matched: {matched_pattern}): {entry['content'][:80]}",
        flush=True,
    )


# ---------------------------------------------------------------------------
# Mention ledger: the durable record that makes silent drops impossible.
# Every mention/DM reaching a terminal state (replied, quarantined,
# skipped with a reason) is written here, and the reconciliation sweep
# (sweep_mentions) diffs recent channel mentions against it and requeues
# anything unaccounted for.
# ---------------------------------------------------------------------------


def _ledger_load():
    d = _read_json(LEDGER_FILE)
    if d and isinstance(d.get("entries"), dict):
        return d
    return {"entries": {}}


def _ledger_save(d):
    items = sorted(d["entries"].items(), key=lambda kv: kv[1].get("updated", 0))
    d["entries"] = dict(items[-1000:])
    atomic_write(LEDGER_FILE, d)


def ledger_record(mid, status, reason="", context=None):
    ledger_record_many([mid], status, reason, context)


def ledger_record_many(mids, status, reason="", context=None):
    """ledger_record for several ids in one write: a burst's trigger and
    its fragments always share one outcome. Each entry also keeps the
    author's id and a reason code (ledger_code; "Why no reply?" in
    /settings reads them), plus detail (which cap, which limit)."""
    context = context or LedgerContext()
    channel_id = context.channel_id
    author = context.author
    recoveries = context.recoveries
    author_id = context.author_id
    detail = context.detail
    mids = [m for m in mids if m]
    if not mids:
        return
    d = _ledger_load()
    code = ledger_code(status, reason)
    TM.ledger(code, [d["entries"].get(m) for m in mids])
    # Where it happened, as the monitor knows it now (a thread's parent
    # only for a thread): /settings' Why reads it later, when the
    # channel may be gone from discovery.
    info = CHANNELS.get(str(channel_id)) if channel_id else None
    where = {}
    if info and info.get("guild_id"):
        where = {
            "guild_id": info["guild_id"],
            "parent_id": info.get("parent_id") if info.get("thread") else None,
        }
    elif channel_id and (channel_id in DM_CHANNELS or channel_id in DM_CHANNEL_USER):
        where = {"dm": True}
    for mid in mids:
        ent = d["entries"].get(mid, {})
        ent.update(where)
        ent.update(
            {
                "status": status,
                "reason": reason,
                "channel_id": channel_id or ent.get("channel_id"),
                "author": author or ent.get("author"),
                "author_id": str(author_id) if author_id else ent.get("author_id"),
                "code": code,
                "updated": time.time(),
            }
        )
        if detail is not None:
            ent["detail"] = detail
        else:
            ent.pop("detail", None)
        if recoveries is not None:
            ent["recoveries"] = recoveries
        d["entries"][mid] = ent
        if author_id and author:
            remember_name(author_id, author)
    _ledger_save(d)


# Reason codes for the ledger, from the reason text every caller
# writes: what /settings' "Why no reply?" explains.
_LEDGER_CODES = (
    ("settings: who.replies is off", "replies-off"),
    ("settings: none of the owners is in", "ownerless"),
    ("settings: who.bots", "bots-ignore"),
    ("settings: not allowed by who.allow", "not-allowed"),
    ("settings: on who.ignore", "not-allowed"),
    ("settings: not in who.allow", "not-allowed"),
    ("settings: who.dms_from", "dm-not-allowed"),
    ("settings: not in who.dms_from", "dm-not-allowed"),
    ("settings: shares no server", "dm-not-allowed"),
    ("daily cap reached", "cap"),
    ("bot exchange limit", "bot-exchange"),
    ("suspicious, ignored", "suspicious-ignored"),
    ("predates bot join", "predates-join"),
    ("no send permission", "no-send"),
    ("403 missing access", "no-send"),
    ("enqueue failed", "internal"),
    ("sweep enqueue failed", "internal"),
    ("recovery cap reached", "internal"),
    ("no reply (worker's call)", "worker-skipped"),
    ("send failed", "send-failed"),
    ("held for owner review (never_share)", "never-share"),
    ("never_share hold discarded", "never-share"),
    # Reminders and watches (muse_watches.REASON_*): a request refused.
    ("reminders: limit reached", "watch-cap"),
    ("reminders: not allowed", "watch-not-allowed"),
    ("reminder held", "watch-held"),
)


def ledger_code(status, reason=""):
    """A ledger entry's reason code: replied, queued, quarantined, or why
    a message was skipped (dm-off, a _LEDGER_CODES code, else other)."""
    if status in ("replied", "queued", "quarantined"):
        return status
    if status == "enqueue_failed":
        return "internal"
    reason = str(reason or "")
    if reason.startswith("settings: who.replies is off for DMs"):
        return "dm-off"
    for prefix, code in _LEDGER_CODES:
        if reason.startswith(prefix):
            return code
    return "other"


# Display names of people the monitor has seen (ledger authors), for
# /settings' "Remove Sam" (bounded; ids only otherwise).
_USER_NAMES = {}


def remember_name(uid, name):
    if uid and name:
        _USER_NAMES[str(uid)] = str(name)
        if len(_USER_NAMES) > MEMBER_CACHE_LIMIT:
            for k in list(_USER_NAMES)[:500]:
                _USER_NAMES.pop(k, None)


def _ledger_terminal(mid):
    return _ledger_load()["entries"].get(mid, {}).get("status") in LEDGER_TERMINAL


def _quarantine_ids():
    ids = set()
    for e in _read_json(QUARANTINE_FILE, list) or []:
        if isinstance(e, dict):
            ids.add(e.get("message_id"))
            ids.update(
                fr.get("message_id") for fr in e.get("fragments") or [] if isinstance(fr, dict)
            )
    return ids


def _response_policy():
    return ResponsePolicy(
        settings,
        lambda: OWNER_USER_IDS,
        _conversation(),
        scope_for,
        scope_of_channel,
        member_roles,
        TM.count,
    )


def rate_block(state, user_id, channel_id=None, scope=None):
    return _response_policy().rate_block(state, user_id, channel_id, scope)


def rate_record(state, user_id, scope=None):
    return _response_policy().rate_record(state, user_id, scope)


# ---------------------------------------------------------------------------
# Join watermark. A message that predates the bot's arrival in a guild is
# never a trigger: not on catch-up after a restart, not in a channel
# discovered late, not on a sweep. The watermark is the bot's own
# joined_at for that guild (member object at discovery, GUILD_CREATE on
# the gateway), kept in state["guild_joined"] and enforced in
# classification and again at enqueue, independent of last_seen seeding.
# Backlog still reaches the worker as history on a post-join trigger.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Scopes and who gets answered. Every decision about a message resolves
# the settings for where it happened: a DM, or a server channel (a thread
# uses its parent channel; a channel its category). A new server has no
# entry of its own and resolves through all_servers, which ships careful
# (plugins not marked safe_in_new_servers stay off).
# When the bot joins a new server, the owners get a DM summary.
# ---------------------------------------------------------------------------
_DISCOVERY_REFRESH: DiscoveryRefresh
# channel id -> its category id (from discovery), for category settings.
CHANNEL_CATEGORY = {}


def _directory_scopes():
    return DirectoryScopes(CHANNELS, GUILDS, CHANNEL_CATEGORY)


def scope_for(guild_id=None, channel_id=None, dm=False):
    return _directory_scopes().for_message(guild_id, channel_id, dm)


def scope_of_channel(channel_id):
    return _directory_scopes().for_channel(channel_id)


# (guild, user) -> (role ids, fetched at): roles of authors whose messages
# carry no member object (the REST poll), for role entries in who.allow /
# who.ignore. Only fetched when such a list names a role.
_MEMBER_ROLES = {}


def member_roles(guild_id, user_id):
    return AuthorRoles(api_get, rest_backing_off, _MEMBER_ROLES).get_roles(guild_id, user_id)


def people_match(value, user_id, roles=lambda: set()):
    return _response_policy().people_match(value, user_id, roles)


def who_allows(msg, guild_id, resolved):
    return _response_policy().who_allows(msg, guild_id, resolved)


# user id -> (shares a server with the bot?, checked at), for
# who.dms_from: shared-servers. Positive answers last an hour, negative
# ones ten minutes.
_SHARED_MEMBERSHIP: SharedServers


def _admission():
    return AdmissionPolicy(
        _response_policy(), _owner_presence(), _directory_scopes(), _SHARED_MEMBERSHIP
    )


def shares_server(user_id):
    return _SHARED_MEMBERSHIP.contains(user_id)


def dm_refusal(author):
    return _admission().dm_refusal(author)


def dm_sender_allowed(author):
    return _admission().dm_sender_allowed(author)


def retire_overlay_changes():
    """Discord-side settings changes that config.yaml has overtaken (the
    file was edited at that spot afterwards) or that no longer validate:
    move them to the overlay's history, one owner DM each (the conflict
    rule: the newest intent wins). Waits out a REST backoff so the DMs
    can land."""
    if not settings().retire or rest_backing_off():
        return 0
    with _OVERLAY_LOCK:
        # Re-checked under the lock: a /settings change written since
        # may have replaced a stale entry, which must not be retired.
        notes = settings().commit_retirements()
    for note in notes:
        print(note, flush=True)
        notify_owners(note)
    return len(notes)


def dms_enabled():
    return _admission().dms_enabled()


# ---------------------------------------------------------------------------
# Conversational follow-ups (who.replies: conversational, the default).
# After the bot's reply to a HUMAN is actually sent in a channel, that
# person's next messages there within FOLLOW_UP_WINDOW_SECS count as
# addressed, without an @mention or a Discord reply. Only that person;
# only the bot's own replies (re)open the window, so it cannot run on
# without the bot; a message that addresses someone else (a reply to
# another person's message, or an @mention of another user or a role)
# closes it and is not a follow-up. Threads are their own channels here.
# Follow-ups are coalesced, gated and ledgered like mentions, and the
# queue item says follow_up: true; the reconciliation sweep never
# requeues them (a missed follow-up is not a dropped mention).
# state["follow_up"]: {"<channel>:<user>": time the window opened}
# ---------------------------------------------------------------------------
def open_follow_up(state, channel_id, user_id):
    return _conversation().open_follow_up(state, channel_id, user_id)


def addresses_someone_else(msg):
    return _conversation().addresses_someone_else(msg)


def follow_up_open(state, msg):
    return _conversation().follow_up_open(state, msg)


def own_replies_in_run(history, msg):
    return _conversation().own_replies_in_run(history, msg)


def _owner_messenger():
    return OwnerMessenger(lambda: OWNER_USER_IDS, guard_outgoing, api_post)


def _update_notices():
    return UpdateNotices(
        UPDATE_CHECK_FILE,
        UPDATE_REQUEST_FILE,
        _owner_messenger(),
        updates_enabled,
        rest_backing_off,
        _version_installed,
        _update_announced,
    )


def _admin_digests():
    return AdminDigests(
        _directory_name,
        _USER_NAMES,
        GUILDS,
        _owner_messenger().notify,
        rest_backing_off,
        _ADMIN_CHANGES,
        _ADMIN_LOCK,
    )


def notify_owners(text, components=None, flags=None):
    return _owner_messenger().notify(text, components, flags)


def dm_owner(uid, payload):
    return _owner_messenger().dm(uid, payload)


# Servers none of the owners is in. A public bot can be
# added to any server by anyone with its install link, and there it would
# answer strangers on the owner's Muse. So in a server where no owner is a
# member it answers nobody until an owner chooses When it answers for that
# server itself (Configure on the join DM, /settings, a DM, config.yaml),
# and the server does not count as shared for who.dms_from:
# shared-servers. Membership is checked when the bot joins a server (it
# counts as ownerless until a check says otherwise), then hourly while no
# owner is there; failed checks back off and the oldest due go first, a
# few per pass. A first run (no state.json, or a lost one) checks every
# server at once: a lost state.json must not open the gate. Servers
# known from before this check existed are not checked. An owner's own
# message in a server settles it at once. The record lives in
# state.json, so a restart keeps it.
# state["owner_in_guild"]: {gid: {"in": an owner is a member,
#   "checked": a check has answered, "next": when to check, "fails": n}}
# OWNER_IN_GUILD mirrors it: True, False (no owner), None (not checked
# yet; gated like False).
OWNER_IN_GUILD = {}


def _owner_presence():
    return OwnerPresence(
        lambda: OWNER_USER_IDS,
        settings,
        api_get,
        rest_backing_off,
        OWNER_IN_GUILD,
        _OWNER_SPOKE,
    )


def _join_notices():
    return JoinNotices(_owner_presence(), _owner_messenger(), rest_backing_off, SETTINGS_PREFIX)


def mirror_owner_presence(state):
    return _owner_presence().restore(state)


def note_guild_join_for_owners(state, gid):
    return _owner_presence().joined(state, gid)


def check_owner_presence(state, guilds, joined=(), every=False):
    return _owner_presence().check(state, guilds, joined, every)


# Servers where an owner spoke since the last check (main loop and the
# gateway path), folded into state by check_owner_presence.
_OWNER_SPOKE = {}


def owners_absent(gid):
    return _owner_presence().absent(gid)


def _guild_lifecycle():
    presence = _owner_presence()
    return GuildLifecycle(
        _discovery_state(),
        CHANNELS,
        presence,
        JoinNotices(presence, _owner_messenger(), rest_backing_off, SETTINGS_PREFIX),
        settings,
        publish_directory,
        GUILD_ALLOWLIST,
        _stale_labels,
    )


def sync_guilds(state, guilds, channels):
    return _guild_lifecycle().sync(state, guilds, channels)


_stale_labels = set()


def warn_stale_labels():
    return _guild_lifecycle().warn_labels()


def flush_join_notices(state, guilds, channels):
    return _join_notices().flush(state, guilds, channels)


def on_guild_event(state, ev):
    if _guild_lifecycle().on_event(state, ev):
        _DISCOVERY_REFRESH.request()


# ---------------------------------------------------------------------------
# Update notices and interactions. Versions are releases: semver tags
# vX.Y.Z. watchdog.sh records a new release in update_check.json
# (latest, installed, entries, action_needed; see docs/PROTOCOL.md). The
# monitor DMs each owner about it once, with [Update now] / [Later]
# buttons. A click arrives as a gateway INTERACTION_CREATE and is
# answered on its own thread, never behind the main loop's REST work:
# Discord fails an interaction that is not acknowledged within 3 seconds.
# "Update now" writes update_request.json, which hooks/update-watch.sh
# turns into a wake of the owner's Muse in its main chat (it follows
# docs/UPDATING.md to that exact release). "Later" only declines: a version is
# offered once, and a newer one is offered again.
# ---------------------------------------------------------------------------
UPDATE_CHECK_FILE = os.path.join(BASE_DIR, "update_check.json")
UPDATE_REQUEST_FILE = os.path.join(BASE_DIR, "update_request.json")
# The newest announced version, mirrored from state.json for the
# interaction thread (only the main loop touches state).
_update_announced = {"version": None}


def updates_enabled():
    return bool(settings().updates_notify)


def _version_installed(tag, timeout=5):
    return version_installed(BASE_DIR, tag, timeout)


def announce_update(state):
    return _update_notices().announce(state)


def telemetry_notice():
    """DM the owners once when usage stats have not reached their
    endpoint for days (telemetry.py, take_notice)."""
    text = TM.take_notice(settings)
    if text:
        notify_owners(text)


def apply_record(state, rec):
    kind = rec.get("kind")
    if kind == "update_choice":
        _update_notices().apply_choice(state, rec)
    elif kind == "hold_choice":
        _reply_review().apply_choice(state, rec)
    elif kind == "proactive_yes":
        PRO.apply_button(rec)


# Interaction dispatch: one handler per key, the custom_id prefix (the
# text before the first ":") for buttons and other components, or
# "/<name>" for a slash command. A handler gets (interaction, the rest of
# the custom_id, the clicking user's id, record) and returns the callback
# response; record(dict) hands a state change to the main loop.
INTERACTION_HANDLERS = {}  # key -> (handler, owner_only)
INTERACTION_CALLBACK_TIMEOUT = 2.5  # inside Discord's 3s deadline


def register_interaction(key, handler, owner_only=True):
    INTERACTION_HANDLERS[key] = (handler, owner_only)


def dispatch_interaction(ix, record):
    data = ix.get("data") or {}
    if ix.get("type") in (3, 5):  # MESSAGE_COMPONENT, MODAL_SUBMIT
        key, _, rest = str(data.get("custom_id") or "").partition(":")
    elif ix.get("type") == APPLICATION_COMMAND_TYPE:
        key, rest = "/" + str(data.get("name") or ""), ""
    else:
        return None  # nothing to answer (e.g. autocomplete)
    entry = INTERACTION_HANDLERS.get(key)
    if entry is None:
        return ephemeral("That control is no longer active.")
    handler, owner_only = entry
    uid = interaction_user_id(ix)
    if owner_only and uid not in OWNER_USER_IDS:
        return ephemeral("Only this bot's owners can use that.")
    return handler(ix, rest, uid, record)


def interaction_callback(ix, response):
    """Anonymous interaction token auth, independent of Bot-token failures."""
    path = (
        f"/interactions/{urllib.parse.quote(str(ix['id']), safe='')}/"
        f"{urllib.parse.quote(str(ix['token']), safe='')}/callback"
    )
    response = guard_outgoing(response, "an interaction response")
    discord_request(
        "POST",
        path,
        response,
        options=RequestOptions(timeout=INTERACTION_CALLBACK_TIMEOUT, anonymous=True),
    )


def _interaction_thread_main(ix_inbox, record):
    """Answer interactions as they arrive (fed by the gateway thread)."""
    while True:
        ix = ix_inbox.get()
        try:
            response = dispatch_interaction(ix, record)
        except Exception as e:
            print(f"Interaction handler error: {e}", flush=True)
            response = ephemeral("Something went wrong; try again.")
        if response is None:
            continue
        try:
            interaction_callback(ix, response)
        except Exception as e:
            print(f"Interaction response failed: {e}", flush=True)


def on_update_button(ix, rest, uid, record):
    return _update_notices().on_button(ix, rest, uid, record)


register_interaction("mdupd", on_update_button)


# ---------------------------------------------------------------------------
# never_share backstop. Before a reply goes anywhere but an owner's own
# DM, its text is checked against privacy.never_share (case-insensitive,
# whole phrases). A hit is not sent: the draft is held in
# state["holds"] and the owners get it in a DM with [Send anyway] /
# [Discard] (owners only). Holding settles the queue item (it leaves the
# queue, so the wake hook never hands it to a worker again, and is
# ledgered as skipped, so the sweep leaves it alone). Send anyway puts
# it back through the outbox as the original reply; Discard and the
# 24-hour expiry drop it. The phrase list is never logged.
# ---------------------------------------------------------------------------
# hold id -> held at, mirrored from state.json for the interaction thread
# (only the main loop touches state).
_holds_live = {}


def _reply_review():
    return ReplyReview(settings, _owner_messenger(), DM_CHANNEL_USER, _settle, _holds_live)


def never_share_hit(state, item, qid, parts):
    return _reply_review().hit(state, item, qid, parts)


def hold_reply(state, item, qid, parts):
    return _reply_review().hold(state, item, qid, parts)


def expire_holds(state):
    return _reply_review().expire(state)


def on_hold_button(ix, rest, uid, record):
    return _reply_review().on_button(ix, rest, uid, record)


register_interaction("mdhold", on_hold_button)


def on_proactive_button(ix, rest, uid, record):
    """mdpro:yes:<token>: the Yes on the monitor's own question to one
    person (proactive.py, asks); anyone may click, only theirs counts."""
    return PRO.on_button(ix, rest, uid, record) or ephemeral("That button is no longer active.")


register_interaction("mdpro", on_proactive_button, owner_only=False)


# ---------------------------------------------------------------------------
# /settings is rendered and dispatched by discord.py (discord_connection.py).
# This context exposes the existing settings engine and discovery directory.
# ---------------------------------------------------------------------------
SETTINGS_PREFIX = "mdset"
SETTINGS_COMMAND = {
    "name": "settings",
    "type": 1,  # CHAT_INPUT
    "description": "See or change this bot's settings (owners; server admins can make it quieter)",
    # Interaction context types: 0 GUILD, 1 BOT_DM (not 2, group DMs).
    "contexts": [0, 1],
    # Installation contexts: 0 GUILD_INSTALL only (no user installs).
    "integration_types": [0],
    # Visible to every member: owners need it in any server, server
    # admins in theirs; settings_ui refuses everyone else.
    "default_member_permissions": None,
}
# The message context command (right-click a message, Apps): why the
# bot did not answer it. Discord may refuse "?" in the name; then the
# second name is registered (WHY_NAMES[1]).
WHY_NAMES = ("Why no reply?", "Why no reply")
WHY_COMMAND = {
    "name": WHY_NAMES[0],
    "type": 3,  # MESSAGE
    "contexts": [0, 1],
    "integration_types": [0],
    # Manage Server: ordinary members don't see it in the menu (owners
    # use Why no reply? on the /settings pages anyway).
    "default_member_permissions": "32",
}

# Directory for SettingsCtx, built on the main loop from discovery and
# the gateway, published through the thread-safe _DIRECTORY_VIEW snapshot.
_MEMBER_COUNTS = {}  # guild id -> member count (approximate)
_GUILD_CHANNELS = {}  # guild id -> [{"id","name","type","parent_id",..}]
_DIRECTORY_VIEW: DirectorySnapshot
# The same snapshot on disk, for bin/settings.py (places, names in its
# sentences): written atomically, only when it changed.
DIRECTORY_FILE = os.path.join(BASE_DIR, "directory.json")


def publish_directory():
    return _DIRECTORY_VIEW.update(GUILDS, CHANNELS, _MEMBER_COUNTS, _GUILD_CHANNELS)


# Private settings screens and drafts, keyed by (user_id, random token).
# Stored only in memory: restart or expiry discards uncommitted edits.
_SESSIONS = {}
_SESSIONS_LOCK = threading.Lock()
SESSIONS_MAX = 200


# Pending admin-change digests: guild id -> {"first": t, "changes": []}.
_ADMIN_CHANGES = {}
_ADMIN_LOCK = threading.Lock()


class SettingsCtx:
    """Settings/directory access for Discord callbacks and worker threads.
    Invalid writes and undo operations raise muse_settings.SettingsError.
    """

    def __init__(self):
        self.now = time.time()
        self.bot_id = str(BOT_ID or "")
        self.owners = set(str(o) for o in self.settings.owners)

    @property
    def settings(self):
        """The current muse_settings.Settings (reloaded on change, so a
        write made through this ctx is visible right away)."""
        return settings()

    def guilds(self):
        return _DIRECTORY_VIEW.guilds()

    def channels(self, guild_id):
        return _DIRECTORY_VIEW.channels(guild_id)

    def write(self, key, value, by):
        """One change by `by` (its role inferred: a non-owner is an
        admin, held to the admin rules)."""
        with _OVERLAY_LOCK:
            MS.set_value(_SETTINGS.config_path, key, value, actor=MS.WriteActor(by=by))
        settings()

    def undo(self, scope_prefix, by, **kw):
        """muse_settings.undo: revert the newest change (and the rest of
        its group) at one scope (scope_prefix as muse_settings.
        scope_prefix gives it, or a Place); the key reverted, or None.
        kw: children, admin_gid, expect (see muse_settings.undo)."""
        if kw.get("admin_gid"):
            kw.setdefault("info", self)
        with _OVERLAY_LOCK:
            actor = MS.WriteActor(
                by, admin_gid=kw.pop("admin_gid", None), info=kw.pop("info", None)
            )
            out = MS.undo(_SETTINGS.config_path, scope_prefix, actor, **kw)
        settings()
        return out

    def apply(self, changes, by, role, group=None, admin_gid=None):
        """muse_settings.apply: several changes in one write and one undo
        group. role ("owner" or "admin") is required; an admin's changes
        are checked against the admin rule with admin_gid and this ctx's
        channel lists."""
        with _OVERLAY_LOCK:
            out = MS.apply(
                _SETTINGS.config_path,
                changes,
                actor=MS.WriteActor(by=by, group=group, role=role, admin_gid=admin_gid, info=self),
            )
        settings()
        return out

    def undo_group(self, group, by, undo_id=None):
        """muse_settings.undo_group: undo one specific group (owners
        only)."""
        with _OVERLAY_LOCK:
            out = MS.undo_group(_SETTINGS.config_path, group, by, role="owner", undo_id=undo_id)
        settings()
        return out

    def display_name(self, user_id):
        """A person's name the monitor has seen (None when unknown)."""
        return _USER_NAMES.get(str(user_id))

    def session_peek(self, user_id, key):
        """The payload session_put kept for (user_id, key), or None
        (never kept, dropped, or expired). Not consumed."""
        with _SESSIONS_LOCK:
            e = _SESSIONS.get((str(user_id), str(key)))
            if e is None or e[1] < time.time():
                _SESSIONS.pop((str(user_id), str(key)), None)
                return None
            return e[0]

    def session_put(self, user_id, key, payload, ttl=900):
        now = time.time()
        with _SESSIONS_LOCK:
            for k in [k for k, v in _SESSIONS.items() if v[1] < now]:
                del _SESSIONS[k]
            while len(_SESSIONS) >= SESSIONS_MAX:
                _SESSIONS.pop(next(iter(_SESSIONS)))
            _SESSIONS[(str(user_id), str(key))] = (payload, now + ttl)

    def session_drop(self, user_id, key):
        with _SESSIONS_LOCK:
            _SESSIONS.pop((str(user_id), str(key)), None)

    def recent_outcomes(self, place, limit=20):
        """The newest mention-ledger entries for a place (a Place from
        muse_settings: a server, a channel and its threads, or DMs),
        newest first: message_id, channel_id, guild_id, parent_id,
        author_id (None in old entries), author,
        status, code, detail, reason, at."""
        where = _DIRECTORY_VIEW.locations(include_unmonitored=True)
        out = []
        fired = watch_store().read()["fired"]  # reminders: not messages
        for mid, e in _ledger_load()["entries"].items():
            if not isinstance(e, dict) or mid in fired:
                continue
            o = _outcome(mid, e, where)
            kind = getattr(place, "kind", None)
            if kind == "dms":
                ok = o["guild_id"] is None and o["dm"]
            elif kind == "server":
                ok = o["guild_id"] == place.guild_id
            elif kind == "channel":
                ok = (o["parent_id"] or o["channel_id"]) == place.channel_id
            else:
                ok = True
            if ok:
                out.append(o)
        out.sort(key=lambda o: o["at"], reverse=True)
        return out[: max(0, int(limit))]

    def outcome(self, message_id):
        """recent_outcomes' entry for one message, or None."""
        e = _ledger_load()["entries"].get(str(message_id))
        if not isinstance(e, dict):
            return None
        where = _DIRECTORY_VIEW.locations()
        return _outcome(str(message_id), e, where)

    def names(self, some_id):
        """A server's or channel's name for muse_settings' sentences."""
        return _directory_name(some_id) or settings().labels.get(str(some_id))

    def reload(self):
        """A fresh Settings through the monitor's own watcher, so the
        pages and the main loop see the same state."""
        return settings()

    def notify_owners_admin_change(self, guild_id, user_id, key, old, new):
        _admin_digests().note(guild_id, user_id, key, old, new)

    def log(self, msg):
        print(f"/settings: {msg}", flush=True)


def _outcome(mid, e, where):
    """One ledger entry for /settings. Its server comes from the entry
    (old entries have none), else from discovery; an entry is a DM's
    only when it says so or its channel is a known DM channel (an
    unmapped server channel is never shown under DMs)."""
    cid = str(e.get("channel_id") or "")
    code = e.get("code") or ledger_code(e.get("status"), e.get("reason"))
    if e.get("guild_id"):
        gid, parent = e["guild_id"], e.get("parent_id")
    else:
        gid, parent = where.get(cid, (None, None))
        info = CHANNELS.get(cid)
        if gid is None and info:
            gid = info.get("guild_id")
            parent = info.get("parent_id") if info.get("thread") else None
    dm = gid is None and bool(
        e.get("dm")
        or cid in DM_CHANNELS
        or cid in DM_CHANNEL_USER
        or code in ("dm-off", "dm-not-allowed")
    )
    return {
        "message_id": mid,
        "channel_id": cid or None,
        "guild_id": gid,
        "parent_id": parent,
        "dm": dm,
        "author_id": e.get("author_id"),
        "author": e.get("author"),
        "status": e.get("status"),
        "code": code,
        "detail": e.get("detail") or {},
        "reason": e.get("reason"),
        "at": e.get("updated") or _snowflake_ts(mid),
    }


def _directory_name(some_id):
    return _DIRECTORY_VIEW.name(some_id)


def admin_digest_text(gid, changes):
    return _admin_digests().text(gid, changes)


def flush_admin_digests(force=False):
    return _admin_digests().flush(force)


# ---------------------------------------------------------------------------
# Owner-DM capabilities: handed only to the worker serving that DM.
# The persisted "nl_nonces" key is retained for existing queued DMs.
# ---------------------------------------------------------------------------
OWNER_DM_NONCES_FILE = os.path.join(BASE_DIR, "owner_dm_nonces.json")
_OWNER_DM_NONCES_WRITTEN: dict[str, dict[str, str] | None] = {"body": None}


def _capabilities():
    return CapabilityIssuer(
        files=CapabilityFiles(
            PrivateFile(OWNER_DM_NONCES_FILE, _OWNER_DM_NONCES_WRITTEN),
            PrivateFile(OWNER_ITEMS_FILE, _OWNER_ITEMS_WRITTEN),
            PrivateFile(ITEM_NONCES_FILE, _ITEM_NONCES_WRITTEN),
        ),
        auth=_owner_auth(),
        targets=_owner_targets(),
        queue=_queue_files(),
        notify=notify_owners,
        upkeep_clock=_OWNER_DM_UPKEEP,
        reply_alert=_ITEM_NONCES_ALERT,
    )


def owner_dm_mint_nonce(state, mid, channel_id=None, fragments=()):
    return _capabilities().mint_owner_dm(state, mid, channel_id, fragments)


def owner_dm_write_nonces(state):
    return _capabilities().write_owner_dms(state)


# ---------------------------------------------------------------------------
# Owner DMs are bound to their wake. Any wake can be started by anyone's
# message, so a worker may be reading a stranger's prompt injection. The
# owner-DM nonce (above) is handed only to a wake that holds nothing but
# owner DMs (hooks/mention-watch.sh), so it proves that the outbox item
# was written in such a run. Every outbox item that answers an owner's
# DM, settles it (silent, reactions, a reply under one of its
# fragments) or posts in an owner's DM channel must carry that item's
# nonce as owner_dm_nonce. Without it the item is refused before anything else looks at
# it: set aside in reply_outbox.rejected.json, logged, and the queue
# item stays open for the owner-DM-only wake that really holds it.
# Other items are untouched.
# ---------------------------------------------------------------------------
_OWNER_DM_UPKEEP = {"next": 0.0}


def _owner_auth():
    return OwnerAuthorization(
        lambda: OWNER_USER_IDS,
        CHANNELS,
        _GUILD_CHANNELS,
        DM_CHANNEL_USER,
        api_get,
        _CHANNEL_LOOKUP,
    )


def _owner_targets():
    return CommandTargets(CHANNELS, _GUILD_CHANNELS)


def _owner_memory():
    return OwnerMemory(
        MEMORY_FILE,
        _owner_auth(),
        lambda: bool(settings().top("memory.save_dms")),
        secret_in,
        _MEMORY_MISSING,
    )


def _meta_owner_dm(meta):
    return owner_dm_metadata(meta, OWNER_USER_IDS)


def is_owner_dm_item(state, mid):
    return _owner_auth().is_owner_dm_item(state, mid)


def owner_dm_channels(state):
    return _owner_auth().dm_channels(state)


def queue_index(state):
    return _queue_files().index(state, OWNER_USER_IDS)


# Channels looked up from the API (owner_dm_gate): channel id -> (is a
# DM, its recipients). Neither changes; who the owners are can.
_CHANNEL_LOOKUP = {}


def owner_dm_gate(state, item, index=None):
    return _owner_auth().gate(state, item, index)


# ---------------------------------------------------------------------------
# What an owner tells the bot in their own DM reaches the Muse's memory:
# the worker lists the lasting facts in the reply's "remember" field and
# the monitor appends them to the Muse's memory file (MEMORY.md), which
# the Muse loads in every chat. Only an owner's own DM, proved by its
# live nonce: memory feeds the Muse itself, so a stranger's message must
# never write there. Each fact becomes one plain bullet line; nothing
# that looks like a secret; at most MEMORY_PER_DAY a day.
# memory.save_dms: off turns it off.
# ---------------------------------------------------------------------------
MEMORY_FILE = os.environ.get("MUSE_MEMORY_FILE") or os.path.expanduser("~/MEMORY.md")
_MEMORY_MISSING = {"logged": False}


def remember_check(state, item, qid, index):
    return _owner_memory().check(state, item, qid, index)


def save_facts(state, item, t):
    return _owner_memory().save(state, item, t)


def owner_dm_upkeep(state, force=False):
    return _capabilities().upkeep(state, force)


# ---------------------------------------------------------------------------
# Owner commands are bound to their wake too. bin/discord-api.py acts in
# a channel (read, post, thread, react, pin, unpin, edit, delete-own)
# only for an open queue item the monitor stamped owner_command (an
# owner's own message, where abilities.owner_commands is on), with that
# item's owner_command_nonce: 128 random bits kept in state.json
# (owner_items) and in owner_items.json (mode 600, the open
# owner-command items only), never in reply_queue.json. An owner's DM
# uses its owner_dm_nonce. The wake hook hands the nonce only to a run
# that holds nothing but owner items (they wake alone), so a run woken
# by someone else's message cannot act through the tool even when it
# knows the owner's message id (it is in the channel history).
# ---------------------------------------------------------------------------
OWNER_ITEMS_FILE = os.path.join(BASE_DIR, "owner_items.json")
_OWNER_ITEMS_WRITTEN = {"body": None}
# A new owner item is kept this long even before the queue file shows
# it (it is noted before the queue write).


# Bare channel names ("post this in announcements") that are never one.


def owner_targets(content, guild_id=None):
    return _owner_targets().resolve(content, guild_id)


def note_owner_item(state, mid, dm=False, command=None):
    return _capabilities().note_command(state, mid, dm, command)


def owner_items_write(state, check=False):
    return _capabilities().write_commands(state, check)


def owner_items_upkeep(state, queue, now):
    return _capabilities().upkeep_commands(state, queue, now)


# ---------------------------------------------------------------------------
# Every queue item is bound to the wake that holds it. The monitor mints
# each queued item (fires included) a reply_nonce: 128 random bits, kept
# in state.json (reply_nonces) and item_nonces.json (mode 600, the open
# items only, read by the wake hook), never in reply_queue.json. The hook
# adds it only to the wake that holds the item's lease, and every outbox
# item must echo its item's (own_channel_problem), so a run can answer,
# silence or put a schedule action on only the items it was handed, not
# on ones whose ids it saw in history. Spent when the item settles.
# ---------------------------------------------------------------------------
REPLY_NONCE_FIELD = "reply_nonce"
ITEM_NONCES_FILE = os.path.join(BASE_DIR, "item_nonces.json")
_ITEM_NONCES_WRITTEN: dict[str, dict[str, str] | None] = {"body": None}
_ITEM_NONCES_ALERT: dict[str, str | None] = {"day": None}


def reply_nonce_mint(state, mid):
    return _capabilities().mint_reply(state, mid)


def reply_nonces_write(state, check=False):
    return _capabilities().write_replies(state, check)


def reply_nonces_upkeep(state, queued, now):
    return _capabilities().upkeep_replies(state, queued, now)


# Registering the command: a global application command, checked once per
# run (GET, then POST when missing or PATCH when it differs; never a bulk
# PUT, which would delete any other command). A failure is retried on a
# later cycle and never stops the monitor.
_APP_ID = {"id": None}
_COMMAND_REG = {"done": False, "next_try": 0.0, "last_error": None}
COMMAND_RETRY_SECS = 60


def _command_differs(have, want):
    for k, v in want.items():
        got = have.get(k)
        if isinstance(v, list):
            got = sorted(got) if isinstance(got, list) else got
            v = sorted(v)
        if got != v:
            return True
    return bool(have.get("options"))


def ensure_settings_command(app_id):
    """Make the global /settings command match SETTINGS_COMMAND, and the
    "Why no reply?" message command WHY_COMMAND. Returns "created",
    "updated" or "unchanged" (the first that applies: /settings, then
    the message command); raises on failure."""
    base = f"/applications/{urllib.parse.quote(str(app_id), safe='')}/commands"
    have = api_get(base)
    if not isinstance(have, list):
        raise ValueError(f"unexpected command list: {str(have)[:80]}")
    r1 = _ensure_command(base, have, SETTINGS_COMMAND, (SETTINGS_COMMAND["name"],))
    r2 = _ensure_command(base, have, WHY_COMMAND, WHY_NAMES)
    return r1 if r1 != "unchanged" else r2


def _ensure_command(base, have, want, names):
    """Create or update one command (names: what it may be called; the
    one already registered is kept). A message command name Discord
    refuses (400) is retried with the next name."""
    mine = [
        c
        for c in have
        if isinstance(c, dict) and c.get("name") in names and c.get("type", 1) == want["type"]
    ]
    if mine:
        want = dict(want, name=mine[0]["name"])
        if not _command_differs(mine[0], want):
            return "unchanged"
        cid = urllib.parse.quote(str(mine[0].get("id")), safe="")
        api_patch(f"{base}/{cid}", want)
        return "updated"
    for i, name in enumerate(names):
        try:
            api_post(base, dict(want, name=name))
            return "created"
        except Exception as e:
            if getattr(e, "code", None) != HTTPStatus.BAD_REQUEST or i == len(names) - 1:
                raise
            print(
                f"Discord refused the command name {name!r}; trying {names[i + 1]!r}.", flush=True
            )
    return "unchanged"


def register_commands(gateway_status=None):
    """Main loop, every tick until it succeeds once (throttled to one
    try per COMMAND_RETRY_SECS). Never raises."""
    reg = _COMMAND_REG
    now = time.time()
    if reg["done"] or now < reg["next_try"] or BOT_USER_ID is None or rest_backing_off():
        return None
    reg["next_try"] = now + COMMAND_RETRY_SECS
    try:
        if not _APP_ID["id"]:
            app = (gateway_status or {}).get("application") or {}
            _APP_ID["id"] = app.get("id") if isinstance(app, dict) else None
        if not _APP_ID["id"]:
            _APP_ID["id"] = (api_get("/applications/@me") or {}).get("id")
        if not _APP_ID["id"]:
            raise ValueError("application id unknown")
        result = ensure_settings_command(_APP_ID["id"])
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        if err != reg["last_error"]:
            hint = (
                " (the bot's invite may lack the applications.commands scope; see docs/SETUP.md)"
                if getattr(e, "code", None) == HTTPStatus.FORBIDDEN
                else ""
            )
            print(
                f"/settings command registration failed ({err}){hint}; "
                f"retrying in {COMMAND_RETRY_SECS}s.",
                flush=True,
            )
        reg["last_error"] = err
        return None
    reg["done"] = True
    reg["last_error"] = None
    if result != "unchanged":
        print(
            f"/settings command {result} (global; it can take a moment to show up in Discord).",
            flush=True,
        )
    return result


def is_mention(msg):
    return _conversation().is_mention(msg)


def trigger_kind(msg, guild_id=None, state=None):
    """Classify how a server message addresses the bot, with the settings
    of its scope: "mention" (a platform @mention or a Discord reply to
    the bot: direct address), "follow_up" (only when state is given: the
    author's conversational follow-up window in this channel is open), or
    None. who.replies: off answers nobody; other bots trigger by direct
    address only, and never under who.bots: ignore; who.allow /
    who.ignore apply to everyone but the owners. The bot's own messages
    never trigger."""
    return trigger_verdict(msg, guild_id, state)[0]


def addresses_bot(msg):
    return _conversation().addresses_bot(msg)


def trigger_verdict(msg, guild_id=None, state=None):
    return _admission().trigger_verdict(msg, guild_id, state)


def note_refused(msg, cname, reason):
    """A message that addressed the bot directly but that the settings
    decline: ledgered as skipped, so the reconciliation sweep (which
    judges by the settings in force when it runs) never answers it
    later, after a setting is relaxed. A deliberately ignored message
    stays ignored. Ordinary chatter is never ledgered."""
    author = _who(msg)
    ledger_record(
        msg["id"],
        "skipped",
        f"settings: {reason}",
        context=LedgerContext(
            channel_id=msg.get("channel_id"),
            author=author.get("username"),
            author_id=author.get("id"),
        ),
    )
    print(f"Not answering {author.get('username')} in {cname}: {reason}.", flush=True)


def is_reply_to_bot(msg):
    return _conversation().is_reply_to_bot(msg)


def get_reply_context(msg):
    """If msg used Discord's reply feature, return the referenced message as
    {author, author_id, content, is_bot} so the worker knows whose
    conversation this message belongs to. Prefers the embedded
    referenced_message; falls back to one API fetch. Returns None when the
    message is not a reply or the referenced message is gone."""
    ref = msg.get("message_reference") or {}
    ref_id = ref.get("message_id")
    if not ref_id:
        return None
    rm = msg.get("referenced_message")
    if rm is None:
        try:
            rm = api_get(f"/channels/{msg['channel_id']}/messages/{ref_id}")
        except Exception:
            return None
    if not rm:
        return None
    author = _who(rm)
    return {
        "author": author.get("username", "?"),
        "author_id": author.get("id"),
        "content": (rm.get("content") or "")[:500],
        "is_bot": author.get("id") == BOT_ID,
    }


_ACTIVITY: GatewayActivity


def _bursts(state):
    return PendingBursts(
        state,
        _ACTIVITY,
        CoalescePolicy(COALESCE_SECS, BOT_COALESCE_SECS, is_bot_author),
        enqueue_message,
        BurstEffects(
            note_ingested,
            lambda mid: mid in _quarantine_ids() or _ledger_terminal(mid),
            ledger_record_many,
        ),
        time.time,
    )


def hold_fragment(state, channel_id, msg):
    return _bursts(state).hold(channel_id, msg)


def stage_trigger(state, msg, cname, kind, place=None):
    return _bursts(state).stage(msg, cname, kind, place)


def stage_found(state, msg, cname, guild_id, kind):
    """Stage one classified guild trigger the moment it is classified, in
    arrival order, so a non-trigger follow-up later in the same poll
    batch or gateway drain finds the open burst (hold_fragment) instead
    of being classified before its trigger was staged. Talk scope: a
    trigger in a channel the bot cannot send in is never staged; it is
    logged once per channel and ledgered as skipped so the sweep leaves
    it alone."""
    info = CHANNELS.get(msg["channel_id"])
    if info is not None and info.get("can_send") is False:
        _note_no_send(msg, cname)
        return
    stage_trigger(state, msg, cname, kind, place=MessagePlace(guild_id=guild_id))


def note_activity(channel_id, user_id, msg_id=None):
    _ACTIVITY.note(channel_id, user_id, msg_id, now=time.time())


def pending_wait(state, now):
    return _bursts(state).wait(now)


def flush_pending(state, queue, force=False):
    return _bursts(state).flush(queue, force)


# What a monitor send may ping: users (the worker addresses people with
# <@id>) and the author of the message replied to. Never roles,
# @everyone or @here, whatever the text says; the zero-width rewrite in
# sanitize_reply is a second layer, not the only one.


def _upload_message(path, payload, blobs):
    return _api_call(
        lambda: discord_request(
            "POST", path, payload, options=RequestOptions(files=blobs, timeout=60)
        )
    )


def _put_reaction(path):
    return _api_call(lambda: discord_request("PUT", path))


def _message_sender():
    return MessageSender(guard_outgoing, api_post, _upload_message, _put_reaction)


def _part_delivery():
    return PartDelivery(_message_sender(), send_typing, time.sleep)


def _typing_service():
    return TypingIndicators(
        api_post,
        rest_backing_off,
        _queue_files(),
        time,
        TypingState(_typing_sources, _typing_last, _typing_lock),
    )


def send_reply(channel_id, text, reply_to=None, components=None, delivery=None):
    return _message_sender().reply(channel_id, text, reply_to, components, delivery)


def send_typing(channel_id):
    return _typing_service().send(channel_id)


# ---------------------------------------------------------------------------
# Typing indicator. Discord shows one typing ping for ~10s, and one main
# loop iteration can take longer (media downloads, a multi-part send),
# so a daemon thread pings every active channel on its own clock and
# the indicator never lapses. The main loop only says which
# channels are active (typing_refresh): a channel types while it has a
# pending burst (not in enqueue back-off) or a queue item, for at most
# TYPING_MAX_SECS per burst or item so a dead worker does not type
# forever. process_outbox stops an item's typing right before its reply
# goes out (sending a message ends the indicator; the beats in
# send_fragmented cover multi-part replies).
# ---------------------------------------------------------------------------
_typing_lock = threading.Lock()
_typing_sources = {}  # "burst:<key>" / "item:<id>" -> (channel_id, start)
_typing_last = {}  # channel_id -> time of the last typing ping


def typing_clear(keys, sent_in=None):
    return _typing_service().clear(keys, sent_in)


def typing_refresh(state, queue=None):
    return _typing_service().refresh(state, queue)


def _typing_thread_main():
    return _typing_service().run()


def add_reaction(channel_id, message_id, emoji):
    return _message_sender().reaction(channel_id, message_id, emoji)


def sanitize_reply(text):
    return reply_formatting.sanitize_reply(text, MAX_REPLY_CHARS)


def normalize_parts(item):
    return reply_formatting.normalize_parts(item, MAX_REPLY_CHARS)


def send_fragmented(channel_id, parts, reply_to=None, attachments=(), delivery=None):
    return _part_delivery().send(channel_id, parts, reply_to, attachments, delivery)


def send_reply_with_files(channel_id, text, files, reply_to=None, delivery=None):
    return _message_sender().files(channel_id, text, files, reply_to, delivery)


def sweep_generated():
    generated_media.sweep_generated(GENERATED_DIR)


def sweep_gen_state(state):
    """Drop generation approvals older than generated_media.TTL_SECS (they are
    single-use and short-lived by design) and bound the sent-file record.
    Approvals from before channel binding (a bare timestamp) go too."""
    approved = state.get("gen_approved", {})
    now = time.time()
    for mid in [
        m
        for m, a in approved.items()
        if not isinstance(a, dict) or now - a.get("ts", 0) > generated_media.TTL_SECS
    ]:
        approved.pop(mid, None)
    if len(state.get("gen_sent", [])) > GENERATED_SEND_HISTORY_LIMIT:
        state["gen_sent"] = state["gen_sent"][-GENERATED_SEND_HISTORY_LIMIT:]


def media_permission(state, scope, here, author_matches):
    return _response_policy().media_permission(state, scope, here, author_matches)


def note_media_sent(state, scope):
    return _response_policy().note_media_sent(state, scope)


def _gen_item_id(state, item):
    """The queue item an outbox item answers, as its trigger message id:
    reply_to, resolved through reply_aliases (the worker replies under
    the LAST burst fragment, while approvals and generated/<id>/ use the
    trigger). An explicit "item" field is honored only when it agrees,
    so a reply can never claim another item's approval and files."""
    reply_to = item.get("reply_to")
    resolved = None
    if reply_to is not None:
        resolved = str(state.get("reply_aliases", {}).get(str(reply_to), reply_to))
    claimed = item.get("item")
    if claimed is None:
        return resolved
    if resolved is not None and str(claimed) != resolved:
        print(
            f"Note: outbox item names item {claimed} but replies to "
            f"{reply_to} (item {resolved}); no attachments",
            flush=True,
        )
        return None
    return str(claimed)


def _gen_approval(state, item_id):
    """The live generation approval for a queue item ({"ts",
    "channel_id"}), or None."""
    a = state.get("gen_approved", {}).get(item_id) if item_id else None
    if not isinstance(a, dict) or time.time() - a.get("ts", 0) > generated_media.TTL_SECS:
        return None
    return a


def collect_media(msg):
    """Collect bounded attachments into this monitor's media directory."""
    return media_ingest.collect_media(msg, MEDIA_DIR)


def sweep_media():
    media_ingest.sweep_media(MEDIA_DIR)


def record_history(state, channel_id, messages):
    return _conversation().record_history(state, channel_id, messages)


# ---------------------------------------------------------------------------
# REST watermark vs gateway dedupe. state["last_seen"][cid] is the REST
# poll's watermark and only the poll advances it (or seeding, the first
# time a channel is seen): if the gateway advanced it, the first message
# after a gap of lost events would move it past the missed ones. Messages
# the gateway handled above the watermark are remembered in
# state["gw_seen"][cid] instead, the poll skips those, and both
# paths skip anything at or below the watermark: every message is
# processed once, by whichever path sees it first.
# ---------------------------------------------------------------------------
GW_SEEN_PER_CHANNEL = 1000
GW_SEEN_MAX_AGE_SECS = 24 * 3600


def gateway_seen(state, cid, mid):
    return mid in state.get("gw_seen", {}).get(cid, ())


def note_gateway_seen(state, cid, mid):
    """Record a message the gateway path handled. The first message ever
    seen in a channel seeds its REST watermark instead (history before it
    counts as seen, as with seed_channel_seen)."""
    if cid not in state["last_seen"]:
        state["last_seen"][cid] = mid
        return
    ids = state.setdefault("gw_seen", {}).setdefault(cid, [])
    ids.append(mid)
    del ids[:-GW_SEEN_PER_CHANNEL]


def advance_rest_watermark(state, cid, mid):
    """Move a channel's REST watermark to mid; gateway ids at or below it
    are covered by the watermark now and are dropped."""
    state["last_seen"][cid] = mid
    ids = state.get("gw_seen", {}).get(cid)
    if ids is not None:
        ids[:] = [i for i in ids if int(i) > int(mid)]
        if not ids:
            del state["gw_seen"][cid]


def prune_gateway_seen(state):
    """Drop gateway ids no poll will ever re-read (channels nobody polls,
    such as DMs under dm_policy: open)."""
    now = time.time()
    for cid in list(state.get("gw_seen", {})):
        ids = [i for i in state["gw_seen"][cid] if now - _snowflake_ts(i) < GW_SEEN_MAX_AGE_SECS]
        if ids:
            state["gw_seen"][cid] = ids
        else:
            del state["gw_seen"][cid]


def check_channel(channel_id, channel_name, state, guild_id=None):
    """Check for new messages in a channel and stage its triggers (see
    stage_found). Returns the (msg, kind) pairs staged (kind as in
    trigger_kind). Messages the gateway already handled are skipped."""
    last_seen = state["last_seen"].get(channel_id)
    if not last_seen:
        # Never seeded (discovery normally does it): history is history.
        seed_channel_seen(channel_id, channel_name, state)
        return []

    # 100 (Discord's max) so the poll keeps up with busy channels: it now
    # re-reads every message the gateway delivered since the last poll.
    params = {"limit": 100, "after": last_seen}

    query = urllib.parse.urlencode(params)
    try:
        messages = api_get(f"/channels/{channel_id}/messages?{query}")
    except Exception as e:
        print(f"Error reading #{channel_name}: {e}", flush=True)
        return []

    if not messages:
        return []

    record_history(state, channel_id, messages)

    newest = max(messages, key=lambda m: int(m["id"]))
    gw_done = set(state.get("gw_seen", {}).get(channel_id, ()))
    advance_rest_watermark(state, channel_id, newest["id"])

    triggers = []
    for msg in sorted(messages, key=lambda m: int(m["id"])):
        if msg["id"] in gw_done:
            continue  # the gateway path already handled it
        kind = classify_message(state, msg, channel_name, guild_id)
        if kind:
            stage_found(state, msg, channel_name, guild_id, kind)
            triggers.append((msg, kind))

    return triggers


# Message types people write: DEFAULT and REPLY. Everything else in a
# channel is a system notice ("X joined the server", a pin, ...).
CONVERSATION_TYPES = (0, 19)


def classify_message(state, msg, channel_name, guild_id=None):
    """Classify a single message exactly the way check_channel's loop does.

    Shared by the REST poll path and the gateway fast path so both ingest
    routes behave identically. Returns "mention", "follow_up", or None
    (folded into a burst, skipped, or not actionable).
    """
    if predates_join(state, msg, guild_id):
        # Older than the bot's arrival in this guild: never a trigger,
        # never a fragment. It can still reach the worker as history.
        return None
    if msg.get("type", 0) not in CONVERSATION_TYPES:
        # Discord's system notices ("X pinned a message", "X started a
        # thread", ...) carry the acting user as author: never a
        # trigger, a follow-up or a fragment.
        return None
    author = _who(msg)
    if author.get("id") == BOT_ID:
        return None  # the bot's own messages: never triggers or fragments
    watch_scan(msg, guild_id)  # Discord watches (cheap; never raises)
    TM.count("messages_seen")
    if (
        is_bot_author(author)
        and settings().get("who.bots", scope_for(guild_id, msg.get("channel_id"))) == "ignore"
    ):
        if addresses_bot(msg):
            note_refused(msg, channel_name, "who.bots: ignore")
        return None
    if hold_fragment(state, msg.get("channel_id"), msg):
        return None  # folded into a burst already being coalesced
    kind, refusal = trigger_verdict(msg, guild_id, state)
    if refusal:
        note_refused(msg, channel_name, refusal)
    return kind


# Discord does not let bots list DM channels: GET /users/@me/channels always
# returns an empty list for bot tokens. Instead, resolve each known user's DM
# channel directly with POST /users/@me/channels (create-or-get: returns the
# existing channel if there is one, creates an empty one otherwise, and never
# sends a message).
#
# The owners' DM channels, and those of the people who.dms_from names
# (a list of user ids), are resolved for REST polling (gap repair and
# the sweep) unless DMs are off (dms.who.replies: off). Anyone else's DMs
# cannot be enumerated (there is no DM list endpoint for bots), so they
# arrive via the gateway only, and only as who.dms_from allows: under the
# default (owners) the bot never even records a stranger's DM, so there
# is nothing to decline or deflect.
def dm_listed_people():
    return _admission().dm_listed_people()


def dm_poll_user_ids():
    return _admission().dm_poll_user_ids()


def dm_swept_author(uid):
    return _admission().dm_swept_author(uid)


# The DM channels the poll has resolved, {channel_id: recipient name}:
# the reconciliation sweep covers these as well as server channels.
# DM_CHANNEL_USER: {channel_id: recipient user id} (an owner's DM or a
# listed person's: never_share treats only the owners' as private).
DM_CHANNELS = {}
DM_CHANNEL_USER = {}


def list_dm_channels():
    """Return [(channel_id, recipient_username)] for known users' DM channels."""
    out = []
    for uid in dm_poll_user_ids():
        try:
            c = api_post("/users/@me/channels", {"recipient_id": uid})
        except Exception as e:
            print(f"DM channel resolve failed for {uid}: {e}", flush=True)
            continue
        if c and c.get("id"):
            recips = c.get("recipients") or []
            name = recips[0].get("username", uid) if recips else uid
            out.append((c["id"], name))
            DM_CHANNELS[c["id"]] = name
            DM_CHANNEL_USER[c["id"]] = str(uid)
    return out


def check_dm_channel(dm_id, dm_name, state):
    """Return unseen non-bot messages in a DM channel. In a DM every message
    is addressed to the bot, so no mention filtering is needed. The first
    time a DM channel is read (first run, or state.json deleted) its
    history counts as already seen, like a new channel's: the backlog is
    never one big fresh trigger. Messages the gateway already handled
    are skipped."""
    try:
        messages = api_get(f"/channels/{dm_id}/messages?limit=20")
    except Exception as e:
        print(f"DM poll error #{dm_name}: {e}", flush=True)
        return []
    last_seen = state["last_seen"].get(dm_id)
    if not messages:
        if not last_seen:
            state["last_seen"][dm_id] = now_snowflake()
        return []

    record_history(state, dm_id, messages)

    if not last_seen:
        newest = max(messages, key=lambda m: int(m["id"]))
        state["last_seen"][dm_id] = newest["id"]
        print(f"Tracking DM with {dm_name} from message {newest['id']}.", flush=True)
        return []
    new = [m for m in messages if int(m["id"]) > int(last_seen)]
    gw_done = set(state.get("gw_seen", {}).get(dm_id, ()))
    if new:
        newest = max(new, key=lambda m: int(m["id"]))
        advance_rest_watermark(state, dm_id, newest["id"])

    out = []
    for m in sorted(new, key=lambda m: int(m["id"])):
        if (
            m.get("author", {}).get("id") == BOT_USER_ID
            or m["id"] in gw_done
            or m.get("type", 0) not in CONVERSATION_TYPES
        ):
            continue
        TM.count("messages_seen")
        if not dm_sender_allowed(m.get("author")):
            note_dm_refused(m)
            continue
        if not hold_fragment(state, dm_id, m):
            out.append(m)
    return out


def note_dm_refused(msg):
    """A DM the DM settings decline (dm_sender_allowed). Only an owner's
    or a who.dms_from-listed person's is ledgered (see note_refused): the
    sweep covers only their DM channels, and a stranger's DMs stay
    unrecorded."""
    author = _who(msg)
    if dm_swept_author(author.get("id")) and not is_bot_author(author):
        note_refused(
            msg,
            "DM with " + (author.get("username") or "?"),
            dm_refusal(author) or "who.replies is off for DMs",
        )


def bot_run_length(history, msg):
    return _conversation().bot_run_length(history, msg)


def conversation_participants(history, msg, reply_ctx):
    return _conversation().conversation_participants(history, msg, reply_ctx)


def link_mentions(text, channel_id, state):
    return _conversation().link_mentions(text, channel_id, state)


def settings_card(scope, here, channel_id, speaker=None):
    """The per-message settings card for the worker (item "settings"):
    where it is, who is speaking, who can read the reply, and the style
    and privacy settings that steer it. In an owner's DM, memory is
    always "everything"."""
    speaker = speaker or Speaker()
    author_id = speaker.author_id
    from_bot = speaker.from_bot
    exchange = speaker.exchange
    dm = scope.is_dm
    owner = str(author_id) in OWNER_USER_IDS
    card: dict[str, object] = {"where": "dm" if dm else "server"}
    if not dm:
        info = CHANNELS.get(channel_id) or {}
        card["server"] = (
            (GUILDS.get(scope.guild_id) or {}).get("name")
            or info.get("guild_name")
            or scope.guild_id
        )
        card["channel"] = info.get("name") or channel_id
    card["speaker"] = "owner" if owner else ("bot" if from_bot else "member")
    card["audience"] = "private" if dm else "public"
    card["memory"] = "everything" if (dm and owner) else here["privacy.memory"]
    card["replies"] = here["who.replies"]
    card["length"] = here["style.length"]
    card["notes"] = here["style.notes"]
    if from_bot:
        card["bot_exchange"] = {"count": exchange + 1, "limit": here["who.bot_exchange_limit"]}
    return card


def queued_item_meta(state, qid, index):
    """Who and where a still-queued item answers (author_id, author,
    channel_id, kind, dm, bot, guild_id): its item_meta record, else
    (state.json lost) the queue file's own entry, which only the monitor
    writes. None when it is not in the queue (index: queue_index()).
    A schedule action and a "stop messaging me" both bind to it."""
    if qid not in index["channels"]:
        return None
    meta = (state.get("item_meta") or {}).get(qid)
    if isinstance(meta, dict):
        return meta
    q = next(
        (q for q in load_queue() if isinstance(q, dict) and str(q.get("message_id")) == qid), {}
    )
    return {
        "author_id": q.get("author_id"),
        "author": q.get("author"),
        "channel_id": q.get("channel_id"),
        "kind": q.get("kind"),
        "dm": q.get("kind") == "dm",
        "bot": bool(q.get("from_bot")),
        "guild_id": q.get("guild_id"),
    }


def started_item_id(now=None):
    """The queue id of an item the monitor starts (a reminder or watch
    fire, a heartbeat slot): a snowflake for now with random low bits,
    so it passes every id check and never names a real message."""
    return W.new_fire_id(now)


def note_item_meta(state, mid, **meta):
    """Remember who a queued item answers (bounded): the send path opens
    follow-up windows and applies the never_share backstop from it."""
    m = state.setdefault("item_meta", {})
    m[mid] = meta
    PRO.note_item(mid, meta)  # who talked to the bot (proactive.py)
    _keep_last(m, 300)


class _MonitorIngestConversation(Conversation):
    @staticmethod
    def collect_media(msg):
        return collect_media(msg)

    @staticmethod
    def get_reply_context(msg):
        return get_reply_context(msg)

    @staticmethod
    def settings_card(scope, here, channel_id, speaker=None):
        return settings_card(scope, here, channel_id, speaker)

    @staticmethod
    def watch_card(guild_id, dm, here, matches, speaker=None):
        return watch_card(guild_id, dm, here, matches, speaker)

    @staticmethod
    def is_forwarded(msg):
        return is_forwarded(msg)


class _MonitorIngestJournal:
    @staticmethod
    def ledger_record_many(mids, status, reason="", context=None):
        return ledger_record_many(mids, status, reason, context)

    @staticmethod
    def is_suspicious(content):
        return is_suspicious(content)

    @staticmethod
    def quarantine(msg, channel_name, matched_pattern, fragments=()):
        return quarantine(msg, channel_name, matched_pattern, fragments)

    @staticmethod
    def alias_fragments(state, trigger_id, fragments):
        return alias_fragments(state, trigger_id, fragments)

    @staticmethod
    def note_queued(state, mid):
        return note_queued(state, mid)

    @staticmethod
    def note_item_meta(state, mid, **meta):
        return note_item_meta(state, mid, **meta)

    @staticmethod
    def owner_dm_mint_nonce(state, mid, channel_id=None, fragments=()):
        return owner_dm_mint_nonce(state, mid, channel_id, fragments)

    @staticmethod
    def note_owner_item(state, mid, dm=False, command=None):
        return note_owner_item(state, mid, dm, command)

    @staticmethod
    def reply_nonce_mint(state, mid):
        return reply_nonce_mint(state, mid)


def enqueue_message(msg, cname, state, queue, context=None):
    """Validate and queue one burst, preserving its single capability and ledger identity."""
    return MessageEnqueuer(
        msg,
        cname,
        state,
        queue,
        context or QueueContext(),
        _response_policy(),
        _MonitorIngestConversation(BOT_ID, BOT_NAME, TREAT_AS_HUMAN, frozenset(RESPOND_TO)),
        _MonitorIngestJournal(),
    ).enqueue()


def note_ingested(state, mid):
    """Remember when the monitor first processed trigger mid (staged
    it), bounded like queued_at, for the latency log's ingest/hold
    split."""
    seen = state.setdefault("ingested_at", {})
    seen.setdefault(mid, time.time())
    _keep_last(seen, 200)


def note_queued(state, mid):
    """Remember when mid was queued (bounded), for the latency log even
    when a worker removes the queue item itself. Returns the time."""
    now = time.time()
    queued = state.setdefault("queued_at", {})
    queued[mid] = now
    _keep_last(queued, 200)
    return now


def _settle(state, item, outcome):
    """Record an outbox item's outcome under its reply_to AND every other
    message of the burst it belongs to (reply_aliases): the worker
    replies under the last fragment, while the ledger and the sweep track
    the trigger, and a fragment left unsettled would be requeued alone.
    replied=True also adds all those ids to replied_to (send dedup)."""
    status = outcome.status
    reason = outcome.reason
    replied = outcome.replied
    detail = outcome.detail
    reply_to = item.get("reply_to")
    aliases = state.get("reply_aliases", {})
    # The queue item it answers (its "item", else reply_to's trigger),
    # and that item's own messages (its burst fragments) only: a
    # reply_to under some other message is not this item's to settle
    # (it may be another item's, still open).
    trigger = item.get("item") or (aliases.get(reply_to, reply_to) if reply_to else None)
    if not trigger:
        return
    trigger = trigger_of(state, trigger)  # an "item" set to a fragment
    ids = [trigger] + [f for f, t in aliases.items() if t == trigger]
    if reply_to and reply_to not in ids and trigger_of(state, reply_to) == trigger:
        ids.append(reply_to)
    for mid in ids:
        if replied and mid not in state["replied_to"]:
            state["replied_to"].append(mid)
    ledger_record_many(
        ids,
        status,
        reason,
        context=LedgerContext(
            channel_id=item.get("channel_id"),
            author=item.get("author"),
            author_id=item.get("author_id"),
            detail=detail,
        ),
    )


def _outbox_problem(item):
    """Why an outbox item cannot be processed, or None. Ids given as
    numbers are normalized to strings in place. Checked first, so a
    wrong type never raises out of process_outbox mid-pass."""
    problems = reply_protocol.shape_problems(item)
    if problems:
        return "; ".join(problems)
    for field in reply_protocol.ID_FIELDS:
        if item.get(field) in (None, ""):
            item.pop(field, None)
        else:
            item[field] = str(item[field])
    if "schedule" in item:
        # Reminders and watches (muse_watches.py): shape only here.
        problems = W.shape_problems(item)
        if problems:
            return "schedule: " + "; ".join(problems)
    if item.get("silent"):
        return reply_protocol.content_problem(item)
    return reply_protocol.content_problem(item, has_text=bool(normalize_parts(item)))


def _log_latency(state, qid, picked, sent):
    """One line per answered item: where the time went, from the trigger
    message (its snowflake time) to the monitor first processing it
    (ingest: the gateway or the poll delivering it, and the main loop
    getting to it), to queued (hold: the burst coalescing), to the
    outbox pickup (worker), to sent. An item that was never staged (a
    sweep recovery) has no hold. Our clock can trail Discord's
    snowflake time by a fraction of a second, so t0 is clamped to the
    first local timestamp: ingest shows 0, never negative, and total
    stays the sum of the parts."""
    t0 = _snowflake_ts(qid)
    queued = state.get("queued_at", {}).pop(qid, None)
    ingested = state.get("ingested_at", {}).pop(qid, None)
    if not t0:
        return
    if queued:
        ingested = min(ingested or queued, queued)
        t0 = min(t0, ingested)
        print(
            f"Latency {qid}: ingest {ingested - t0:.1f}s, hold "
            f"{queued - ingested:.1f}s, worker {picked - queued:.1f}s, "
            f"send {sent - picked:.1f}s, total {sent - t0:.1f}s",
            flush=True,
        )
    else:
        t0 = min(t0, picked)
        print(f"Latency {qid}: send {sent - picked:.1f}s, total {sent - t0:.1f}s", flush=True)
    TM.latency(sent - t0)


# Progress belongs to this running monitor; outbox_store owns recovery rules.
_OUTBOX_CURRENT: dict[str, str | None] = {"key": None}
_OUTBOX_POISON = {}


def outbox_pass(state):
    return _outbox_processor().pass_once(state, process_outbox)


def _refuse(rejects, item, why, note=""):
    """Log a refused outbox item and set it aside (nonces blanked)."""
    where = item.get("channel_name") if isinstance(item, dict) else None
    print(
        f"Refused outbox item for {where or '?'}: {why}{note} (set aside "
        f"in {os.path.basename(OUTBOX_REJECTED_FILE)})",
        flush=True,
    )
    rejects.append((redact_nonces(item), why))


def bind_started(state, item, settled, remaining, rejects):
    """The one binding step for outbox items that answer an item the
    monitor started, run before owner_dm_gate (docs/PROTOCOL.md, "Fires: the
    monitor starts a queue item"): a reminder or watch fire (watch_bind: its
    requester, its place) or a heartbeat slot (proactive.py: its
    targets, nonce and caps; sent there, never by the path below).
    Returns (fire, handled): handled means the item was settled, set
    aside or kept for a retry here; fire is a bound fire for the send
    path below, else None."""
    fire = watch_bind(item)
    if isinstance(fire, dict) and fire.get("gone"):
        print(
            f"Outbox item for reminder {fire['gone']}: it was settled already; nothing sent.",
            flush=True,
        )
        # (Its ledger entry already says how it ended.)
        settled.append({"item": fire["gone"]})
        return None, True
    if isinstance(fire, str):
        _refuse(rejects, item, fire)
        return None, True
    if fire is not None:
        return fire, False
    try:
        pro = PRO.outbox_item(state, item)
    except Exception as e:  # never blocks the replies
        print(f"Proactive outbox error: {e}", flush=True)
        pro = None
        if could_name_slot(item):
            # Never left to the ordinary path: set aside.
            _refuse(rejects, item, "proactive: its slot could not be checked")
            return None, True
    if pro is None:
        return None, False
    if pro.reject:
        _refuse(rejects, item, pro.reject)
    if pro.retry:
        remaining.append(item)
    if pro.spent:
        settled.append({"item": pro.spent})
    return None, True


def could_name_slot(item):
    """When proactive.py cannot say: an item shaped like a slot's answer
    (its nonce field and no reply_nonce) or naming a queued slot."""
    if item.get("owner_dm_nonce") and not item.get("reply_nonce"):
        return True
    names = {str(item[f]) for f in ("item", "reply_to") if item.get(f)}
    try:
        return any(
            isinstance(q, dict) and q.get("kind") == PRO_KIND and str(q.get("message_id")) in names
            for q in load_queue()
        )
    except Exception:
        return True


# An outdated worker prompt (from before reply nonces) writes outbox
# items without reply_nonce, every one of which is refused: the bot goes
# silent. STALE_PROMPT_HITS such refusals within STALE_PROMPT_WINDOW
# mark it (prompt_outdated.json, for bin/doctor.py) and DM the owners
# once a day; an outbox item that carries its reply_nonce clears it.
MISSING_REPLY_NONCE = "no reply_nonce (an outdated worker prompt?)"
STALE_PROMPT_FILE = os.path.join(BASE_DIR, "prompt_outdated.json")
STALE_PROMPT_HITS = 3
STALE_PROMPT_WINDOW = 3600
STALE_PROMPT_DM = (
    "My instructions for writing replies are out of date, "
    "so I can't send replies. Ask your Muse to update "
    "muse-discord; that refreshes them."
)


def note_reply_nonce(state, refusal):
    """After own_channel_problem: count refusals for a missing
    reply_nonce (see STALE_PROMPT_HITS); a passing item clears them."""
    now = time.time()
    if refusal is None:
        if state.pop("stale_prompt_hits", None) is not None or os.path.exists(STALE_PROMPT_FILE):
            with contextlib.suppress(OSError):
                os.remove(STALE_PROMPT_FILE)
        return
    if MISSING_REPLY_NONCE not in refusal:
        return
    hits = [
        t
        for t in state.get("stale_prompt_hits") or []
        if isinstance(t, (int, float)) and now - t < STALE_PROMPT_WINDOW
    ]
    hits.append(now)
    state["stale_prompt_hits"] = hits[-STALE_PROMPT_HITS:]
    if len(hits) < STALE_PROMPT_HITS:
        return
    if not os.path.exists(STALE_PROMPT_FILE):
        print(
            "WORKER PROMPT OUT OF DATE: outbox items carry no "
            "reply_nonce; re-render the worker prompt (docs/UPDATING.md).",
            flush=True,
        )
        with contextlib.suppress(OSError):
            atomic_write(STALE_PROMPT_FILE, {"since": now, "refused": len(hits)})
    day = time.strftime("%Y-%m-%d")
    if state.get("stale_prompt_alert") != day:
        state["stale_prompt_alert"] = day
        notify_owners(STALE_PROMPT_DM)


def bound_elsewhere(state, item):
    """True for an outbox item whose own binder has checked it in full,
    so own_channel_problem leaves it alone: a held reply an owner chose
    to send (hold_released_ok), or an item for a heartbeat slot
    (proactive.py, through bind_started: the slot's own nonce, its
    targets and caps bind it; it has no reply_nonce). The one place to
    add another such binder. A reminder or watch firing is not one:
    watch_bind checks its place, and its reply_nonce still binds it to
    the run that was handed it."""
    if hold_released_ok(state, item):
        return True
    try:
        return bool(PRO.names_slot(item))
    except Exception as e:  # then the reply_nonce check binds it
        print(f"Proactive: could not check for a slot ({e}).", flush=True)
        return False


def _reply_nonce_problem(state, item, t):
    want = ((state.get("reply_nonces") or {}).get(t) or {}).get("nonce")
    if not want:
        return f"{t}: no live reply_nonce yet (its next wake gets one)"
    got = item.get(REPLY_NONCE_FIELD)
    if got is None:
        if t not in (_ITEM_NONCES_WRITTEN["body"] or {}):
            # Never written for the hook: no wake could have had it.
            return f"{t}: its reply_nonce was never written (see the log)"
        return (
            f"{t}: {MISSING_REPLY_NONCE} (copy the item's "
            f"{REPLY_NONCE_FIELD} from the wake payload)"
        )
    if not isinstance(got, str) or not hmac.compare_digest(
        str(want).encode(), got.encode("utf-8", "replace")
    ):
        return (
            f"{t}: bad reply_nonce (missing or wrong: copy the item's "
            f"{REPLY_NONCE_FIELD} from the wake payload)"
        )
    return None


def own_channel_problem(state, item, index):
    """None when this outbox item answers an open queue item, in that
    item's own channel (a thread, for a message in a thread), and
    carries that item's reply_nonce (handed only to the wake holding the
    item's lease); else why it is refused. So a run can neither post
    anywhere else under a made-up item, nor answer, silence or schedule
    on an item it was not handed (its id may be in the history it read).
    An owner's command to post elsewhere goes through bin/discord-api.py
    with the item's owner_command_nonce. A held reply an owner chose to
    send was checked when it was first drafted (bound_elsewhere)."""
    if bound_elsewhere(state, item):
        return None
    qid = _answered_id(state, item)
    if not qid:
        return "answers no queued message (set item to its message_id)"
    t = trigger_of(state, qid, index["frags"])
    cid = index["channels"].get(t)
    if not cid:
        return f"{t} is not an open queue item (answered already, or never queued)"
    if str(item.get("channel_id")) != cid:
        return f"channel_id is not the channel of {t}"
    # It replies (and reacts: reactions go on reply_to) under its own
    # message or one of its own burst fragments, never under another
    # open item in the channel.
    rt = item.get("reply_to")
    if rt and trigger_of(state, rt, index["frags"]) != t:
        return f"reply_to is not {t} or one of its fragments"
    return _reply_nonce_problem(state, item, t)


def _new_reply_attachments(state, item, gen_id):
    attachments, gen_files = [], []
    candidates = generated_media.validate_paths(GENERATED_DIR, item.get("attachments"), gen_id)
    if not candidates:
        # The media tool usually returns a remote URL or handle, not
        # the local filename, so the worker can't name the file.
        found = generated_media.discover_generated(GENERATED_DIR, state.get("gen_sent", []), gen_id)
        if found:
            print(f"Attaching discovered generated file: {found}", flush=True)
            candidates.append(found)
        else:
            print(
                f"Note: approved generation for {gen_id} but no file in generated/{gen_id}/",
                flush=True,
            )
    for rp in candidates:
        send_path, fname = generated_media.fit_for_discord(rp)
        if send_path:
            attachments.append((send_path, fname))
            gen_files.append([send_path, rp])
        else:
            print(f"Attachment too large even compressed: {rp}", flush=True)
    return attachments, gen_files


def _reply_attachments(state, item, retry):
    """(gen_id, approval, attachments, gen_files) for a reply: only
    generated files bound to the queue item it answers
    (generated/<its message_id>/), under the monitor's live may_generate
    approval for that item (gen_approved; the outbox item does not carry
    the flag), and only into the channel the item came from. No approval,
    no files: generation is gated in code. attachments are (send path,
    filename) for the send; gen_files [send path, original] pairs, kept
    in _retry so a retry re-attaches the same files."""
    gen_id = _gen_item_id(state, item)
    approval = _gen_approval(state, gen_id)
    if approval and approval.get("channel_id") != item.get("channel_id"):
        print(
            f"Note: generation approval for item {gen_id} belongs to "
            f"channel {approval.get('channel_id')}, not "
            f"{item.get('channel_id')}; no attachments",
            flush=True,
        )
        approval = None
    attachments, gen_files = [], []
    if approval and "attachments" in retry:
        # A retry: the files the first attempt resolved. Re-checked,
        # because the outbox (and so _retry) is worker-writable.
        for pair in retry.get("attachments") or []:
            if (
                isinstance(pair, list)
                and len(pair) == GENERATED_PATH_PAIR_LENGTH
                and len(generated_media.validate_paths(GENERATED_DIR, pair, gen_id))
                == GENERATED_PATH_PAIR_LENGTH
            ):
                attachments.append((pair[0], os.path.basename(pair[0])))
                gen_files.append(pair)
    elif approval:
        attachments, gen_files = _new_reply_attachments(state, item, gen_id)
    elif item.get("attachments"):
        print(
            f"Note: reply to {item.get('reply_to')} listed attachments "
            f"without a live generation approval for its item; dropped",
            flush=True,
        )
    return gen_id, approval, attachments, gen_files


class _MonitorReplyPolicy:
    @staticmethod
    def _outbox_problem(item):
        return _outbox_problem(item)

    @staticmethod
    def _refuse(rejects, item, why, note=""):
        return _refuse(rejects, item, why, note)

    @staticmethod
    def bind_started(state, item, settled, remaining, rejects):
        return bind_started(state, item, settled, remaining, rejects)

    @staticmethod
    def owner_dm_gate(state, item, index=None):
        return owner_dm_gate(state, item, index)

    @staticmethod
    def own_channel_problem(state, item, index):
        return own_channel_problem(state, item, index)

    @staticmethod
    def bound_elsewhere(state, item):
        return bound_elsewhere(state, item)

    @staticmethod
    def note_reply_nonce(state, refusal):
        return note_reply_nonce(state, refusal)

    @staticmethod
    def _answered_id(state, item):
        return _answered_id(state, item)

    @staticmethod
    def never_share_hit(state, item, qid, parts):
        return never_share_hit(state, item, qid, parts)

    @staticmethod
    def hold_reply(state, item, qid, parts):
        return hold_reply(state, item, qid, parts)

    @staticmethod
    def _reply_attachments(state, item, retry):
        return _reply_attachments(state, item, retry)


class _MonitorReplyEffects:
    @staticmethod
    def _settle(state, item, outcome):
        return _settle(state, item, outcome)

    @staticmethod
    def remember_check(state, item, qid, index):
        return remember_check(state, item, qid, index)

    @staticmethod
    def watch_schedule(state, item, index):
        return watch_schedule(state, item, index)

    @staticmethod
    def link_mentions(text, channel_id, state):
        return link_mentions(text, channel_id, state)

    @staticmethod
    def normalize_parts(item):
        return normalize_parts(item)

    @staticmethod
    def typing_clear(keys, sent_in=None):
        return typing_clear(keys, sent_in)

    @staticmethod
    def watch_fire_send(state, fire, parts):
        return watch_fire_send(state, fire, parts)

    @staticmethod
    def send_fragmented(channel_id, parts, reply_to=None, attachments=(), delivery=None):
        return send_fragmented(channel_id, parts, reply_to, attachments, delivery)

    @staticmethod
    def add_reaction(channel_id, message_id, emoji):
        return add_reaction(channel_id, message_id, emoji)

    @staticmethod
    def note_media_sent(state, scope):
        return note_media_sent(state, scope)

    @staticmethod
    def scope_of_channel(channel_id):
        return scope_of_channel(channel_id)

    @staticmethod
    def save_facts(state, item, t):
        return save_facts(state, item, t)

    @staticmethod
    def open_follow_up(state, channel_id, user_id):
        return open_follow_up(state, channel_id, user_id)

    @staticmethod
    def _log_latency(state, qid, picked, sent):
        return _log_latency(state, qid, picked, sent)

    @staticmethod
    def opt_action(state, item, index):
        return PRO.opt_action(state, item, index)


def process_outbox(state):
    return _outbox_processor().process(state)


# ---------------------------------------------------------------------------
# Reminders and watches (muse_watches.py; docs/PROTOCOL.md, "Reminders and
# watches"). The worker asks for one with a `schedule` action on its
# reply to a queue item; the requester is that item's author and the
# place its channel, from item_meta, never from the outbox
# (watch_schedule). watches.json keeps them across restarts. When a
# reminder is due, or a watch matches a server message the monitor
# ingests (classify_message: gateway and poll, once each), watch_tick
# queues a "reminder" item (a fire) for the worker. The worker's reply
# to it is bound to the requester and the place (watch_bind) and may
# ping the requester only (watch_fire_send). A fire is kept in
# watches.json until the ledger settles it, so a restart between
# saving it and queueing it re-queues it instead of losing it.
# ---------------------------------------------------------------------------
WATCHES_FILE = os.path.join(BASE_DIR, "watches.json")
# Web watches: the live checks for hooks/watch-due.sh (mode 600), and
# the checker's reports from bin/watch-report.py (drained each tick).
WEB_DUE_FILE = os.path.join(BASE_DIR, "web_watches_due.json")
WEB_RESULTS_FILE = os.path.join(BASE_DIR, "web_watch_results.json")
_WEB_DUE_WRITTEN = {"body": None, "path": None}
_WATCH_STORE: dict[str, W.Store | None] = {"store": None}
_GUILD_PERMS = {}  # guild id -> {"roles": {id: perms}, "owner_id"}
_CHANNEL_OVERWRITES = {}  # channel id -> its overwrites (discovery)
_WATCH_MEMBERS = {}  # (guild, user) -> (role ids or None, fetched at)
_WATCH_MATCHES = []  # [(watch id, what matched)] since the last tick
_WATCH_TICK = {"next": 0.0, "upkeep": 0.0}


def watch_store():
    s = _WATCH_STORE["store"]
    if s is None or s.path != WATCHES_FILE:
        s = _WATCH_STORE["store"] = W.Store(WATCHES_FILE)
    return s


# -- what a requester can read -------------------------------------------
def _channel_access():
    return ChannelAccess(
        CHANNELS,
        api_get,
        rest_backing_off,
        _GUILD_PERMS,
        _CHANNEL_OVERWRITES,
        _WATCH_MEMBERS,
    )


def watch_note_guild(gid, gobj):
    return _channel_access().note_guild(gid, gobj)


def watch_perm_event(ev):
    return _channel_access().permission_event(ev)


# -- asking for one ---------------------------------------------------------


def _watch_audience():
    return WatchAudience(
        _response_policy(),
        _channel_access(),
        GUILDS,
        _USER_NAMES,
        owners_absent,
        dm_refusal,
    )


def _watch_requests():
    return WatchRequests(watch_store(), _watch_audience(), queued_item_meta, _note_watch_timezone)


def watch_card(guild_id, dm, here, matches, speaker=None):
    return _watch_requests().card(guild_id, dm, here, matches, speaker)


def watch_schedule(state, item, index):
    return _watch_requests().schedule(state, item, index)


def _note_watch_timezone(uid, fresh):
    for op, value in (fresh or {}).get("done") or []:
        if op == "tz":
            try:  # their quiet hours: only their own button moves them
                PRO.note_tz(uid, value)
            except Exception as e:
                print(f"Proactive: time zone note error: {e}", flush=True)


# -- firing -----------------------------------------------------------------


def _watch_delivery():
    return WatchDelivery(watch_store(), api_get, normalize_parts)


def _watch_runtime():
    audience = _watch_audience()
    return WatchRuntime(
        store=watch_store(),
        audience=audience,
        fires=WatchFireBuilder(audience, ledger_record, settings_card),
        queue=WatchQueue(
            _queue_files(),
            _MonitorIngestJournal(),
            lambda: OWNER_USER_IDS,
            owner_dm_write_nonces,
        ),
        web=WebWatchWork(
            WEB_RESULTS_FILE,
            WEB_DUE_FILE,
            _publish,
            lambda: OWNER_USER_IDS,
            is_suspicious,
            _WEB_DUE_WRITTEN,
        ),
        entries=lambda: _ledger_load()["entries"],
        matches=_WATCH_MATCHES,
        clock=_WATCH_TICK,
    )


def watch_tick(state, now=None):
    return _watch_runtime().tick(state, now)


def watch_fire_upkeep(state, now=None):
    return _watch_runtime().upkeep(state, now)


def watch_scan(msg, guild_id):
    return _watch_runtime().scan(msg, guild_id)


# -- answering a fire -------------------------------------------------------


def watch_bind(item):
    return _watch_delivery().bind(item)


def watch_fire_send(state, fire, parts):
    return _watch_delivery().send(state, fire, parts)


PIDFILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor.pid")


_PIDFILE_LOCK = None  # fd holding the pidfile lock for the process lifetime


def _acquire_singleton():
    """Ensure only one monitor runs. The pidfile is locked with flock for
    as long as this process lives, so two monitors started at the same
    moment (say, overlapping watchdog runs) cannot both pass the check;
    the lock disappears with the process, so there is no stale state.
    Two monitors would each send everything in the outbox, posting every
    reply twice. Exits the process if a live peer holds the lock."""
    global _PIDFILE_LOCK
    mypid = os.getpid()
    fd = os.open(PIDFILE, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Another monitor holds the pidfile lock; exiting.", flush=True)
        sys.exit(0)
    # A monitor from before this lock existed holds no flock, so also
    # check whether the recorded pid is still a live monitor.
    try:
        oldpid = int(os.read(fd, 32).decode().strip() or 0)
    except ValueError:
        oldpid = 0
    if oldpid and oldpid != mypid:
        try:
            with open(f"/proc/{oldpid}/cmdline", "rb") as f:
                alive = "monitor.py" in f.read().decode(errors="replace")
        except OSError:
            alive = False
        if alive:
            print(f"Another monitor (pid {oldpid}) is already running; exiting.", flush=True)
            sys.exit(0)
        print(f"Replacing stale monitor pidfile (pid {oldpid} gone).", flush=True)
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, str(mypid).encode())
    _PIDFILE_LOCK = fd  # never closed: the lock lives as long as we do


# ---------------------------------------------------------------------------
# Gateway fast path (realtime ingest).
#
# Discord's Gateway is a websocket that pushes MESSAGE_CREATE events the
# instant they happen, for every guild the bot is in plus DMs, instead of
# waiting for the next REST poll.
#
# The gateway thread ONLY enqueues raw message dicts into _gateway_inbox;
# the main loop drains it and runs the exact same classify/stage path as
# polled messages, so behavior is identical. REST polling still covers
# every channel too: it acts as gap repair (anything the gateway missed
# while reconnecting) and as the full fallback if the gateway is down. The
# REST watermark plus the gateway-seen ids (see note_gateway_seen) prevent
# double-processing without letting the gateway skip the poll past a gap.
#
# Requires the Message Content privileged intent (Discord dev portal -> Bot
# -> Privileged Gateway Intents). Without the toggle, Discord closes the
# connection with code 4014 and we fall back to polling with a loud log.
# ---------------------------------------------------------------------------
# GUILDS | GUILD_MESSAGES | DIRECT_MESSAGES | MESSAGE_CONTENT, plus
# GUILD_MESSAGE_TYPING | DIRECT_MESSAGE_TYPING for the early burst flush
# (burst_quiet). The typing intents are not privileged.
GATEWAY_INTENTS = (1 << 0) | (1 << 9) | (1 << 12) | (1 << 15) | (1 << 11) | (1 << 14)


async def _gateway_run(token, inbox, status):
    from muse_discord.discord_connection import Connection, run

    await run(token, Connection(inbox, status, SettingsCtx, guard_outgoing, GATEWAY_INTENTS))


def _gateway_thread_main(token, inbox, status):
    import asyncio

    try:
        asyncio.run(_gateway_run(token, inbox, status))
    except Exception as e:
        print(
            f"GATEWAY DISABLED (thread died: {e}): realtime ingest is off; REST polling continues.",
            flush=True,
        )
        status["dead"] = True


def _gateway_metadata(state, m):
    if "_guild_event" in m:
        on_guild_event(state, m["_guild_event"])
        return True
    if "_perm_event" in m:
        watch_perm_event(m["_perm_event"])
        return True
    if "_typing" in m:
        note_activity(m["_typing"].get("channel_id"), m["_typing"].get("user_id"))
        return True
    if "_record" in m:
        apply_record(state, m["_record"])
        return True
    return False


def _gateway_guild_message(state, m, channels):
    cid, gid = m["channel_id"], m["guild_id"]
    info = channels.get(cid)
    if info is None:
        # Channel/thread discovery hasn't registered yet: resolve it
        # once via the API so the message isn't missed. A failure
        # (e.g. 403 private channel) skips it here only; last_seen
        # is untouched, so the REST poll can still pick it up.
        try:
            ch = api_get(f"/channels/{cid}")
        except Exception:
            return None
        if not ch or not ch.get("id"):
            return None
        g = ch.get("guild_id") or gid
        info = {
            "name": ch.get("name") or "?",
            "guild_id": g,
            "guild_name": (GUILDS.get(g) or {}).get("name") or g,
            "can_send": None,
            "thread": ch.get("type") in _THREAD_TYPES,
            "parent_id": ch.get("parent_id"),
            "private": ch.get("type") == PRIVATE_THREAD_TYPE,
        }
        if not info["thread"] and ch.get("parent_id"):
            CHANNEL_CATEGORY[cid] = ch["parent_id"]
        if not info["thread"]:  # watches: who can read it
            _CHANNEL_OVERWRITES[cid] = ch.get("permission_overwrites") or []
        channels[cid] = info
    cname = channel_display(info)
    record_history(state, cid, [m])
    note_gateway_seen(state, cid, m["id"])
    kind = classify_message(state, m, cname, gid)
    if kind:
        stage_found(state, m, cname, gid, kind)
        return m, cname, gid, kind
    return None


def _gateway_dm_message(state, m):
    cid = m["channel_id"]
    # DM event (no guild_id). The DM settings decide whose DMs
    # are even read (dm_sender_allowed): under the default
    # (dms_from: owners) everyone else's DMs are dropped unread
    # and unrecorded; dms.who.replies: off drops all of them.
    author = _who(m)
    own = author.get("id") == BOT_ID
    if not own:
        TM.count("messages_seen", int(m.get("type", 0) in CONVERSATION_TYPES))
        if not dm_sender_allowed(author):
            note_dm_refused(m)
            return None
    record_history(state, cid, [m])
    note_gateway_seen(state, cid, m["id"])
    if own or m.get("type", 0) not in CONVERSATION_TYPES:
        return None  # its own messages, system notices: context only
    if hold_fragment(state, cid, m):
        return None  # folded into a burst already being coalesced
    return m, "DM with " + (author.get("username") or cid)


def _gateway_message_new(state, m):
    cid = m["channel_id"]
    last = state["last_seen"].get(cid)
    if last and int(m["id"]) <= int(last):
        return False  # the poll loop already handled it
    if gateway_seen(state, cid, m["id"]):
        return False  # delivered twice (e.g. replayed on resume)
    gid = m.get("guild_id")
    return not (gid and GUILD_ALLOWLIST and gid not in GUILD_ALLOWLIST)


def drain_gateway(state, inbox, channels):
    """Pull gateway-delivered messages (all guilds + DMs) and classify them
    exactly like polled messages. Returns (triggers, dms): guild
    triggers as (msg, cname, guild_id, kind) tuples (already staged, see
    stage_found), DMs as (msg, cname).
    Guild join/leave events are applied to state on the way, and typing
    events and message arrivals are recorded for the early burst flush
    (note_activity). Skips anything the REST poll already processed (at
    or below its last_seen watermark), and leaves that watermark alone
    (see note_gateway_seen). Messages from servers outside a non-empty
    bot.guild_ids are ignored."""
    mentions, dms = [], []
    while True:
        try:
            m = inbox.get_nowait()
        except pyqueue.Empty:
            break
        if _gateway_metadata(state, m):
            continue
        cid = m.get("channel_id")
        if not cid:
            continue
        # Recorded even when the poll got here first: the burst holding
        # this message needs to know when its author last spoke.
        note_activity(cid, _who(m).get("id"), m["id"])
        if not _gateway_message_new(state, m):
            continue
        gid = m.get("guild_id")
        if gid:
            trigger = _gateway_guild_message(state, m, channels)
            if trigger:
                mentions.append(trigger)
        else:
            dm = _gateway_dm_message(state, m)
            if dm:
                dms.append(dm)
    return mentions, dms


# ---------------------------------------------------------------------------
# Reconciliation sweep. Every engine.sweep.every_cycles poll cycles,
# independently re-read recent channel history (and the owners' polled DM
# channels) and diff every mention / reply-to-bot / owner DM against the
# durable records (replied_to, queue, outbox, quarantine, ledger; a burst
# fragment counts as its trigger). Anything older than the grace period with no
# terminal state is requeued on the spot. This is the backstop that turns
# every silent failure mode (storm loss, mid-cycle kill, worker drop)
# into a delayed reply instead of a permanent miss. Aborts on the first
# 429: during a rate-limit storm the sweep must not add request pressure.
# (The SWEEP_* constants are resolved from config in the header block.)
# ---------------------------------------------------------------------------
@dataclass
class MentionSweep:
    state: dict
    queue: list
    channels: dict
    dms: bool = True

    def run(self):
        if rest_backing_off():
            return False
        self.prepare_indexes()
        # The owners' DM channels are swept too: a DM lost in a gateway gap
        # is as lost as a mention.
        dm_channels = (
            [(dm_id, {"dm": True, "name": name}) for dm_id, name in DM_CHANNELS.items()]
            if self.dms
            else []
        )
        self.now = time.time()
        self.changed = False
        for cid, info in list(self.channels.items()) + dm_channels:
            try:
                msgs = api_get(f"/channels/{cid}/messages?limit=20")
            except Exception as e:
                if getattr(e, "code", None) == HTTPStatus.TOO_MANY_REQUESTS:
                    print("Sweep aborted: rate limited.", flush=True)
                    return self.changed
                continue
            if not isinstance(msgs, list):
                continue
            for m in sorted(msgs, key=lambda x: int(x["id"])):
                self.consider(m, cid, info)
        return self.changed

    def prepare_indexes(self):
        self.entries = _ledger_load()["entries"]
        self.q_ids = {q.get("message_id") for q in self.queue}
        self.outbox_targets = set()
        for path in (OUTBOX_FILE, OUTBOX_PROCESSING_FILE):
            for i in _read_json(path, list) or []:
                if isinstance(i, dict):
                    self.outbox_targets.update((i.get("reply_to"), i.get("item")))
        self.quar_ids = _quarantine_ids()
        self.replied = set(self.state.get("replied_to", []))
        self.pending_ids = set()
        for entry in self.state.get("pending", {}).values():
            self.pending_ids.add(entry.get("first_id"))
            for frag in entry.get("fragments", []):
                self.pending_ids.add(frag.get("id"))
        # Burst fragment id -> trigger id. A fragment is answered by its
        # trigger's queue item, so it is accounted for whenever its trigger
        # is (and an outbox reply under a fragment accounts for the trigger).
        self.aliases = self.state.get("reply_aliases", {})
        self.outbox_targets |= {self.aliases[t] for t in self.outbox_targets if t in self.aliases}

    def consider(self, m, cid, info):
        dm = bool(info.get("dm"))
        mid = m["id"]
        author = _who(m)
        if author.get("id") == BOT_ID:
            return  # (other bots: trigger_kind applies behavior.bots)
        if m.get("type", 0) not in CONVERSATION_TYPES:
            return  # a system notice (a pin, a new thread)
        if dm:
            # Owner DM channels: every owner message is a trigger.
            if (
                is_bot_author(author)
                or not dm_swept_author(author.get("id"))
                or not dm_sender_allowed(author)
            ):
                return
        # Direct address only: follow-ups are never requeued.
        elif trigger_kind(m, info.get("guild_id")) != "mention":
            return
        if predates_join(self.state, m, info.get("guild_id")):
            return
        age = self.now - _snowflake_ts(mid)
        if age < SWEEP_GRACE_SECS or age > SWEEP_MAX_AGE_SECS:
            return
        self.recover(m, cid, info)

    def recover(self, m, cid, info):
        mid = m["id"]
        author = _who(m)
        cname = "DM with " + info["name"] if info.get("dm") else channel_display(info)
        if mid in self.replied:
            if self.entries.get(mid, {}).get("status") != "replied":
                ledger_record(
                    mid,
                    "replied",
                    "sweep sync",
                    context=LedgerContext(
                        channel_id=cid, author=author.get("username"), author_id=author.get("id")
                    ),
                )
            return
        ids = {mid, self.aliases.get(mid, mid)}
        if any(
            ids & s
            for s in (
                self.replied,
                self.quar_ids,
                self.q_ids,
                self.outbox_targets,
                self.pending_ids,
            )
        ):
            return  # accounted for and in flight
        ent = self.entries.get(mid)
        if any(self.entries.get(i, {}).get("status") in LEDGER_TERMINAL for i in ids):
            return
        if info.get("can_send") is False:
            ledger_record(
                mid,
                "skipped",
                "no send permission in channel",
                context=LedgerContext(
                    channel_id=cid, author=author.get("username"), author_id=author.get("id")
                ),
            )
            return
        recs = (ent or {}).get("recoveries", 0)
        if recs >= SWEEP_MAX_RECOVERIES:
            if ent and ent.get("status") != "skipped":
                ledger_record(
                    mid,
                    "skipped",
                    "recovery cap reached",
                    context=LedgerContext(
                        channel_id=cid, author=author.get("username"), author_id=author.get("id")
                    ),
                )
                print(
                    f"Sweep: giving up on unrecoverable mention "
                    f"{mid} from {author.get('username')} in "
                    f"{cname} (recorded in mention ledger).",
                    flush=True,
                )
            return
        ok = enqueue_message(
            m,
            cname,
            self.state,
            self.queue,
            context=QueueContext(
                dm=bool(info.get("dm")), fragments=[], guild_id=info.get("guild_id")
            ),
        )
        if ok:
            self.changed = True
            self.q_ids.add(mid)
            ledger_record(
                mid,
                "queued",
                f"sweep recovery #{recs + 1}",
                context=LedgerContext(
                    channel_id=cid,
                    author=author.get("username"),
                    recoveries=recs + 1,
                    author_id=author.get("id"),
                ),
            )
            print(
                f"Sweep recovered lost mention {mid} from {author.get('username')} in {cname}.",
                flush=True,
            )
        elif not _ledger_terminal(mid):
            # (A gate inside enqueue, e.g. quarantine or the join
            # watermark, may already have recorded a terminal state.)
            ledger_record(
                mid,
                "enqueue_failed",
                "sweep enqueue failed",
                context=LedgerContext(
                    channel_id=cid,
                    author=author.get("username"),
                    recoveries=recs + 1,
                    author_id=author.get("id"),
                ),
            )


def sweep_mentions(state, queue, channels, dms=True):
    """Requeue mentions that no durable record accounts for."""
    return MentionSweep(state, queue, channels, dms).run()


# ---------------------------------------------------------------------------
_no_send_logged = set()


def _note_no_send(msg, cname):
    """Record a mention that cannot be answered because the bot has no
    send permission in the channel: ledgered as skipped (terminal, so the
    sweep won't resurrect it) and logged once per channel per run."""
    cid = msg.get("channel_id")
    if cid not in _no_send_logged:
        _no_send_logged.add(cid)
        print(f"Skipping replies in {cname}: the bot cannot send messages there.", flush=True)
    ledger_record(
        msg["id"],
        "skipped",
        "no send permission in channel",
        context=LedgerContext(
            channel_id=cid, author=_who(msg).get("username"), author_id=_who(msg).get("id")
        ),
    )


def poll_targets():
    """The targets of one rotation. Empty until the bot knows its own id:
    nothing can be classified before that, so watermarks stay put."""
    if BOT_USER_ID is None:
        return []
    targets = [("dms",)] if dm_poll_user_ids() else []
    return targets + [("channel", cid) for cid in CHANNELS]


def thread_refresh_iter(state):
    """Light thread-only refresh: pick up brand-new posts and threads
    minutes after they appear instead of waiting for the full channel
    refresh. Only threads not already monitored cost an API probe."""
    guilds = get_guilds()
    yield
    for gid, gname in guilds or []:
        try:
            chans = api_get(f"/guilds/{gid}/channels")
        except Exception:
            continue
        finally:
            yield
        names_by_id = {c["id"]: c.get("name", "?") for c in chans}
        for c in chans:
            if c.get("parent_id"):
                CHANNEL_CATEGORY[c["id"]] = c["parent_id"]
        new_threads = yield from discover_threads_iter(
            gid, gname, snapshot=ThreadSnapshot(names=names_by_id, known_ids=CHANNELS)
        )
        for tid, info in new_threads.items():
            seed_channel_seen(tid, channel_display(info), state)
            CHANNELS[tid] = info
            print(f"Now monitoring {channel_display(info)}.", flush=True)
            yield
        if new_threads:
            save_state(state)


def discovery_iter(state):
    """Periodic re-discovery of readable channels, so permission changes
    (grants or revocations) take effect without a restart. Applied when
    complete; a channel the gateway or the thread refresh registered
    while it ran is kept."""
    global CHANNELS, GUILDS
    before = dict(CHANNELS)
    fresh, fresh_guilds = yield from discover_channels_iter(before)
    GUILDS = fresh_guilds
    for cid, info in fresh.items():
        if cid not in CHANNELS:
            # Seeds only a channel with no last_seen yet.
            seed_channel_seen(cid, channel_display(info), state)
            yield
    for cid, info in CHANNELS.items():
        if cid not in before and cid not in fresh:
            fresh[cid] = info
    if set(fresh) != set(CHANNELS):
        added = [channel_display(n) for i, n in fresh.items() if i not in CHANNELS]
        removed = [channel_display(n) for i, n in CHANNELS.items() if i not in fresh]
        if added:
            print(f"Now monitoring {', '.join(added)}.", flush=True)
        if removed:
            print(f"Lost access to {', '.join(removed)}; unmonitoring.", flush=True)
        CHANNELS = fresh
    publish_directory()
    sync_guilds(state, fresh_guilds, fresh)
    save_state(state)


def _adopt_identity(me, how=""):
    global BOT_ID, BOT_USER_ID, BOT_NAME
    BOT_ID = BOT_USER_ID = me.get("id")
    BOT_NAME = me.get("username")
    print(f"Bot user id: {BOT_ID}{how}", flush=True)


def outbox_waiting(since):
    """True when reply_outbox.json holds a reply written after since (the
    last outbox pass): a cheap stat, as the monitor leaves the file as
    "[]". A file the last pass could not take (mid-write, or a pass that
    failed) does not count, so it cannot make the loop spin."""
    try:
        st = os.stat(OUTBOX_FILE)
    except OSError:
        return False
    # (50ms of slack: file times can trail time.time() slightly.)
    return st.st_size > EMPTY_OUTBOX_MAX_BYTES and st.st_mtime > since - 0.05


def _drain_live_messages(state, _gateway_inbox):
    # Gateway fast path first: messages pushed in realtime are
    # classified (and triggers staged) like polled ones. Anything
    # the poll already processed is skipped. Guild join/leave
    # events ride the same inbox. Waits for the bot's identity,
    # like the poll.
    if BOT_USER_ID is not None:
        gw_mentions, gw_dms = drain_gateway(state, _gateway_inbox, CHANNELS)
        if gw_mentions or gw_dms:
            print(
                f"Gateway delivered {len(gw_mentions)} mention(s), {len(gw_dms)} DM(s).",
                flush=True,
            )
        for msg, cname in gw_dms:
            stage_trigger(state, msg, cname, "dm", place=MessagePlace(guild_id=None, dm=True))


def _stage_polled_messages(state, dm_items):
    # Stage polled DMs into the coalescing buffer (bursts from
    # the same author fold into one pending entry; guild triggers
    # were staged as they were classified). Matured bursts flush
    # into single queue items.
    # The queue snapshot only serves the duplicate checks: what
    # gets written is just the items appended to it, applied to a
    # fresh read under the lock (queue_edit), because the flush
    # can take tens of seconds (media downloads, ffmpeg) while the
    # monitor's own sends remove answered items.
    queue = load_queue()
    queued = {q.get("message_id") for q in queue if isinstance(q, dict)}
    queue.extend(q for q in state.get("queue_backlog", []) if q["message_id"] not in queued)
    n_before = len(queue)
    for msg, cname in dm_items:
        stage_trigger(state, msg, cname, "dm", place=MessagePlace(guild_id=None, dm=True))

    flush_pending(state, queue)
    publish_queue_additions(state, queue[n_before:])
    save_state(state)
    return queue


def _retry_cycle_identity(cycle_done):
    if cycle_done:
        _rl_note_clean_cycle()
        if BOT_USER_ID is None:
            try:
                _adopt_identity(api_get("/users/@me"), " (recovered in-loop)")
            except Exception as e:
                if getattr(e, "code", None) == HTTPStatus.UNAUTHORIZED:
                    token_rejected_exit("401 from /users/@me")


def _cleanup_cycle(state, cycle_done):
    if cycle_done:
        flush_join_notices(state, GUILDS, CHANNELS)
        announce_update(state)
        telemetry_notice()
        expire_holds(state)
        sweep_media()
        sweep_generated()
        sweep_gen_state(state)
        prune_gateway_seen(state)
        save_state(state)


def _wait_for_work(state, gateway_status, scheduler, outbox_pass_at):
    # Event-driven wait: a gateway MESSAGE_CREATE wakes the loop at
    # once. Otherwise it idles in ticks of at most 2s, shorter while
    # REST slices are due (none while they are behind), a burst is
    # about to flush, or the outbox holds a reply.
    now = time.time()
    wait_for = scheduler.next_wait(now, rest_backing_off())
    burst_wait = pending_wait(state, now)
    if burst_wait is not None:
        # A burst can flush early (burst_quiet): wake when it may.
        wait_for = min(wait_for, burst_wait)
    # A reply the worker writes goes out within ~0.25s, even when it
    # lands mid-wait: the wait is cut into 0.25s steps that stat the
    # outbox.
    until = now + (0.0 if outbox_waiting(outbox_pass_at) else wait_for)
    while True:
        left = until - time.time()
        if left <= 0 or gateway_status["wake"].wait(timeout=min(0.25, left)):
            break
        if outbox_waiting(outbox_pass_at):
            break
    gateway_status["wake"].clear()


def _rest_scheduler():
    return RestScheduler(
        RestContext(
            RestSchedule(
                POLL_INTERVAL, SWEEP_EVERY_CYCLES, CHANNEL_REFRESH_CYCLES, THREAD_REFRESH_CYCLES
            ),
            RestClock(time.time, rest_backing_off),
            RestOperations(
                lambda: CHANNELS,
                lambda: BOT_USER_ID is not None,
                poll_targets,
                list_dm_channels,
                check_dm_channel,
                check_channel,
                load_queue,
                sweep_mentions,
                publish_queue_additions,
                save_state,
                discovery_iter,
                thread_refresh_iter,
            ),
        ),
        _DISCOVERY_REFRESH,
    )


def _settings_upkeep(status):
    register_commands(status)
    flush_admin_digests()


def _send_outbox(state):
    outbox_pass(state)
    save_state(state)


def _start_monitor_threads(state):
    token = TOKEN
    _gateway_inbox = pyqueue.Queue()
    _ix_inbox = pyqueue.Queue()  # interactions, see below
    _gateway_status = {"wake": threading.Event(), "interactions": _ix_inbox}
    gw_thread = threading.Thread(
        target=_gateway_thread_main,
        args=(token, _gateway_inbox, _gateway_status),
        daemon=True,
        name="discord-gateway",
    )
    gw_thread.start()
    print("Gateway thread started (realtime ingest).", flush=True)
    # Typing indicator on its own clock (see typing_refresh).
    threading.Thread(target=_typing_thread_main, daemon=True, name="typing").start()
    # Interactions (button clicks) on their own thread, fed straight by
    # the gateway thread: the 3s acknowledgement deadline never waits on
    # the main loop. What they change in state comes back through the
    # gateway inbox ("_record", applied by drain_gateway).
    _holds_live.update({hid: h.get("at", 0) for hid, h in (state.get("holds") or {}).items()})
    _update_announced["version"] = (state.get("update_notice") or {}).get("version")
    threading.Thread(
        target=_interaction_thread_main,
        args=(_ix_inbox, lambda rec: _gateway_inbox.put({"_record": rec})),
        daemon=True,
        name="interactions",
    ).start()

    return _gateway_inbox, _gateway_status


def _start_monitor():
    global CHANNELS, GUILDS
    _acquire_singleton()
    TM.start()
    touch_heartbeat()
    # Identity first, discovery second: the watchdog's health check waits
    # for the identity marker, and full discovery (a probe per channel
    # and thread) can take longer than its window.
    #
    # Marker contract with watchdog.sh:
    #   "Bot user id: <id>"    identity confirmed (here, in-loop, or from
    #                          the gateway's READY).
    #   "Bot identity pending" /users/@me failed for a reason that can
    #                          heal (429 storm, network): the monitor is
    #                          up, keeps retrying, and the gateway still
    #                          delivers. The watchdog counts it as a
    #                          start, without hiding a revoked token.
    #   "TOKEN REJECTED"       a 401: the monitor exits (status 3) and the
    #                          watchdog raises the token alert.
    try:
        _adopt_identity(api_get("/users/@me"))
    except Exception as e:
        if getattr(e, "code", None) == HTTPStatus.UNAUTHORIZED:
            token_rejected_exit("401 from /users/@me")
        print(
            f"Bot identity pending: /users/@me failed at boot ({e}); retrying in-loop.", flush=True
        )
    CHANNELS, GUILDS = discover_channels()
    publish_directory()
    print(
        f"muse-discord monitor starting. Polling {len(CHANNELS)} "
        f"channels + DMs every {POLL_INTERVAL}s.",
        flush=True,
    )
    if not CHANNELS:
        print(
            "WARNING: no readable channels found (check the bot's "
            "guild membership and channel permissions); discovery "
            "keeps retrying on the refresh cadence.",
            flush=True,
        )
    state = load_state()
    # Join watermarks for every guild, plus the owner summary for any
    # guild joined while the monitor was down.
    sync_guilds(state, GUILDS, CHANNELS)

    # Seed last_seen for any monitored channel not tracked yet, so its
    # history is never mistaken for fresh mentions (first run, or a
    # newly discovered channel after a permission grant).
    for cid, info in CHANNELS.items():
        if cid not in state["last_seen"]:
            seed_channel_seen(cid, channel_display(info), state)

    # Gateway fast path (realtime MESSAGE_CREATE for all guilds + DMs).
    # Daemon thread: it only enqueues raw messages and sets the wake event;
    # the main loop below classifies and stages them. REST polling keeps
    # covering everything as gap repair and full fallback, so a dead
    # gateway degrades to the 30s poll.
    inbox, status = _start_monitor_threads(state)

    # A restart must not drop bursts staged before it: their messages are
    # already marked seen, so flush them immediately instead of waiting out
    # a coalesce window that mostly elapsed while the monitor was down.
    if state.get("pending"):
        queue = load_queue()
        n_before = len(queue)
        if flush_pending(state, queue, force=True):
            try:
                publish_queue_additions(state, queue[n_before:])
            except SharedLockTimeout as exc:
                print(f"Startup queue publication deferred: {exc}", flush=True)
    save_state(state)

    return state, inbox, status


def initialize(config_path=None, token_override=None):
    """Load this monitor's configuration and services once, after importing it."""
    global BACKOFF_INITIAL_SECS, BACKOFF_MAX_SECS, BOT_COALESCE_SECS, CFG, CHANNEL_REFRESH_CYCLES
    global COALESCE_SECS, GUILD_ALLOWLIST, MAX_REPLY_CHARS, OWNER_USER_IDS, POLL_INTERVAL
    global PRO, SWEEP_EVERY_CYCLES, SWEEP_GRACE_SECS, SWEEP_MAX_AGE_SECS, SWEEP_MAX_RECOVERIES
    global TM, TOKEN, TREAT_AS_HUMAN, _ENGINE, _INITIALIZED
    global _S0, _SETTINGS, _DIRECTORY_VIEW, _ACTIVITY, _DISCOVERY_REFRESH, _SHARED_MEMBERSHIP
    if _INITIALIZED:
        raise RuntimeError("monitor is already initialized")
    try:
        _SETTINGS = MS.Watcher(config_path or _config_path())
    except MS.SettingsError as e:
        print(
            f"CONFIG ERROR: config.yaml is invalid; not starting.\n{e}", file=sys.stderr, flush=True
        )
        sys.exit(1)
    _S0 = _SETTINGS.current
    CFG = _S0.raw
    OWNER_USER_IDS = set(_S0.owners)
    GUILD_ALLOWLIST = set(_S0.guild_ids())
    TREAT_AS_HUMAN = {str(u) for u in (CFG.get("testing") or {}).get("treat_as_human") or []}
    if TREAT_AS_HUMAN:
        print(
            f"WARNING: testing.treat_as_human is set: {len(TREAT_AS_HUMAN)} "
            f"bot account(s) are treated as humans. Test servers only.",
            flush=True,
        )
    _ENGINE = _S0.engine
    for _warning in _S0.engine_warnings:
        print(_warning, flush=True)
    COALESCE_SECS = _ENGINE.coalesce_seconds
    BOT_COALESCE_SECS = max(COALESCE_SECS, 15.0)
    MAX_REPLY_CHARS = _ENGINE.max_reply_chars
    POLL_INTERVAL = _ENGINE.rest_poll_seconds
    SWEEP_EVERY_CYCLES = _ENGINE.sweep.every_cycles
    SWEEP_GRACE_SECS = _ENGINE.sweep.grace_seconds
    SWEEP_MAX_AGE_SECS = _ENGINE.sweep.max_age_hours * 3600
    SWEEP_MAX_RECOVERIES = _ENGINE.sweep.max_recoveries
    BACKOFF_INITIAL_SECS = _ENGINE.backoff.initial_seconds
    BACKOFF_MAX_SECS = _ENGINE.backoff.max_seconds
    CHANNEL_REFRESH_CYCLES = max(1, round(_ENGINE.discovery_refresh_minutes * 60 / POLL_INTERVAL))
    TOKEN = token_override if token_override is not None else (_load_token(_S0) or "")
    if not TOKEN:
        print(
            "No bot token: pass one as argv[1], or point token_from at an environment "
            "variable or a file holding it (mode 600).",
            file=sys.stderr,
        )
        sys.exit(1)
    _DIRECTORY_VIEW = DirectorySnapshot(lambda: DIRECTORY_FILE)
    _ACTIVITY = GatewayActivity()
    _DISCOVERY_REFRESH = DiscoveryRefresh()
    _SHARED_MEMBERSHIP = SharedServers(
        lambda path: api_get(path),
        lambda: rest_backing_off(),
        lambda: list(GUILDS),
        lambda gid: owners_absent(gid),
        lambda: time.time(),
    )
    TM = _import_telemetry()
    PRO = _import_proactive()
    _INITIALIZED = True


def main():
    if not _INITIALIZED:
        initialize()
    state, inbox, status = _start_monitor()
    scheduler = _rest_scheduler()
    outbox_pass_at = 0.0
    while True:
        if _token_rejected or status.get("token_rejected"):
            save_state(state)
            token_rejected_exit(_token_rejected or "gateway closed with code 4004")
        touch_heartbeat()
        if BOT_USER_ID is None and status.get("user"):
            _adopt_identity(status["user"], " (from gateway READY)")
        now, backing = time.time(), rest_backing_off()
        scheduler.begin_tick(now, backing)
        dm_items = []
        _run_phase("Gateway drain", _drain_live_messages, state, inbox)
        _run_phase("Settings overlay", retire_overlay_changes)
        _run_phase("/settings upkeep", _settings_upkeep, status)
        _run_phase("Owner DM upkeep", owner_dm_upkeep, state)
        _run_phase("Reminders and watches", watch_tick, state)
        _run_phase("Proactive", PRO.tick, state)
        poll_turn, rest_spent = scheduler.poll_slice(state, now, backing, dm_items)
        queue = _run_phase("Queue/staging", _stage_polled_messages, state, dm_items)
        _run_phase("Typing", typing_refresh, state, queue)
        outbox_pass_at = time.time()
        _run_phase("Outbox", _send_outbox, state)
        recovery = scheduler.recovery_cycle()
        _run_phase("Identity retry", _retry_cycle_identity, scheduler.cycle_done)
        _run_phase("Job scheduling", scheduler.schedule_jobs, state, recovery)
        scheduler.run_job(state, poll_turn, rest_spent, queue is not None)
        _run_phase("Cleanup", _cleanup_cycle, state, scheduler.cycle_done)
        TM.tick(settings, GUILDS, _MEMBER_COUNTS)
        _wait_for_work(state, status, scheduler, outbox_pass_at)


if __name__ == "__main__":
    try:
        main()
    except BaseException as e:
        TM.crashed(e)  # its class name only; SystemExit is not a crash
        raise
