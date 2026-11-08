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
     is held for the owners instead of posted (see owner.review.ReplyReview).

Configuration: config.yaml next to this file (or --config PATH, or the
MUSE_DISCORD_CONFIG env var), plus config.discord.yaml (changes made
from Discord), both read through muse_settings.py. See
config.example.yaml for the schema.
"""

import os
import sys
import threading
import time
from http import HTTPStatus

try:
    import yaml  # noqa: F401 (muse_settings needs it)
    from pydantic import TypeAdapter  # noqa: F401 (Pydantic v2 required)
except ImportError:
    print("PyYAML and Pydantic v2 are required: run uv sync", file=sys.stderr)
    sys.exit(1)

from muse_discord import muse_settings as MS
from muse_discord import muse_watches as W
from muse_discord.admission import AdmissionPolicy, SharedServers
from muse_discord.bursts import (
    BurstEffects,
    CoalescePolicy,
    GatewayActivity,
    PendingBursts,
)
from muse_discord.channel_access import ChannelAccess
from muse_discord.conversation import (
    Conversation,
    channel_display,
)
from muse_discord.conversation import _snowflake_ts as _snowflake_ts
from muse_discord.conversation import _who as _who
from muse_discord.directory.discovery import (
    Discovery,
)
from muse_discord.directory.lifecycle import GuildLifecycle
from muse_discord.directory.membership import AuthorRoles
from muse_discord.directory.refresh import DirectoryRefresh
from muse_discord.directory.snapshot import DirectorySnapshot
from muse_discord.directory.state import DirectoryState
from muse_discord.durable_state import QueuePublication, StateFile
from muse_discord.gateway_session import GatewaySession
from muse_discord.generated_replies import GeneratedReplies
from muse_discord.http_runtime import DiscordCalls, RestBackoff
from muse_discord.ingest import IngestJournal, MessageEnqueuer
from muse_discord.interaction_dispatch import (
    InteractionResponder,
    InteractionRouter,
    InteractionWorker,
)
from muse_discord.interactions import ephemeral
from muse_discord.media import collector as media
from muse_discord.message_ledger import MessageLedger
from muse_discord.message_models import (
    LedgerContext,
    MessagePlace,
    QueueContext,
)
from muse_discord.message_outcomes import MessageOutcomes
from muse_discord.message_queue import QueueFiles
from muse_discord.messaging import formatting as reply_formatting
from muse_discord.messaging.parts import PartDelivery
from muse_discord.messaging.sending import MessageSender
from muse_discord.messaging.typing import TypingIndicators
from muse_discord.monitor_health import MonitorHealth, token_rejected_exit
from muse_discord.monitor_process import MonitorLock
from muse_discord.outbox_store import OutboxFiles, OutboxProcessor
from muse_discord.outgoing_guard import OutgoingGuard, collect_nonces
from muse_discord.owner.admin_digest import AdminDigests
from muse_discord.owner.authorization import OwnerAuthorization
from muse_discord.owner.capabilities import CapabilityFiles, CapabilityIssuer
from muse_discord.owner.join_notices import JoinNotices
from muse_discord.owner.memory import OwnerMemory
from muse_discord.owner.messaging import OwnerMessenger
from muse_discord.owner.presence import OwnerPresence
from muse_discord.owner.review import ReplyReview
from muse_discord.owner.targets import CommandTargets
from muse_discord.owner.updates import UpdateNotices, version_installed
from muse_discord.paths import project_root
from muse_discord.receiving.cursor import ReceiveCursor
from muse_discord.receiving.direct import DirectChannels
from muse_discord.receiving.gateway import GatewayEvents, GatewayIngress
from muse_discord.receiving.messages import ReceivedMessages, ReceptionEffects
from muse_discord.receiving.quarantine import MessageQuarantine, is_suspicious
from muse_discord.receiving.reconciliation import MentionReconciler, RecoveryWindow
from muse_discord.reply_binding import ReplyBinding, StalePrompt
from muse_discord.reply_delivery import OutboxDelivery, ReplyEffects
from muse_discord.reply_policy import ReplyPolicy
from muse_discord.reply_state import ReplyState
from muse_discord.response_policy import (
    ResponsePolicy,
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
from muse_discord.settings_commands import SETTINGS_PREFIX, CommandRegistrar
from muse_discord.settings_runtime import SettingsContext, SettingsSessions
from muse_discord.settings_watch import LiveSettings
from muse_discord.storage import PrivateFile, SharedLockTimeout, atomic_write
from muse_discord.storage import publish_private as _publish
from muse_discord.watches.audience import WatchAudience
from muse_discord.watches.delivery import WatchDelivery
from muse_discord.watches.fires import WatchFireBuilder
from muse_discord.watches.queue import WatchQueue
from muse_discord.watches.requests import WatchRequests
from muse_discord.watches.runtime import WatchRuntime, WatchState
from muse_discord.watches.web import WebWatchWork
from muse_discord.worker_context import WorkerContext

CONFIG_PATH_ARGUMENT = 2
MEMBER_CACHE_LIMIT = 2000


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
# this file (see QueueFiles.lock and docs/PROTOCOL.md), so neither side can write
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
_SETTINGS: LiveSettings
GUILD_ALLOWLIST: set[str]
TREAT_AS_HUMAN: set[str]
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
        from muse_discord.settings_presets import PRESETS
        from muse_discord.telemetry import Recorder
        from muse_discord.usage_stats.settings_metrics import SettingsMetrics

        return Recorder(
            lambda: (TELEMETRY_FILE, TELEMETRY_ID_FILE, TELEMETRY_WAKES_FILE),
            metrics=SettingsMetrics(PRESETS),
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
    def __init__(self):
        self.store = self.consent = self.policy = self.delivery = self.actions = self

    def __getattr__(self, name):
        return lambda *a, **kw: None


def _proactive_context():
    from muse_discord.proactive import ProactiveContext

    return ProactiveContext(
        path=lambda: PROACTIVE_FILE,
        nonce_path=lambda: PROACTIVE_NONCE_FILE,
        bot_ids=lambda: (BOT_ID, BOT_USER_ID),
        owners=lambda: _SETTINGS.owners,
        channels=lambda: _DIRECTORY.channels,
        dm_channels=lambda: _DIRECT_MESSAGES.names,
        dm_users=lambda: _DIRECT_MESSAGES.users,
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
        owner_check_pending=lambda state, gid: _OWNER_PRESENCE.pending(state, gid),
        owners_absent=lambda *args, **kwargs: owners_absent(*args, **kwargs),
        queue_edit=lambda *args, **kwargs: queue_edit(*args, **kwargs),
        queued_item_meta=lambda *args, **kwargs: queued_item_meta(*args, **kwargs),
        rest_backing_off=lambda *args, **kwargs: rest_backing_off(*args, **kwargs),
        scope_for=lambda *args, **kwargs: scope_for(*args, **kwargs),
        send_fragmented=lambda *args, **kwargs: _PARTS.send(*args, **kwargs),
        send_reply=lambda *args, **kwargs: _SENDER.reply(*args, **kwargs),
        settings=lambda *args, **kwargs: settings(*args, **kwargs),
        settings_card=lambda *args, **kwargs: _worker_context().settings_card(*args, **kwargs),
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
# Configuration reloads, owner state and edit serialization belong to LiveSettings.


def settings():
    if not _INITIALIZED:
        raise RuntimeError("call initialize() before using the monitor")
    return _SETTINGS.read()


# --- Startup identity and engine settings ---
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
# DirectoryState.channels: {channel_id: {"name", "guild_id", "guild_name", "can_send"}}
#   can_send is the bot's effective Send Messages permission for the
#   channel (Send Messages in Threads for threads), or None when it
#   could not be computed; callers then attempt the send and rely on
#   the 403 safety net in process_outbox.
# DirectoryState.guilds: {guild_id: {"name", "joined_at"}}


# Channel types the bot can meaningfully read: 0 = text, 5 = announcement.
# Forum (15) and media (16) channels hold no messages themselves, only
# threads (posts), whose send permission comes from them.

# Re-check for brand-new threads this often (in poll cycles). Threads are
# where forum posts live; a new post should be picked up within a few
# minutes, not at the next full channel refresh.
THREAD_REFRESH_CYCLES = 5


_DIRECTORY: DirectoryState


def _directory_refresh():
    return DirectoryRefresh(_DIRECTORY, _discovery(), publish_directory, sync_guilds, save_state)


def _discovery():
    return Discovery(
        api_get,
        rest_backing_off,
        lambda: BOT_ID,
        GUILD_ALLOWLIST,
        _DIRECTORY,
        _ACCESS,
    )


def get_guilds():
    return _discovery().guilds()


# Discord permission bits used by the talk-scope computation.


def discover_channels(previous=None):
    return _discovery().channels(previous)


# Brand-new threads. A thread created since the previous successful
# thread listing of its guild (or since the monitor started) is seeded at
# its own id, not at its newest message: a mention posted in it before
# the gateway event was drained would otherwise sit at or below the
# watermark, be skipped by the drain as "poll already handled", and wait
# for the sweep. Its whole (short) history is read by the poll instead;
# the gateway/poll dedup (gw_seen, the watermark) keeps that to one
# reply. Older threads seen for the first time keep the usual seeding:
# their history is history.


def seed_channel_seen(cid, cname, state):
    return _discovery().seed(cid, cname, state)


_BACKOFF: RestBackoff
_HEALTH: MonitorHealth
_DISCORD: DiscordCalls


def rest_backing_off():
    return _BACKOFF.active()


def touch_heartbeat():
    return _HEALTH.progress()


def api_get(path):
    return _DISCORD.get(path)


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


_OUTPUT: OutgoingGuard


def _live_nonces():
    return _CAPABILITIES.published_nonces() | collect_nonces(
        (),
        (_WATCH_STATE.web_cache.get("body"),),
        lambda: PRO.store.secret_nonces(),
    )


def secret_in(text):
    return _OUTPUT.find(text)


def guard_outgoing(data, where):
    return _OUTPUT.guard(data, where)


def api_post(path, data):
    return _DISCORD.post(path, data)


def api_patch(path, data):
    return _DISCORD.patch(path, data)


# Bound contention without bypassing mutual exclusion. The main loop retries
# deferred work after the holder releases the lock.
LOCK_WAIT_SECS = 20


def _queue_files():
    return QueueFiles(QUEUE_FILE, LOCK_FILE, LOCK_WAIT_SECS)


def _outbox_files():
    return OutboxFiles(OUTBOX_FILE, OUTBOX_PROCESSING_FILE, OUTBOX_REJECTED_FILE, _queue_files())


def _deliver_outbox_item(batch, item):
    policy = ReplyPolicy(
        binding=_reply_binding(),
        owners=_OWNER_AUTH,
        review=_REVIEW,
        generated=_generated_replies(),
        watches=_watch_delivery(),
        proactive_outbox=PRO.delivery.outbox_item,
        queue=_queue_files(),
        rejected_path=OUTBOX_REJECTED_FILE,
        max_reply_chars=_SETTINGS.engine.max_reply_chars,
    )
    effects = ReplyEffects(
        state=_reply_state(),
        memory=_owner_memory(),
        schedules=_watch_requests(),
        watches=policy.watches,
        conversation=_conversation(),
        typing=_TYPING,
        parts=_PARTS,
        sender=_SENDER,
        response=_response_policy(),
        directory=_DIRECTORY.scopes,
        opt_action=PRO.actions.opt_action,
        max_reply_chars=_SETTINGS.engine.max_reply_chars,
    )
    OutboxDelivery(batch, item, policy, effects, batch.current).process()


def load_queue():
    return _queue_files().load()


def queue_edit(add=(), drop=None):
    return _queue_files().edit(add, drop)


def publish_queue_additions(state, additions=()):
    return QueuePublication(_queue_files(), _STATE_STORE, _message_ledger()).publish(
        state, additions
    )


def load_state():
    state = _STATE_STORE.load()
    # Restore owner admission before failed startup discovery can expose a server.
    mirror_owner_presence(state)
    return state


_STATE_STORE: StateFile


def save_state(state):
    return _STATE_STORE.save(state)


def _quarantine_store():
    return MessageQuarantine(QUARANTINE_FILE, _message_ledger())


# ---------------------------------------------------------------------------
# Mention ledger: the durable record that makes silent drops impossible.
# Every mention/DM reaching a terminal state (replied, quarantined,
# skipped with a reason) is written here, and the reconciliation sweep
# (sweep_mentions) diffs recent channel mentions against it and requeues
# anything unaccounted for.
# ---------------------------------------------------------------------------


def _message_ledger():
    return MessageLedger(
        LEDGER_FILE, _DIRECTORY.channels, _DIRECT_MESSAGES, TM.ledger, remember_name, time.time
    )


def _ledger_load():
    return _message_ledger().load()


def ledger_record(mid, status, reason="", context=None):
    return _message_ledger().record(mid, status, reason, context)


def ledger_record_many(mids, status, reason="", context=None):
    return _message_ledger().record_many(mids, status, reason, context)


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
    return _message_ledger().terminal(mid)


def _quarantine_ids():
    return _quarantine_store().ids()


def _response_policy():
    return ResponsePolicy(
        settings,
        lambda: _SETTINGS.owners,
        _conversation(),
        scope_for,
        scope_of_channel,
        _AUTHOR_ROLES.get_roles,
        TM.count,
    )


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


def scope_for(guild_id=None, channel_id=None, dm=False):
    return _DIRECTORY.scopes.for_message(guild_id, channel_id, dm)


def scope_of_channel(channel_id):
    return _DIRECTORY.scopes.for_channel(channel_id)


# (guild, user) -> (role ids, fetched at): roles of authors whose messages
# carry no member object (the REST poll), for role entries in who.allow /
# who.ignore. Only fetched when such a list names a role.


# user id -> (shares a server with the bot?, checked at), for
# who.dms_from: shared-servers. Positive answers last an hour, negative
# ones ten minutes.
_SHARED_MEMBERSHIP: SharedServers


def _admission():
    return AdmissionPolicy(
        _response_policy(), _OWNER_PRESENCE, _DIRECTORY.scopes, _SHARED_MEMBERSHIP
    )


def dm_refusal(author):
    return _admission().dm_refusal(author)


def retire_overlay_changes():
    """Discord-side settings changes that config.yaml has overtaken (the
    file was edited at that spot afterwards) or that no longer validate:
    move them to the overlay's history, one owner DM each (the conflict
    rule: the newest intent wins). Waits out a REST backoff so the DMs
    can land."""
    if not settings().retire or rest_backing_off():
        return 0
    with _SETTINGS.overlay_lock:
        # Re-checked under the lock: a /settings change written since
        # may have replaced a stale entry, which must not be retired.
        notes = settings().commit_retirements()
    for note in notes:
        print(note, flush=True)
        notify_owners(note)
    return len(notes)


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


_OWNER_MESSAGES: OwnerMessenger
_OWNER_PRESENCE: OwnerPresence
_REVIEW: ReplyReview
_UPDATE_NOTICES: UpdateNotices
_ADMIN_DIGESTS: AdminDigests
_JOIN_NOTICES: JoinNotices
_GUILD_LIFECYCLE: GuildLifecycle


def notify_owners(text, components=None, flags=None):
    return _OWNER_MESSAGES.notify(text, components, flags)


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
# OwnerPresence.membership mirrors it: True, False (no owner), None (not checked
# yet; gated like False).


def mirror_owner_presence(state):
    return _OWNER_PRESENCE.restore(state)


def owners_absent(gid):
    return _OWNER_PRESENCE.absent(gid)


def sync_guilds(state, guilds, channels):
    return _GUILD_LIFECYCLE.sync(state, guilds, channels)


def flush_join_notices(state, guilds, channels):
    return _JOIN_NOTICES.flush(state, guilds, channels)


def on_guild_event(state, ev):
    if _GUILD_LIFECYCLE.on_event(state, ev):
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


def updates_enabled():
    return bool(settings().updates_notify)


def _version_installed(tag, timeout=5):
    return version_installed(BASE_DIR, tag, timeout)


def announce_update(state):
    return _UPDATE_NOTICES.announce(state)


def telemetry_notice():
    """DM the owners once when usage stats have not reached their
    endpoint for days (telemetry.py, take_notice)."""
    text = TM.take_notice(settings)
    if text:
        notify_owners(text)


def apply_record(state, rec):
    kind = rec.get("kind")
    if kind == "update_choice":
        _UPDATE_NOTICES.apply_choice(state, rec)
    elif kind == "hold_choice":
        _REVIEW.apply_choice(state, rec)
    elif kind == "proactive_yes":
        PRO.consent.apply_button(rec)


# Interaction dispatch: one handler per key, the custom_id prefix (the
# text before the first ":") for buttons and other components, or
# "/<name>" for a slash command. A handler gets (interaction, the rest of
# the custom_id, the clicking user's id, record) and returns the callback
# response; record(dict) hands a state change to the main loop.
_INTERACTIONS: InteractionRouter
_INTERACTION_RESPONDER: InteractionResponder
_INTERACTION_WORKER: InteractionWorker


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


def never_share_hit(state, item, qid, parts):
    return _REVIEW.hit(state, item, qid, parts)


def expire_holds(state):
    return _REVIEW.expire(state)


def on_proactive_button(ix, rest, uid, record):
    """mdpro:yes:<token>: the Yes on the monitor's own question to one
    person (proactive.py, asks); anyone may click, only theirs counts."""
    return PRO.consent.on_button(ix, rest, uid, record) or ephemeral(
        "That button is no longer active."
    )


# ---------------------------------------------------------------------------
# /settings is rendered and dispatched by discord.py (discord_connection.py).
# This context exposes the existing settings engine and discovery directory.
# ---------------------------------------------------------------------------
# Directory for settings screens, built on the main loop from discovery and
# the gateway, published through the thread-safe _DIRECTORY_VIEW snapshot.
_DIRECTORY_VIEW: DirectorySnapshot
# The same snapshot on disk, for bin/settings.py (places, names in its
# sentences): written atomically, only when it changed.
DIRECTORY_FILE = os.path.join(BASE_DIR, "directory.json")


def publish_directory():
    return _DIRECTORY_VIEW.update(
        _DIRECTORY.guilds, _DIRECTORY.channels, _DIRECTORY.member_counts, _DIRECTORY.guild_channels
    )


# Private settings screens and drafts, keyed by (user_id, random token).
# Stored only in memory: restart or expiry discards uncommitted edits.
_SETTINGS_SESSIONS: SettingsSessions


def settings_context():
    return SettingsContext(
        current=settings,
        config_path=_SETTINGS.watcher.config_path,
        overlay_lock=_SETTINGS.overlay_lock,
        directory=_DIRECTORY_VIEW,
        people=_USER_NAMES,
        sessions=_SETTINGS_SESSIONS,
        outcomes=MessageOutcomes(
            _message_ledger,
            _DIRECTORY_VIEW,
            _DIRECT_MESSAGES,
            lambda: _WATCH_STORE.read()["fired"],
        ),
        admin_change=_ADMIN_DIGESTS.note,
        bot_id=str(BOT_ID or ""),
        now=time.time(),
    )


def _directory_name(some_id):
    return _DIRECTORY_VIEW.name(some_id)


def flush_admin_digests(force=False):
    return _ADMIN_DIGESTS.flush(force)


# ---------------------------------------------------------------------------
# Owner-DM capabilities: handed only to the worker serving that DM.
# The persisted "nl_nonces" key is retained for existing queued DMs.
# ---------------------------------------------------------------------------
_CAPABILITIES: CapabilityIssuer
_OWNER_AUTH: OwnerAuthorization

OWNER_DM_NONCES_FILE = os.path.join(BASE_DIR, "owner_dm_nonces.json")


def _owner_memory():
    return OwnerMemory(
        MEMORY_FILE,
        _OWNER_AUTH,
        lambda: bool(settings().top("memory.save_dms")),
        secret_in,
        _MEMORY_MISSING,
    )


def queue_index(state):
    return _queue_files().index(state, _SETTINGS.owners)


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


OWNER_ITEMS_FILE = os.path.join(BASE_DIR, "owner_items.json")


ITEM_NONCES_FILE = os.path.join(BASE_DIR, "item_nonces.json")


_COMMANDS: CommandRegistrar


def register_commands(gateway_status=None):
    return _COMMANDS.register(gateway_status)


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


_ACTIVITY: GatewayActivity


def _bursts(state):
    return PendingBursts(
        state,
        _ACTIVITY,
        CoalescePolicy(
            _SETTINGS.engine.coalesce_seconds, _SETTINGS.engine.bot_coalesce_seconds, is_bot_author
        ),
        enqueue_message,
        BurstEffects(
            note_ingested,
            lambda mid: mid in _quarantine_ids() or _ledger_terminal(mid),
            ledger_record_many,
        ),
        time.time,
    )


def stage_trigger(state, msg, cname, kind, place=None):
    return _bursts(state).stage(msg, cname, kind, place)


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


_SENDER: MessageSender
_PARTS: PartDelivery
_TYPING: TypingIndicators


def normalize_parts(item):
    return reply_formatting.normalize_parts(item, _SETTINGS.engine.max_reply_chars)


def _generated_replies():
    return GeneratedReplies(GENERATED_DIR, time.time)


def collect_media(msg):
    """Collect bounded attachments into this monitor's media directory."""
    return media.collect_media(msg, MEDIA_DIR)


def sweep_media():
    media.sweep_media(MEDIA_DIR)


# Shared REST/Gateway reception and gap repair.


def prune_gateway_seen(state):
    return ReceiveCursor(state).prune(time.time())


def _receiver(state):
    return ReceivedMessages(
        _admission(),
        _bursts(state),
        api_get,
        ReceptionEffects(watch_scan, TM.count, note_refused, _note_no_send, seed_channel_seen),
    )


def _gateway_ingress(state, channels):
    return GatewayIngress(
        _receiver(state),
        channels,
        _ACCESS,
        GatewayEvents(on_guild_event, watch_perm_event, note_activity, apply_record),
        GUILD_ALLOWLIST,
    )


def check_channel(channel_id, channel_name, state, guild_id=None):
    return _receiver(state).poll_channel(channel_id, channel_name, guild_id)


def dm_poll_user_ids():
    return _admission().dm_poll_user_ids()


_DIRECT_MESSAGES: DirectChannels


def list_dm_channels():
    return _DIRECT_MESSAGES.resolve(dm_poll_user_ids(), api_post)


def check_dm_channel(dm_id, dm_name, state):
    return _receiver(state).poll_dm(dm_id, dm_name)


def _reply_state():
    return ReplyState(
        _message_ledger(), _queue_files(), PRO.policy.note_item, TM.latency, time.time
    )


def queued_item_meta(state, qid, index):
    return _reply_state().queued_meta(state, qid, index)


def started_item_id(now=None):
    """The queue id of an item the monitor starts (a reminder or watch
    fire, a heartbeat slot): a snowflake for now with random low bits,
    so it passes every id check and never names a real message."""
    return W.new_fire_id(now)


def note_item_meta(state, mid, **meta):
    return _reply_state().note_item(state, mid, **meta)


def _worker_context():
    return WorkerContext(
        _conversation(),
        _DIRECTORY,
        lambda: _SETTINGS.owners,
        api_get,
        collect_media,
        _watch_requests(),
    )


def _ingest_journal():
    return IngestJournal(_message_ledger(), _quarantine_store(), _reply_state(), _CAPABILITIES)


def enqueue_message(msg, cname, state, queue, context=None):
    """Validate and queue one burst, preserving its single capability and ledger identity."""
    return MessageEnqueuer(
        msg,
        cname,
        state,
        queue,
        context or QueueContext(),
        _response_policy(),
        _worker_context(),
        _ingest_journal(),
    ).enqueue()


def note_ingested(state, mid):
    return _reply_state().note_ingested(state, mid)


def _settle(state, item, outcome):
    return _reply_state().settle(state, item, outcome)


# Processing progress and poison-item counts belong to OutboxProcessor.
_OUTBOX: OutboxProcessor


STALE_PROMPT_FILE = os.path.join(BASE_DIR, "prompt_outdated.json")


def _reply_binding():
    return ReplyBinding(
        _CAPABILITIES.files.replies,
        lambda item: PRO.delivery.names_slot(item),
        StalePrompt(STALE_PROMPT_FILE, notify_owners, time.time, lambda: time.strftime("%Y-%m-%d")),
    )


# ---------------------------------------------------------------------------
# Reminders and watches (muse_watches.py; docs/PROTOCOL.md, "Reminders and
# watches"). The worker asks for one with a `schedule` action on its
# reply to a queue item; the requester is that item's author and the
# place its channel, from item_meta, never from the outbox
# (watches.requests.WatchRequests). watches.json keeps them across restarts. When a
# reminder is due, or a watch matches a server message the monitor
# ingests (ReceivedMessages.classify: gateway and poll, once each), watch_tick
# queues a "reminder" item (a fire) for the worker. The worker's reply
# to it is bound to the requester and the place (watches.delivery.WatchDelivery.bind) and may
# ping the requester only (WatchDelivery.send). A fire is kept in
# watches.json until the ledger settles it, so a restart between
# saving it and queueing it re-queues it instead of losing it.
# ---------------------------------------------------------------------------
_ACCESS: ChannelAccess
_AUTHOR_ROLES: AuthorRoles
_WATCH_STORE: W.Store
_WATCH_STATE: WatchState

WATCHES_FILE = os.path.join(BASE_DIR, "watches.json")
# Web watches: the live checks for hooks/watch-due.sh (mode 600), and
# the checker's reports from bin/watch-report.py (drained each tick).
WEB_DUE_FILE = os.path.join(BASE_DIR, "web_watches_due.json")
WEB_RESULTS_FILE = os.path.join(BASE_DIR, "web_watch_results.json")


# -- what a requester can read -------------------------------------------


def watch_perm_event(ev):
    return _ACCESS.permission_event(ev)


# -- asking for one ---------------------------------------------------------


def _watch_audience():
    return WatchAudience(
        _response_policy(),
        _ACCESS,
        _DIRECTORY.guilds,
        _USER_NAMES,
        owners_absent,
        dm_refusal,
    )


def _watch_requests():
    return WatchRequests(_WATCH_STORE, _watch_audience(), queued_item_meta, _note_watch_timezone)


def _note_watch_timezone(uid, fresh):
    for op, value in (fresh or {}).get("done") or []:
        if op == "tz":
            try:  # their quiet hours: only their own button moves them
                PRO.actions.note_tz(uid, value)
            except Exception as e:
                print(f"Proactive: time zone note error: {e}", flush=True)


# -- firing -----------------------------------------------------------------


def _watch_delivery():
    return WatchDelivery(_WATCH_STORE, api_get, normalize_parts)


def _watch_runtime():
    audience = _watch_audience()
    return WatchRuntime(
        store=_WATCH_STORE,
        audience=audience,
        fires=WatchFireBuilder(audience, ledger_record, _worker_context().settings_card),
        queue=WatchQueue(
            _queue_files(),
            _reply_state(),
            _CAPABILITIES,
            lambda: _SETTINGS.owners,
            _CAPABILITIES.write_owner_dms,
        ),
        web=WebWatchWork(
            WEB_RESULTS_FILE,
            WEB_DUE_FILE,
            _publish,
            lambda: _SETTINGS.owners,
            is_suspicious,
            _WATCH_STATE.web_cache,
        ),
        entries=lambda: _ledger_load()["entries"],
        state=_WATCH_STATE,
    )


def watch_tick(state, now=None):
    return _watch_runtime().tick(state, now)


def watch_scan(msg, guild_id):
    return _watch_runtime().scan(msg, guild_id)


# -- answering a fire -------------------------------------------------------


_PROCESS_LOCK: MonitorLock
_GATEWAY: GatewaySession

PIDFILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor.pid")


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


def drain_gateway(state, inbox, channels):
    return _gateway_ingress(state, channels).drain(inbox)


def sweep_mentions(state, queue, channels, dms=True):
    return MentionReconciler(
        admission=_admission(),
        ledger=_message_ledger(),
        get=api_get,
        enqueue=enqueue_message,
        quarantined=_quarantine_ids,
        outbox_paths=(OUTBOX_FILE, OUTBOX_PROCESSING_FILE),
        direct=_DIRECT_MESSAGES,
        window=RecoveryWindow(
            _SETTINGS.engine.sweep.grace_seconds,
            _SETTINGS.engine.sweep.max_age_seconds,
            _SETTINGS.engine.sweep.max_recoveries,
        ),
        backing_off=rest_backing_off,
        now=time.time,
    ).run(state, queue, channels, dms)


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
    return targets + [("channel", cid) for cid in _DIRECTORY.channels]


def _adopt_identity(me, how=""):
    global BOT_ID, BOT_USER_ID, BOT_NAME
    BOT_ID = BOT_USER_ID = me.get("id")
    BOT_NAME = me.get("username")
    print(f"Bot user id: {BOT_ID}{how}", flush=True)


def _drain_live_messages(state, _gateway_inbox):
    # Gateway fast path first: messages pushed in realtime are
    # classified (and triggers staged) like polled ones. Anything
    # the poll already processed is skipped. Guild join/leave
    # events ride the same inbox. Waits for the bot's identity,
    # like the poll.
    if BOT_USER_ID is not None:
        gw_mentions, gw_dms = drain_gateway(state, _gateway_inbox, _DIRECTORY.channels)
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
        _BACKOFF.clean_cycle()
        if BOT_USER_ID is None:
            try:
                _adopt_identity(api_get("/users/@me"), " (recovered in-loop)")
            except Exception as e:
                if getattr(e, "code", None) == HTTPStatus.UNAUTHORIZED:
                    token_rejected_exit("401 from /users/@me")


def _cleanup_cycle(state, cycle_done):
    if cycle_done:
        flush_join_notices(state, _DIRECTORY.guilds, _DIRECTORY.channels)
        announce_update(state)
        telemetry_notice()
        expire_holds(state)
        sweep_media()
        _generated_replies().sweep(state)
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
    until = now + (0.0 if _OUTBOX.waiting(outbox_pass_at) else wait_for)
    while True:
        left = until - time.time()
        if left <= 0 or gateway_status["wake"].wait(timeout=min(0.25, left)):
            break
        if _OUTBOX.waiting(outbox_pass_at):
            break
    gateway_status["wake"].clear()


def _rest_scheduler():
    return RestScheduler(
        RestContext(
            RestSchedule(
                _SETTINGS.engine.rest_poll_seconds,
                _SETTINGS.engine.sweep.every_cycles,
                _SETTINGS.engine.discovery_refresh_cycles,
                THREAD_REFRESH_CYCLES,
            ),
            RestClock(time.time, rest_backing_off),
            RestOperations(
                lambda: _DIRECTORY.channels,
                lambda: BOT_USER_ID is not None,
                poll_targets,
                list_dm_channels,
                check_dm_channel,
                check_channel,
                load_queue,
                sweep_mentions,
                publish_queue_additions,
                save_state,
                lambda state: _directory_refresh().channels(state),
                lambda state: _directory_refresh().threads(state),
            ),
        ),
        _DISCOVERY_REFRESH,
    )


def _settings_upkeep(status):
    register_commands(status)
    _ADMIN_DIGESTS.flush()


def _send_outbox(state):
    _OUTBOX.pass_once(state)
    save_state(state)


def _start_monitor_threads(state):
    _gateway_inbox, _gateway_status = _GATEWAY.start()
    _ix_inbox = _GATEWAY.interactions
    # Typing indicator on its own clock (see _TYPING.refresh).
    threading.Thread(target=_TYPING.run, daemon=True, name="typing").start()
    # Interactions (button clicks) on their own thread, fed straight by
    # the gateway thread: the 3s acknowledgement deadline never waits on
    # the main loop. What they change in state comes back through the
    # gateway inbox ("_record", applied by drain_gateway).
    _REVIEW.live.update({hid: h.get("at", 0) for hid, h in (state.get("holds") or {}).items()})
    _UPDATE_NOTICES.announced["version"] = (state.get("update_notice") or {}).get("version")
    threading.Thread(
        target=_INTERACTION_WORKER.run,
        args=(_ix_inbox, lambda rec: _gateway_inbox.put({"_record": rec})),
        daemon=True,
        name="interactions",
    ).start()

    return _gateway_inbox, _gateway_status


def _start_monitor():
    _PROCESS_LOCK.acquire()
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
    _DIRECTORY.adopt(*discover_channels())
    publish_directory()
    print(
        f"muse-discord monitor starting. Polling {len(_DIRECTORY.channels)} "
        f"channels + DMs every {_SETTINGS.engine.rest_poll_seconds}s.",
        flush=True,
    )
    if not _DIRECTORY.channels:
        print(
            "WARNING: no readable channels found (check the bot's "
            "guild membership and channel permissions); discovery "
            "keeps retrying on the refresh cadence.",
            flush=True,
        )
    state = load_state()
    # Join watermarks for every guild, plus the owner summary for any
    # guild joined while the monitor was down.
    sync_guilds(state, _DIRECTORY.guilds, _DIRECTORY.channels)

    # Seed last_seen for any monitored channel not tracked yet, so its
    # history is never mistaken for fresh mentions (first run, or a
    # newly discovered channel after a permission grant).
    for cid, info in _DIRECTORY.channels.items():
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


def _initialize_services():
    """Build process-owned services after configuration and token validation."""
    global _OUTBOX
    global _SENDER, _PARTS, _TYPING
    global _CAPABILITIES, _OWNER_AUTH
    global _OWNER_MESSAGES, _OWNER_PRESENCE, _REVIEW, _UPDATE_NOTICES
    global _ADMIN_DIGESTS, _JOIN_NOTICES, _GUILD_LIFECYCLE
    global _PROCESS_LOCK, _GATEWAY, _ACCESS, _AUTHOR_ROLES, _WATCH_STORE, _WATCH_STATE
    global _DIRECTORY, TM, PRO, _DIRECTORY_VIEW, _ACTIVITY, _DISCOVERY_REFRESH, _SHARED_MEMBERSHIP
    global _DIRECT_MESSAGES, _DISCORD, _BACKOFF, _HEALTH, _OUTPUT, _STATE_STORE
    global _SETTINGS_SESSIONS, _COMMANDS, _INTERACTIONS, _INTERACTION_RESPONDER, _INTERACTION_WORKER
    _PROCESS_LOCK = MonitorLock(lambda: PIDFILE)
    _GATEWAY = GatewaySession(TOKEN, settings_context, guard_outgoing)
    _DIRECTORY = DirectoryState()
    _ACCESS = ChannelAccess(
        _DIRECTORY.channels, lambda path: api_get(path), lambda: rest_backing_off()
    )
    _AUTHOR_ROLES = AuthorRoles(lambda path: api_get(path), lambda: rest_backing_off())
    _WATCH_STORE = W.Store(lambda: WATCHES_FILE)
    _WATCH_STATE = WatchState()
    _SETTINGS_SESSIONS = SettingsSessions(lambda: time.time())
    _STATE_STORE = StateFile(lambda: STATE_FILE)
    _HEALTH = MonitorHealth(lambda: HEARTBEAT_FILE, lambda: time.time())
    _BACKOFF = RestBackoff(
        _SETTINGS.engine.backoff.initial_seconds,
        _SETTINGS.engine.backoff.max_seconds,
        lambda: time.time(),
    )
    _OUTPUT = OutgoingGuard(
        lambda: TOKEN,
        _live_nonces,
        lambda text: notify_owners(text),
        lambda: time.strftime("%Y-%m-%d"),
    )
    _DISCORD = DiscordCalls(
        lambda: TOKEN, _BACKOFF, _HEALTH, lambda data, where: guard_outgoing(data, where)
    )
    _SENDER = MessageSender(
        lambda data, where: guard_outgoing(data, where),
        lambda path, body: api_post(path, body),
        lambda path, payload, blobs: _DISCORD.upload(path, payload, blobs),
        lambda path: _DISCORD.put(path),
    )
    _TYPING = TypingIndicators(
        lambda path, body: api_post(path, body),
        lambda: rest_backing_off(),
        lambda: load_queue(),
        lambda: time.time(),
        lambda seconds: time.sleep(seconds),
    )
    _PARTS = PartDelivery(_SENDER, lambda cid: _TYPING.send(cid), lambda secs: time.sleep(secs))
    _DIRECTORY_VIEW = DirectorySnapshot(lambda: DIRECTORY_FILE)
    _DIRECT_MESSAGES = DirectChannels()
    _OWNER_MESSAGES = OwnerMessenger(
        lambda: _SETTINGS.owners,
        lambda data, where: guard_outgoing(data, where),
        lambda path, body: api_post(path, body),
    )
    _OWNER_PRESENCE = OwnerPresence(
        lambda: _SETTINGS.owners,
        settings,
        lambda path: api_get(path),
        lambda: rest_backing_off(),
    )
    _OWNER_AUTH = OwnerAuthorization(
        lambda: _SETTINGS.owners,
        _DIRECTORY.channels,
        _DIRECTORY.guild_channels,
        _DIRECT_MESSAGES.users,
        lambda path: api_get(path),
    )
    _CAPABILITIES = CapabilityIssuer(
        CapabilityFiles(
            PrivateFile(lambda: OWNER_DM_NONCES_FILE),
            PrivateFile(lambda: OWNER_ITEMS_FILE),
            PrivateFile(lambda: ITEM_NONCES_FILE),
        ),
        _OWNER_AUTH,
        CommandTargets(_DIRECTORY.channels, _DIRECTORY.guild_channels),
        lambda: load_queue(),
        lambda text: notify_owners(text),
    )
    _OUTBOX = OutboxProcessor(
        _outbox_files,
        lambda state: queue_index(state),
        lambda state: save_state(state),
        _CAPABILITIES.publish,
        _deliver_outbox_item,
    )
    _REVIEW = ReplyReview(settings, _OWNER_MESSAGES, _DIRECT_MESSAGES.users, _settle)
    _UPDATE_NOTICES = UpdateNotices(
        lambda: UPDATE_CHECK_FILE,
        lambda: UPDATE_REQUEST_FILE,
        _OWNER_MESSAGES,
        updates_enabled,
        lambda: rest_backing_off(),
        lambda *args, **kwargs: _version_installed(*args, **kwargs),
    )
    _ADMIN_DIGESTS = AdminDigests(
        _directory_name,
        _USER_NAMES,
        _DIRECTORY.guilds,
        _OWNER_MESSAGES.notify,
        lambda: rest_backing_off(),
    )
    _JOIN_NOTICES = JoinNotices(
        _OWNER_PRESENCE, _OWNER_MESSAGES, lambda: rest_backing_off(), SETTINGS_PREFIX
    )
    _GUILD_LIFECYCLE = GuildLifecycle(
        _DIRECTORY,
        _DIRECTORY.channels,
        _OWNER_PRESENCE,
        _JOIN_NOTICES,
        settings,
        publish_directory,
        GUILD_ALLOWLIST,
    )
    _INTERACTIONS = InteractionRouter(lambda: _SETTINGS.owners)
    _INTERACTIONS.register("mdupd", _UPDATE_NOTICES.on_button)
    _INTERACTIONS.register("mdhold", _REVIEW.on_button)
    _INTERACTIONS.register("mdpro", on_proactive_button, owner_only=False)
    _INTERACTION_RESPONDER = InteractionResponder(_DISCORD)
    _INTERACTION_WORKER = InteractionWorker(_INTERACTIONS, _INTERACTION_RESPONDER)
    _ACTIVITY = GatewayActivity()
    _DISCOVERY_REFRESH = DiscoveryRefresh()
    _SHARED_MEMBERSHIP = SharedServers(
        lambda path: api_get(path),
        lambda: rest_backing_off(),
        lambda: list(_DIRECTORY.guilds),
        lambda gid: owners_absent(gid),
        lambda: time.time(),
    )
    _COMMANDS = CommandRegistrar(
        get=lambda path: api_get(path),
        post=lambda path, data: api_post(path, data),
        patch=lambda path, data: api_patch(path, data),
        identity_ready=lambda: BOT_USER_ID is not None,
        backing_off=lambda: rest_backing_off(),
        now=lambda: time.time(),
    )
    TM = _import_telemetry()
    PRO = _import_proactive()


def initialize(config_path=None, token_override=None):
    """Load this monitor's configuration and services once, after importing it."""
    global GUILD_ALLOWLIST
    global TOKEN, TREAT_AS_HUMAN, _INITIALIZED
    global _SETTINGS
    if _INITIALIZED:
        raise RuntimeError("monitor is already initialized")
    try:
        _SETTINGS = LiveSettings(MS.Watcher(config_path or _config_path()))
    except MS.SettingsError as e:
        print(
            f"CONFIG ERROR: config.yaml is invalid; not starting.\n{e}", file=sys.stderr, flush=True
        )
        sys.exit(1)
    initial = _SETTINGS.initial
    GUILD_ALLOWLIST = set(initial.guild_ids())
    TREAT_AS_HUMAN = {
        str(u) for u in (initial.raw.get("testing") or {}).get("treat_as_human") or []
    }
    if TREAT_AS_HUMAN:
        print(
            f"WARNING: testing.treat_as_human is set: {len(TREAT_AS_HUMAN)} "
            f"bot account(s) are treated as humans. Test servers only.",
            flush=True,
        )
    for _warning in initial.engine_warnings:
        print(_warning, flush=True)
    TOKEN = token_override if token_override is not None else (_load_token(initial) or "")
    if not TOKEN:
        print(
            "No bot token: pass one as argv[1], or point token_from at an environment "
            "variable or a file holding it (mode 600).",
            file=sys.stderr,
        )
        sys.exit(1)
    _initialize_services()
    _INITIALIZED = True


def main():
    if not _INITIALIZED:
        initialize()
    state, inbox, status = _start_monitor()
    scheduler = _rest_scheduler()
    outbox_pass_at = 0.0
    while True:
        if _HEALTH.rejected or status.get("token_rejected"):
            save_state(state)
            token_rejected_exit(_HEALTH.rejected or "gateway closed with code 4004")
        touch_heartbeat()
        if BOT_USER_ID is None and status.get("user"):
            _adopt_identity(status["user"], " (from gateway READY)")
        now, backing = time.time(), rest_backing_off()
        scheduler.begin_tick(now, backing)
        dm_items = []
        _run_phase("Gateway drain", _drain_live_messages, state, inbox)
        _run_phase("Settings overlay", retire_overlay_changes)
        _run_phase("/settings upkeep", _settings_upkeep, status)
        _run_phase("Owner DM upkeep", _CAPABILITIES.upkeep, state)
        _run_phase("Reminders and watches", watch_tick, state)
        _run_phase("Proactive", PRO.tick, state)
        poll_turn, rest_spent = scheduler.poll_slice(state, now, backing, dm_items)
        queue = _run_phase("Queue/staging", _stage_polled_messages, state, dm_items)
        _run_phase("Typing", _TYPING.refresh, state, queue)
        outbox_pass_at = time.time()
        _run_phase("Outbox", _send_outbox, state)
        recovery = scheduler.recovery_cycle()
        _run_phase("Identity retry", _retry_cycle_identity, scheduler.cycle_done)
        _run_phase("Job scheduling", scheduler.schedule_jobs, state, recovery)
        scheduler.run_job(state, poll_turn, rest_spent, queue is not None)
        _run_phase("Cleanup", _cleanup_cycle, state, scheduler.cycle_done)
        TM.tick(settings, _DIRECTORY.guilds, _DIRECTORY.member_counts)
        _wait_for_work(state, status, scheduler, outbox_pass_at)


if __name__ == "__main__":
    try:
        main()
    except BaseException as e:
        TM.crashed(e)  # its class name only; SystemExit is not a crash
        raise
